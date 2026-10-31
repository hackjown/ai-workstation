import SwiftUI
import UserNotifications
import WebKit
import ImageIO

extension Notification.Name {
    /// A computer was renamed or picked in the 电脑 sheet: pickers and the switcher bar reload their list.
    static let aiwMachinesChanged = Notification.Name("aiwMachinesChanged")
}

/// A downsampled, already-decoded image (ImageIO thumbnail + preparingForDisplay), safe to call off the main thread.
func aiwDecodeScreenshot(_ d: Data, maxSide: CGFloat) -> UIImage? {
    guard let src = CGImageSourceCreateWithData(d as CFData, [kCGImageSourceShouldCache: false] as CFDictionary) else { return nil }
    let opts: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceShouldCacheImmediately: true,
                                 kCGImageSourceCreateThumbnailWithTransform: true, kCGImageSourceThumbnailMaxPixelSize: maxSide]
    guard let cg = CGImageSourceCreateThumbnailAtIndex(src, 0, opts as CFDictionary) else { return UIImage(data: d) }
    let img = UIImage(cgImage: cg)
    return img.preparingForDisplay() ?? img
}

/// The agent's computer, pinned above the conversation: a live picture while you watch, the real desktop when you take over.
struct DesktopPane: View {
    @EnvironmentObject var api: API
    let liveSession: String?          // session of the task running in this thread, if any
    let onChange: () async -> Void
    var follow: String? = nil         // aiw-roles-1: the computer this conversation's task runs on; picked when it changes
    @State private var open = false
    @State private var image: UIImage?
    @State private var note = ""
    @State private var taking = false
    @State private var busy = false
    @State private var machines: [JSON] = []
    @State private var grabbing = false
    @State private var keyboard = false
    @State private var lastGrab = Date.distantPast
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
        .onAppear {
            if liveSession != nil { open = true }
            if let f = follow, f != api.machine { pick(f) }
        }
        .onChange(of: follow) { f in if let f = f, f != api.machine { pick(f) } }
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
        // 3.9.0 typing lag: one picture at a time, and every 6 s instead of 1.5 s while the keyboard is up
        .onReceive(timer) { _ in
            guard open && phase == .active && !taking && !grabbing else { return }
            if keyboard && Date().timeIntervalSince(lastGrab) < 6 { return }
            Task { await grab() }
        }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in keyboard = true }
        .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillHideNotification)) { _ in keyboard = false }
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
        grabbing = true; lastGrab = Date()
        defer { grabbing = false }
        guard let (d, resp) = try? await URLSession.shared.data(for: r) else { note = "连不上电脑"; return }
        // 3.9.0: decode off the main thread, scaled to the pane (a full-size desktop PNG decoded on the main thread
        // every 1.5 s is what made typing stutter in the task view)
        let ok = (resp as? HTTPURLResponse)?.statusCode == 200
        let img = ok ? await Task.detached(priority: .utility) { aiwDecodeScreenshot(d, maxSide: 1280) }.value : nil
        if let img = img { image = img; note = "" } else { note = "电脑未就绪" }
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
/// 3.8.4: touches and keys no longer go into the viewer page: takeover.js sends them to the VM over /ws/input/<mid>
/// (absolute xdotool through the vm-agent), so 云电脑 (InstaBox viewer, another origin) and portrait work too, and the
/// keyboard is native (TakeoverHost / KeyField: a hidden first-responder field and a key bar).
/// 3.7: the desktop is the takeover page (apps/web/takeover.html + takeover.js, bundled, loaded with the Aiw server as
/// its origin so the noVNC iframe inside is same-origin) and touches go through its touch layer: 触控板 mode (default:
/// a cursor moved relatively with acceleration, tap = click, two-finger tap = right click, long press / tap-then-drag =
/// drag, two-finger drag = scroll), 直点 mode (a loupe with a crosshair, click on release), pinch zoom + pan, and a
/// toolbar (mode, keyboard, Esc / Tab / Ctrl / Enter / arrows, 横屏). 横屏 turns this view (and only this view, see
/// OrientationLock) to landscape and shows the desktop full screen; 退出横屏 in the toolbar brings it back.
struct TakeoverView: View {
    @EnvironmentObject var api: API
    let liveSession: String?
    /// 3.8.6: 电脑 › 查看 opens one computer's desktop on its own (no task): that machine, a cloud one woken first
    let machineId: String?
    let wake: Bool
    let title: String
    let done: () -> Void
    init(liveSession: String?, machineId: String? = nil, wake: Bool = false, title: String = "", done: @escaping () -> Void) {
        self.liveSession = liveSession; self.machineId = machineId; self.wake = wake; self.title = title; self.done = done
    }
    var standalone: Bool { machineId != nil && liveSession == nil }
    @State private var waking = false
    @State private var url: URL?
    @State private var err = ""
    @State private var landscape = false
    @State private var mid = ""
    @ObservedObject private var bus = TakeoverBus.shared
    var asked: JSON? { (bus.pending?.s("session_id") ?? "") == (liveSession ?? "-") ? bus.pending : nil }
    var body: some View {
        VStack(spacing: 0) {
            if !landscape {
                HStack {
                    Circle().fill(Color.red).frame(width: 8, height: 8)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(liveSession != nil ? "你在操作桌面，Aiw 已暂停" : standalone ? "查看「\(title.isEmpty ? mid : title)」" : "你在操作桌面")
                            .font(.subheadline.weight(.semibold)).lineLimit(1)
                        if standalone { Text("可以直接操作，不会开始任务").font(.caption).foregroundColor(.secondary) }
                        if let a = asked {   // what the agent asked for, so the user knows what to do here
                            Text(a.s("steps").isEmpty ? a.s("title") : a.s("steps")).font(.caption).foregroundColor(.secondary).lineLimit(2)
                        }
                    }
                    Spacer()
                    if standalone {
                        Button { leave() } label: { Label("关闭", systemImage: "xmark") }.buttonStyle(.borderedProminent)
                    } else {
                        Button(asked != nil ? "完成，交还给 Aiw" : liveSession != nil ? "交还给 Aiw" : "结束操作") { leave() }.buttonStyle(.borderedProminent)
                    }
                }.padding(.horizontal, 14).padding(.vertical, 10).background(.bar)
            }
            ZStack {
                Color.black
                if let u = url {
                    TakeoverWeb(url: u, server: api.server, mid: mid, token: api.token) { op in setLandscape(op == "landscape") }
                }
                else if !err.isEmpty { Text(err).foregroundColor(.white).padding() }
                else {
                    VStack(spacing: 10) {
                        ProgressView().tint(.white)
                        if waking { Text("正在唤醒云电脑…").font(.footnote).foregroundColor(.white.opacity(0.8)) }
                    }
                }
            }
            .overlay(alignment: .topTrailing) {
                if standalone && (landscape || url == nil) {   // 横屏 hides the bar: still a way out
                    Button { leave() } label: {
                        Image(systemName: "xmark").font(.system(size: 15, weight: .bold)).foregroundColor(.white)
                            .frame(width: 34, height: 34).background(Color.black.opacity(0.55)).clipShape(Circle())
                    }.padding(10).accessibilityLabel("关闭")
                }
            }
            .ignoresSafeArea(.container, edges: landscape ? .all : [])   // the keyboard still shrinks it (you see what you type)
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
            mid = machineId ?? api.machine
            waking = wake
            let d = try await api.obj("GET", "/api/machines/\(mid)/desktop?view_only=false" + (wake ? "&wake=1" : ""))
            waking = false
            let raw = d.s("novnc_url")
            if raw.isEmpty { err = d.b("paused") ? (wake ? "云电脑没能唤醒，稍后再试" : "云电脑暂停中，发个任务会自动唤醒") : "这台电脑没有桌面"; return }
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
/// its base URL. A server copy (/takeover.html) is the fallback when the bundle has none. `op` gets the page's 横屏 /
/// 退出横屏. 3.8.4: the page sends every touch straight to the VM over /ws/input/<mid> (so it gets the machine id and
/// the token), and its ⌨ button opens the native keyboard below (KeyField) instead of a hidden web input.
struct TakeoverWeb: UIViewRepresentable {
    let url: URL
    let server: String
    var mid: String = ""
    var token: String = ""
    let op: (String) -> Void

    final class Coord: NSObject, WKScriptMessageHandler {
        var op: (String) -> Void = { _ in }
        weak var host: TakeoverHost?
        func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
            guard let b = m.body as? [String: Any], let o = b["op"] as? String else { return }
            DispatchQueue.main.async {
                switch o {
                case "keyboard": self.host?.keyboard(true)
                case "keyboard-off": self.host?.keyboard(false)
                default: self.op(o)
                }
            }
        }
    }
    /// WKUserContentController keeps its handlers strongly: this breaks the cycle
    final class Weak: NSObject, WKScriptMessageHandler {
        weak var to: Coord?
        init(_ c: Coord) { to = c }
        func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) { to?.userContentController(c, didReceive: m) }
    }
    func makeCoordinator() -> Coord { Coord() }

