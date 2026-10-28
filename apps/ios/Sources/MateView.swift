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

/// v2 home (3.5): the small 球球 beside each of 球球's messages. One still frame of the real 球球 (the bundled mate.html,
/// same look as everywhere), rendered once off screen and reused as an image, so a long chat holds no WKWebViews.
final class MateSnapshot: NSObject, ObservableObject, WKNavigationDelegate {
    static let shared = MateSnapshot()
    @Published var image: UIImage?
    private var web: WKWebView?
    private var started = false
    private var tries = 0

    func start() {
        guard !started, tries < 3 else { return }
        started = true
        tries += 1
        let w = WKWebView(frame: CGRect(x: 0, y: 0, width: 96, height: 96), configuration: WKWebViewConfiguration())
        w.isOpaque = false
        w.backgroundColor = .clear
        w.scrollView.backgroundColor = .clear
        w.navigationDelegate = self
        web = w
        // WebKit only paints a web view that is in a window: park it off screen in the key window until the frame is taken
        let win = UIApplication.shared.connectedScenes.compactMap { ($0 as? UIWindowScene)?.keyWindow }.first
        w.frame.origin = CGPoint(x: -400, y: -400)
        win?.addSubview(w)
        if let u = Bundle.main.url(forResource: "mate", withExtension: "html", subdirectory: "mate"),
           let full = URL(string: u.absoluteString + "?static=1&ring=amber&e=02&lite=1") {
            w.loadFileURL(full, allowingReadAccessTo: u.deletingLastPathComponent())
        } else if let u = API.shared.url("/mate.html", query: [URLQueryItem(name: "static", value: "1"), URLQueryItem(name: "ring", value: "amber"),
                                                              URLQueryItem(name: "e", value: "02"), URLQueryItem(name: "lite", value: "1")]) {
            w.load(URLRequest(url: u))
        } else { started = false }
    }
    func webView(_ w: WKWebView, didFinish navigation: WKNavigation!) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            let cfg = WKSnapshotConfiguration()
            cfg.afterScreenUpdates = true
            w.takeSnapshot(with: cfg) { img, _ in
                DispatchQueue.main.async {
                    if let img = img { self?.image = img } else { self?.started = false }
                    w.removeFromSuperview()
                    self?.web = nil
                }
            }
        }
    }
    func webView(_ w: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { started = false; w.removeFromSuperview(); web = nil }
}

/// The small 球球 avatar (a still frame; an amber disc until the frame is ready).
struct MateAvatar: View {
    var size: CGFloat = 30
    @ObservedObject private var snap = MateSnapshot.shared
    var body: some View {
        Group {
            if let img = snap.image { Image(uiImage: img).resizable().scaledToFit() }
            else { Circle().fill(amber) }
        }
        .frame(width: size, height: size)
        .clipShape(Circle())
        .onAppear { snap.start() }
        .accessibilityHidden(true)
    }
}
