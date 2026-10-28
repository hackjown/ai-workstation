import SwiftUI
import PhotosUI
import UniformTypeIdentifiers

/// iOS 3.4 home: ONE persistent chat with 球球 (server: /api/chat*, chat.py), the same as the web main screen.
/// * history loads on open, scrolled to the bottom; older pages load when the top comes into view
/// * replies stream over SSE (POST /api/chat/send); no thinking and no agent steps are ever shown here
/// * a task is a compact card (title, status, step) that follows the task live (/ws/events kind "chat");
///   tapping it pushes the existing task view (ChatView: the computer on top, the steps below); back keeps the scroll
/// * questions / confirmations come in as messages with buttons; 我来接管桌面 opens the task view in takeover
/// * a reply that needs the computer has 交给电脑去做 (POST /api/chat/messages/{id}/to_task)
/// * 新建任务 (top right) sends the next message straight to the computer (mode "task")
enum Route: Hashable {
    case task(String, takeover: Bool)   // a task's thread
    case newTask                       // a blank task view (files shared from other apps land here)
}

final class HomeChat: ObservableObject {
    @Published var msgs: [JSON] = []
    @Published var hasMore = false
    @Published var loadingOlder = false
    @Published var loaded = false
    @Published var stream: String? = nil       // the reply being streamed ("" = still waiting)
    @Published var streamNote = ""
    @Published var sending = false
    @Published var err = ""
    @Published var agent = "小秘"
    @Published var bottomTick = 0              // bumped when the view should scroll to the newest message

    @MainActor func index(_ id: String) -> Int? { msgs.firstIndex { $0.s("id") == id } }

    @MainActor func load() async {
        do {
            let r = try await API.shared.obj("GET", "/api/chat?limit=60")
            msgs = r.a("messages"); hasMore = r.b("has_more"); loaded = true; err = ""
            if !r.s("agent_name").isEmpty { agent = r.s("agent_name") }
            bottomTick += 1
            markRead()
        } catch { err = "聊天加载失败：\(error.localizedDescription)" }
    }
    /// Older page above the first message; returns the id that was first, to keep it in place.
    @MainActor func older() async -> String? {
        guard hasMore, !loadingOlder, let first = msgs.first(where: { $0.d("seq") > 0 }) else { return nil }
        loadingOlder = true
        defer { loadingOlder = false }
        guard let r = try? await API.shared.obj("GET", "/api/chat?limit=40&before=\(Int(first.d("seq")))") else { return nil }
        let have = Set(msgs.map { $0.s("id") })
        msgs = r.a("messages").filter { !have.contains($0.s("id")) } + msgs
        hasMore = r.b("has_more")
        return first.s("id")
    }
    /// Messages that came in while the socket was down (app in the background, network change).
    @MainActor func sync() async {
        guard loaded, let r = try? await API.shared.obj("GET", "/api/chat?limit=60") else { return }
        var added = false
        for m in r.a("messages") {
            if let i = index(m.s("id")) { msgs[i] = m } else { msgs.append(m); added = true }
        }
        sortMsgs()
        if added { bottomTick += 1 }
    }
    static func order(_ m: JSON) -> Double { let s = m.d("seq"); return s > 0 ? s : 1e15 }   // unsent ones last
    @MainActor func sortMsgs() { msgs.sort { HomeChat.order($0) < HomeChat.order($1) } }

    @MainActor func upsert(_ m: JSON, stick: Bool = false) {
        guard !m.s("id").isEmpty else { return }
        if let i = index(m.s("id")) { msgs[i] = m; return }
        if m.s("role") == "user" { msgs.removeAll { $0.b("tmp") && $0.s("text") == m.s("text") } }
        if m.s("role") != "user" && m.s("kind") == "text" { stream = nil }
        msgs.append(m); sortMsgs()
        bottomTick += 1
        markRead()
    }
    @MainActor func onEvent(_ e: JSON) {
        if e.s("kind") == "chat" { upsert(e.o("message")) }
    }
    @MainActor func markRead() {
        guard let last = msgs.last(where: { $0.d("seq") > 0 }) else { return }
        let seq = Int(last.d("seq"))
        Task { _ = try? await API.shared.call("POST", "/api/chat/read", ["seq": seq]) }
    }

