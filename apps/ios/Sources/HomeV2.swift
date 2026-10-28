import SwiftUI
import PhotosUI
import UniformTypeIdentifiers
import UIKit

// iOS 3.5 home, from the approved v2 mockup (same as the web home.js / home.css):
// * nothing new: 球球 in the middle, its briefing (GET /api/greeting) as speech bubbles around it, one every 4.5 s
//   (the old one fades and shrinks away), 球球's face follows each item's mood, tapping a bubble runs its action,
//   quick chips above the input, typing stops the rotation
// * user bubbles: amber gradient, dark text, corners 20/20/6/20; 球球's: dark with an amber border, 20/20/20/6, a small
//   球球 beside them, bold text in light amber; task cards: amber accent on the left, amber status, a progress bar
// * thinking: 球球 with eyes up (emotion 30) inside a turning dashed ring + three bouncing dots and what it is doing
// * "/" opens the skill picker above the input; "+" a bottom sheet (拍照 / 录视频 / 照片/视频 / 文件 / 新建任务 / 链接 / 技能 /)
// The 球球 itself (MateView, mate.html) is unchanged: only where it sits, how it moves and its face are set here.

let hAmber = Color(red: 0xF0 / 255, green: 0xA0 / 255, blue: 0x30 / 255)
let hAmber2 = Color(red: 0xFF / 255, green: 0xBE / 255, blue: 0x4D / 255)
let hInk = Color(red: 0x2A / 255, green: 0x1A / 255, blue: 0x05 / 255)
let hDark = Color(red: 0x15 / 255, green: 0x12 / 255, blue: 0x0D / 255)
let hHL = Color(red: 0xFF / 255, green: 0xC3 / 255, blue: 0x5C / 255)
let hTok = Color(red: 0x2A / 255, green: 0x1D / 255, blue: 0x08 / 255)
let hStageBubble = Color(red: 0x1C / 255, green: 0x16 / 255, blue: 0x10 / 255)
let hSheetBg = Color(red: 0x16 / 255, green: 0x16 / 255, blue: 0x16 / 255)

/// A rounded rectangle with its own radius per corner (UnevenRoundedRectangle is iOS 17+; the app runs on 16).
struct BubbleShape: Shape {
    var tl: CGFloat, tr: CGFloat, br: CGFloat, bl: CGFloat
    static let user = BubbleShape(tl: 20, tr: 20, br: 6, bl: 20)
    static let aiw = BubbleShape(tl: 20, tr: 20, br: 20, bl: 6)
    func path(in r: CGRect) -> Path {
        let m = min(r.width, r.height) / 2
        let tl = min(self.tl, m), tr = min(self.tr, m), br = min(self.br, m), bl = min(self.bl, m)
        var p = Path()
        p.move(to: CGPoint(x: r.minX + tl, y: r.minY))
        p.addLine(to: CGPoint(x: r.maxX - tr, y: r.minY))
        p.addArc(center: CGPoint(x: r.maxX - tr, y: r.minY + tr), radius: tr, startAngle: .degrees(-90), endAngle: .degrees(0), clockwise: false)
        p.addLine(to: CGPoint(x: r.maxX, y: r.maxY - br))
        p.addArc(center: CGPoint(x: r.maxX - br, y: r.maxY - br), radius: br, startAngle: .degrees(0), endAngle: .degrees(90), clockwise: false)
        p.addLine(to: CGPoint(x: r.minX + bl, y: r.maxY))
        p.addArc(center: CGPoint(x: r.minX + bl, y: r.maxY - bl), radius: bl, startAngle: .degrees(90), endAngle: .degrees(180), clockwise: false)
        p.addLine(to: CGPoint(x: r.minX, y: r.minY + tl))
        p.addArc(center: CGPoint(x: r.minX + tl, y: r.minY + tl), radius: tl, startAngle: .degrees(180), endAngle: .degrees(270), clockwise: false)
        p.closeSubpath()
        return p
    }
}

