import SwiftUI
import PhotosUI
import QuickLook
import UniformTypeIdentifiers

let toolCN: [String: String] = [
    "screenshot": "截图", "click": "点击", "double_click": "双击", "right_click": "右键", "move": "移动鼠标", "drag": "拖动",
    "type": "输入文字", "keypress": "按键", "scroll": "滚动", "open_app": "打开应用", "close_app": "关闭应用", "shell": "执行命令",
    "browser_open": "打开浏览器", "browser_navigate": "打开网址", "browser_back": "后退", "browser_forward": "前进", "files_list": "列文件",
    "files_read": "读文件", "files_write": "写文件", "files_download": "下载", "send_file": "发送文件", "done": "完成", "fail": "放弃",
    "update_plan": "更新计划", "ask_user": "提问", "remember": "记住", "delegate": "交给助手", "remote_exec": "远程执行",
    "deploy_server": "部署", "message": "说明"]
func toolName(_ t: String) -> String {
    if let n = toolCN[t] { return n }
    if t.hasPrefix("mcp_") { return "MCP · " + t.dropFirst(4) }
    return t
}

struct Step: Identifiable {
    let id = UUID()
    var tool: String
    var text: String
    var ok: Bool? = nil
}

/// Everything shown for one task: its steps from the session stream, and what it waits on.
final class TaskLive: ObservableObject {
    @Published var steps: [Step] = []
    @Published var status = ""
    @Published var activity = ""
    @Published var result = ""
    @Published var confirm: JSON? = nil
    @Published var question: JSON? = nil
    @Published var files: [JSON] = []
    @Published var plan: [String] = []
    @Published var planDone: Set<Int> = []
    var sock: Socket?
    var loaded = false

    func apply(_ e: JSON) {
        let type = e.s("type"), d = e.o("data")
        switch type {
        case "agent.plan":
            plan = (d["plan"] as? [String]) ?? plan
            planDone = Set((d["done"] as? [Int]) ?? [])
        case "agent.plan_update":
            planDone = Set((d["done"] as? [Int]) ?? Array(planDone))
        case "agent.thinking":
            activity = d.s("phase") == "vision" ? "正在看屏幕…" : "正在思考…"
        case "agent.observation":
            activity = "正在看屏幕…"
        case "agent.decision":
            let tool = d.s("tool")
            let a = d.o("args")
            var txt = ["command", "url", "text", "key", "name", "path", "goal", "question", "host"].lazy.map { a.s($0) }.first { !$0.isEmpty } ?? ""
            if txt.isEmpty, let x = a["x"] { txt = "(\(x), \(a.s("y")))" }
            let th = d.s("thought")
            if !th.isEmpty && tool != "done" { steps.append(Step(tool: "message", text: String(th.prefix(400)))) }
            if !["done", "fail", "update_plan"].contains(tool) { steps.append(Step(tool: tool, text: String(txt.prefix(200)))) }
            activity = "正在\(toolName(tool))…"
        case "agent.message":
            steps.append(Step(tool: "message", text: d.s("text")))
        case "agent.action_result":
            if let i = steps.lastIndex(where: { $0.tool != "message" }) {
                steps[i].ok = d.b("success")
                if !d.b("success") && !d.s("error").isEmpty { steps[i].text += " — " + String(d.s("error").prefix(160)) }
            }
            if confirm != nil { confirm = nil; if status == "waiting_confirm" { status = "running" } }
        case "agent.confirm_required":
            confirm = d; status = "waiting_confirm"
        case "agent.input_required":
            question = d; status = "waiting_input"
        case "agent.input_answered":
            question = nil; if status == "waiting_input" { status = "running" }
        case "agent.paused": status = "paused"
        case "agent.resumed": status = "running"
        case "agent.takeover": status = "takeover"
        case "agent.file": files.append(d)
        case "task.finished", "task.failed":
            status = d.s("status").isEmpty ? (type == "task.finished" ? "succeeded" : "failed") : d.s("status")
            result = d.s("result"); activity = ""; confirm = nil; question = nil
        default: break
        }
    }
    func follow(_ sid: String) {
        guard sock == nil, !sid.isEmpty, let u = API.shared.wsURL("/ws/session/\(sid)") else { return }
        steps = []; files = []; loaded = true
        let s = Socket(u)
        s.onMessage = { [weak self] m in if m.s("kind") == "event" { self?.apply(m) } }
        s.start(); sock = s
    }
    func loadHistory(_ sid: String) async {
        guard !loaded, !sid.isEmpty else { return }
        loaded = true
        guard let s = try? await API.shared.obj("GET", "/api/sessions/\(sid)") else { return }
        await MainActor.run {
            let keep = (status, result)
            steps = []
            for e in s.a("events") { apply(e) }
            status = keep.0; if !keep.1.isEmpty { result = keep.1 }
        }
    }
    deinit { sock?.stop() }
}