    /// POST /api/chat/send as an event stream: user → route → delta… → message | task → done.
    @MainActor func send(_ text: String, attachments: [JSON], mode: String) async -> Bool {
        guard !sending else { return false }
        sending = true; err = ""
        defer { sending = false; stream = nil; streamNote = "" }
        let tmp: JSON = ["id": "tmp-\(Date().timeIntervalSince1970)", "role": "user", "kind": "text",
                         "text": text.isEmpty ? "（附件）" : text, "data": ["attachments": attachments], "tmp": true]
        msgs.append(tmp); bottomTick += 1
        stream = ""
        guard let u = API.shared.url("/api/chat/send") else { return false }
        var r = URLRequest(url: u, timeoutInterval: 300)
        r.httpMethod = "POST"
        r.setValue("Bearer \(API.shared.token)", forHTTPHeaderField: "Authorization")
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let body: JSON = ["text": text, "attachments": attachments, "machine_id": API.shared.machine, "mode": mode, "stream": true]
        r.httpBody = try? JSONSerialization.data(withJSONObject: body)
        do {
            let (bytes, resp) = try await URLSession.shared.bytes(for: r)
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            if code == 401 { throw APIError(message: "令牌不对或已失效") }
            if code >= 400 { throw APIError(message: "发送失败（\(code)）") }
            var event = "message"
            var acc = ""
            for try await line in bytes.lines {
                if line.hasPrefix("event:") { event = String(line.dropFirst(6)).trimmingCharacters(in: .whitespaces); continue }
                guard line.hasPrefix("data:") else { continue }
                let raw = String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces)
                guard let d = raw.data(using: .utf8), let obj = try? JSONSerialization.jsonObject(with: d) as? JSON else { continue }
                switch event {
                case "user": upsert(obj, stick: true)
                case "route": if obj.s("route") == "task" { streamNote = "正在交给电脑…" }
                case "delta": acc += obj.s("text"); stream = acc; bottomTick += 1
                case "message", "task": stream = nil; upsert(obj, stick: true)
                case "done": break
                default: break
                }
                event = "message"
            }
            return true
        } catch {
            msgs.removeAll { $0.b("tmp") }
            err = "发送失败：\(error.localizedDescription)"
            return false
        }
    }

    @MainActor func answer(_ id: String, _ body: JSON) async -> JSON? {
        do {
            let r = try await API.shared.obj("POST", "/api/chat/messages/\(id)/answer", body)
            upsert(r.o("message"))
            return r
        } catch { err = error.localizedDescription; await sync(); return nil }
    }
    @MainActor func toTask(_ id: String) async {
        do {
            let card = try await API.shared.obj("POST", "/api/chat/messages/\(id)/to_task", ["machine_id": API.shared.machine])
            upsert(card, stick: true)
            if let i = index(id) {
                var m = msgs[i]; var d = m.o("data"); d["task_started"] = card.s("id"); m["data"] = d; msgs[i] = m
            }
        } catch { err = "没能交给电脑：\(error.localizedDescription)" }
    }
    @MainActor func retry(_ taskId: String) async {
        do { _ = try await API.shared.call("POST", "/api/tasks/\(taskId)/retry"); err = "" }
        catch { err = "重试失败：\(error.localizedDescription)" }
    }
}

struct HomeChatView: View {
    @EnvironmentObject var api: API
    @EnvironmentObject var hub: Hub
    @StateObject private var m = HomeChat()
    @Binding var path: [Route]
    let openSheet: (HomeSheet) -> Void
    let openSettings: () -> Void
    @State private var text = ""
    @State private var mode = "auto"
    @State private var attach: [JSON] = []
    @State private var uploading = false
    @State private var photo: PhotosPickerItem?
    @State private var showPhotos = false
    @State private var showFiles = false
    @State private var preview: Preview?
    @State private var keepTop: String? = nil
    @State private var atBottom = true
    @FocusState private var focused: Bool
    @Environment(\.scenePhase) private var phase
    @AppStorage("hdraft") private var draft = ""