/// Inline markdown with bold in light amber (short one-line replies; longer ones go through RichText(strong:)).
func hInline(_ s: String) -> AttributedString { RichText(text: "", strong: hHL).md(s) }

/// The user's bubble: amber gradient, dark text, skill chips, attachment chips.
struct UserBubbleV2: View {
    let text: String
    let attachments: [JSON]
    let skillTitle: (String) -> String
    var body: some View {
        let p = PromptParts(text)
        VStack(alignment: .trailing, spacing: 6) {
            VStack(alignment: .leading, spacing: 6) {
                if !p.skills.isEmpty {
                    HStack(spacing: 4) {
                        ForEach(p.skills, id: \.self) { id in
                            Text("✨ " + skillTitle(id)).font(.caption.weight(.bold)).lineLimit(1)
                                .padding(.horizontal, 7).padding(.vertical, 2)
                                .background(hInk.opacity(0.12)).overlay(Capsule().stroke(hInk.opacity(0.35))).clipShape(Capsule())
                        }
                    }
                }
                if !p.text.isEmpty {
                    Text(inlineMD(p.text)).font(.callout.weight(.medium)).lineSpacing(2).textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .foregroundColor(hInk)
            .padding(.horizontal, 14).padding(.vertical, 10)
            .background(BubbleShape.user.fill(LinearGradient(colors: [hAmber2, hAmber], startPoint: .topLeading, endPoint: .bottomTrailing)))
            ForEach(attachments.indices, id: \.self) { i in
                let a = attachments[i]
                Label(a.s("name"), systemImage: a.s("kind") == "video" ? "film" : fileIcon(a.s("name"))).font(.caption).foregroundColor(.secondary)
                    .lineLimit(1).truncationMode(.middle)
            }
        }
    }
}

/// 球球's reply bubble: dark, amber border, light-amber bold.
struct AiwBubble: View {
    let text: String
    var error = false
    var body: some View {
        Group {
            if !text.contains("\n") && text.count <= 24 {
                Text(hInline(text)).font(.callout).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            } else {
                RichText(text: text, strong: hHL)
            }
        }
        .foregroundColor(Color(white: 0.93))
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(BubbleShape.aiw.fill(hDark))
        .overlay(BubbleShape.aiw.stroke(error ? Color.red.opacity(0.6) : hAmber.opacity(0.67), lineWidth: 1.5))
        .environment(\.colorScheme, .dark)
    }
}

/// Three dots bouncing one after the other.
struct BouncingDots: View {
    @Environment(\.accessibilityReduceMotion) private var still
    var body: some View {
        TimelineView(.animation(minimumInterval: 1 / 30, paused: still)) { ctx in
            let t = ctx.date.timeIntervalSinceReferenceDate
            HStack(spacing: 5) {
                ForEach(0..<3, id: \.self) { i in
                    let ph = max(0, sin(t * 5.7 - Double(i) * 0.7))
                    Circle().fill(hAmber).frame(width: 7, height: 7)
                        .offset(y: still ? 0 : -5 * ph).opacity(still ? 0.7 : 0.35 + 0.65 * ph)
                }
            }
        }
    }
}

/// Before the first token: 球球 thinking (eyes up), a dashed ring turning around it, dots + what it is doing.
struct ThinkingRow: View {
    let note: String
    @State private var spin = false
    @Environment(\.accessibilityReduceMotion) private var still
    var body: some View {
        HStack(spacing: 10) {
            ZStack {
                MateView(status: "", threadId: nil, bus: false, emotion: "30", animate: true).frame(width: 40, height: 40)
                Circle().stroke(hAmber.opacity(0.67), style: StrokeStyle(lineWidth: 2, dash: [4, 4]))
                    .frame(width: 52, height: 52)
                    .rotationEffect(.degrees(spin ? 360 : 0))
                    .animation(still ? nil : Animation.linear(duration: 3.2).repeatForever(autoreverses: false), value: spin)
            }.frame(width: 52, height: 52)
            HStack(spacing: 8) {
                BouncingDots()
                Text(note).font(.footnote).foregroundColor(Color(red: 0xC9 / 255, green: 0xA4 / 255, blue: 0x6A / 255))
            }
            .padding(.horizontal, 14).padding(.vertical, 10)
            .background(BubbleShape(tl: 18, tr: 18, br: 18, bl: 18).fill(hDark))
            .overlay(BubbleShape(tl: 18, tr: 18, br: 18, bl: 18).stroke(hAmber.opacity(0.67), lineWidth: 1.5))
            Spacer(minLength: 20)
        }
        .onAppear { spin = true }
        .accessibilityElement(children: .combine).accessibilityLabel("球球在想：\(note)")
    }
}

/// 球球 in the middle of the home screen with its briefing popping up around it.
struct HomeStage: View {
    @ObservedObject var m: HomeChat
    let typing: Bool
    let act: (JSON) -> Void
    let showChat: () -> Void
    @State private var idx = 0
    @Environment(\.accessibilityReduceMotion) private var still
    private let tick = Timer.publish(every: 4.5, on: .main, in: .common).autoconnect()

    var item: JSON? { m.greet.isEmpty ? nil : m.greet[idx % m.greet.count] }
    var body: some View {
        GeometryReader { geo in
            let orb: CGFloat = min(150, geo.size.width * 0.36)
            let bw = min(260, geo.size.width - 48)
            ZStack {
                MateView(status: "", threadId: nil, bus: false, emotion: "02", animate: true, mood: item?.s("mood") ?? "02")
                    .frame(width: orb, height: orb)
                    .overlay(alignment: idx % 2 == 0 ? Alignment.top : Alignment.bottom) {
                        if let it = item { bubble(it, slot: idx % 4, width: bw).id("b\(idx)") }
                    }
                    .position(x: geo.size.width / 2, y: geo.size.height * 0.52)
                VStack {
                    if !m.msgs.isEmpty {
                        Button(action: showChat) {
                            Text("↑ 聊天记录").font(.caption.weight(.medium)).foregroundColor(hHL)
                                .padding(.horizontal, 12).padding(.vertical, 5)
                                .background(hDark.opacity(0.85)).overlay(Capsule().stroke(hAmber.opacity(0.35))).clipShape(Capsule())
                        }.padding(.top, 10)
                    }
                    if !m.err.isEmpty { Text(m.err).font(.footnote).foregroundColor(.red).padding(.horizontal) }
                    Spacer()
                }
            }
        }
        .onReceive(tick) { _ in
            guard !typing, m.greet.count > 1 else { return }
            withAnimation(still ? nil : Animation.easeInOut(duration: 0.45)) { idx += 1 }
        }
        .onChange(of: m.greetAt) { _ in idx = 0 }
    }
    /// slot 0 above → right, 1 below ← left, 2 above ← left, 3 below → right; the tail (two dots) points at 球球
    @ViewBuilder func bubble(_ it: JSON, slot: Int, width: CGFloat) -> some View {
        let below = slot % 2 == 1
        let dx: CGFloat = (slot == 0 || slot == 3) ? 36 : -36
        let tailRight = dx < 0
        let text = it.s("text")
        let w = min(width, CGFloat(text.count) * 15.5 + 30)
        Button { act(it.o("action")) } label: {
            Text(text).font(.system(size: 15)).foregroundColor(.white).lineSpacing(2).multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 14).padding(.vertical, 10)
                .frame(width: w, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(hStageBubble))
                .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).stroke(hAmber, lineWidth: 1.5))
                .shadow(color: hAmber.opacity(0.15), radius: 12, y: 6)
                .overlay(alignment: tailAlign(below: below, right: tailRight)) {
                    ZStack {
                        Circle().fill(hAmber).frame(width: 11, height: 11).offset(y: below ? -17 : 17)
                        Circle().fill(hAmber.opacity(0.7)).frame(width: 6, height: 6).offset(x: tailRight ? 8 : -8, y: below ? -27 : 27)
                    }.padding(tailRight ? Edge.Set.trailing : Edge.Set.leading, 44)
                }
        }
        .buttonStyle(.plain)
        .opacity(typing ? 0.45 : 1)
        .alignmentGuide(below ? VerticalAlignment.bottom : VerticalAlignment.top) { d in below ? d[VerticalAlignment.top] - 28 : d[VerticalAlignment.bottom] + 28 }
        .offset(x: dx)
        .transition(.asymmetric(insertion: .scale(scale: 0.9).combined(with: .opacity),
                                removal: .scale(scale: 0.75).combined(with: .opacity)))
        .accessibilityHint("点一下就去做")
    }
}