final class ChatModel: ObservableObject {
    @Published var threadId: String?
    @Published var title = "新对话"
    @Published var tasks: [JSON] = []
    @Published var err = ""
    var lives: [String: TaskLive] = [:]
    init(_ id: String?) { threadId = id }

    func live(_ t: JSON) -> TaskLive { lives[t.s("id")] ?? TaskLive() }

    /// Bring each task's live state in line with what the server just said (called after every load, never from a view body).
    @MainActor func sync(_ t: JSON) {
        let id = t.s("id")
        let l = lives[id] ?? TaskLive()
        lives[id] = l
        if l.sock == nil || !isLive(t.s("status")) { l.status = t.s("status") }
        if !isLive(t.s("status")) { l.sock?.stop(); l.sock = nil }
        if !t.s("result").isEmpty { l.result = t.s("result") }
        if let p = t["plan"] as? JSON, let steps = p["steps"] as? [String] { l.plan = steps }
        if let p = t["plan"] as? JSON, let done = p["done"] as? [Int] { l.planDone = Set(done) }
        if let c = t["pending_confirm"] as? JSON { l.confirm = c }
        if let q = t["pending_question"] as? JSON { l.question = q }
        if l.files.isEmpty { l.files = t.a("files").map { ["name": ($0.s("path") as NSString).lastPathComponent, "artifact": $0.s("path")] } }
        if isLive(t.s("status")) { l.follow(t.s("session_id")) }
    }

    @MainActor func load() async {
        guard let id = threadId else { return }
        do {
            let th = try await API.shared.obj("GET", "/api/threads/\(id)")
            title = th.s("title").isEmpty ? "对话" : th.s("title")
            let ts = th.a("tasks")
            for t in ts { sync(t) }
            tasks = ts
            err = ""
        } catch { err = error.localizedDescription }
    }

    @MainActor func send(_ text: String) async -> Bool {
        do {
            if threadId == nil {
                let th = try await API.shared.obj("POST", "/api/threads", ["title": String(text.prefix(30))])
                threadId = th.s("id")
            }
            _ = try await API.shared.obj("POST", "/api/tasks", ["prompt": text, "machine_id": API.shared.machine, "thread_id": threadId ?? ""])
            await load()
            return true
        } catch { err = error.localizedDescription; return false }
    }
}

struct ChatView: View {
    @EnvironmentObject var hub: Hub
    @StateObject private var m: ChatModel
    let onThread: (String) -> Void
    @State private var text = ""
    @State private var attach: [JSON] = []
    @State private var photo: PhotosPickerItem?
    @State private var showPhotos = false
    @State private var showFiles = false
    @State private var showCamera = false
    @State private var skills: [JSON] = []
    @State private var picked: [String] = []
    @State private var uploading = false
    @State private var sending = false
    @State private var preview: Preview?
    @FocusState private var focused: Bool