    var liveStatus: String {
        m.msgs.last(where: { $0.s("kind") == "task" && isLive($0.o("data").s("status")) })?.o("data").s("status") ?? ""
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    if m.hasMore {
                        HStack { Spacer(); if m.loadingOlder { ProgressView() } else { Text("加载更早的消息").font(.caption).foregroundColor(.secondary) }; Spacer() }
                            .padding(.vertical, 6).id("older")
                            .onAppear { Task {
                                if let keep = await m.older() { proxy.scrollTo(keep, anchor: .top) }
                            } }
                    } else if !m.msgs.isEmpty {
                        Text("· 聊天从这里开始 ·").font(.caption2).foregroundColor(.secondary).frame(maxWidth: .infinity)
                    }
                    if m.loaded && m.msgs.isEmpty { HomeChatEmpty(agent: m.agent) { text = $0; focused = true } }
                    ForEach(m.msgs.indices, id: \.self) { i in
                        row(m.msgs[i]).id(m.msgs[i].s("id"))
                    }
                    if let s = m.stream { streamRow(s) }
                    if !m.err.isEmpty { Text(m.err).font(.footnote).foregroundColor(.red) }
                    Color.clear.frame(height: 1).id("end")
                        .onAppear { atBottom = true }.onDisappear { atBottom = false }
                }.padding(.horizontal, 14).padding(.vertical, 10)
            }
            .scrollDismissesKeyboard(.interactively)
            .onChange(of: m.bottomTick) { _ in
                if atBottom || m.sending { withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo("end", anchor: .bottom) } }
            }
            .onChange(of: m.loaded) { ok in if ok { DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { proxy.scrollTo("end", anchor: .bottom) } } }
            .safeAreaInset(edge: .bottom) { composer }
        }
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .navigationBarLeading) {
                Button(action: openSettings) { Image(systemName: "gearshape").font(.system(size: 17)) }.accessibilityLabel("设置")
            }
            ToolbarItem(placement: .principal) { MateTitle(title: m.agent, status: liveStatus, threadId: nil, showMate: true) }
            ToolbarItemGroup(placement: .navigationBarTrailing) {
                Button { mode = "task"; focused = true } label: { Image(systemName: "square.and.pencil").font(.system(size: 17)) }
                    .accessibilityLabel("新建任务")
                Menu {
                    Button { mode = "task"; focused = true } label: { Label("新建任务", systemImage: "square.and.pencil") }
                    Button { openSheet(.history) } label: { Label("任务", systemImage: "list.bullet.rectangle") }
                    Button { openSheet(.archive) } label: { Label("归档", systemImage: "archivebox") }
                    Button { openSheet(.tracking) } label: { Label("追踪", systemImage: "scope") }
                    Button { openSheet(.routine) } label: { Label("例程", systemImage: "arrow.triangle.2.circlepath") }
                    Button { openSheet(.computer) } label: { Label("电脑", systemImage: "desktopcomputer") }
                } label: { Image(systemName: "plus").font(.system(size: 19, weight: .medium)) }.accessibilityLabel("菜单")
            }
        }
        .task { if !m.loaded { await m.load() }; if text.isEmpty { text = draft } }
        .refreshable { await m.load() }
        .onReceive(hub.events) { e in m.onEvent(e) }
        .onChange(of: phase) { p in if p == .active { Task { await m.sync() } } }
        .onChange(of: text) { t in draft = t }
        .onChange(of: photo) { item in Task { await addPhoto(item) } }
        .photosPicker(isPresented: $showPhotos, selection: $photo, matching: .images)
        .fileImporter(isPresented: $showFiles, allowedContentTypes: [.item], allowsMultipleSelection: true) { r in
            if case .success(let urls) = r { Task { for u in urls { await addFile(u) } } }
        }
        .sheet(item: $preview) { p in
            if !textPreviewKind(p.url).isEmpty, textPreviewPage() != nil { FilePreviewView(url: p.url, kind: textPreviewKind(p.url)) }
            else { QuickLookView(url: p.url).ignoresSafeArea() }
        }
    }

    // MARK: rows
    @ViewBuilder func row(_ msg: JSON) -> some View {
        let d = msg.o("data")
        if msg.s("role") == "user" {
            HStack { Spacer(minLength: 40)
                VStack(alignment: .trailing, spacing: 3) {
                    Text(msg.s("text")).font(.callout).textSelection(.enabled)
                        .padding(.horizontal, 13).padding(.vertical, 9)
                        .background(Color(.secondarySystemBackground)).clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
                    ForEach(d.a("attachments").indices, id: \.self) { i in
                        Label(d.a("attachments")[i].s("name"), systemImage: "paperclip").font(.caption).foregroundColor(.secondary)
                    }
                    Text(msg.b("tmp") ? "发送中…" : when(msg.d("created_at"))).font(.caption2).foregroundColor(.secondary)
                }
            }
        } else {
            switch msg.s("kind") {
            case "task": TaskCardRow(msg: msg, open: { open(msg, takeover: false) }, retry: { Task { await m.retry(d.s("task_id")) } })
            case "ask": AskRow(msg: msg, model: m, open: { take in open(msg, takeover: take) })
            case "confirm": ConfirmRow(msg: msg, model: m, open: { open(msg, takeover: false) })
            case "task_result": resultRow(msg)
            default: textRow(msg)
            }
        }
    }
    @ViewBuilder func textRow(_ msg: JSON) -> some View {
        let d = msg.o("data")
        VStack(alignment: .leading, spacing: 6) {
            RichText(text: msg.s("text"))
                .padding(.horizontal, 13).padding(.vertical, 9)
                .background(Color(.systemBackground))
                .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous).stroke(d.b("error") ? Color.red.opacity(0.5) : Color(.separator).opacity(0.5)))
                .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
            if !d.s("offer_task").isEmpty {
                if d.s("task_started").isEmpty {
                    Button { Task { await m.toTask(msg.s("id")) } } label: { Label("交给电脑去做", systemImage: "desktopcomputer") }
                        .buttonStyle(.borderedProminent).tint(amber).controlSize(.small)
                } else {
                    Label("已交给电脑", systemImage: "checkmark").font(.caption).foregroundColor(.secondary)
                }
            }
            Text(when(msg.d("created_at"))).font(.caption2).foregroundColor(.secondary)
        }.padding(.trailing, 30)
    }
    @ViewBuilder func streamRow(_ s: String) -> some View {
        HStack {
            if s.isEmpty {
                HStack(spacing: 6) { ProgressView().controlSize(.small); Text(m.streamNote.isEmpty ? "正在想…" : m.streamNote).font(.caption).foregroundColor(.secondary) }
                    .padding(.horizontal, 13).padding(.vertical, 10)
                    .background(Color(.systemBackground)).clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
            } else {
                RichText(text: s).padding(.horizontal, 13).padding(.vertical, 9)
                    .background(Color(.systemBackground)).clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
            }
            Spacer(minLength: 30)
        }.id("stream")
    }
    @ViewBuilder func resultRow(_ msg: JSON) -> some View {
        let d = msg.o("data")
        let ok = d.s("status") == "succeeded"
        VStack(alignment: .leading, spacing: 6) {
            RichText(text: msg.s("text"))
                .padding(.leading, 15).padding(.trailing, 12).padding(.vertical, 9)
                .background(Color(.systemBackground))
                .overlay(alignment: .leading) { Rectangle().fill(ok ? okGreen : Color.red).frame(width: 3) }
                .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            ForEach(d.a("files").indices, id: \.self) { i in
                let f = d.a("files")[i]
                Button { Task { await openFile(f.s("path"), f.s("name")) } } label: {
                    HStack(spacing: 10) {
                        Image(systemName: f.s("kind") == "image" ? "photo" : f.s("kind") == "video" ? "film" : "doc").foregroundColor(amber)
                            .frame(width: 32, height: 32).background(amber.opacity(0.12)).clipShape(RoundedRectangle(cornerRadius: 8))
                        Text(f.s("name")).font(.callout.weight(.medium)).lineLimit(1).foregroundColor(.primary)
                        Spacer(minLength: 0)
                        Image(systemName: "chevron.right").font(.caption).foregroundColor(.secondary)
                    }.padding(8).background(Color(.secondarySystemBackground)).clipShape(RoundedRectangle(cornerRadius: 12))
                }.buttonStyle(.plain)
            }
            HStack(spacing: 6) {
                Text(when(msg.d("created_at"))).font(.caption2).foregroundColor(.secondary)
                if !d.s("thread_id").isEmpty {
                    Button(d.b("has_more") ? "查看完整结果 ›" : "查看任务 ›") { open(msg, takeover: false) }.font(.caption2)
                }
            }
        }.padding(.trailing, 30)
    }

    // MARK: composer
    var composer: some View {
        VStack(spacing: 6) {
            if mode == "task" {
                HStack(spacing: 6) {
                    Image(systemName: "desktopcomputer")
                    Text("新建任务：这条消息直接交给电脑去做").lineLimit(1)
                    Spacer()
                    Button { mode = "auto" } label: { Image(systemName: "xmark.circle.fill") }.accessibilityLabel("取消新建任务")
                }.font(.caption.weight(.medium)).foregroundColor(amber)
                .padding(.horizontal, 10).padding(.vertical, 7).background(amber.opacity(0.12)).clipShape(RoundedRectangle(cornerRadius: 10))
            }
            if !attach.isEmpty || uploading {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack {
                        if uploading { ProgressView().controlSize(.small) }
                        ForEach(attach.indices, id: \.self) { i in
                            HStack(spacing: 4) {
                                Image(systemName: "paperclip"); Text(attach[i].s("name")).lineLimit(1)
                                Button { attach.remove(at: i) } label: { Image(systemName: "xmark.circle.fill") }
                            }.font(.caption).padding(.horizontal, 8).padding(.vertical, 5).background(Color(.secondarySystemBackground)).clipShape(Capsule())
                        }
                    }
                }
            }
            HStack(alignment: .bottom, spacing: 8) {
                Menu {
                    Button { showPhotos = true } label: { Label("照片", systemImage: "photo") }
                    Button { showFiles = true } label: { Label("文件", systemImage: "folder") }
                } label: { Image(systemName: "plus").font(.system(size: 18, weight: .medium)).frame(width: 34, height: 34) }
                    .accessibilityLabel("添加附件")
                TextField(mode == "task" ? "要电脑做什么？" : "和我聊聊，或者让电脑去办件事…", text: $text, axis: .vertical)
                    .lineLimit(1...6).focused($focused).padding(.vertical, 8)
                Button { Task { await send() } } label: {
                    Image(systemName: m.sending ? "ellipsis" : "arrow.up").font(.system(size: 16, weight: .bold)).foregroundColor(.white)
                        .frame(width: 34, height: 34).background(canSend ? amber : Color.gray.opacity(0.4)).clipShape(Circle())
                }.disabled(!canSend).accessibilityLabel("发送")
            }
            .padding(.horizontal, 8).padding(.vertical, 4)
            .background(Color(.systemBackground))
            .overlay(RoundedRectangle(cornerRadius: 24, style: .continuous).stroke(focused ? amber.opacity(0.6) : Color(.separator), lineWidth: 1))
            .clipShape(RoundedRectangle(cornerRadius: 24, style: .continuous))
        }
        .padding(.horizontal, 12).padding(.top, 6).padding(.bottom, 8)
        .background(.bar)
    }
    var canSend: Bool { !m.sending && !uploading && (!text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !attach.isEmpty) }

    func send() async {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let files = attach, md = mode
        text = ""; attach = []; mode = "auto"; draft = ""
        if !(await m.send(t, attachments: files, mode: md)) { text = t; attach = files; mode = md }
    }
    func open(_ msg: JSON, takeover: Bool) {
        let tid = msg.o("data").s("thread_id")
        guard !tid.isEmpty else { m.err = "这个任务还没有开始"; return }
        path.append(.task(tid, takeover: takeover))
    }
    func openFile(_ path: String, _ name: String) async {
        guard let u = API.shared.artifactURL(path) else { return }
        guard let (tmp, _) = try? await URLSession.shared.download(from: u) else { m.err = "下载失败：\(name)"; return }
        let dest = FileManager.default.temporaryDirectory.appendingPathComponent(name)
        try? FileManager.default.removeItem(at: dest)
        try? FileManager.default.moveItem(at: tmp, to: dest)
        preview = Preview(url: dest)
    }
    func addPhoto(_ item: PhotosPickerItem?) async {
        guard let item = item else { return }
        photo = nil
        guard let d = try? await item.loadTransferable(type: Data.self) else { m.err = "读不了这张照片"; return }
        let jpg = UIImage(data: d)?.jpegData(compressionQuality: 0.85) ?? d
        await upload(name: "photo-\(Int(Date().timeIntervalSince1970)).jpg", data: jpg)
    }
    func addFile(_ u: URL) async {
        let ok = u.startAccessingSecurityScopedResource()
        defer { if ok { u.stopAccessingSecurityScopedResource() } }
        guard let d = try? Data(contentsOf: u) else { m.err = "读不了 \(u.lastPathComponent)"; return }
        await upload(name: u.lastPathComponent, data: d)
    }
    /// Chat attachments stay on the API host (local_only); a task the chat starts copies them to its computer.
    func upload(name: String, data: Data) async {
        if data.count > 40 * 1024 * 1024 { m.err = "\(name) 超过 40MB"; return }
        uploading = true
        defer { uploading = false }
        do {
            let r = try await API.shared.postWithProgress("/api/uploads", ["name": name, "content_b64": data.base64EncodedString(),
                                                                           "machine_id": API.shared.machine, "local_only": true]) { _ in }
            attach.append(r)
        } catch { m.err = "上传失败：\(error.localizedDescription)" }
    }
}

