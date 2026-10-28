import SwiftUI

// 3.7: one look for task cards, step cards and question cards, in the chat (HomeChat.swift) AND the task view
// (ChatView.swift), light and dark (same as the web's .tcard / .hq in home.css):
// * finished: amber-tinted fill (light #FFF4E0, dark #2A2010) with an amber border and an amber progress bar
// * running / waiting: white (light) or the dark bubble fill (dark) with an amber border
// * failed / stopped: the same with a subtle red accent (thin red bar on the left, red-tinted border)
// 3.6 drew every task card black (a forced dark colour scheme), which read as "something is wrong" in light mode.
// Question / form / confirmation cards look like 球球's bubbles: white + amber border in light, dark + amber in dark.

enum CardTone { case done, live, failed }

func cardTone(_ st: String) -> CardTone {
    if st == "succeeded" { return .done }
    if ["failed", "interrupted", "stopped", "cancelled"].contains(st) { return .failed }
    return .live
}

let cardRed = Color(red: 0xD9 / 255, green: 0x48 / 255, blue: 0x3B / 255)

func cardFill(_ t: CardTone, _ cs: ColorScheme) -> Color {
    switch t {
    case .done: return cs == .dark ? Color(red: 0x2A / 255, green: 0x20 / 255, blue: 0x10 / 255) : Color(red: 0xFF / 255, green: 0xF4 / 255, blue: 0xE0 / 255)
    case .live: return hBubbleFill(cs)
    case .failed: return cs == .dark ? Color(red: 0x1E / 255, green: 0x15 / 255, blue: 0x13 / 255) : Color(red: 0xFF / 255, green: 0xF8 / 255, blue: 0xF6 / 255)
    }
}
func cardBorder(_ t: CardTone, _ cs: ColorScheme) -> Color {
    switch t {
    case .done: return hAmber.opacity(cs == .dark ? 0.55 : 0.8)
    case .live: return hAmber.opacity(cs == .dark ? 0.67 : 0.85)
    case .failed: return cardRed.opacity(cs == .dark ? 0.5 : 0.4)
    }
}
/// the status line / accent text on a card (readable on both fills)
func cardAccent(_ t: CardTone, _ cs: ColorScheme) -> Color {
    t == .failed ? cardRed : hStrong(cs)
}
func cardTitle(_ cs: ColorScheme) -> Color { hBubbleText(cs) }

extension View {
    /// The card background + border of the shared style (rounded 14, a red bar on the left when it failed).
    func taskCardStyle(_ t: CardTone, _ cs: ColorScheme, radius: CGFloat = 14) -> some View {
        self
            .background(RoundedRectangle(cornerRadius: radius, style: .continuous).fill(cardFill(t, cs)))
            .overlay(alignment: .leading) {
                if t == .failed { Rectangle().fill(cardRed.opacity(0.85)).frame(width: 3) }
            }
            .clipShape(RoundedRectangle(cornerRadius: radius, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: radius, style: .continuous).stroke(cardBorder(t, cs), lineWidth: t == .live ? 1.5 : 1.2))
    }
    /// 球球's bubble look for question / confirmation cards (dimmer border once answered or stale).
    func askCardStyle(_ cs: ColorScheme, settled: Bool) -> some View {
        self
            .background(BubbleShape.aiw.fill(hBubbleFill(cs)))
            .clipShape(BubbleShape.aiw)
            .overlay(BubbleShape.aiw.stroke(hAmber.opacity(settled ? (cs == .dark ? 0.4 : 0.5) : (cs == .dark ? 0.67 : 0.85)), lineWidth: 1.5))
    }
}

/// The progress bar of a task card: amber (finished too), red when it failed.
struct CardProgress: View {
    let frac: Double
    let tone: CardTone
    @Environment(\.colorScheme) private var cs
    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(cs == .dark ? Color.white.opacity(0.12) : hAmber.opacity(0.18))
                Capsule().fill(tone == .failed ? cardRed.opacity(0.8) : hAmber).frame(width: max(4, g.size.width * min(1, frac)))
            }
        }.frame(height: 4)
    }
}

/// The square icon on a task card: a spinner while it runs, a check when done, ! when it failed.
struct CardIcon: View {
    let st: String
    var side: CGFloat = 36
    var body: some View {
        let t = cardTone(st)
        ZStack {
            RoundedRectangle(cornerRadius: 9).fill(t == .done ? hAmber : t == .failed ? cardRed.opacity(0.13) : hAmber.opacity(0.16))
            if t == .live && st == "running" { ProgressView().controlSize(.small).tint(hAmber) }
            else if t == .done { Image(systemName: "checkmark").font(.system(size: side * 0.42, weight: .bold)).foregroundColor(.white) }
            else if t == .failed { Image(systemName: "exclamationmark").font(.system(size: side * 0.42, weight: .bold)).foregroundColor(cardRed) }
            else { Image(systemName: st == "takeover" ? "hand.raised.fill" : "desktopcomputer").foregroundColor(hAmber) }
        }.frame(width: side, height: side)
    }
}

/// What a resolved ask / form card says. 3.6 printed the raw answer: a form answered with nothing (JSON null → NSNull)
/// came out as "已回答：<null>". The server sends resolved.text since 3.7 (chat.py answer_text); older messages are
/// derived here the same way.
func resolvedText(_ r: JSON) -> String {
    if let b = r["answer"] as? Bool { return b ? "已允许" : "已拒绝" }
    var t = (r["text"] as? String) ?? ""
    if r["text"] == nil, let a = r["answer"] as? String {
        if a == "done" { t = "已完成，交还给电脑" }
        else if a.hasPrefix("{"), let d = a.data(using: .utf8), let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
            t = o.compactMap { (kv) -> String? in
                let s = "\(kv.value)"
                return s.isEmpty || kv.value is NSNull ? nil : "\(kv.key)：\(s)"
            }.sorted().joined(separator: "；")
        } else { t = a }
    }
    t = t.trimmingCharacters(in: .whitespacesAndNewlines)
    if t.isEmpty || t == "<null>" || t.lowercased() == "null" || t == "{}" { return "已回答" }
    if t.hasPrefix("已") { return t }
    return "已回答：" + String(t.prefix(60))
}