    init(threadId: String?, onThread: @escaping (String) -> Void = { _ in }) {
        _m = StateObject(wrappedValue: ChatModel(threadId)); self.onThread = onThread
    }
    var liveSession: String? {
        guard let t = m.tasks.last(where: { isLive($0.s("status")) }), !t.s("session_id").isEmpty else { return nil }
        return t.s("session_id")
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    if m.tasks.isEmpty {
                        VStack(spacing: 10) {
                            Image(systemName: "face.smiling").font(.system(size: 36)).foregroundColor(amber)
                            Text("让 Aiw 在电脑上替你做事").foregroundColor(.secondary)
                            Text("点 + 上传文件或选技能").font(.caption).foregroundColor(.secondary)
                        }.frame(maxWidth: .infinity).padding(.top, 80)
                    }
                    ForEach(m.tasks.indices, id: \.self) { i in
                        TaskBlock(t: m.tasks[i], live: m.live(m.tasks[i]), preview: $preview, reload: { await m.load() })
                    }
                    if !m.err.isEmpty { Text(m.err).font(.footnote).foregroundColor(.red) }
                    Color.clear.frame(height: 1).id("end")
                }.padding(.horizontal, 14).padding(.vertical, 10)
            }
            .scrollDismissesKeyboard(.interactively)
            .onChange(of: m.tasks.count) { _ in withAnimation { proxy.scrollTo("end") } }
            .safeAreaInset(edge: .bottom) { composer }
            .safeAreaInset(edge: .top, spacing: 0) { DesktopPane(liveSession: liveSession, onChange: { await m.load() }) }
        }
        .navigationTitle(m.threadId == nil ? "新对话" : m.title)
        .navigationBarTitleDisplayMode(.inline)
        .task { await m.load() }
        .refreshable { await m.load() }
        .onChange(of: hub.tick) { _ in
            let e = hub.lastEvent
            let tid = e.s("thread_id").isEmpty ? e.o("task").s("thread_id") : e.s("thread_id")
            if tid == m.threadId && (e.s("kind") == "task" || e.s("type").hasPrefix("task.")) { Task { await m.load() } }
        }
        .onChange(of: photo) { item in Task { await upload(item) } }
        .photosPicker(isPresented: $showPhotos, selection: $photo, matching: .images)
        .fileImporter(isPresented: $showFiles, allowedContentTypes: [.item], allowsMultipleSelection: true) { r in
            if case .success(let urls) = r { Task { for u in urls { await uploadFile(u) } } }
        }
        .sheet(isPresented: $showCamera) { CameraPicker { img in Task { await uploadImage(img) } }.ignoresSafeArea() }
        .task { skills = (try? await API.shared.list("/api/skills")) ?? [] }
        .onChange(of: m.threadId) { id in if let id = id { onThread(id) } }
        .sheet(item: $preview) { p in QuickLookView(url: p.url).ignoresSafeArea() }
    }

    var composer: some View {
        VStack(spacing: 6) {
            if !picked.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack {
                        ForEach(picked, id: \.self) { id in
                            HStack(spacing: 4) {
                                Image(systemName: "sparkles"); Text(skillTitle(id)).lineLimit(1)
                                Button { picked.removeAll { $0 == id } } label: { Image(systemName: "xmark.circle.fill") }
                            }.font(.caption.weight(.medium)).foregroundColor(amber)
                            .padding(.horizontal, 8).padding(.vertical, 5).background(amber.opacity(0.12)).clipShape(Capsule())
                        }
                    }
                }
            }
            if !attach.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack {
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
                    Section("添加文件") {
                        Button { showPhotos = true } label: { Label("照片", systemImage: "photo.on.rectangle") }
                        if UIImagePickerController.isSourceTypeAvailable(.camera) {
                            Button { showCamera = true } label: { Label("拍照", systemImage: "camera") }
                        }
                        Button { showFiles = true } label: { Label("文件", systemImage: "folder") }
                    }
                    Section("技能") {
                        ForEach(skills.indices, id: \.self) { i in
                            let id = skills[i].s("id")
                            Button { toggleSkill(id) } label: {
                                if picked.contains(id) { Label(skills[i].s("title"), systemImage: "checkmark") }
                                else { Text(skills[i].s("title")) }
                            }
                        }
                        if skills.isEmpty { Text("没有可用技能") }
                    }
                } label: {
                    Group { if uploading { ProgressView() } else { Image(systemName: "plus").font(.system(size: 18, weight: .semibold)) } }
                        .frame(width: 36, height: 36)
                        .background(Color(.secondarySystemBackground)).clipShape(Circle())
                }
                TextField("给 Aiw 一个任务…", text: $text, axis: .vertical)
                    .lineLimit(1...6).focused($focused)
                    .padding(.horizontal, 12).padding(.vertical, 9)
                    .background(Color(.secondarySystemBackground)).clipShape(RoundedRectangle(cornerRadius: 18))
                Button { Task { await send() } } label: {
                    Group { if sending { ProgressView().tint(.white) } else { Image(systemName: "arrow.up").font(.system(size: 17, weight: .bold)) } }
                        .foregroundColor(.white).frame(width: 36, height: 36).background(amber).clipShape(Circle())
                }.disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || sending)
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(.bar)
    }

    func send() async {
        var p = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if !picked.isEmpty { p = "【技能：\(picked.joined(separator: "、"))】\n" + p }
        if !attach.isEmpty { p += "\n\n附件（已放到电脑上）：\n" + attach.map { "- " + $0.s("vm_path") }.joined(separator: "\n") }
        sending = true
        if await m.send(p) { text = ""; attach = []; picked = []; focused = false }
        sending = false
    }

    func skillTitle(_ id: String) -> String { skills.first { $0.s("id") == id }?.s("title") ?? id }
    func toggleSkill(_ id: String) { if picked.contains(id) { picked.removeAll { $0 == id } } else { picked.append(id) } }

    func send(name: String, data: Data) async {
        uploading = true; defer { uploading = false }
        if data.count > 40 * 1024 * 1024 { m.err = "\(name) 超过 40MB"; return }
        do {
            let r = try await API.shared.obj("POST", "/api/uploads", ["name": name, "content_b64": data.base64EncodedString(), "machine_id": API.shared.machine])
            attach.append(r)
        } catch { m.err = "上传失败：" + error.localizedDescription }
    }
    func upload(_ item: PhotosPickerItem?) async {
        guard let item = item, let data = try? await item.loadTransferable(type: Data.self) else { return }
        photo = nil
        if let img = UIImage(data: data) { await uploadImage(img) } else { await send(name: "photo-\(Int(Date().timeIntervalSince1970)).jpg", data: data) }
    }
    func uploadImage(_ img: UIImage) async {
        guard let jpg = img.jpegData(compressionQuality: 0.85) else { return }
        await send(name: "photo-\(Int(Date().timeIntervalSince1970)).jpg", data: jpg)
    }
    func uploadFile(_ u: URL) async {
        let ok = u.startAccessingSecurityScopedResource()
        defer { if ok { u.stopAccessingSecurityScopedResource() } }
        guard let d = try? Data(contentsOf: u) else { m.err = "读不了 \(u.lastPathComponent)"; return }
        await send(name: u.lastPathComponent, data: d)
    }
}

