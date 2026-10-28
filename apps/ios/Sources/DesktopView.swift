import SwiftUI
import UserNotifications
import WebKit

extension Notification.Name {
    /// A computer was renamed or picked in the 电脑 sheet: pickers and the switcher bar reload their list.
    static let aiwMachinesChanged = Notification.Name("aiwMachinesChanged")
}

/// The agent's computer, pinned above the conversation: a live picture while you watch, the real desktop when you take over.
struct DesktopPane: View {
    @EnvironmentObject var api: API
    let liveSession: String?          // session of the task running in this thread, if any
    let onChange: () async -> Void
    @State private var open = false
    @State private var image: UIImage?
    @State private var note = ""
    @State private var taking = false
    @State private var busy = false
    @State private var machines: [JSON] = []
    @Environment(\.scenePhase) var phase
    private let timer = Timer.publish(every: 1.5, on: .main, in: .common).autoconnect()

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Circle().fill(liveSession != nil ? amber : Color.secondary.opacity(0.5)).frame(width: 7, height: 7)
                Menu {
                    ForEach(machines.indices, id: \.self) { i in
                        let mm = machines[i]
                        Button { pick(mm.s("id")) } label: {
                            if mm.s("id") == api.machine { Label(label(mm), systemImage: "checkmark") } else { Text(label(mm)) }
                        }
                    }
                    if machines.isEmpty { Text("没有电脑") }
                } label: {
                    HStack(spacing: 3) {
                        Image(systemName: current.s("provider") == "instabox" ? "cloud" : "desktopcomputer").font(.caption2)
                        Text(current.isEmpty ? api.machine : label(current)).font(.caption.weight(.semibold)).lineLimit(1)
                        Image(systemName: "chevron.up.chevron.down").font(.system(size: 8, weight: .bold))
                    }.foregroundColor(.primary)
                }.disabled(liveSession != nil)
                if liveSession != nil { Text("执行中").font(.caption2).foregroundColor(amber) }
                if !note.isEmpty { Text(note).font(.caption2).foregroundColor(.secondary).lineLimit(1) }
                Spacer()
                Button { Task { await takeover() } } label: {
                    Label("接管", systemImage: "hand.raised.fill").font(.caption.weight(.semibold))
                }.buttonStyle(.bordered).controlSize(.mini).disabled(busy)
                Button { withAnimation(.easeOut(duration: 0.2)) { open.toggle() }; if open { Task { await grab() } } } label: {
                    Image(systemName: open ? "chevron.up" : "chevron.down").font(.caption.weight(.bold)).frame(width: 26, height: 22)
                }
            }.padding(.horizontal, 12).padding(.vertical, 7)
            if open {
                ZStack {
                    Color.black
                    if let img = image { Image(uiImage: img).resizable().scaledToFit() }
                    else { ProgressView().tint(.white) }
                }
                .frame(maxWidth: .infinity).aspectRatio(16 / 10, contentMode: .fit)
                .onTapGesture { Task { await takeover() } }
                .transition(.opacity)
            }
        }
        .background(.bar)
        .overlay(Divider(), alignment: .bottom)
        .onAppear { if liveSession != nil { open = true } }
        .task { machines = (try? await api.list("/api/machines")) ?? [] }
        .onReceive(NotificationCenter.default.publisher(for: .aiwMachinesChanged)) { _ in
            Task { machines = (try? await api.list("/api/machines")) ?? [] }
        }
        .onReceive(NotificationCenter.default.publisher(for: .aiwOpenTakeover)) { n in
            // a takeover card's 打开桌面接管: go to the computer the agent is on, then the real desktop
            let mid = (n.userInfo?["machine"] as? String) ?? ""
            if !mid.isEmpty && mid != api.machine { pick(mid) }
            Task { await takeover() }
        }
        .onChange(of: liveSession) { s in if s != nil { withAnimation { open = true } } }
        .onReceive(timer) { _ in if open && phase == .active && !taking { Task { await grab() } } }
        .fullScreenCover(isPresented: $taking) {
            TakeoverView(liveSession: liveSession) { Task { await handBack() } }
        }
    }

    var current: JSON { machines.first { $0.s("id") == api.machine } ?? [:] }
    func label(_ m: JSON) -> String {
        let n = m.s("name").isEmpty ? m.s("id") : m.s("name")   // display name: 云电脑 N or the user's own (shared with web)
        if m.s("provider") == "instabox" { return n.contains("云") ? n : n + "（云电脑）" }
        return n + "（本地）"
    }
    func pick(_ id: String) {
        api.setMachine(id); image = nil; note = ""
        if open { Task { await grab() } }
    }
    func grab() async {
        guard let u = api.url("/api/machines/\(api.machine)/screenshot") else { return }
        var r = URLRequest(url: u, timeoutInterval: 12)
        r.setValue("Bearer \(api.token)", forHTTPHeaderField: "Authorization")
        guard let (d, resp) = try? await URLSession.shared.data(for: r) else { note = "连不上电脑"; return }
        if (resp as? HTTPURLResponse)?.statusCode == 200, let img = UIImage(data: d) { image = img; note = "" }
        else { note = "电脑未就绪" }
    }
    func takeover() async {
        busy = true; defer { busy = false }
        if let s = liveSession { _ = try? await api.call("POST", "/api/agent/takeover", ["session_id": s]) }
        taking = true
        await onChange()
    }
    func handBack() async {
        taking = false
        if let s = liveSession { _ = try? await api.call("POST", "/api/agent/resume", ["session_id": s]) }
        // the agent asked for this takeover: handing back also answers it (完成，交还给 Aiw)
        if let p = TakeoverBus.shared.pending, !p.s("session_id").isEmpty, p.s("session_id") == liveSession {
            await TakeoverBus.shared.answer(p.s("session_id"), cancel: false)
        }
        await onChange()
    }
}