func tailAlign(below: Bool, right: Bool) -> Alignment {
    if below { return right ? .topTrailing : .topLeading }
    return right ? .bottomTrailing : .bottomLeading
}

/// "+" sheet items.
enum AttachKind: CaseIterable, Hashable {
    case photo, video, library, file, task, link, skill
    var emoji: String {
        switch self { case .photo: return "📷"; case .video: return "🎥"; case .library: return "🖼"; case .file: return "📁"
        case .task: return "🖥"; case .link: return "🔗"; case .skill: return "✨" }
    }
    var title: String {
        switch self { case .photo: return "拍照"; case .video: return "录视频"; case .library: return "照片/视频"; case .file: return "文件"
        case .task: return "新建任务"; case .link: return "链接"; case .skill: return "技能 /" }
    }
    var lit: Bool { self == .photo || self == .video }
}

struct AttachSheet: View {
    let pick: (AttachKind) -> Void
    private let cols = Array(repeating: GridItem(.flexible(), spacing: 12), count: 4)
    var body: some View {
        VStack(spacing: 16) {
            Capsule().fill(Color(white: 0.27)).frame(width: 40, height: 5).padding(.top, 10)
            LazyVGrid(columns: cols, spacing: 14) {
                ForEach(AttachKind.allCases, id: \.self) { k in
                    Button { pick(k) } label: {
                        VStack(spacing: 6) {
                            Text(k.emoji).font(.system(size: 28))
                                .frame(width: 62, height: 62)
                                .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(k.lit ? hTok : Color(white: 0.13)))
                                .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).stroke(k.lit ? hAmber : Color(white: 0.23), lineWidth: 1))
                            Text(k.title).font(.caption).foregroundColor(Color(white: 0.8)).lineLimit(1)
                        }
                    }.buttonStyle(.plain).accessibilityLabel(k.title)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 18)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(hSheetBg.ignoresSafeArea())
        .environment(\.colorScheme, .dark)
    }
}

