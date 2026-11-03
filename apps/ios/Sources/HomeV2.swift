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
// 3.6: bubbles placed from the 球球 circle (StageLayout), no quick chips, light-mode white 球球 bubbles, the small 球球
// head beside replies / cards / thinking, a WeChat-like "+" panel.
// The 球球 itself (MateView, mate.html) is unchanged: only where it sits, how it moves and its face are set here.

let hAmber = Color(red: 0xF0 / 255, green: 0xA0 / 255, blue: 0x30 / 255)
let hAmber2 = Color(red: 0xFF / 255, green: 0xBE / 255, blue: 0x4D / 255)
let hInk = Color(red: 0x2A / 255, green: 0x1A / 255, blue: 0x05 / 255)
let hDark = Color(red: 0x15 / 255, green: 0x12 / 255, blue: 0x0D / 255)
let hHL = Color(red: 0xFF / 255, green: 0xC3 / 255, blue: 0x5C / 255)
let hTok = Color(red: 0x2A / 255, green: 0x1D / 255, blue: 0x08 / 255)
let hStageBubble = Color(red: 0x1C / 255, green: 0x16 / 255, blue: 0x10 / 255)

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
    /// 3.9.17: a pasted plan of thousands of characters froze the task view; the bubble shows 600 and 展开全文
    @State var expanded = false
    var body: some View {
        let p = PromptParts(text)
        let fold = expanded ? (p.text, false) : foldedText(p.text, limit: 600)
        let shownText = fold.0, folded = fold.1
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
                if !p.text.isEmpty {   // 3.8: partial selection + 复制 (SelectableText.swift)
                    SelectableText(text: uiAttributed(inlineMD(shownText), style: .callout, weight: .medium, color: UIColor(hInk), lineSpacing: 2),
                                   whole: p.text, link: UIColor(hInk))
                    if folded || expanded {
                        Button { expanded.toggle() } label: {
                            Text(expanded ? "收起" : "展开全文（共 \(p.text.count) 字）").font(.caption.weight(.bold)).underline()
                        }.buttonStyle(.plain)
                    }
                }
            }
            .foregroundColor(hInk)
            .padding(.horizontal, 14).padding(.vertical, 10)
            .background(BubbleShape.user.fill(LinearGradient(colors: [hAmber2, hAmber], startPoint: .topLeading, endPoint: .bottomTrailing))
                .copyMenu(p.text))
            ForEach(attachments.indices, id: \.self) { i in
                let a = attachments[i]
                Label(a.s("name"), systemImage: a.s("kind") == "video" ? "film" : fileIcon(a.s("name"))).font(.caption).foregroundColor(.secondary)
                    .lineLimit(1).truncationMode(.middle)
            }
        }
    }
}

/// Light mode (3.6): 球球's bubbles are white with an amber border and dark text; bold / links in a deeper amber
/// (the light #ffc35c of the dark bubble is unreadable on white). Dark mode keeps the v2 dark bubble.
let hHLLight = Color(red: 0xB8 / 255, green: 0x68 / 255, blue: 0x0C / 255)
func hStrong(_ cs: ColorScheme) -> Color { cs == .dark ? hHL : hHLLight }
func hBubbleFill(_ cs: ColorScheme) -> Color { cs == .dark ? hDark : Color.white }
func hBubbleText(_ cs: ColorScheme) -> Color { cs == .dark ? Color(white: 0.93) : Color(red: 0x1D / 255, green: 0x1A / 255, blue: 0x16 / 255) }

