import SwiftUI
import WebKit

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
    @Environment(\.scenePhase) var phase
    private let timer = Timer.publish(every: 1.5, on: .main, in: .common).autoconnect()

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Circle().fill(liveSession != nil ? amber : Color.secondary.opacity(0.5)).frame(width: 7, height: 7)
                Text(liveSession != nil ? "Agent 正在操作电脑" : "Agent 电脑").font(.caption.weight(.semibold))
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
        .onChange(of: liveSession) { s in if s != nil { withAnimation { open = true } } }
        .onReceive(timer) { _ in if open && phase == .active && !taking { Task { await grab() } } }
        .fullScreenCover(isPresented: $taking) {
            TakeoverView(liveSession: liveSession) { Task { await handBack() } }
        }
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
        await onChange()
    }
}

/// Full screen desktop you can drive (noVNC from the Aiw server), with a bar to hand control back.
struct TakeoverView: View {
    @EnvironmentObject var api: API
    let liveSession: String?
    let done: () -> Void
    @State private var url: URL?
    @State private var err = ""
    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Circle().fill(Color.red).frame(width: 8, height: 8)
                Text(liveSession != nil ? "你在操作桌面，Agent 已暂停" : "你在操作桌面").font(.subheadline.weight(.semibold))
                Spacer()
                Button(liveSession != nil ? "交还给 Agent" : "结束操作") { done() }.buttonStyle(.borderedProminent)
            }.padding(.horizontal, 14).padding(.vertical, 10).background(.bar)
            ZStack {
                Color.black
                if let u = url { WebDesk(url: u) }
                else if !err.isEmpty { Text(err).foregroundColor(.white).padding() }
                else { ProgressView().tint(.white) }
            }
        }
        .task { await load() }
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

struct WebDesk: UIViewRepresentable {
    let url: URL
    func makeUIView(context: Context) -> WKWebView {
        let c = WKWebViewConfiguration()
        c.allowsInlineMediaPlayback = true
        let w = WKWebView(frame: .zero, configuration: c)
        w.isOpaque = false; w.backgroundColor = .black
        w.scrollView.isScrollEnabled = false
        w.load(URLRequest(url: url))
        return w
    }
    func updateUIView(_ w: WKWebView, context: Context) {}
}