/// Empty chat: 球球 and a few things to try.
struct HomeChatEmpty: View {
    let agent: String
    let use: (String) -> Void
    @AppStorage("mate") private var mateOn = true
    var body: some View {
        VStack(spacing: 14) {
            if mateOn { MateView(status: "", threadId: nil).frame(width: 96, height: 96) }
            Text("我是\(agent)").font(.title3.bold())
            Text("随便聊，问我问题；要在电脑上动手的事我会交给电脑去做，进度会以卡片出现在这里。")
                .font(.footnote).foregroundColor(.secondary).multilineTextAlignment(.center)
            ForEach(["今天有什么值得关注的科技新闻？", "帮我在电脑上打开 Hacker News，把前 5 条整理成表格发给我", "我有哪些任务没完成？"], id: \.self) { s in
                Button { use(s) } label: {
                    Text(s).font(.callout).foregroundColor(.primary).multilineTextAlignment(.leading)
                        .frame(maxWidth: .infinity, alignment: .leading).padding(10)
                        .background(Color(.secondarySystemBackground)).clipShape(RoundedRectangle(cornerRadius: 12))
                }.buttonStyle(.plain)
            }
        }.padding(.top, 40).padding(.horizontal, 10).frame(maxWidth: .infinity)
    }
}

/// A task in the chat: title, status, step / plan progress; tap → the task view.
struct TaskCardRow: View {
    let msg: JSON
    let open: () -> Void
    let retry: () -> Void
    var body: some View {
        let d = msg.o("data")
        let st = d.s("status").isEmpty ? "running" : d.s("status")
        let live = isLive(st)
        let total = d.d("plan_total"), done = d.d("plan_done")
        Button(action: open) {
            HStack(spacing: 12) {
                ZStack {
                    RoundedRectangle(cornerRadius: 9).fill((st == "succeeded" ? okGreen : amber).opacity(0.13))
                    if live && st == "running" { ProgressView().controlSize(.small) }
                    else { Image(systemName: st == "succeeded" ? "checkmark" : "desktopcomputer").foregroundColor(st == "succeeded" ? okGreen : amber) }
                }.frame(width: 36, height: 36)
                VStack(alignment: .leading, spacing: 3) {
                    Text(cleanTitle(d.s("title").isEmpty ? msg.s("text") : d.s("title"))).font(.callout.weight(.semibold)).lineLimit(2).foregroundColor(.primary)
                    Text(sub(d, st)).font(.caption).foregroundColor(.secondary).lineLimit(1)
                    if total > 0 {
                        ProgressView(value: min(done, total), total: total).tint(amber).scaleEffect(x: 1, y: 0.6, anchor: .center)
                    }
                }
                Spacer(minLength: 4)
                VStack(alignment: .trailing, spacing: 6) {
                    Chip(text: statusText[st] ?? st, color: chipColor(st), dot: true)
                    if ["failed", "stopped", "interrupted"].contains(st) && !d.s("task_id").isEmpty {
                        Button("重试", action: retry).font(.caption.weight(.semibold)).buttonStyle(.bordered).controlSize(.mini)
                    } else { Image(systemName: "chevron.right").font(.caption).foregroundColor(.secondary) }
                }
            }
            .padding(12)
            .background(Color(.systemBackground))
            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(["waiting_input", "waiting_confirm", "takeover"].contains(st) ? amber : Color(.separator).opacity(0.7), lineWidth: 1))
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        }
        .buttonStyle(.plain)
        .padding(.trailing, 20)
        .accessibilityHint("打开任务")
    }
    func chipColor(_ st: String) -> Color {
        if st == "succeeded" { return okGreen }
        if ["failed", "interrupted", "stopped"].contains(st) { return Color.red }
        return amber
    }
    func sub(_ d: JSON, _ st: String) -> String {
        var parts: [String] = []
        let line = d.s("status_line")
        if !line.isEmpty && line != statusText[st] { parts.append(line) }
        if d.d("plan_total") > 0 { parts.append("\(Int(d.d("plan_done")))/\(Int(d.d("plan_total"))) 项") }
        if isLive(st) && d.d("step") > 0 { parts.append("第 \(Int(d.d("step")))\(d.d("max_steps") > 0 ? "/\(Int(d.d("max_steps")))" : "") 步") }
        return parts.isEmpty ? "交给电脑的任务" : parts.joined(separator: " · ")
    }
}

