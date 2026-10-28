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

/// v2 home: the small 球球 head beside each of 球球's messages, task cards and the thinking row. One still frame of the
/// real 球球 (the bundled mate.html, amber disc, same look as in the title), rendered once and reused as an image, so a
/// long chat holds no WKWebViews.
/// 3.6 fix (the "eye" avatar): 3.5 parked the web view at (-400,-400), outside the window, and snapped it 0.4 s after
/// load. WebKit does not lay out / paint a view outside the window's bounds reliably, so the frame came back with the
/// disc squashed into a lens and the ball tiny. Now the view sits inside the window (under the app's root view, so it
/// is never seen), the disc is pinned to an exact square in CSS, and the frame is only taken once the page reports a
/// square disc with the ball drawn; the image itself must be square too, else it is retried (3 times, then the amber disc).
final class MateSnapshot: NSObject, ObservableObject, WKNavigationDelegate {
    static let shared = MateSnapshot()
    static let px: CGFloat = 96
    @Published var image: UIImage?
    private var web: WKWebView?
    private var started = false
    private var tries = 0

    func start() {
        guard !started, image == nil, tries < 3 else { return }
        guard let win = UIApplication.shared.connectedScenes.compactMap({ ($0 as? UIWindowScene)?.keyWindow }).first else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in self?.start() }   // no window yet (launch)
            return
        }
        started = true
        tries += 1
        let w = WKWebView(frame: CGRect(x: 0, y: 0, width: MateSnapshot.px, height: MateSnapshot.px), configuration: WKWebViewConfiguration())
        w.isOpaque = false
        w.backgroundColor = .clear
        w.scrollView.backgroundColor = .clear
        w.scrollView.isScrollEnabled = false
        w.isUserInteractionEnabled = false
        w.navigationDelegate = self
        web = w
        win.insertSubview(w, at: 0)   // inside the window, behind the root view: painted, never visible
        if let u = Bundle.main.url(forResource: "mate", withExtension: "html", subdirectory: "mate"),
           let full = URL(string: u.absoluteString + "?static=1&ring=amber&e=02&lite=1") {
            w.loadFileURL(full, allowingReadAccessTo: u.deletingLastPathComponent())
        } else if let u = API.shared.url("/mate.html", query: [URLQueryItem(name: "static", value: "1"), URLQueryItem(name: "ring", value: "amber"),
                                                              URLQueryItem(name: "e", value: "02"), URLQueryItem(name: "lite", value: "1")]) {
            w.load(URLRequest(url: u))
        } else { fail(w) }
    }
    func webView(_ w: WKWebView, didFinish navigation: WKNavigation!) {
        let px = Int(MateSnapshot.px)
        // an exact square disc, whatever the viewport turned out to be, then one static frame
        let js = "(function(){var m=document.getElementById('m');m.style.inset='auto';m.style.left='0px';m.style.top='0px';"
            + "m.style.width='\(px)px';m.style.height='\(px)px';if(window.aiwMate)aiwMate.active(false);return 1;})()"
        w.evaluateJavaScript(js) { [weak self] _, _ in self?.check(w, left: 12) }
    }
    /// wait until the disc is square and the ball is drawn, then take the frame
    private func check(_ w: WKWebView, left: Int) {
        let probe = "(function(){var m=document.getElementById('m'),r=m.getBoundingClientRect(),s=m.querySelector('svg');"
            + "return [r.width,r.height,s?s.getBoundingClientRect().width:0];})()"
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { [weak self] in
            w.evaluateJavaScript(probe) { v, _ in
                let a = (v as? [NSNumber])?.map { CGFloat($0.doubleValue) } ?? []
                let ok = a.count == 3 && a[0] > 40 && abs(a[0] - a[1]) < 1 && a[2] > a[0] * 0.5
                if ok || left <= 0 { self?.snap(w) } else { self?.check(w, left: left - 1) }
            }
        }
    }
    private func snap(_ w: WKWebView) {
        let cfg = WKSnapshotConfiguration()
        cfg.afterScreenUpdates = true
        cfg.rect = CGRect(x: 0, y: 0, width: MateSnapshot.px, height: MateSnapshot.px)
        w.takeSnapshot(with: cfg) { [weak self] img, _ in
            DispatchQueue.main.async {
                guard let self = self else { return }
                if let img = img, img.size.width > 10, abs(img.size.width - img.size.height) < 1 {
                    self.image = img
                    w.removeFromSuperview(); self.web = nil
                } else { self.fail(w) }
            }
        }
    }
    private func fail(_ w: WKWebView) {
        w.removeFromSuperview(); web = nil; started = false
        if tries < 3 { DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in self?.start() } }
    }
    func webView(_ w: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { fail(w) }
    func webView(_ w: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { fail(w) }
}

/// The small 球球 head avatar (a still frame of the real ball; an amber disc until the frame is ready), always round.
struct MateAvatar: View {
    static let side: CGFloat = 30
    var size: CGFloat = MateAvatar.side
    @ObservedObject private var snap = MateSnapshot.shared
    @AppStorage("mate") private var mateOn = true
    var body: some View {
        Group {
            if mateOn, let img = snap.image { Image(uiImage: img).resizable().aspectRatio(1, contentMode: .fill) }
            else { Circle().fill(amber) }
        }
        .frame(width: size, height: size)
        .clipShape(Circle())
        .onAppear { if mateOn { snap.start() } }
        .accessibilityHidden(true)
    }
}