/// The "/" skill picker above the input: name, title, description; tap (or Return for the first) picks one.
struct SkillPicker: View {
    let rows: [JSON]
    let query: String
    let pick: (String) -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(query.isEmpty ? "技能" : "技能 · “\(query)”").font(.caption).foregroundColor(Color(white: 0.5)).padding(.horizontal, 8).padding(.top, 4)
            if rows.isEmpty {
                Text("没有匹配的技能（设置 › 技能 可以添加）").font(.footnote).foregroundColor(Color(white: 0.6)).padding(8)
            } else {
                ScrollView {
                    VStack(spacing: 2) {
                        ForEach(rows.indices, id: \.self) { i in
                            let s = rows[i]
                            Button { pick(s.s("id")) } label: {
                                HStack(spacing: 10) {
                                    Text("/" + s.s("id")).font(.system(.footnote, design: .monospaced).weight(.bold)).foregroundColor(hHL)
                                        .lineLimit(1).frame(minWidth: 96, alignment: .leading)
                                    VStack(alignment: .leading, spacing: 1) {
                                        Text(s.s("title").isEmpty ? s.s("id") : s.s("title")).font(.subheadline).foregroundColor(.white).lineLimit(1)
                                        if !s.s("desc").isEmpty { Text(s.s("desc")).font(.caption2).foregroundColor(Color(white: 0.6)).lineLimit(1) }
                                    }
                                    Spacer(minLength: 0)
                                }
                                .padding(.horizontal, 8).padding(.vertical, 8)
                                .background(RoundedRectangle(cornerRadius: 12).fill(i == 0 ? hTok : Color.clear))
                                .contentShape(Rectangle())
                            }.buttonStyle(.plain)
                        }
                    }
                }.frame(maxHeight: 250)
            }
        }
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Color(white: 0.08)))
        .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).stroke(hAmber.opacity(0.47)))
        .shadow(color: .black.opacity(0.4), radius: 14, y: -4)
        .environment(\.colorScheme, .dark)
    }
}

