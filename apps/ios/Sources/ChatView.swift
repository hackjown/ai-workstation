import SwiftUI
import PhotosUI
import QuickLook
import UniformTypeIdentifiers
import AVFoundation

let toolCN: [String: String] = [
    "screenshot": "截图", "click": "点击", "double_click": "双击", "right_click": "右键", "move": "移动鼠标", "drag": "拖动",
    "type": "输入文字", "keypress": "按键", "scroll": "滚动", "open_app": "打开应用", "close_app": "关闭应用", "shell": "执行命令",
    "browser_open": "打开浏览器", "browser_navigate": "打开网址", "browser_back": "后退", "browser_forward": "前进", "files_list": "列文件",
    "files_read": "读文件", "files_write": "写文件", "files_download": "下载", "send_file": "发送文件", "done": "完成", "fail": "放弃",
    "update_plan": "更新计划", "ask_user": "提问", "remember": "记住", "delegate": "交给助手", "remote_exec": "远程执行",
    "deploy_server": "部署", "dispatch": "分派到多台电脑", "message": "说明"]
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
    @Published var thinking = ""     // reasoning streamed while the model thinks
    @Published var thought = ""      // the latest step's stated thought
    @Published var result = ""
    @Published var confirm: JSON? = nil
    @Published var question: JSON? = nil
    @Published var approvals: [JSON] = []   // past confirmations / takeovers with the user's decision (Approval.swift)
    var lastTakeover: JSON? = nil
    var sessionId = ""
    @Published var files: [JSON] = []
    @Published var plan: [String] = []
    @Published var planDone: Set<Int> = []
    @Published var children: [JSON] = []   // pieces dispatched to other computers (agent.dispatch / thread view)
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
            activity = d.s("phase") == "vision" ? "视觉模型在看屏幕…" : "正在思考…"
            if d.b("live") { thinking = d.s("text") } else { thinking = "" }
        case "agent.observation":
            activity = "正在看屏幕…"
        case "agent.decision":
            let tool = d.s("tool")
            let a = d.o("args")
            var txt = ["command", "url", "text", "key", "name", "path", "goal", "question", "host"].lazy.map { a.s($0) }.first { !$0.isEmpty } ?? ""
            if txt.isEmpty, let x = a["x"] { txt = "(\(x), \(a.s("y")))" }
            let th = d.s("thought")
            if !th.isEmpty && tool != "done" { steps.append(Step(tool: "message", text: String(th.prefix(400)))) }
            thinking = ""; if !th.isEmpty { thought = th }
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
        case "agent.confirm_answered":
            approvals.append(d)
            if confirm != nil { confirm = nil; if status == "waiting_confirm" { status = "running" } }
        case "agent.input_required":
            question = d; status = "waiting_input"
            let f = d.o("form")
            if f.s("kind") == "takeover" { lastTakeover = f }
        case "agent.input_answered":
            question = nil; if status == "waiting_input" { status = "running" }
            if d.b("takeover"), let f = lastTakeover {
                approvals.append(["kind": "takeover", "title": d.s("title").isEmpty ? f.s("title") : d.s("title"),
                                  "cancelled": d.b("cancelled"), "timeout": d["answer"] == nil || d["answer"] is NSNull])
                lastTakeover = nil
            }
            if !sessionId.isEmpty { TakeoverBus.shared.clear(sessionId) }
        case "agent.paused": status = "paused"
        case "agent.resumed": status = "running"
        case "agent.takeover": status = "takeover"
        case "agent.file": files.append(d)
        case "agent.dispatch": mergeChildren(d.a("children"))
        case "task.finished", "task.failed":
            status = d.s("status").isEmpty ? (type == "task.finished" ? "succeeded" : "failed") : d.s("status")
            result = d.s("result"); activity = ""; confirm = nil; question = nil; thinking = ""
        default: break
        }
    }
    /// Live status from the parent's stream, the thread id from the thread view: keep both.
    func mergeChildren(_ list: [JSON]) {
        guard !list.isEmpty else { return }
        var known: [String: JSON] = [:]
        for c in children { known[c.s("task_id")] = c }
        children = list.map { c in
            var m = known[c.s("task_id")] ?? [:]
            for (k, v) in c where !(v is NSNull) { m[k] = v }
            return m
        }
    }
    func follow(_ sid: String) {
        guard sock == nil, !sid.isEmpty, let u = API.shared.wsURL("/ws/session/\(sid)") else { return }
        steps = []; files = []; approvals = []; loaded = true; sessionId = sid
        let s = Socket(u)
        s.onMessage = { [weak self] m in if m.s("kind") == "event" { self?.apply(m); MateBus.shared.event(m) } }
        s.start(); sock = s
    }
    func loadHistory(_ sid: String) async {
        guard !loaded, !sid.isEmpty else { return }
        loaded = true
        guard let s = try? await API.shared.obj("GET", "/api/sessions/\(sid)") else { return }
        await MainActor.run {
            let keep = (status, result)
            steps = []; approvals = []
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
    @Published var parentThread: String?   // a child thread: the conversation whose task dispatched it
    @Published var parentTitle = ""
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
        if !t.a("children").isEmpty { l.mergeChildren(t.a("children")) }
        if l.files.isEmpty { l.files = t.a("files").map { ["name": ($0.s("path") as NSString).lastPathComponent, "artifact": $0.s("path")] } }
        if isLive(t.s("status")) { l.follow(t.s("session_id")) }
    }

    @MainActor func load() async {
        guard let id = threadId else { return }
        do {
            let th = try await API.shared.obj("GET", "/api/threads/\(id)")
            title = th.s("title").isEmpty ? "对话" : th.s("title")
            let pt = th.s("parent_thread_id")
            parentThread = pt.isEmpty || pt == "<null>" ? nil : pt
            parentTitle = th.s("parent_title")
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
    let fromParent: Bool
    @Environment(\.dismiss) private var dismiss
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
    @State private var upName = ""
    @State private var upProgress: Double = 0
    @State private var toast = ""
    @State private var importing = false
    @ObservedObject private var inbox = ImportCenter.shared
    @FocusState private var focused: Bool

    init(threadId: String?, fromParent: Bool = false, onThread: @escaping (String) -> Void = { _ in }) {
        _m = StateObject(wrappedValue: ChatModel(threadId)); self.onThread = onThread; self.fromParent = fromParent
    }
    var liveSession: String? {
        guard let t = m.tasks.last(where: { isLive($0.s("status")) }), !t.s("session_id").isEmpty else { return nil }
        return t.s("session_id")
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    if let p = m.parentThread { parentBanner(p) }
                    if m.tasks.isEmpty { HomeEmpty(threadId: m.threadId) }
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
        .navigationTitle(m.threadId == nil ? "新对话" : cleanTitle(m.title))
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { ToolbarItem(placement: .principal) {   // 云宝 beside the title (MateView.swift)
            MateTitle(title: m.threadId == nil ? "新对话" : cleanTitle(m.title),
                      status: m.tasks.last.map { t in let l = m.live(t).status; return l.isEmpty ? t.s("status") : l } ?? "",
                      threadId: m.threadId, showMate: !m.tasks.isEmpty)
        } }
        .task { await m.load() }
        .refreshable { await m.load() }
        .onChange(of: hub.tick) { _ in
            let e = hub.lastEvent
            let tid = e.s("thread_id").isEmpty ? e.o("task").s("thread_id") : e.s("thread_id")
            if tid == m.threadId && (e.s("kind") == "task" || e.s("type").hasPrefix("task.")) { Task { await m.load() } }
        }
        .onChange(of: photo) { item in Task { await upload(item) } }
        .photosPicker(isPresented: $showPhotos, selection: $photo, matching: .any(of: [.images, .videos]))
        .fileImporter(isPresented: $showFiles, allowedContentTypes: [.item], allowsMultipleSelection: true) { r in
            if case .success(let urls) = r { Task { for u in urls { await uploadFile(u) } } }
        }
        .sheet(isPresented: $showCamera) { CameraPicker(done: { img in Task { await uploadImage(img) } }, video: { u in Task { await uploadFile(u) } }).ignoresSafeArea() }
        .task { skills = (try? await API.shared.list("/api/skills")) ?? [] }
        // files sent here from WeChat「用其他应用打开」or the share sheet (Import.swift); only the new-chat home takes them
        .task { await takeImports() }
        .onChange(of: inbox.tick) { _ in Task { await takeImports() } }
        .overlay(alignment: .top) {
            if !toast.isEmpty {
                Text(toast).font(.footnote.weight(.medium)).lineLimit(2).multilineTextAlignment(.center)
                    .padding(.horizontal, 14).padding(.vertical, 9)
                    .background(.regularMaterial).clipShape(Capsule())
                    .shadow(color: .black.opacity(0.12), radius: 8, y: 2)
                    .padding(.top, 8).padding(.horizontal, 20)
                    .transition(.move(edge: .top).combined(with: .opacity))
                    .onTapGesture { toast = "" }
            }
        }
        .onChange(of: m.threadId) { id in if let id = id { onThread(id) } }
        .sheet(item: $preview) { p in
            // .md / .json / text: rendered in-app (MarkdownPreview.swift); everything else in QuickLook
            if !textPreviewKind(p.url).isEmpty, textPreviewPage() != nil { FilePreviewView(url: p.url, kind: textPreviewKind(p.url)) }
            else { QuickLookView(url: p.url).ignoresSafeArea() }
        }
    }

    /// A child thread (one dispatched piece) says where it came from and leads back to it.
    @ViewBuilder func parentBanner(_ p: String) -> some View {
        let label = HStack(spacing: 6) {
            Image(systemName: "arrow.turn.left.up").foregroundColor(amber)
            Text("来自主任务").font(.caption.bold()).foregroundColor(amber)
            Text(m.parentTitle).font(.caption).lineLimit(1).foregroundColor(.primary)
            Spacer(minLength: 0)
        }.padding(.horizontal, 10).padding(.vertical, 8).background(amber.opacity(0.1)).clipShape(RoundedRectangle(cornerRadius: 10))
        if fromParent {
            Button { dismiss() } label: { label }.buttonStyle(.plain)
        } else {
            NavigationLink { ChatView(threadId: p) } label: { label }.buttonStyle(.plain)
        }
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
            if uploading && !upName.isEmpty {
                HStack(spacing: 8) {
                    ProgressView(value: upProgress).tint(amber)
                    Text("上传 \(upName) \(Int(upProgress * 100))%").font(.caption).foregroundColor(.secondary).lineLimit(1)
                }.padding(.horizontal, 4)
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
                        Button { showPhotos = true } label: { Label("照片或视频", systemImage: "photo.on.rectangle") }
                        if UIImagePickerController.isSourceTypeAvailable(.camera) {
                            Button { showCamera = true } label: { Label("拍照或录像", systemImage: "camera") }
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
                TextField("点击 + 上传文件或选技能", text: $text, axis: .vertical)
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

    @discardableResult
    func send(name: String, data: Data) async -> Bool {
        uploading = true; upName = name; upProgress = 0
        defer { uploading = false; upName = "" }
        if data.count > 40 * 1024 * 1024 { m.err = "\(name) 超过 40MB，请先剪短或压缩"; return false }
        do {
            let r = try await API.shared.postWithProgress("/api/uploads", ["name": name, "content_b64": data.base64EncodedString(), "machine_id": API.shared.machine]) { f in
                upProgress = f
            }
            attach.append(r)
            return true
        } catch { m.err = "\(name) 上传失败：" + error.localizedDescription; return false }
    }

    /// Take what other apps handed to Aiw and add it to this (new) conversation's composer, one upload at a time.
    func takeImports() async {
        guard m.threadId == nil, inbox.hasPending, !importing else { return }
        importing = true
        let got = inbox.take()
        if !got.texts.isEmpty {
            let t = got.texts.joined(separator: "\n")
            text = text.isEmpty ? t : text + "\n" + t
        }
        var added: [String] = []
        var refused: [String] = []
        for f in got.files {
            defer { inbox.discard(f) }
            let name = f.lastPathComponent
            var src = f
            let size = (try? FileManager.default.attributesOfItem(atPath: f.path)[.size] as? Int) ?? 0
            let isVideo = UTType(filenameExtension: f.pathExtension)?.conforms(to: .movie) ?? false
            if isVideo && size > 38 * 1024 * 1024 {
                uploading = true; upName = "正在压缩 \(name)"; upProgress = 0
                let small = await shrinkVideo(f)
                uploading = false; upName = ""
                guard let s2 = small else { refused.append(name); continue }
                src = s2
            } else if size > 40 * 1024 * 1024 {
                refused.append(name); continue
            }
            defer { if src != f { try? FileManager.default.removeItem(at: src) } }
            guard let d = try? Data(contentsOf: src, options: .mappedIfSafe) else { refused.append(name); continue }
            let upload = src == f ? name : (name as NSString).deletingPathExtension + ".mp4"
            if await send(name: upload, data: d) { added.append(upload) }
        }
        var msg: [String] = []
        if !added.isEmpty { msg.append("已添加附件：" + added.joined(separator: "、")) }
        if !refused.isEmpty { msg.append(refused.joined(separator: "、") + " 超过 40MB 或读取失败，没有添加") }
        if added.isEmpty && refused.isEmpty && !got.texts.isEmpty { msg.append("已填入分享的内容") }
        if !msg.isEmpty { showToast(msg.joined(separator: "\n")) }
        importing = false
        if inbox.hasPending { await takeImports() }   // more arrived while uploading
    }
    func showToast(_ t: String) {
        withAnimation { toast = t }
        let shown = t
        DispatchQueue.main.asyncAfter(deadline: .now() + 3.5) { if toast == shown { withAnimation { toast = "" } } }
    }
    func upload(_ item: PhotosPickerItem?) async {
        guard let item = item else { return }
        photo = nil
        let stamp = Int(Date().timeIntervalSince1970)
        if let t = item.supportedContentTypes.first(where: { $0.conforms(to: .movie) || $0.conforms(to: .video) }) {
            uploading = true
            if let mv = try? await item.loadTransferable(type: PickedMovie.self) {
                defer { try? FileManager.default.removeItem(at: mv.url) }
                var src = mv.url
                let size = (try? FileManager.default.attributesOfItem(atPath: src.path)[.size] as? Int) ?? 0
                if size > 38 * 1024 * 1024, let small = await shrinkVideo(src) { src = small }
                defer { if src != mv.url { try? FileManager.default.removeItem(at: src) } }
                guard let d = try? Data(contentsOf: src, options: .mappedIfSafe) else { uploading = false; m.err = "读不了这个视频"; return }
                let ext = src != mv.url ? "mp4" : mv.url.pathExtension.isEmpty ? (t.preferredFilenameExtension ?? "mov") : mv.url.pathExtension
                await send(name: "video-\(stamp).\(ext.lowercased())", data: d)
            } else { uploading = false; m.err = "读不了这个视频（可能还在 iCloud 里没下载）" }
            return
        }
        guard let data = try? await item.loadTransferable(type: Data.self) else { m.err = "读不了这张图片"; return }
        if let img = UIImage(data: data) { await uploadImage(img) } else { await send(name: "file-\(stamp)", data: data) }
    }
    /// Re-encode a video that is over the 40 MB upload limit: 720p, then 540p if still too big.
    func shrinkVideo(_ u: URL) async -> URL? {
        for preset in [AVAssetExportPreset1280x720, AVAssetExportPreset960x540, AVAssetExportPresetMediumQuality] {
            let out = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".mp4")
            guard let ex = AVAssetExportSession(asset: AVURLAsset(url: u), presetName: preset) else { continue }
            ex.outputURL = out; ex.outputFileType = .mp4; ex.shouldOptimizeForNetworkUse = true
            await ex.export()
            guard ex.status == .completed else { continue }
            let n = (try? FileManager.default.attributesOfItem(atPath: out.path)[.size] as? Int) ?? 0
            if n <= 39 * 1024 * 1024 { return out }
            try? FileManager.default.removeItem(at: out)
        }
        return nil
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

/// iOS v2 empty home: the real 球球 (amber disc) centred, one line of slogan under it.
struct HomeEmpty: View {
    let threadId: String?
    @AppStorage("mate") private var mateOn = true
    var body: some View {
        VStack(spacing: 18) {
            if mateOn {
                MateView(status: "", threadId: threadId).frame(width: 112, height: 112).accessibilityLabel("球球")
            } else {
                Circle().fill(amber).frame(width: 112, height: 112)
                    .overlay(Image(systemName: "sparkles").font(.system(size: 40, weight: .medium)).foregroundColor(.white))
                    .accessibilityHidden(true)
            }
            Text("让 Aiw 在云电脑上替你干活")
                .font(.system(size: 15, weight: .regular))
                .foregroundColor(Color(.secondaryLabel))
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, max(60, UIScreen.main.bounds.height * 0.25))
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
    /// A file sent twice (a retry, history replayed over live events) shows once, newest last.
    var uniqueFiles: [JSON] {
        var seen = Set<String>(), out: [JSON] = []
        for f in live.files.reversed() {
            let k = f.s("artifact").isEmpty ? f.s("name") : f.s("artifact")
            if seen.insert(k).inserted { out.insert(f, at: 0) }
        }
        return out
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // the user's message
            HStack { Spacer(minLength: 56); UserBubble(prompt: t.s("prompt")) }
            // the agent's side
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 6) {
                    if running {   // the live 球球 moves while this task works (MateView.swift)
                        MateView(status: "", threadId: nil, bus: false, emotion: live.question != nil || live.confirm != nil ? "35" : "32", animate: true)
                            .frame(width: 26, height: 26).accessibilityHidden(true)
                    } else {
                        Image(systemName: live.status == "failed" ? "exclamationmark" : "face.smiling").foregroundColor(.white).font(.system(size: 12, weight: .bold))
                            .frame(width: 22, height: 22).background(live.status == "failed" ? Color.red.opacity(0.8) : amber).clipShape(RoundedRectangle(cornerRadius: 6))
                    }
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
                if !live.children.isEmpty { childList }
                if running && !live.activity.isEmpty {
                    HStack(spacing: 6) { ProgressView().controlSize(.mini); Text(live.activity).font(.caption).foregroundColor(.secondary) }
                }
                if running && (!live.thinking.isEmpty || !live.thought.isEmpty) {
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 4) {
                            Image(systemName: "lightbulb"); Text("思考过程")
                            if !live.thinking.isEmpty { Text("\(live.thinking.count) 字").foregroundColor(.secondary) }
                        }.font(.caption.weight(.semibold)).foregroundColor(amber)
                        ScrollViewReader { sp in
                            ScrollView {
                                Text(live.thinking.isEmpty ? live.thought : live.thinking)
                                    .font(.caption).foregroundColor(.secondary).frame(maxWidth: .infinity, alignment: .leading).id("end")
                            }.frame(maxHeight: 110)
                            .onChange(of: live.thinking) { _ in sp.scrollTo("end", anchor: .bottom) }
                        }
                    }.padding(8).background(amber.opacity(0.07)).clipShape(RoundedRectangle(cornerRadius: 10))
                }
                if running, !live.steps.isEmpty && !showSteps {
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(live.steps.filter { $0.tool != "message" }.suffix(3)) { s in
                            HStack(alignment: .top, spacing: 6) {
                                Image(systemName: s.ok == false ? "xmark.circle" : s.ok == true ? "checkmark.circle" : "circle.dotted")
                                    .foregroundColor(s.ok == false ? .red : amber).font(.caption2)
                                Text("\(toolName(s.tool)) \(s.text)").font(.caption2).foregroundColor(.secondary).lineLimit(1)
                            }
                        }
                    }
                }

                if let c = live.confirm, running {
                    ApprovalCard(c: c) { ok, all in await decide(ok, scope: all ? "task" : nil) }
                }
                if let q = live.question {
                    if q.o("form").s("kind") == "takeover" { TakeoverCard(form: q.o("form"), session: t.s("session_id")) }
                    else { questionCard(q) }
                }
                if !live.approvals.isEmpty && (running || showSteps) { ApprovalLog(items: live.approvals) }

                if !live.result.isEmpty && !running {
                    RichText(text: live.result)
                }
                ForEach(uniqueFiles.indices, id: \.self) { i in fileRow(uniqueFiles[i]) }

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
            .padding(.horizontal, 14).padding(.vertical, 12).background(Color(.secondarySystemBackground)).clipShape(RoundedRectangle(cornerRadius: 18))
        }
    }

    /// The pieces this task dispatched: machine, live status, tap to open that piece's own conversation.
    var childList: some View {
        let finals = ["succeeded", "failed", "stopped", "cancelled", "interrupted"]
        let done = live.children.filter { finals.contains($0.s("status")) }.count
        let machines = Set(live.children.map { $0.s("machine") }).count
        return VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 5) {
                Image(systemName: "desktopcomputer"); Text("分派到 \(machines) 台电脑 · \(done)/\(live.children.count) 完成")
            }.font(.caption2.weight(.medium)).foregroundColor(.secondary).padding(.horizontal, 10).padding(.vertical, 7)
            ForEach(live.children.indices, id: \.self) { i in
                let c = live.children[i]
                Divider()
                NavigationLink { ChatView(threadId: c.s("thread_id"), fromParent: true) } label: {
                    HStack(spacing: 8) {
                        Text(c.s("machine_name").isEmpty ? c.s("machine") : c.s("machine_name"))
                            .font(.caption.weight(.semibold)).foregroundColor(amber).lineLimit(1)
                        Text(c.s("goal")).font(.caption).foregroundColor(.primary).lineLimit(1)
                        Spacer(minLength: 4)
                        if c.s("status") == "running" { ProgressView().controlSize(.mini) }
                        Text(statusText[c.s("status")] ?? (c.s("status") == "cancelled" ? "已取消" : c.s("status")))
                            .font(.caption2.bold()).foregroundColor(c.s("status") == "failed" ? .red : isLive(c.s("status")) ? amber : .secondary)
                        Image(systemName: "chevron.right").font(.caption2).foregroundColor(.secondary)
                    }.padding(.horizontal, 10).padding(.vertical, 8).contentShape(Rectangle())
                }.buttonStyle(.plain).disabled(c.s("thread_id").isEmpty)
            }
        }.background(Color(.systemBackground)).clipShape(RoundedRectangle(cornerRadius: 10))
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

    func decide(_ ok: Bool, scope: String? = nil) async {
        var body: JSON = ["session_id": t.s("session_id"), "approve": ok]
        if let s = scope { body["scope"] = s }
        _ = try? await API.shared.call("POST", "/api/agent/confirm", body)
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
    var video: (URL) -> Void = { _ in }
    @Environment(\.dismiss) var dismiss
    func makeUIViewController(context: Context) -> UIImagePickerController {
        let p = UIImagePickerController(); p.sourceType = .camera; p.delegate = context.coordinator
        p.mediaTypes = ["public.image", "public.movie"]; p.videoQuality = .typeMedium; return p
    }
    func updateUIViewController(_ vc: UIImagePickerController, context: Context) {}
    func makeCoordinator() -> Coord { Coord(self) }
    final class Coord: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let parent: CameraPicker
        init(_ p: CameraPicker) { parent = p }
        func imagePickerController(_ picker: UIImagePickerController, didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            if let u = info[.mediaURL] as? URL { parent.video(u) }
            else if let img = info[.originalImage] as? UIImage { parent.done(img) }
            parent.dismiss()
        }
        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) { parent.dismiss() }
    }
}


/// A video from the photo library, copied to a temp file (videos are too big to load as Data first).
struct PickedMovie: Transferable {
    let url: URL
    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(contentType: .movie) { m in SentTransferredFile(m.url) } importing: { f in
            let dst = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + "." + (f.file.pathExtension.isEmpty ? "mov" : f.file.pathExtension))
            try FileManager.default.copyItem(at: f.file, to: dst)
            return PickedMovie(url: dst)
        }
    }
}
