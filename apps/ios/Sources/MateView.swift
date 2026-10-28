import SwiftUI
import WebKit

// 球球 expression assistant in the chat header. The character is the Emotion Ball 球球 in its 暗黑 look
// (github.com/sam70361/aora-bot emotion-ball/; visuals for personal learning/research only, no commercial use, see
// apps/web/vendor/emotionball/LICENSE + NOTICE.md) running in a small transparent WKWebView that loads
// the bundled mate/mate.html (copied from apps/web by build-ipa.sh; the server's /mate.html is the fallback).
// The mapping task event -> emotion id lives in mate-map.js, shared with the web app; Swift only forwards the raw
// live events of the shown thread (MateBus) and the shown task's stored status. Off switch: 设置 › 表情助手.

/// Forwards live session events of the thread on screen to the visible 球球.
final class MateBus {
    static let shared = MateBus()
    private weak var web: WKWebView?
    private var thread: String?
    private var ready = false
    private var pending: [String] = []

    func attach(_ w: WKWebView, thread t: String?) {
        if web !== w { web = w; ready = false; pending = [] }
        thread = t
    }
    func loaded(_ w: WKWebView) {
        guard w === web else { return }
        ready = true
        let q = pending; pending = []
        for s in q { w.evaluateJavaScript(s, completionHandler: nil) }
    }
    func js(_ s: String) {
        DispatchQueue.main.async {
            guard let w = self.web else { return }
            if self.ready { w.evaluateJavaScript(s, completionHandler: nil) } else { self.pending.append(s) }
        }
    }
    /// A live event from a session socket (any thread): only the thread on screen moves the header 球球.
    func event(_ e: JSON) {
        let tid = e.s("thread_id")
        guard !tid.isEmpty, tid == thread else { return }
        var slim: JSON = ["type": e.s("type")]
        if let d = e["data"] as? JSON { slim["data"] = d }
        guard JSONSerialization.isValidJSONObject(slim), let data = try? JSONSerialization.data(withJSONObject: slim),
              let s = String(data: data, encoding: .utf8) else { return }
        js("window.aiwMate&&aiwMate.event(\(s))")
    }
}

/// The inline navigation title with the 球球 beside it (hidden on the empty home, where the big 球球 is the mascot).
struct MateTitle: View {
    let title: String
    let status: String
    let threadId: String?
    var showMate = true
    @AppStorage("mate") private var on = true
    var body: some View {
        HStack(spacing: 6) {
            if on && showMate { MateView(status: status, threadId: threadId).frame(width: 30, height: 30).accessibilityLabel("球球") }
            Text(title).font(.headline).lineLimit(1)
        }
    }
}

/// iOS v2: the disc behind 球球 is the app accent (mate.html?ring=amber); the web keeps the dark disc.
/// `bus: false` makes a standalone picture (computer list) that never takes the live events from the chat's 球球;
/// `emotion` then picks its face (e.g. "00" asleep/offline, "02" idle).
struct MateView: UIViewRepresentable {
    let status: String
    let threadId: String?
    var bus = true
    var emotion: String? = nil
    var ring = "amber"
    /// For a standalone picture (`bus: false`): keep it moving (an online computer, the card of a working task).
    var animate = false
    /// For a standalone picture (`bus: false`): change its face live (the home stage follows each briefing item's mood).
    var mood: String? = nil
    @Environment(\.scenePhase) private var phase
    @Environment(\.accessibilityReduceMotion) private var still

    final class Coord: NSObject, WKNavigationDelegate {
        var status = ""
        var active: Bool?
        var bus = true
        var ready = false
        var mood: String?
        var shown: String?
        func webView(_ w: WKWebView, didFinish navigation: WKNavigation!) {
            if bus { MateBus.shared.loaded(w); return }
            ready = true
            push(w)
        }
        /// standalone 球球: set the face it should show now (aiwMate.set in mate.html; burst on 完成)
        func push(_ w: WKWebView) {
            guard ready, let m = mood, m != shown else { return }
            shown = m
            let id = m.filter { $0.isNumber }
            w.evaluateJavaScript("window.aiwMate&&aiwMate.set('\(id)', \(id == "33"))", completionHandler: nil)
        }
    }
    func makeCoordinator() -> Coord { Coord() }

    func makeUIView(context: Context) -> WKWebView {
        let w = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        w.isOpaque = false
        w.backgroundColor = .clear
        w.scrollView.backgroundColor = .clear
        w.scrollView.isScrollEnabled = false
        w.scrollView.contentInsetAdjustmentBehavior = .never   // never pushed down by a safe area (see MateAvatar)
        w.isUserInteractionEnabled = false
        context.coordinator.bus = bus
        context.coordinator.mood = mood
        context.coordinator.shown = mood ?? emotion
        w.navigationDelegate = context.coordinator
        if bus { MateBus.shared.attach(w, thread: threadId) }
        let frozen = still || (!bus && !animate)
        var q = "?static=\(frozen ? 1 : 0)&ring=\(ring)"
        if let e = mood ?? emotion { q += "&e=\(e.filter { $0.isNumber })" }
        if !bus { q += "&lite=1" }
        if let u = Bundle.main.url(forResource: "mate", withExtension: "html", subdirectory: "mate"),
           let full = URL(string: u.absoluteString + q) {
            w.loadFileURL(full, allowingReadAccessTo: u.deletingLastPathComponent())
        } else if let u = API.shared.url("/mate.html", query: [URLQueryItem(name: "static", value: frozen ? "1" : "0"),
                                                              URLQueryItem(name: "ring", value: ring), URLQueryItem(name: "e", value: mood ?? emotion ?? "02")]) {
            w.load(URLRequest(url: u))
        }
        return w
    }

