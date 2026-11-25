import SwiftUI
import AVKit
import AVFoundation
import UIKit

/// v3.9.53: a photo / video the user sent shows itself under their bubble (a thumbnail; tap for full screen / the
/// player), not only its file name. The file is the upload's copy on the API host (`artifact`); anything else, or an
/// upload still in flight, stays the name row.
func attKind(_ a: JSON) -> String {
    let n = (a.s("name").isEmpty ? a.s("artifact") : a.s("name")).lowercased()
    let ext = (n as NSString).pathExtension
    if a.s("kind") == "video" || ["mp4", "mov", "m4v", "webm"].contains(ext) { return "video" }
    if a.s("kind") == "image" || ["jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "bmp"].contains(ext) { return "image" }
    return ""
}

struct AttachmentPreviews: View {
    let attachments: [JSON]
    @State private var viewing: AttViewing?
    var body: some View {
        let media = attachments.filter { !$0.s("artifact").isEmpty && !attKind($0).isEmpty }
        let rest = attachments.filter { $0.s("artifact").isEmpty || attKind($0).isEmpty }
        VStack(alignment: .trailing, spacing: 6) {
            ForEach(media.indices, id: \.self) { i in
                let a = media[i]
                Button { viewing = AttViewing(path: a.s("artifact"), name: a.s("name"), video: attKind(a) == "video") } label: {
                    if attKind(a) == "video" { VideoThumb(path: a.s("artifact")) } else { ImageThumb(path: a.s("artifact")) }
                }.buttonStyle(.plain).accessibilityLabel((attKind(a) == "video" ? "播放视频 " : "查看图片 ") + a.s("name"))
            }
            ForEach(rest.indices, id: \.self) { i in
                let a = rest[i]
                Label(a.s("name"), systemImage: a.s("kind") == "video" ? "film" : fileIcon(a.s("name"))).font(.caption).foregroundColor(.secondary)
                    .lineLimit(1).truncationMode(.middle)
            }
        }
        .fullScreenCover(item: $viewing) { v in AttViewer(v: v) }
    }
}

struct AttViewing: Identifiable {
    let path: String
    let name: String
    let video: Bool
    var url: String = ""
    var id: String { path.isEmpty ? url : path }
    var mediaURL: URL? {
        if !path.isEmpty { return API.shared.artifactURL(path, download: false) }
        return URL(string: url)
    }
    var downloadURL: URL? {
        if !path.isEmpty { return API.shared.artifactURL(path, download: true) }
        return URL(string: url)
    }
}

struct ImageThumb: View {
    let path: String
    var body: some View {
        AsyncImage(url: API.shared.artifactURL(path, download: false)) { img in
            img.resizable().scaledToFill()
        } placeholder: {
            ZStack { Color(.secondarySystemBackground); ProgressView() }
        }
        .frame(width: 200, height: 200).clipped()
        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
    }
}

struct VideoThumb: View {
    let path: String
    @State private var frame: UIImage?
    var body: some View {
        ZStack {
            if let f = frame { Image(uiImage: f).resizable().scaledToFill() } else { Color.black.opacity(0.85) }
            Image(systemName: "play.circle.fill").font(.system(size: 44)).foregroundColor(.white.opacity(0.92)).shadow(radius: 4)
        }
        .frame(width: 160, height: 220).clipped()
        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        .task(id: path) { frame = await videoFrame(path) }
    }
}

/// The first frame of a video on the API host (HTTP range reads; only a few hundred KB come down). Cached per path.
private let frameCache = NSCache<NSString, UIImage>()
func videoFrame(_ path: String) async -> UIImage? {
    if let hit = frameCache.object(forKey: path as NSString) { return hit }
    guard let u = API.shared.artifactURL(path, download: false) else { return nil }
    let gen = AVAssetImageGenerator(asset: AVURLAsset(url: u))
    gen.appliesPreferredTrackTransform = true
    gen.maximumSize = CGSize(width: 480, height: 480)
    let img: UIImage? = await withCheckedContinuation { cont in
        gen.generateCGImagesAsynchronously(forTimes: [NSValue(time: CMTime(seconds: 0.1, preferredTimescale: 600))]) { _, cg, _, _, _ in
            cont.resume(returning: cg.map { UIImage(cgImage: $0) })
        }
    }
    if let i = img { frameCache.setObject(i, forKey: path as NSString) }
    return img
}

struct AttViewer: View {
    let v: AttViewing
    @Environment(\.dismiss) private var dismiss
    @State private var player: AVPlayer?
    @State private var image: UIImage?
    @State private var loading = true
    @State private var scale: CGFloat = 1.0
    @State private var lastScale: CGFloat = 1.0
    @State private var offset: CGSize = .zero
    @State private var lastOffset: CGSize = .zero
    @State private var saving = false
    @State private var shareFile: URL?
    @State private var saveMsg = ""

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            if v.video {
                if let p = player { VideoPlayer(player: p).ignoresSafeArea() }
                else { ProgressView().tint(.white).frame(maxWidth: .infinity, maxHeight: .infinity) }
            } else if let img = image {
                Image(uiImage: img)
                    .resizable()
                    .scaledToFit()
                    .scaleEffect(scale)
                    .offset(offset)
                    .gesture(
                        MagnificationGesture()
                            .onChanged { val in
                                scale = min(max(lastScale * val, 1.0), 5.0)
                            }
                            .onEnded { _ in
                                if scale <= 1.02 { scale = 1.0; offset = .zero; lastOffset = .zero }
                                lastScale = scale
                            }
                    )
                    .simultaneousGesture(
                        DragGesture()
                            .onChanged { g in
                                guard scale > 1.01 else { return }
                                offset = CGSize(width: lastOffset.width + g.translation.width,
                                                height: lastOffset.height + g.translation.height)
                            }
                            .onEnded { _ in
                                if scale > 1.01 { lastOffset = offset }
                            }
                    )
                    .onTapGesture(count: 2) {
                        withAnimation(.spring(response: 0.25)) {
                            if scale > 1.1 {
                                scale = 1.0; lastScale = 1.0; offset = .zero; lastOffset = .zero
                            } else {
                                scale = 2.5; lastScale = 2.5
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if loading {
                ProgressView().tint(.white).frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                Text("图片加载失败").foregroundColor(.white.opacity(0.8))
            }

            VStack {
                HStack(spacing: 12) {
                    if !v.name.isEmpty {
                        Text(v.name).font(.caption).foregroundColor(.white.opacity(0.85)).lineLimit(1).truncationMode(.middle)
                            .padding(.horizontal, 10).padding(.vertical, 6).background(Capsule().fill(Color.black.opacity(0.5)))
                    }
                    Spacer()
                    Button { Task { await downloadAndShare() } } label: {
                        if saving {
                            ProgressView().tint(.white).frame(width: 34, height: 34).background(Circle().fill(Color.black.opacity(0.55)))
                        } else {
                            Image(systemName: "square.and.arrow.down").font(.system(size: 16, weight: .semibold)).foregroundColor(.white)
                                .frame(width: 34, height: 34).background(Circle().fill(Color.black.opacity(0.55)))
                        }
                    }.disabled(saving).accessibilityLabel("下载")
                    Button { player?.pause(); dismiss() } label: {
                        Image(systemName: "xmark.circle.fill").font(.system(size: 30)).foregroundColor(.white.opacity(0.92))
                    }.accessibilityLabel("关闭")
                }
                .padding(.horizontal, 16).padding(.top, 12)
                Spacer()
                if !saveMsg.isEmpty {
                    Text(saveMsg).font(.caption).foregroundColor(.white)
                        .padding(.horizontal, 12).padding(.vertical, 6).background(Capsule().fill(Color.black.opacity(0.75)))
                        .padding(.bottom, 24)
                }
            }
        }
        .sheet(isPresented: Binding(get: { shareFile != nil }, set: { if !$0 { shareFile = nil } })) {
            if let u = shareFile { ActivityView(items: [u]) }
        }
        .task {
            if v.video {
                if player == nil, let u = v.mediaURL {
                    let p = AVPlayer(url: u); player = p; p.play()
                }
                loading = false
            } else if let u = v.mediaURL {
                loading = true
                if let (data, _) = try? await URLSession.shared.data(from: u), let ui = UIImage(data: data) {
                    image = ui
                }
                loading = false
            } else {
                loading = false
            }
        }
        .onDisappear { player?.pause() }
    }

    @MainActor func downloadAndShare() async {
        guard let u = v.downloadURL ?? v.mediaURL else { return }
        saving = true; defer { saving = false }
        do {
            let (data, _) = try await URLSession.shared.data(from: u)
            let fallback = v.video ? "video.mp4" : "image.png"
            let rawName = v.name.isEmpty ? (u.lastPathComponent.isEmpty ? fallback : u.lastPathComponent) : v.name
            let safe = rawName.replacingOccurrences(of: "/", with: "_")
            let dst = FileManager.default.temporaryDirectory.appendingPathComponent(safe)
            try data.write(to: dst, options: .atomic)
            shareFile = dst
        } catch {
            saveMsg = "下载失败：\(error.localizedDescription)"
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { saveMsg = "" }
        }
    }
}

private struct ActivityView: UIViewControllerRepresentable {
    let items: [Any]
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }
    func updateUIViewController(_ uiViewController: UIActivityViewController, context: Context) {}
}


/// v3.9.86: pictures, videos and files an engine or AIW 管家 put in a message (data.media: name, path on the API host,
/// kind image|video|audio|file, optional label such as 「方案 A」): design options, run screenshots, a patch file.
struct EngineMediaList: View {
    let media: [JSON]
    @State private var viewing: AttViewing?
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(media.indices, id: \.self) { i in
                let x = media[i], kind = x.s("kind"), path = x.s("path"), url = x.s("url")
                VStack(alignment: .leading, spacing: 3) {
                    if (kind == "image" || kind == "video") && (!path.isEmpty || !url.isEmpty) {
                        Group {
                            if kind == "video" { VideoThumb(path: path) } else { ShotThumb(path: path, fallbackURL: url) }
                        }
                        .contentShape(Rectangle())
                        .onTapGesture {
                            viewing = AttViewing(path: path, name: x.s("name"), video: kind == "video", url: url)
                        }
                        .accessibilityAddTraits(.isButton)
                        .accessibilityLabel((kind == "video" ? "播放视频 " : "查看图片 ") + (x.s("label").isEmpty ? x.s("name") : x.s("label")))
                    } else if let u = path.isEmpty ? URL(string: url) : API.shared.artifactURL(path, download: true) {
                        Link(destination: u) {
                            HStack(spacing: 6) {
                                Image(systemName: kind == "audio" ? "waveform" : fileIcon(x.s("name"))).foregroundColor(amber)
                                Text(x.s("name").isEmpty ? "文件" : x.s("name")).lineLimit(1).truncationMode(.middle)
                                Text("下载").foregroundColor(.secondary)
                            }.font(.caption)
                            .padding(.horizontal, 10).padding(.vertical, 6)
                            .background(RoundedRectangle(cornerRadius: 10).fill(Color(.secondarySystemBackground)))
                        }
                    }
                    if !x.s("label").isEmpty { Text(x.s("label")).font(.caption2.weight(.semibold)).foregroundColor(.secondary) }
                }
            }
        }
        .fullScreenCover(item: $viewing) { v in AttViewer(v: v) }
    }
}

/// A screenshot keeps its shape (a page is wide, a phone design is tall); tap for full screen.
struct ShotThumb: View {
    let path: String
    var fallbackURL: String = ""
    var url: URL? {
        if !path.isEmpty { return API.shared.artifactURL(path, download: false) }
        return URL(string: fallbackURL)
    }
    var body: some View {
        AsyncImage(url: url) { img in
            img.resizable().scaledToFit()
        } placeholder: {
            ZStack { Color(.secondarySystemBackground); ProgressView() }.frame(width: 220, height: 150)
        }
        .frame(maxWidth: 280, maxHeight: 340, alignment: .leading)
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).stroke(Color.secondary.opacity(0.25)))
    }
}