/// A task's question (escalation, takeover, form, free question) with buttons.
struct AskRow: View {
    let msg: JSON
    @ObservedObject var model: HomeChat
    let open: (Bool) -> Void
    @State private var reply = ""
    @State private var busy = false
    var body: some View {
        let d = msg.o("data")
        let k = d.s("ask_kind").isEmpty ? "question" : d.s("ask_kind")
        let resolved = d.o("resolved")
        let done = !resolved.isEmpty
        let stale = d.b("stale") && !done
        VStack(alignment: .leading, spacing: 8) {
            Text(head(k) + (d.s("title").isEmpty ? "" : " · " + cleanTitle(d.s("title")))).font(.caption.weight(.bold)).foregroundColor(done || stale ? Color.secondary : amber)
            Text(msg.s("text")).font(.callout.weight(.semibold)).textSelection(.enabled)
            if k == "takeover", !d.o("takeover").s("steps").isEmpty {
                Text(d.o("takeover").s("steps")).font(.footnote).foregroundColor(.secondary)
            }
            if done {
                Label(answerText(resolved), systemImage: "checkmark").font(.footnote).foregroundColor(okGreen)
            } else if stale {
                Text("已过期（任务已经不在等这个回答）").font(.footnote).foregroundColor(.secondary)
            } else if k == "takeover" {
                HStack {
                    Button { open(true) } label: { Label("接管桌面", systemImage: "hand.raised.fill") }.buttonStyle(.borderedProminent).tint(amber)
                    Button("完成，继续") { go(["choice": "done"]) }.buttonStyle(.bordered)
                }.controlSize(.small).disabled(busy)
            } else if k == "form" {
                Button("去填写") { open(false) }.buttonStyle(.borderedProminent).tint(amber).controlSize(.small)
            } else {
                let opts = (d["options"] as? [Any])?.map { "\($0)" } ?? []
                if !opts.isEmpty {
                    FlowButtons(options: opts) { o in go(["choice": o]) }
                }
                HStack {
                    TextField(opts.isEmpty ? "回答…" : "或者直接告诉它…", text: $reply).textFieldStyle(.roundedBorder).submitLabel(.send)
                        .onSubmit { send() }
                    Button("回复") { send() }.buttonStyle(.borderedProminent).tint(amber).controlSize(.small)
                        .disabled(reply.trimmingCharacters(in: .whitespaces).isEmpty || busy)
                }
            }
            if !d.s("thread_id").isEmpty { Button("查看任务 ›") { open(false) }.font(.caption) }
        }
        .padding(12)
        .background((done || stale ? Color(.secondarySystemBackground) : amber.opacity(0.08)))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).stroke(done || stale ? Color(.separator).opacity(0.6) : amber.opacity(0.5)))
        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        .padding(.trailing, 20)
    }
    func head(_ k: String) -> String {
        switch k { case "takeover": return "需要你接管"; case "form": return "需要你填写"; case "escalate": return "任务卡住了"; default: return "任务在问你" }
    }
    func answerText(_ r: JSON) -> String {
        let a = r["answer"]
        if let b = a as? Bool { return b ? "已允许" : "已拒绝" }
        let s = r.s("answer")
        return s == "done" ? "已完成，交还给电脑" : "已回答：" + String(s.prefix(60))
    }
    func send() { let s = reply.trimmingCharacters(in: .whitespacesAndNewlines); guard !s.isEmpty else { return }; go(["text": s]); reply = "" }
    func go(_ body: JSON) {
        busy = true
        Task {
            let r = await model.answer(msg.s("id"), body)
            busy = false
            if let r = r, r.b("open_task") { open(true) }   // 我来接管桌面 → the task view, straight into the desktop
        }
    }
}