/// 球球's reply bubble: dark with an amber border (dark mode) / white with an amber border (light mode).
struct AiwBubble: View {
    let text: String
    var error = false
    @Environment(\.colorScheme) private var cs
    var body: some View {
        Group {
            if !text.contains("\n") && text.count <= 24 {   // short: the bubble hugs the text (still selectable)
                SelectableText(text: uiAttributed(inlineMD(text), style: .callout, color: UIColor(hBubbleText(cs)), strong: UIColor(hStrong(cs))),
                               whole: text, link: UIColor(hStrong(cs)))
            } else {
                RichText(text: text, strong: hStrong(cs))
            }
        }
        .foregroundColor(hBubbleText(cs))
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(BubbleShape.aiw.fill(hBubbleFill(cs)).copyMenu(text))
        .overlay(BubbleShape.aiw.stroke(error ? Color.red.opacity(0.6) : hAmber.opacity(cs == .dark ? 0.67 : 0.85), lineWidth: 1.5))
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

/// Before the first token (3.6): the small 球球 head in the avatar column, moving and thinking (eyes up, emotion 30),
/// next to a bubble with the bouncing dots + what it is doing. Same size and place as the avatar of every reply.
struct ThinkingRow: View {
    let note: String
    @AppStorage("mate") private var mateOn = true
    @Environment(\.colorScheme) private var cs
    var body: some View {
        HStack(alignment: .bottom, spacing: 8) {
            Group {
                if mateOn {
                    MateView(status: "", threadId: nil, bus: false, emotion: "30", animate: true)
                } else { MateHead(size: MateAvatar.side, face: false) }
            }
            .frame(width: MateAvatar.side, height: MateAvatar.side).clipShape(Circle())
            .padding(.bottom, 2)
            HStack(spacing: 8) {
                BouncingDots()
                Text(note).font(.footnote).foregroundColor(cs == .dark ? Color(red: 0xC9 / 255, green: 0xA4 / 255, blue: 0x6A / 255) : hHLLight)
            }
            .padding(.horizontal, 14).padding(.vertical, 10)
            .background(BubbleShape.aiw.fill(hBubbleFill(cs)))
            .overlay(BubbleShape.aiw.stroke(hAmber.opacity(cs == .dark ? 0.67 : 0.85), lineWidth: 1.5))
            Spacer(minLength: 20)
        }
        .accessibilityElement(children: .combine).accessibilityLabel("球球在想：\(note)")
    }
}

// MARK: - 球球 stage layout (3.6)
// 3.5 hung the bubble in an .overlay of the 球球 frame and moved it with alignment guides + offsets: on the phone the guide
// did not take and the bubble sat on top of 球球. Now every position is computed from the 球球 circle and the stage
// rectangle: a bubble goes into a slot outside the circle (above / below, and left / right when there is room beside it),
// never closer than `gap` to the circle, never outside the stage (safe areas are outside the stage already), and when no
// slot can hold the whole text (a small screen, landscape) it takes the roomier side and is cut to the lines that fit.
// The same math is in apps/web/home.js (hPlace).
enum StageSide { case above, below, left, right }

struct StagePlacement: Equatable {
    var rect: CGRect
    var side: StageSide
    var lines: Int?          // nil: the whole text fits; else cut to this many lines
}

enum StageLayout {
    static let gap: CGFloat = 30      // bubble edge <-> 球球 circle (room for the two tail dots)
    static let edge: CGFloat = 14     // bubble <-> stage edge
    static let lineH: CGFloat = 20    // 15 pt text, 2 pt line spacing
    static let padV: CGFloat = 20     // vertical padding inside the bubble
    static let sideMin: CGFloat = 170 // narrowest bubble beside 球球

    /// The order slots are tried in, starting at the bubble's turn: (side, lean); lean moves an above/below bubble
    /// towards one side (-1 left … +1 right) so consecutive bubbles alternate.
    static func slots(sides: Bool) -> [(StageSide, CGFloat)] {
        if sides { return [(StageSide.above, 1), (StageSide.right, 0), (StageSide.below, -1), (StageSide.left, 0), (StageSide.above, -1), (StageSide.below, 1)] }
        return [(StageSide.above, 1), (StageSide.below, -1), (StageSide.above, -1), (StageSide.below, 1)]
    }
    static func sideRoom(_ c: CGPoint, _ r: CGFloat, _ area: CGRect) -> CGFloat {
        min(area.maxX - edge - (c.x + r + gap), (c.x - r - gap) - (area.minX + edge))
    }
    /// Where the bubble of natural size `b` goes for turn `slot`.
    static func place(slot: Int, bubble b: CGSize, orb c: CGPoint, r: CGFloat, area: CGRect) -> StagePlacement {
        let list = slots(sides: sideRoom(c, r, area) >= sideMin)
        for i in 0..<list.count {
            let (side, lean) = list[(slot % list.count + i) % list.count]
            let f = frame(side, lean, b, c, r, area)
            if fits(f, c, r, area) { return StagePlacement(rect: f, side: side, lines: nil) }
        }
        // nothing holds the whole text: the placement that shows the most lines, cut to what fits —
        // above / below at full width, or beside 球球 at the width there is (landscape, small screens)
        var best: (StagePlacement, Int)? = nil
        func offer(_ p: StagePlacement, _ shown: Int) { if fits(p.rect, c, r, area), shown > (best?.1 ?? 0) { best = (p, shown) } }
        let need = max(1, Int(((b.height - padV) / lineH).rounded()))
        for side in [StageSide.above, .below] {
            let room = side == .above ? (c.y - r - gap) - (area.minY + edge) : (area.maxY - edge) - (c.y + r + gap)
            let lines = min(need, Int(((room - padV) / lineH).rounded(.down)))
            guard lines >= 1 else { continue }
            let f = frame(side, side == .above ? 1 : -1, CGSize(width: b.width, height: padV + CGFloat(lines) * lineH), c, r, area)
            offer(StagePlacement(rect: f, side: side, lines: lines), lines)
        }
        let sr = sideRoom(c, r, area)
        if sr >= 110 {
            let w2 = min(b.width, sr)
            let want = Int((CGFloat(need) * b.width / w2).rounded(.up))
            let lines = min(want, Int(((area.height - 2 * edge - padV) / lineH).rounded(.down)))
            if lines >= 1 {
                for side in [StageSide.right, .left] {
                    let f = frame(side, 0, CGSize(width: w2, height: padV + CGFloat(lines) * lineH), c, r, area)
                    offer(StagePlacement(rect: f, side: side, lines: lines), lines)
                }
            }
        }
        if let bp = best { return bp.0 }
        // last resort (a stage too small for anything): one line in the roomier band, kept inside the stage
        let up = (c.y - r - gap) - (area.minY + edge), down = (area.maxY - edge) - (c.y + r + gap)
        let side: StageSide = up >= down ? .above : .below
        var f = frame(side, 0, CGSize(width: b.width, height: padV + lineH), c, r, area)
        f.origin.y = max(area.minY + edge, min(f.origin.y, area.maxY - edge - f.height))
        return StagePlacement(rect: f, side: side, lines: 1)
    }
    static func frame(_ side: StageSide, _ lean: CGFloat, _ b: CGSize, _ c: CGPoint, _ r: CGFloat, _ area: CGRect) -> CGRect {
        let minX = area.minX + edge, maxX = area.maxX - edge, minY = area.minY + edge, maxY = area.maxY - edge
        func clampX(_ x: CGFloat) -> CGFloat { max(minX, min(x, maxX - b.width)) }
        func clampY(_ y: CGFloat) -> CGFloat { max(minY, min(y, maxY - b.height)) }
        switch side {
        case .above: return CGRect(x: clampX(c.x - b.width / 2 + lean * b.width * 0.28), y: c.y - r - gap - b.height, width: b.width, height: b.height)
        case .below: return CGRect(x: clampX(c.x - b.width / 2 + lean * b.width * 0.28), y: c.y + r + gap, width: b.width, height: b.height)
        case .right: return CGRect(x: c.x + r + gap, y: clampY(c.y - b.height * 0.65), width: b.width, height: b.height)
        case .left: return CGRect(x: c.x - r - gap - b.width, y: clampY(c.y - b.height * 0.65), width: b.width, height: b.height)
        }
    }
    /// Inside the stage and clear of the circle (+ most of the gap).
    static func fits(_ f: CGRect, _ c: CGPoint, _ r: CGFloat, _ area: CGRect) -> Bool {
        let inner = area.insetBy(dx: edge - 0.5, dy: edge - 0.5)
        guard inner.contains(f) else { return false }
        return distance(c, f) >= r + gap * 0.8
    }
    /// Distance from a point to a rectangle (0 inside).
    static func distance(_ p: CGPoint, _ f: CGRect) -> CGFloat {
        let dx = max(f.minX - p.x, 0, p.x - f.maxX), dy = max(f.minY - p.y, 0, p.y - f.maxY)
        return (dx * dx + dy * dy).squareRoot()
    }
    /// The tail: two dots between the bubble edge facing 球球 and the circle.
    /// 3.8.2: 球球 + its briefing bubble as ONE group, centred vertically in the stage (the space between the 聊天记录
    /// button and the input bar; the keyboard shrinks the stage). The bubble sits under 球球, never over it: before,
    /// 球球 was centred alone at 52 % and the bubble placed around it, so on a phone the pair sat low and a long bubble
    /// could cover the orb (IMG_2936). Returns 球球's centre and the bubble (cut to the lines that fit).
    static func group(bubble b: CGSize, d: CGFloat, area: CGRect) -> (CGPoint, StagePlacement) {
        let minY = area.minY + edge, maxY = area.maxY - edge
        let room = max(0, (maxY - minY) - d - gap)              // height left for the bubble under 球球
        var h = b.height, lines: Int? = nil
        if h > room {
            let n = max(1, Int(((room - padV) / lineH).rounded(.down)))
            lines = n; h = min(h, padV + CGFloat(n) * lineH)
        }
        let groupH = d + gap + h
        let top = max(minY, area.minY + (area.height - groupH) / 2)
        let c = CGPoint(x: area.midX, y: top + d / 2)
        let x = max(area.minX + edge, min(area.midX - b.width / 2, area.maxX - edge - b.width))
        return (c, StagePlacement(rect: CGRect(x: x, y: top + d + gap, width: b.width, height: h), side: .below, lines: lines))
    }
    static func tail(_ p: StagePlacement, _ c: CGPoint, _ r: CGFloat) -> (CGPoint, CGPoint) {
        let f = p.rect
        let start: CGPoint
        switch p.side {
        case .above: start = CGPoint(x: max(f.minX + 24, min(c.x, f.maxX - 24)), y: f.maxY)
        case .below: start = CGPoint(x: max(f.minX + 24, min(c.x, f.maxX - 24)), y: f.minY)
        case .right: start = CGPoint(x: f.minX, y: max(f.minY + 16, min(c.y, f.maxY - 16)))
        case .left: start = CGPoint(x: f.maxX, y: max(f.minY + 16, min(c.y, f.maxY - 16)))
        }
        let vx = start.x - c.x, vy = start.y - c.y, len = max(1, (vx * vx + vy * vy).squareRoot())
        let edgePt = CGPoint(x: c.x + vx / len * r, y: c.y + vy / len * r)
        func lerp(_ t: CGFloat) -> CGPoint { CGPoint(x: start.x + (edgePt.x - start.x) * t, y: start.y + (edgePt.y - start.y) * t) }
        return (lerp(0.32), lerp(0.68))
    }
}

private struct StageSizeKey: PreferenceKey {
    static var defaultValue: CGSize = .zero
    static func reduce(value: inout CGSize, nextValue: () -> CGSize) { let n = nextValue(); if n != .zero { value = n } }
}
private struct StageWidthKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

/// 球球 in the middle of the home screen with its briefing popping up around it.
struct HomeStage: View {
    @ObservedObject var m: HomeChat
    let typing: Bool
    let act: (JSON) -> Void
    let showChat: () -> Void
    @State private var idx = 0
    @State private var lineW: CGFloat = 0      // the text on one line (natural width)
    @State private var natural: CGSize = .zero // the bubble at its width, uncut
    @Environment(\.accessibilityReduceMotion) private var still
    @Environment(\.colorScheme) private var cs
    private let tick = Timer.publish(every: 4.5, on: .main, in: .common).autoconnect()

    var item: JSON? { m.greet.isEmpty ? nil : m.greet[idx % m.greet.count] }
    /// the briefing text without Markdown marks (a bubble is plain text)
    func plain(_ s: String) -> String {
        s.replacingOccurrences(of: "**", with: "").replacingOccurrences(of: "__", with: "").replacingOccurrences(of: "`", with: "")
    }
    var body: some View {
        GeometryReader { geo in
            let top: CGFloat = m.msgs.isEmpty ? 4 : 46               // below the 聊天记录 button
            let area = CGRect(x: 0, y: top, width: geo.size.width, height: max(120, geo.size.height - top - 4))
            let orb: CGFloat = max(72, min(150, geo.size.width * 0.36, area.height * 0.3))
            let maxW = min(270, area.width - 2 * StageLayout.edge)
            let text = plain(item?.s("text") ?? "")
            let w = max(60, min(maxW, ceil(lineW) + 28 + 1))
            // 3.8.2: 球球 and its bubble are centred together; with no bubble yet, 球球 alone sits in the middle
            let speaking = item != nil && natural != .zero
            let g = StageLayout.group(bubble: CGSize(width: w, height: speaking ? natural.height : 0), d: orb, area: area)
            let c = speaking ? g.0 : CGPoint(x: area.midX, y: area.midY)
            ZStack(alignment: .topLeading) {
                // measuring copies (never shown): the one-line width, then the height at the chosen width
                Text(text).font(.system(size: 15)).fixedSize()
                    .background(GeometryReader { g in Color.clear.preference(key: StageWidthKey.self, value: g.size.width) })
                    .hidden().accessibilityHidden(true)
                label(text, width: w, lines: nil)
                    .background(GeometryReader { g in Color.clear.preference(key: StageSizeKey.self, value: g.size) })
                    .hidden().accessibilityHidden(true)

                MateView(status: "", threadId: nil, bus: false, emotion: "02", animate: true, mood: item?.s("mood") ?? "02")
                    .frame(width: orb, height: orb)
                    .position(c)
                if let it = item, speaking {
                    let p = g.1
                    let dots = StageLayout.tail(p, c, orb / 2)
                    ZStack(alignment: .topLeading) {
                        Circle().fill(hAmber).frame(width: 11, height: 11).position(dots.0)
                        Circle().fill(hAmber).frame(width: 6, height: 6).position(dots.1)
                        // anchored by its top-left corner (a height measured a bit short grows downwards, away from 球球,
                        // instead of spilling over it as a centred .position did)
                        Button { act(it.o("action")) } label: { label(text, width: p.rect.width, lines: p.lines) }
                            .buttonStyle(.plain)
                            .copyMenu(text)   // 3.8: tap runs it, long-press copies it (a tap target can't also select)
                            .frame(width: p.rect.width, alignment: .topLeading)
                            .offset(x: p.rect.minX, y: p.rect.minY)
                            .accessibilityHint("点一下就去做")
                    }
                    .frame(width: geo.size.width, height: geo.size.height, alignment: .topLeading)
                    .opacity(typing ? 0.45 : 1)
                    .id("b\(idx)")
                    .transition(.asymmetric(insertion: .scale(scale: 0.9).combined(with: .opacity),
                                            removal: .scale(scale: 0.75).combined(with: .opacity)))
                }
                VStack {
                    if !m.msgs.isEmpty {
                        Button(action: showChat) {
                            Text("↑ 聊天记录").font(.caption.weight(.medium)).foregroundColor(hStrong(cs))
                                .padding(.horizontal, 12).padding(.vertical, 5)
                                .background(cs == .dark ? hDark.opacity(0.85) : Color.white).overlay(Capsule().stroke(hAmber.opacity(cs == .dark ? 0.35 : 0.7))).clipShape(Capsule())
                        }.padding(.top, 10)
                    }
                    if !m.err.isEmpty { Text(m.err).font(.footnote).foregroundColor(.red).padding(.horizontal) }
                    Spacer()
                }.frame(width: geo.size.width, height: geo.size.height)
            }
            .onPreferenceChange(StageWidthKey.self) { lineW = $0 }
            .onPreferenceChange(StageSizeKey.self) { natural = $0 }
        }
        .onReceive(tick) { _ in
            guard !typing, m.greet.count > 1 else { return }
            withAnimation(still ? nil : Animation.easeInOut(duration: 0.45)) { idx += 1 }
        }
        .onChange(of: m.greetAt) { _ in idx = 0 }
    }
    /// The bubble itself (3.7): white with an amber border and dark text in light mode, the dark fill with the amber
    /// border in dark mode; the two tail dots are amber in both.
    func label(_ text: String, width: CGFloat, lines: Int?) -> some View {
        Text(text).font(.system(size: 15)).foregroundColor(cs == .dark ? .white : hBubbleText(cs)).lineSpacing(2).multilineTextAlignment(.leading)
            .lineLimit(lines).truncationMode(.tail)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.horizontal, 14).padding(.vertical, 10)
            .frame(width: width, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(cs == .dark ? hStageBubble : Color.white))
            .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).stroke(hAmber, lineWidth: 1.5))
            .shadow(color: hAmber.opacity(cs == .dark ? 0.15 : 0.18), radius: 12, y: 6)
    }
}