/// Full screen desktop you can drive (noVNC from the Aiw server), with a bar to hand control back.
/// 3.7: the desktop is the takeover page (apps/web/takeover.html + takeover.js, bundled, loaded with the Aiw server as
/// its origin so the noVNC iframe inside is same-origin) and touches go through its touch layer: 触控板 mode (default:
/// a cursor moved relatively with acceleration, tap = click, two-finger tap = right click, long press / tap-then-drag =
/// drag, two-finger drag = scroll), 直点 mode (a loupe with a crosshair, click on release), pinch zoom + pan, and a
/// toolbar (mode, keyboard, Esc / Tab / Ctrl / Enter / arrows, 横屏). 横屏 turns this view (and only this view, see
/// OrientationLock) to landscape and shows the desktop full screen; 退出横屏 in the toolbar brings it back.
struct TakeoverView: View {
    @EnvironmentObject var api: API
    let liveSession: String?
    let done: () -> Void
    @State private var url: URL?
    @State private var err = ""
    @State private var landscape = false
    @ObservedObject private var bus = TakeoverBus.shared
    var asked: JSON? { (bus.pending?.s("session_id") ?? "") == (liveSession ?? "-") ? bus.pending : nil }
    var body: some View {
        VStack(spacing: 0) {
            if !landscape {
                HStack {
                    Circle().fill(Color.red).frame(width: 8, height: 8)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(liveSession != nil ? "你在操作桌面，Aiw 已暂停" : "你在操作桌面").font(.subheadline.weight(.semibold))
                        if let a = asked {   // what the agent asked for, so the user knows what to do here
                            Text(a.s("steps").isEmpty ? a.s("title") : a.s("steps")).font(.caption).foregroundColor(.secondary).lineLimit(2)
                        }
                    }
                    Spacer()
                    Button(asked != nil ? "完成，交还给 Aiw" : liveSession != nil ? "交还给 Aiw" : "结束操作") { leave() }.buttonStyle(.borderedProminent)
                }.padding(.horizontal, 14).padding(.vertical, 10).background(.bar)
            }
            ZStack {
                Color.black
                if let u = url { TakeoverWeb(url: u, server: api.server) { op in setLandscape(op == "landscape") } }
                else if !err.isEmpty { Text(err).foregroundColor(.white).padding() }
                else { ProgressView().tint(.white) }
            }
            .ignoresSafeArea(edges: landscape ? .all : [])
        }
        .statusBarHidden(landscape)
        .persistentSystemOverlays(landscape ? .hidden : .automatic)
        .task { await load() }
        .onDisappear { if landscape { OrientationLock.set(landscape: false) } }
    }
    func setLandscape(_ on: Bool) {
        withAnimation(.easeInOut(duration: 0.2)) { landscape = on }
        OrientationLock.set(landscape: on)
    }
    func leave() {
        if landscape { setLandscape(false) }
        done()
    }
    func load() async {
        do {
            let d = try await api.obj("GET", "/api/machines/\(api.machine)/desktop?view_only=false")
            let raw = d.s("novnc_url")
            if raw.isEmpty { err = d.b("paused") ? "云电脑暂停中，发个任务会自动唤醒" : "这台电脑没有桌面"; return }
            let full = d.b("external") ? raw : api.server + raw + "&token=" + (api.token.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")
            url = URL(string: full)
        } catch { err = error.localizedDescription }
    }
}