/// A confirmation the task needs (plain-language summary), 允许 / 不允许.
struct ConfirmRow: View {
    let msg: JSON
    @ObservedObject var model: HomeChat
    let open: () -> Void
    @State private var busy = false
    var body: some View {
        let d = msg.o("data"), s = d.o("summary")
        let resolved = d.o("resolved")
        let done = !resolved.isEmpty, stale = d.b("stale") && !done
        VStack(alignment: .leading, spacing: 8) {
            Text("需要你确认" + (d.s("title").isEmpty ? "" : " · " + cleanTitle(d.s("title")))).font(.caption.weight(.bold)).foregroundColor(done || stale ? Color.secondary : amber)
            Text(s.s("title").isEmpty ? msg.s("text") : s.s("title")).font(.callout.weight(.semibold))
            if !s.s("what").isEmpty { Text(s.s("what")).font(.footnote).foregroundColor(.secondary) }
            let why = [s.s("risk_cn"), s.s("why")].filter { !$0.isEmpty }.joined(separator: " · ")
            if !why.isEmpty { Text(why).font(.footnote).foregroundColor(.secondary) }
            if done {
                Label((resolved["answer"] as? Bool) == true ? "已允许" : "已拒绝", systemImage: "checkmark").font(.footnote).foregroundColor(okGreen)
            } else if stale {
                Text("已过期（任务已经不在等这个确认）").font(.footnote).foregroundColor(.secondary)
            } else {
                HStack {
                    Button("允许") { go(true) }.buttonStyle(.borderedProminent).tint(amber)
                    Button("不允许") { go(false) }.buttonStyle(.bordered)
                }.controlSize(.small).disabled(busy)
            }
            if !d.s("thread_id").isEmpty { Button("查看任务 ›", action: open).font(.caption) }
        }
        .padding(12)
        .background((done || stale ? Color(.secondarySystemBackground) : amber.opacity(0.08)))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).stroke(done || stale ? Color(.separator).opacity(0.6) : amber.opacity(0.5)))
        .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
        .padding(.trailing, 20)
    }
    func go(_ ok: Bool) { busy = true; Task { _ = await model.answer(msg.s("id"), ["approve": ok]); busy = false } }
}
