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

struct AttViewing: Identifiable { let path: String; let name: String; let video: Bool; var id: String { path } }

private struct ImageThumb: View {
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

private struct VideoThumb: View {
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

private struct AttViewer: View {
    let v: AttViewing
    @Environment(\.dismiss) private var dismiss
    @State private var player: AVPlayer?
    var body: some View {
        ZStack(alignment: .topTrailing) {
            Color.black.ignoresSafeArea()
            if v.video {
                if let p = player { VideoPlayer(player: p).ignoresSafeArea() } else { ProgressView().tint(.white).frame(maxWidth: .infinity, maxHeight: .infinity) }
            } else {
                AsyncImage(url: API.shared.artifactURL(v.path, download: false)) { img in
                    img.resizable().scaledToFit()
                } placeholder: { ProgressView().tint(.white) }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            Button { player?.pause(); dismiss() } label: {
                Image(systemName: "xmark.circle.fill").font(.system(size: 30)).foregroundColor(.white.opacity(0.9)).padding()
            }.accessibilityLabel("关闭")
        }
        .onAppear {
            if v.video, player == nil, let u = API.shared.artifactURL(v.path, download: false) {
                let p = AVPlayer(url: u); player = p; p.play()
            }
        }
        .onDisappear { player?.pause() }
    }
}