/// Only the takeover view turns to landscape (its 横屏 button): the rest of the app stays portrait on a phone.
/// AppDelegate.supportedInterfaceOrientationsFor reads `mask`; iOS 16+ rotates with requestGeometryUpdate.
enum OrientationLock {
    static var pad: Bool { UIDevice.current.userInterfaceIdiom == .pad }
    static var mask: UIInterfaceOrientationMask = UIDevice.current.userInterfaceIdiom == .pad ? .all : .portrait
    static func set(landscape: Bool) {
        mask = landscape ? .landscape : (pad ? .all : .portrait)
        guard let scene = UIApplication.shared.connectedScenes.compactMap({ $0 as? UIWindowScene }).first else { return }
        var vc = scene.keyWindow?.rootViewController
        while let c = vc {   // the root and the full-screen cover on top of it re-read the mask
            c.setNeedsUpdateOfSupportedInterfaceOrientations()
            vc = c.presentedViewController
        }
        scene.requestGeometryUpdate(.iOS(interfaceOrientations: landscape ? .landscapeRight : (pad ? .all : .portrait))) { e in
            print("Aiw rotate:", e.localizedDescription)
        }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate {
    /// 3.8: the notification delegate is set before launch finishes, so a tap that cold-launches Aiw reaches it.
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = NotifyDelegate.shared
        return true
    }
    func application(_ application: UIApplication, supportedInterfaceOrientationsFor window: UIWindow?) -> UIInterfaceOrientationMask {
        OrientationLock.mask
    }
}

/// The takeover page in a WKWebView: the bundled takeover.html with takeover.js inlined, loaded with the Aiw server as
/// its base URL (so the noVNC iframe is same-origin and the touch layer can reach its canvas). A server copy
/// (/takeover.html) is the fallback when the bundle has none. `op` gets the page's 横屏 / 退出横屏.
struct TakeoverWeb: UIViewRepresentable {
    let url: URL
    let server: String
    let op: (String) -> Void

    final class Coord: NSObject, WKScriptMessageHandler {
        var op: (String) -> Void = { _ in }
        func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
            guard let b = m.body as? [String: Any], let o = b["op"] as? String else { return }
            DispatchQueue.main.async { self.op(o) }
        }
    }
    /// WKUserContentController keeps its handlers strongly: this breaks the cycle
    final class Weak: NSObject, WKScriptMessageHandler {
        weak var to: Coord?
        init(_ c: Coord) { to = c }
        func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) { to?.userContentController(c, didReceive: m) }
    }
    func makeCoordinator() -> Coord { Coord() }

    func makeUIView(context: Context) -> WKWebView {
        let c = WKWebViewConfiguration()
        c.allowsInlineMediaPlayback = true
        c.userContentController.add(Weak(context.coordinator), name: "aiwTakeover")
        let w = WKWebView(frame: .zero, configuration: c)
        w.isOpaque = false; w.backgroundColor = .black
        w.scrollView.isScrollEnabled = false
        w.scrollView.bounces = false
        w.scrollView.contentInsetAdjustmentBehavior = .never
        context.coordinator.op = op
        if let html = TakeoverWeb.page(src: url.absoluteString), let base = URL(string: server.hasSuffix("/") ? server : server + "/") {
            w.loadHTMLString(html, baseURL: base)
        } else if var u = URLComponents(string: server + "/takeover.html") {
            u.queryItems = [URLQueryItem(name: "src", value: url.absoluteString), URLQueryItem(name: "native", value: "1")]
            if let full = u.url { w.load(URLRequest(url: full)) } else { w.load(URLRequest(url: url)) }
        } else { w.load(URLRequest(url: url)) }
        return w
    }
    func updateUIView(_ w: WKWebView, context: Context) { context.coordinator.op = op }
    static func dismantleUIView(_ w: WKWebView, coordinator: Coord) {
        w.configuration.userContentController.removeScriptMessageHandler(forName: "aiwTakeover")
    }
    /// takeover.html from the bundle with takeover.js inlined and the desktop address set
    static func page(src: String) -> String? {
        guard let h = Bundle.main.url(forResource: "takeover", withExtension: "html", subdirectory: "takeover"),
              let j = Bundle.main.url(forResource: "takeover", withExtension: "js", subdirectory: "takeover"),
              var html = try? String(contentsOf: h, encoding: .utf8), let js = try? String(contentsOf: j, encoding: .utf8),
              let data = try? JSONSerialization.data(withJSONObject: [src]), let arr = String(data: data, encoding: .utf8) else { return nil }
        let lit = String(arr.dropFirst().dropLast())   // the JSON string literal of src
        let tag = "<script src=\"takeover.js\"></script>"
        guard html.contains(tag) else { return nil }
        html = html.replacingOccurrences(of: tag, with: "<script>window.AIW_TAKEOVER_SRC=\(lit);window.AIW_TAKEOVER_NATIVE=true;</script>\n<script>\(js)</script>")
        return html
    }
}