struct Preview: Identifiable { let url: URL; var id: String { url.absoluteString } }

struct TaskBlock: View {
    let t: JSON
    @ObservedObject var live: TaskLive
    @Binding var preview: Preview?
    let reload: () async -> Void
    @State private var showSteps = false
    @State private var answer = ""
    @State private var downloading = ""

    var running: Bool { isLive(live.status) }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // the user's message
            HStack { Spacer(minLength: 50)
                Text(t.s("prompt")).textSelection(.enabled)
                    .padding(.horizontal, 13).padding(.vertical, 9)
                    .background(amber.opacity(0.14)).clipShape(RoundedRectangle(cornerRadius: 16))
            }
            // the agent's side
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 6) {
                    Image(systemName: "face.smiling").foregroundColor(.white).font(.system(size: 12, weight: .bold))
                        .frame(width: 22, height: 22).background(amber).clipShape(RoundedRectangle(cornerRadius: 6))
                    Text(statusText[live.status] ?? live.status).font(.caption.bold())
                        .foregroundColor(live.status == "failed" ? .red : running ? amber : .secondary)
                    if running { ProgressView().scaleEffect(0.7) }
                    Spacer()
                    if running {
                        Button(role: .destructive) { Task { _ = try? await API.shared.call("DELETE", "/api/tasks/\(t.s("id"))"); await reload() } } label: {
                            Image(systemName: "stop.circle.fill").font(.system(size: 20))
                        }
                    } else if ["failed", "stopped", "interrupted"].contains(live.status) {
                        Button("重试") { Task { _ = try? await API.shared.call("POST", "/api/tasks/\(t.s("id"))/retry"); await reload() } }.font(.caption)
                    }
                }
                if !live.plan.isEmpty && running {
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(live.plan.indices, id: \.self) { i in
                            HStack(alignment: .top, spacing: 6) {
                                Image(systemName: live.planDone.contains(i) ? "checkmark.circle.fill" : "circle")
                                    .foregroundColor(live.planDone.contains(i) ? amber : .secondary).font(.caption)
                                Text(live.plan[i]).font(.caption).foregroundColor(live.planDone.contains(i) ? .secondary : .primary)
                            }
                        }
                    }
                }
                if running && !live.activity.isEmpty { Text(live.activity).font(.caption).foregroundColor(.secondary) }

                if let c = live.confirm { confirmCard(c) }
                if let q = live.question { questionCard(q) }

                if !live.result.isEmpty && !running {
                    Text(.init(live.result)).textSelection(.enabled).font(.body)
                }
                ForEach(live.files.indices, id: \.self) { i in fileRow(live.files[i]) }

                DisclosureGroup(isExpanded: $showSteps) {
                    VStack(alignment: .leading, spacing: 6) {
                        if live.steps.isEmpty { Text("加载中…").font(.caption).foregroundColor(.secondary) }
                        ForEach(live.steps.suffix(120)) { s in
                            HStack(alignment: .top, spacing: 6) {
                                Text(toolName(s.tool)).font(.caption.bold()).foregroundColor(s.ok == false ? .red : s.tool == "message" ? .secondary : amber)
                                    .frame(width: 58, alignment: .leading)
                                Text(s.text).font(.caption).foregroundColor(s.tool == "message" ? .secondary : .primary).textSelection(.enabled)
                            }
                        }
                    }.padding(.top, 4)
                } label: {
                    Text(live.steps.isEmpty ? "步骤" : "\(live.steps.filter { $0.tool != "message" }.count) 个操作").font(.caption).foregroundColor(.secondary)
                }
                .onChange(of: showSteps) { open in if open { Task { await live.loadHistory(t.s("session_id")) } } }
            }
            .padding(12).background(Color(.secondarySystemBackground)).clipShape(RoundedRectangle(cornerRadius: 16))
        }
    }

    func confirmCard(_ c: JSON) -> some View {
        let p = c.o("payload")
        let detail = ["command", "url", "text", "host", "name", "path"].lazy.map { p.s($0) }.first { !$0.isEmpty } ?? ""
        return VStack(alignment: .leading, spacing: 8) {
            Label("需要你确认", systemImage: "exclamationmark.shield.fill").font(.subheadline.bold()).foregroundColor(.orange)
            Text("\(c.s("reason"))：\(toolName(c.s("tool")))").font(.subheadline)
            if !detail.isEmpty { Text(detail).font(.system(.caption, design: .monospaced)).lineLimit(8).padding(8)
                .frame(maxWidth: .infinity, alignment: .leading).background(Color(.systemBackground)).clipShape(RoundedRectangle(cornerRadius: 8)) }
            HStack {
                Button("拒绝") { Task { await decide(false) } }.buttonStyle(.bordered)
                Button("允许") { Task { await decide(true) } }.buttonStyle(.borderedProminent)
            }
        }.padding(10).background(Color.orange.opacity(0.1)).clipShape(RoundedRectangle(cornerRadius: 12))
    }

    func questionCard(_ q: JSON) -> some View {
        let opts = (q["options"] as? [String]) ?? []
        return VStack(alignment: .leading, spacing: 8) {
            Label("Aiw 想问你", systemImage: "questionmark.bubble.fill").font(.subheadline.bold()).foregroundColor(amber)
            Text(q.s("question")).font(.subheadline)
            if q["form"] != nil {
                Text("这一步要填表或接管桌面，请在网页里完成。").font(.caption).foregroundColor(.secondary)
                if let u = API.shared.url("/") { Link("打开网页", destination: u).font(.caption.bold()) }
            }
            if !opts.isEmpty {
                FlowButtons(options: opts) { o in Task { await reply(o) } }
            }
            HStack {
                TextField("回答…", text: $answer).textFieldStyle(.roundedBorder)
                Button("发送") { Task { await reply(answer) } }.disabled(answer.isEmpty)
            }
        }.padding(10).background(amber.opacity(0.08)).clipShape(RoundedRectangle(cornerRadius: 12))
    }

    func fileRow(_ f: JSON) -> some View {
        let path = f.s("artifact")
        let name = f.s("name").isEmpty ? (path as NSString).lastPathComponent : f.s("name")
        return Button { Task { await open(path, name) } } label: {
            HStack {
                Image(systemName: "doc.fill").foregroundColor(amber)
                Text(name).lineLimit(1).foregroundColor(.primary)
                Spacer()
                if downloading == path { ProgressView() } else { Image(systemName: "arrow.down.circle").foregroundColor(.secondary) }
            }.padding(10).background(Color(.systemBackground)).clipShape(RoundedRectangle(cornerRadius: 10))
        }
    }

    func decide(_ ok: Bool) async {
        _ = try? await API.shared.call("POST", "/api/agent/confirm", ["session_id": t.s("session_id"), "approve": ok])
        await MainActor.run { live.confirm = nil; live.status = "running" }
    }
    func reply(_ text: String) async {
        let s = text.trimmingCharacters(in: .whitespacesAndNewlines); guard !s.isEmpty else { return }
        _ = try? await API.shared.call("POST", "/api/agent/answer", ["session_id": t.s("session_id"), "text": s])
        await MainActor.run { live.question = nil; live.status = "running"; answer = "" }
    }
    func open(_ path: String, _ name: String) async {
        guard let u = API.shared.artifactURL(path) else { return }
        downloading = path
        defer { downloading = "" }
        guard let (tmp, _) = try? await URLSession.shared.download(from: u) else { return }
        let dest = FileManager.default.temporaryDirectory.appendingPathComponent(name)
        try? FileManager.default.removeItem(at: dest)
        try? FileManager.default.moveItem(at: tmp, to: dest)
        preview = Preview(url: dest)
    }
}