/// A chosen skill inside the input box (amber token with ×).
struct SkillToken: View {
    let title: String
    let remove: () -> Void
    var body: some View {
        HStack(spacing: 3) {
            Text("✨ " + title).font(.footnote.weight(.semibold)).lineLimit(1)
            Button(action: remove) { Image(systemName: "xmark").font(.system(size: 10, weight: .bold)) }.accessibilityLabel("移除技能")
        }
        .foregroundColor(hHL)
        .padding(.leading, 8).padding(.trailing, 6).padding(.vertical, 4)
        .background(Capsule().fill(hTok)).overlay(Capsule().stroke(hAmber))
        .fixedSize()
    }
}

/// Camera for 拍照 / 录视频 (UIImagePickerController; NSCameraUsageDescription / NSMicrophoneUsageDescription in Info.plist).
enum CamMode: String, Identifiable { case photo, video; var id: String { rawValue } }

struct HomeCamera: UIViewControllerRepresentable {
    let mode: CamMode
    let done: (URL?, UIImage?) -> Void
    func makeCoordinator() -> Coord { Coord(done) }
    func makeUIViewController(context: Context) -> UIImagePickerController {
        let p = UIImagePickerController()
        p.sourceType = .camera
        if mode == .video {
            p.mediaTypes = [UTType.movie.identifier]
            p.cameraCaptureMode = .video
            p.videoQuality = .typeHigh
            p.videoMaximumDuration = 600
        } else {
            p.mediaTypes = [UTType.image.identifier]
            p.cameraCaptureMode = .photo
        }
        p.delegate = context.coordinator
        return p
    }
    func updateUIViewController(_ vc: UIImagePickerController, context: Context) {}
    final class Coord: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let done: (URL?, UIImage?) -> Void
        init(_ d: @escaping (URL?, UIImage?) -> Void) { done = d }
        func imagePickerController(_ p: UIImagePickerController, didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            var keep: URL? = nil
            if let u = info[.mediaURL] as? URL {   // the recorded movie: copy it out before the picker cleans up
                let dst = FileManager.default.temporaryDirectory.appendingPathComponent("video-\(Int(Date().timeIntervalSince1970)).\(u.pathExtension.isEmpty ? "mov" : u.pathExtension)")
                try? FileManager.default.removeItem(at: dst)
                if (try? FileManager.default.copyItem(at: u, to: dst)) != nil { keep = dst } else { keep = u }
            }
            done(keep, info[.originalImage] as? UIImage)
        }
        func imagePickerControllerDidCancel(_ p: UIImagePickerController) { done(nil, nil) }
    }
}

// A video from the photo library comes in as PickedMovie (ChatView.swift): a temp file, never Data in memory.

/// "/" right before the cursor (start of the text or after a space): the query typed after it, else nil.
func slashQuery(_ t: String) -> String? {
    guard let r = t.range(of: "(^|\\s)/[^\\s/]*$", options: .regularExpression) else { return nil }
    let frag = t[r].trimmingCharacters(in: .whitespacesAndNewlines)
    return String(frag.dropFirst())
}

/// Characters a query value may carry unescaped (ASCII only: URLComponents on iOS 16 refuses raw non-ASCII).
let queryValueChars = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")