/// "+" sheet items.
enum AttachKind: CaseIterable, Hashable {
    case photo, video, library, file, task, link, skill
    /// SF Symbols, line style (WeChat-like panel, 3.6)
    var symbol: String {
        switch self { case .photo: return "camera"; case .video: return "video"; case .library: return "photo.on.rectangle"; case .file: return "folder"
        case .task: return "desktopcomputer"; case .link: return "link"; case .skill: return "sparkles" }
    }
    var title: String {
        switch self { case .photo: return "拍照"; case .video: return "录视频"; case .library: return "照片/视频"; case .file: return "文件"
        case .task: return "新建任务"; case .link: return "链接"; case .skill: return "技能 /" }
    }
}

/// The "+" panel (3.6, like WeChat's): light grey panel (#F7F7F7; dark grey in dark mode), white rounded-square tiles
/// (dark grey in dark mode) with a thin amber border, grey line icons, the label under the tile, 4 per row, 2 rows a
/// page, more pages swipe sideways with dots.
struct AttachSheet: View {
    /// which tiles (the task view shows only the file / skill ones, 3.7)
    var kinds: [AttachKind] = AttachKind.allCases
    let pick: (AttachKind) -> Void
    @Environment(\.colorScheme) private var cs
    static let perPage = 8
    static let height: CGFloat = 262
    var pages: [[AttachKind]] {
        let all = kinds
        return stride(from: 0, to: all.count, by: AttachSheet.perPage).map { Array(all[$0..<min($0 + AttachSheet.perPage, all.count)]) }
    }
    var panel: Color { cs == .dark ? Color(red: 0x1E / 255, green: 0x1E / 255, blue: 0x1E / 255) : Color(red: 0xF7 / 255, green: 0xF7 / 255, blue: 0xF7 / 255) }
    var tile: Color { cs == .dark ? Color(red: 0x2C / 255, green: 0x2C / 255, blue: 0x2E / 255) : Color.white }
    var glyph: Color { cs == .dark ? Color(white: 0.78) : Color(white: 0.32) }
    var caption: Color { cs == .dark ? Color(white: 0.62) : Color(white: 0.45) }
    var body: some View {
        VStack(spacing: 0) {
            Capsule().fill(Color.gray.opacity(0.35)).frame(width: 36, height: 5).padding(.top, 8).padding(.bottom, 14)
            if pages.count > 1 {
                TabView { ForEach(pages.indices, id: \.self) { i in grid(pages[i]) } }
                    .tabViewStyle(.page(indexDisplayMode: .always))
                    .indexViewStyle(.page(backgroundDisplayMode: .never))
            } else {
                grid(pages.first ?? [])
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(panel.ignoresSafeArea())
    }
    func grid(_ items: [AttachKind]) -> some View {
        LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 10), count: 4), spacing: 16) {
            ForEach(items, id: \.self) { k in
                Button { pick(k) } label: {
                    VStack(spacing: 7) {
                        Image(systemName: k.symbol).font(.system(size: 25, weight: .regular)).foregroundColor(glyph)
                            .frame(width: 62, height: 62)
                            .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(tile))
                            .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).stroke(hAmber.opacity(cs == .dark ? 0.35 : 0.3), lineWidth: 0.75))
                        Text(k.title).font(.system(size: 12)).foregroundColor(caption).lineLimit(1)
                    }.frame(maxWidth: .infinity).contentShape(Rectangle())
                }.buttonStyle(.plain).accessibilityLabel(k.title)
            }
        }
        .padding(.horizontal, 20)
        .frame(maxHeight: .infinity, alignment: .top)
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