struct FlowButtons: View {
    let options: [String]
    let tap: (String) -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(options, id: \.self) { o in
                Button(o) { tap(o) }.buttonStyle(.bordered).font(.subheadline)
            }
        }
    }
}

/// QuickLook preview with its own share button: PDFs, images, Office files, DXF as a file to share.
struct QuickLookView: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> UINavigationController {
        let q = QLPreviewController(); q.dataSource = context.coordinator
        return UINavigationController(rootViewController: q)
    }
    func updateUIViewController(_ vc: UINavigationController, context: Context) {}
    func makeCoordinator() -> Coord { Coord(url) }
    final class Coord: NSObject, QLPreviewControllerDataSource {
        let url: URL
        init(_ u: URL) { url = u }
        func numberOfPreviewItems(in controller: QLPreviewController) -> Int { 1 }
        func previewController(_ controller: QLPreviewController, previewItemAt index: Int) -> QLPreviewItem { url as NSURL }
    }
}


struct CameraPicker: UIViewControllerRepresentable {
    let done: (UIImage) -> Void
    @Environment(\.dismiss) var dismiss
    func makeUIViewController(context: Context) -> UIImagePickerController {
        let p = UIImagePickerController(); p.sourceType = .camera; p.delegate = context.coordinator; return p
    }
    func updateUIViewController(_ vc: UIImagePickerController, context: Context) {}
    func makeCoordinator() -> Coord { Coord(self) }
    final class Coord: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let parent: CameraPicker
        init(_ p: CameraPicker) { parent = p }
        func imagePickerController(_ picker: UIImagePickerController, didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            if let img = info[.originalImage] as? UIImage { parent.done(img) }
            parent.dismiss()
        }
        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) { parent.dismiss() }
    }
}