    func makeUIView(context: Context) -> TakeoverHost {
        let c = WKWebViewConfiguration()
        c.allowsInlineMediaPlayback = true
        c.userContentController.add(Weak(context.coordinator), name: "aiwTakeover")
        let w = WKWebView(frame: .zero, configuration: c)
        w.isOpaque = false; w.backgroundColor = .black
        w.scrollView.isScrollEnabled = false
        w.scrollView.bounces = false
        w.scrollView.contentInsetAdjustmentBehavior = .never
        context.coordinator.op = op
        if let html = TakeoverWeb.page(src: url.absoluteString, mid: mid, token: token),
           let base = URL(string: server.hasSuffix("/") ? server : server + "/") {
            w.loadHTMLString(html, baseURL: base)
        } else if var u = URLComponents(string: server + "/takeover.html") {
            u.queryItems = [URLQueryItem(name: "src", value: url.absoluteString), URLQueryItem(name: "native", value: "1"),
                            URLQueryItem(name: "mid", value: mid), URLQueryItem(name: "token", value: token)]
            if let full = u.url { w.load(URLRequest(url: full)) } else { w.load(URLRequest(url: url)) }
        } else { w.load(URLRequest(url: url)) }
        let host = TakeoverHost(web: w)
        context.coordinator.host = host
        return host
    }
    func updateUIView(_ h: TakeoverHost, context: Context) { context.coordinator.op = op }
    static func dismantleUIView(_ h: TakeoverHost, coordinator: Coord) {
        h.keys.show(false)
        h.web.configuration.userContentController.removeScriptMessageHandler(forName: "aiwTakeover")
    }
    /// a JS string literal
    static func lit(_ s: String) -> String? {
        guard let d = try? JSONSerialization.data(withJSONObject: [s]), let a = String(data: d, encoding: .utf8) else { return nil }
        return String(a.dropFirst().dropLast())
    }
    /// takeover.html from the bundle with takeover.js inlined and the desktop address, machine and token set
    static func page(src: String, mid: String, token: String) -> String? {
        guard let h = Bundle.main.url(forResource: "takeover", withExtension: "html", subdirectory: "takeover"),
              let j = Bundle.main.url(forResource: "takeover", withExtension: "js", subdirectory: "takeover"),
              var html = try? String(contentsOf: h, encoding: .utf8), let js = try? String(contentsOf: j, encoding: .utf8),
              let s1 = lit(src), let s2 = lit(mid), let s3 = lit(token) else { return nil }
        let tag = "<script src=\"takeover.js\"></script>"
        guard html.contains(tag) else { return nil }
        let vars = "window.AIW_TAKEOVER_SRC=\(s1);window.AIW_TAKEOVER_NATIVE=true;window.AIW_TAKEOVER_MID=\(s2);window.AIW_TAKEOVER_TOKEN=\(s3);"
        html = html.replacingOccurrences(of: tag, with: "<script>\(vars)</script>\n<script>\(js)</script>")
        return html
    }
}