    func updateUIView(_ w: WKWebView, context: Context) {
        guard bus else {
            if let m = mood, m != context.coordinator.mood { context.coordinator.mood = m; context.coordinator.push(w) }
            return
        }
        let c = context.coordinator
        MateBus.shared.attach(w, thread: threadId)
        if c.status != status {
            c.status = status
            let safe = status.filter { $0.isLetter || $0 == "_" }
            MateBus.shared.js("window.aiwMate&&aiwMate.status('\(safe)')")
        }
        let act = phase == .active && !still
        if c.active != act {
            c.active = act
            MateBus.shared.js("window.aiwMate&&(aiwMate.still(\(still)),aiwMate.active(\(act)))")
        }
    }
}

/// v2 home: the small 球球 head beside each of 球球's messages, task cards and the thinking row.
/// 3.7: drawn natively (MateHead), no WKWebView snapshot any more. Root cause of the "eye / lid" avatar on iOS 17:
/// 3.6 put the hidden snapshot web view at the window's origin (behind the root view). There the web view's scroll view
/// lies under the status bar, so UIKit gives it an automatic top content inset (the safe area, ~47-59 pt on a notched
/// iPhone; WebKit on iOS 17 applies it to the page as an obscured inset). The 96 pt page was drawn that far DOWN inside
/// its own 96 pt frame, so the frame held an empty top half and the TOP half of the disc + ball at the bottom; clipped
/// to a circle that is a lens: the amber rim as the upper curve, the face with the two eyes, the clip as the lower curve.
/// The page's own probe (getBoundingClientRect: document coordinates, inset-free) could not see it, the image was
/// square, so every check passed. A drawing has no web view, no layout timing, no insets: always a full circle.
struct MateAvatar: View {
    static let side: CGFloat = 30
    var size: CGFloat = MateAvatar.side
    @AppStorage("mate") private var mateOn = true
    var body: some View {
        Group {
            if mateOn { MateHead(size: size) } else { Circle().fill(amber) }
        }
        .frame(width: size, height: size)
        .clipShape(Circle())
        .accessibilityHidden(true)
    }
}

/// The 球球 head as the bundled mate.html draws it (?ring=amber&e=02, still): the amber #C87618 disc with its warm
/// glow, the cream ball (radial #f6f3ef → #F3F0EA → #d6d3ce, light from the upper left) with a soft shadow, and the
/// two dark eyes of the idle face up and to the right. Proportions measured from that page's render (ball 73 % of the
/// disc; eyes at 57 % / 36 % and 71 % / 33 %, tilted about 25°). The orb's own look is unchanged; this is its picture.
struct MateHead: View {
    var size: CGFloat
    static let disc = Color(red: 0xC8 / 255, green: 0x76 / 255, blue: 0x18 / 255)
    static let eye = Color(red: 0x1A / 255, green: 0x1A / 255, blue: 0x1A / 255)
    var body: some View {
        let s = size
        ZStack {
            Circle().fill(MateHead.disc)
            Circle().fill(RadialGradient(colors: [Color(red: 1, green: 214 / 255, blue: 150 / 255).opacity(0.38),
                                                  Color(red: 1, green: 190 / 255, blue: 110 / 255).opacity(0.10), .clear],
                                         center: UnitPoint(x: 0.5, y: 0.36), startRadius: 0, endRadius: s * 0.62))
            Circle().strokeBorder(Color.white.opacity(0.16), lineWidth: max(0.5, s / 96))
            Circle()
                .fill(RadialGradient(stops: [.init(color: Color(red: 0xF6 / 255, green: 0xF3 / 255, blue: 0xEF / 255), location: 0),
                                             .init(color: Color(red: 0xF3 / 255, green: 0xF0 / 255, blue: 0xEA / 255), location: 0.62),
                                             .init(color: Color(red: 0xD6 / 255, green: 0xD3 / 255, blue: 0xCE / 255), location: 1)],
                                     center: UnitPoint(x: 0.38, y: 0.32), startRadius: 0, endRadius: s * 0.73 * 0.75))
                .frame(width: s * 0.73, height: s * 0.73)
                .shadow(color: Color(red: 90 / 255, green: 40 / 255, blue: 0).opacity(0.45), radius: max(0.5, s * 0.03), y: s * 0.02)
                .position(x: s * 0.501, y: s * 0.504)
            Capsule().fill(MateHead.eye)
                .frame(width: s * 0.105, height: s * 0.235)
                .rotationEffect(.degrees(-26))
                .position(x: s * 0.573, y: s * 0.357)
            Capsule().fill(MateHead.eye)
                .frame(width: s * 0.06, height: s * 0.225)
                .rotationEffect(.degrees(-20))
                .position(x: s * 0.714, y: s * 0.331)
        }
        .frame(width: s, height: s)
        .compositingGroup()
    }
}