// MARK: - Bottom pinning for the chat and the task view (3.6)
// Both screens are "newest at the bottom" scroll views with the composer in .safeAreaInset(edge: .bottom).
// Where the content ends and where the composer starts are both measured in global coordinates, so "how far the
// newest line is below what you see" is exact (no guessing at safe areas or keyboard insets):
// * pinned (within 60 pt of the end): new messages, streaming text and content that grows after layout (pictures,
//   steps, RichText measuring late, LazyVStack replacing estimated row heights) keep it at the end
// * scrolled up: nothing moves; a "↓ 最新消息" pill (like the web's #hToLatest) shows when the end is more than 300 pt
//   away or something new came in, and tapping it goes to the newest message
struct ScrollMetrics: Equatable { var endY: CGFloat = 0; var contentH: CGFloat = 0 }
struct ScrollMetricsKey: PreferenceKey {
    static var defaultValue = ScrollMetrics()
    static func reduce(value: inout ScrollMetrics, nextValue: () -> ScrollMetrics) { let n = nextValue(); if n.contentH > 0 { value = n } }
}
struct ComposerTopKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { let n = nextValue(); if n > 0 { value = n } }
}
extension View {
    /// on the scroll view's content stack: where it ends (global) and how tall it is
    func reportsScrollMetrics() -> some View {
        background(GeometryReader { g in
            Color.clear.preference(key: ScrollMetricsKey.self, value: ScrollMetrics(endY: g.frame(in: .global).maxY, contentH: g.size.height))
        })
    }
    /// on the composer in .safeAreaInset(edge: .bottom): where the visible part of the scroll view ends (global)
    func reportsComposerTop() -> some View {
        background(GeometryReader { g in Color.clear.preference(key: ComposerTopKey.self, value: g.frame(in: .global).minY) })
    }
}