/// The web view plus the hidden keyboard field. Keys go into the page (`aiwTouch.input`), which sends them to the VM
/// on the same socket as the touches, so they stay in order with the clicks.
final class TakeoverHost: UIView {
    let web: WKWebView
    let keys = KeyField(frame: CGRect(x: 0, y: 0, width: 2, height: 2))

    init(web: WKWebView) {
        self.web = web
        super.init(frame: .zero)
        backgroundColor = .black
        addSubview(web)
        addSubview(keys)
        keys.send = { [weak self] op in self?.forward(op) }
        keys.closed = { [weak self] in self?.js("window.aiwTouch&&aiwTouch.kb(false)") }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    override func layoutSubviews() {
        super.layoutSubviews()
        web.frame = bounds
        keys.frame = CGRect(x: 0, y: max(0, bounds.height - 2), width: 2, height: 2)
    }
    func keyboard(_ on: Bool) {
        keys.show(on)
        js("window.aiwTouch&&aiwTouch.kb(\(on ? "true" : "false"))")
    }
    func forward(_ op: [String: Any]) {
        guard let d = try? JSONSerialization.data(withJSONObject: op), let s = String(data: d, encoding: .utf8) else { return }
        js("window.aiwTouch&&aiwTouch.input(\(s))")
    }
    func js(_ s: String) { web.evaluateJavaScript(s, completionHandler: nil) }
}

/// 3.8.4 takeover keyboard: a hidden text field that stays first responder while the keyboard is on (taps on the
/// desktop can't take it away: canResignFirstResponder is false until 收起), with a key bar above the keyboard.
/// Committed text only (a Chinese IME's marked pinyin is left alone until it commits) goes out as unicode text;
/// ⌫ on the empty field, return, the bar's keys and a hardware keyboard's arrows / Esc / Tab go out as key names.
final class KeyField: UITextField, UITextFieldDelegate {
    var send: (([String: Any]) -> Void)?
    var closed: (() -> Void)?
    private var keepUp = false
    private var mods: [String] = []            // sticky Ctrl / Alt / Super for the next key
    private var modButtons: [String: UIButton] = [:]
    private static let hw: [String: String] = [
        UIKeyCommand.inputUpArrow: "Up", UIKeyCommand.inputDownArrow: "Down", UIKeyCommand.inputLeftArrow: "Left",
        UIKeyCommand.inputRightArrow: "Right", UIKeyCommand.inputEscape: "Escape", "\t": "Tab",
    ]

    override init(frame: CGRect) {
        super.init(frame: frame)
        delegate = self
        autocorrectionType = .no
        autocapitalizationType = .none
        spellCheckingType = .no
        smartQuotesType = .no
        smartDashesType = .no
        smartInsertDeleteType = .no
        keyboardAppearance = .dark
        textColor = .clear
        tintColor = .clear
        backgroundColor = .clear
        alpha = 0.02
        addTarget(self, action: #selector(changed), for: .editingChanged)
        inputAccessoryView = makeBar()
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    override var canResignFirstResponder: Bool { !keepUp }

    func show(_ on: Bool) {
        if on {
            keepUp = true
            text = ""
            _ = becomeFirstResponder()
        } else {
            keepUp = false
            _ = resignFirstResponder()
        }
    }

    @objc private func changed() {
        guard markedTextRange == nil, let t = text, !t.isEmpty else { return }   // IME: wait for the committed string
        text = ""
        if !mods.isEmpty && t.count == 1 { key(t == " " ? "space" : t.lowercased()); return }
        send?(["t": "text", "text": t])
    }
    func key(_ k: String) {
        let combo = (["ctrl", "alt", "super"].filter { mods.contains($0) } + [k]).joined(separator: "+")
        clearMods()
        send?(["t": "key", "key": combo])
    }
    override func deleteBackward() {
        if markedTextRange == nil && (text ?? "").isEmpty { key("BackSpace") } else { super.deleteBackward() }
    }
    func textFieldShouldReturn(_ textField: UITextField) -> Bool {
        key("Return")
        return false
    }
    override var keyCommands: [UIKeyCommand]? {
        KeyField.hw.keys.map { input in
            let c = UIKeyCommand(input: input, modifierFlags: [], action: #selector(hardware(_:)))
            if #available(iOS 15.0, *) { c.wantsPriorityOverSystemBehavior = true }
            return c
        }
    }
    @objc private func hardware(_ c: UIKeyCommand) {
        if let i = c.input, let k = KeyField.hw[i] { key(k) }
    }

    // ---- the key bar
    private func clearMods() {
        mods = []
        paintMods()
    }
    private func paintMods() {
        for (m, b) in modButtons {
            let on = mods.contains(m)
            b.backgroundColor = on ? UIColor(red: 0.94, green: 0.63, blue: 0.19, alpha: 1) : UIColor.white.withAlphaComponent(0.14)
            b.setTitleColor(on ? UIColor(red: 0.16, green: 0.10, blue: 0.02, alpha: 1) : .white, for: .normal)
        }
    }
    private func makeBar() -> UIView {
        let bar = UIInputView(frame: CGRect(x: 0, y: 0, width: 320, height: 46), inputViewStyle: .keyboard)
        bar.autoresizingMask = [.flexibleWidth]
        let scroll = UIScrollView()
        scroll.showsHorizontalScrollIndicator = false
        scroll.translatesAutoresizingMaskIntoConstraints = false
        let stack = UIStackView()
        stack.axis = .horizontal
        stack.spacing = 6
        stack.alignment = .center
        stack.translatesAutoresizingMaskIntoConstraints = false
        bar.addSubview(scroll)
        scroll.addSubview(stack)
        NSLayoutConstraint.activate([
            scroll.leadingAnchor.constraint(equalTo: bar.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: bar.trailingAnchor),
            scroll.topAnchor.constraint(equalTo: bar.topAnchor),
            scroll.bottomAnchor.constraint(equalTo: bar.bottomAnchor),
            stack.leadingAnchor.constraint(equalTo: scroll.contentLayoutGuide.leadingAnchor, constant: 8),
            stack.trailingAnchor.constraint(equalTo: scroll.contentLayoutGuide.trailingAnchor, constant: -8),
            stack.topAnchor.constraint(equalTo: scroll.contentLayoutGuide.topAnchor),
            stack.bottomAnchor.constraint(equalTo: scroll.contentLayoutGuide.bottomAnchor),
            stack.heightAnchor.constraint(equalTo: scroll.frameLayoutGuide.heightAnchor),
        ])
        let items: [(String, String)] = [
            ("收起", "hide"), ("Esc", "Escape"), ("Tab", "Tab"), ("Ctrl", "mod:ctrl"), ("Alt", "mod:alt"), ("⌘", "mod:super"),
            ("←", "Left"), ("↑", "Up"), ("↓", "Down"), ("→", "Right"), ("^C", "ctrl+c"), ("^V", "ctrl+v"), ("^A", "ctrl+a"),
            ("⌫", "BackSpace"), ("↵", "Return"),
        ]
        for (title, action) in items {
            let b = UIButton(type: .system)
            b.setTitle(title, for: .normal)
            b.titleLabel?.font = .systemFont(ofSize: 15, weight: .semibold)
            b.setTitleColor(.white, for: .normal)
            b.backgroundColor = UIColor.white.withAlphaComponent(0.14)
            b.layer.cornerRadius = 8
            b.accessibilityIdentifier = action
            b.accessibilityLabel = title == "⌘" ? "Super" : title
            b.translatesAutoresizingMaskIntoConstraints = false
            b.heightAnchor.constraint(equalToConstant: 34).isActive = true
            b.widthAnchor.constraint(greaterThanOrEqualToConstant: title.count > 2 ? 48 : 40).isActive = true
            b.addTarget(self, action: #selector(tapBar(_:)), for: .touchUpInside)
            if action.hasPrefix("mod:") { modButtons[String(action.dropFirst(4))] = b }
            stack.addArrangedSubview(b)
        }
        return bar
    }
    @objc private func tapBar(_ b: UIButton) {
        guard let a = b.accessibilityIdentifier else { return }
        if a == "hide" {
            show(false)
            closed?()
        } else if a.hasPrefix("mod:") {
            let m = String(a.dropFirst(4))
            if let i = mods.firstIndex(of: m) { mods.remove(at: i) } else { mods.append(m) }
            paintMods()
        } else if a.contains("+") {
            clearMods()
            send?(["t": "key", "key": a])
        } else {
            key(a)
        }
    }
}