final class BottomPin: ObservableObject {
    static let near: CGFloat = 60, far: CGFloat = 300
    @Published private(set) var showJump = false
    private(set) var pinned = true
    private(set) var opened = false
    private var last = ScrollMetrics()
    private var bottom: CGFloat = 0
    private var unseen = false
    private var followUntil = Date.distantPast
    private var holdUntil = Date.distantPast   // a jump is under way: the frames in between do not unpin
    private var lastFix = Date.distantPast
    private var fixes = 0   // 3.9.17: at most a few empty-space fixes per jump (a mis-measured end looped forever)
    /// scrolls the ScrollViewReader to the "end" anchor (set by the view; animated or not)
    var jumpAction: ((Bool) -> Void)?

    /// Go to the newest message and stay there while the content settles (`follow` seconds of growth are followed).
    func toEnd(animated: Bool = false, follow: TimeInterval = 1.5) {
        opened = true
        pinned = true; unseen = false; fixes = 0
        followUntil = max(followUntil, Date().addingTimeInterval(follow))
        holdUntil = Date().addingTimeInterval(animated ? 0.6 : 0.25)
        publish()
        DispatchQueue.main.async { self.jumpAction?(animated) }
        // once more after layout: rows measured late (pictures, lazy rows) move the end after the first jump
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { if self.pinned { self.jumpAction?(false) } }
        // 3.9.0: and once more when a tall task card has been measured (the chat went blank after a new task)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.9) { if self.pinned { self.jumpAction?(false) } }
    }
    /// Something new arrived (a message, a streamed piece, a task). `own`: the user's own send.
    func newContent(own: Bool = false) {
        if own || pinned { toEnd(animated: false, follow: 1.2) } else { unseen = true; publish() }
    }
    /// The content's end / height changed (scrolling, or the content grew). `live`: a reply or task is running.
    func metrics(_ mm: ScrollMetrics, live: Bool) {
        let grew = last.contentH > 0 && abs(mm.contentH - last.contentH) > 0.5
        last = mm
        // 3.9.0 blank chat after a new task: the lazy list shrank under a scroll position past its new end (the
        // "正在想…" row and the temporary message gave way to the task card), so the screen showed nothing until you
        // scrolled up. When the content's end is well above the visible bottom while the content is taller than the
        // screen, we are scrolled into empty space: go back to the real end.
        if pinned, bottom > 0, mm.contentH > bottom * 0.8, mm.endY < bottom - BottomPin.near,
           Date().timeIntervalSince(lastFix) > 0.4, fixes < 3 {
            lastFix = Date(); fixes += 1
            holdUntil = max(holdUntil, Date().addingTimeInterval(0.2))
            DispatchQueue.main.async { self.jumpAction?(false) }
            return
        }
        if grew && pinned && (live || Date() < followUntil) {
            holdUntil = max(holdUntil, Date().addingTimeInterval(0.1))
            DispatchQueue.main.async { self.jumpAction?(false) }
            return
        }
        recompute()
    }
    /// The composer's top moved (keyboard, attachments, skill picker): stay at the end if we were there.
    func composer(_ top: CGFloat) {
        guard top > 0 else { return }
        let moved = bottom > 0 && abs(top - bottom) > 0.5
        bottom = top
        if moved && pinned { holdUntil = Date().addingTimeInterval(0.3); DispatchQueue.main.async { self.jumpAction?(false) } }
        else { recompute() }
    }
    private func recompute() {
        guard bottom > 0, last.contentH > 0 else { return }
        let d = last.endY - bottom          // how far the newest line is below the visible area
        if Date() < holdUntil && d > BottomPin.near { publish(); return }
        pinned = d < BottomPin.near
        if pinned { unseen = false }
        publish()
    }
    private func publish() {
        let d = bottom > 0 ? last.endY - bottom : 0
        let show = !pinned && (d > BottomPin.far || unseen)
        if show != showJump { showJump = show }
    }
}

/// The floating "↓ 最新消息" pill (3.7, same as the web's .fab #hToLatest): white (light) / the dark bubble fill (dark)
/// with an amber border, soft shadow, 13 pt semibold; it floats above the composer (see HomeChatView / ChatView).
struct JumpToLatest: View {
    let go: () -> Void
    @Environment(\.colorScheme) private var cs
    var body: some View {
        Button(action: go) {
            Text("↓ 最新消息").font(.system(size: 13, weight: .semibold)).foregroundColor(hBubbleText(cs))
                .padding(.horizontal, 14).padding(.vertical, 8)
                .background(Capsule().fill(hBubbleFill(cs)))
                .overlay(Capsule().stroke(hAmber.opacity(cs == .dark ? 0.75 : 0.9), lineWidth: 1.5))
                .shadow(color: .black.opacity(cs == .dark ? 0.4 : 0.14), radius: 10, y: 3)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("跳到最新消息")
        .transition(.opacity.combined(with: .scale(scale: 0.9)))
    }
}
