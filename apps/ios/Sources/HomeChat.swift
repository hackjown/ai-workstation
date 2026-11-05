import SwiftUI
import UIKit
import PhotosUI
import UniformTypeIdentifiers

/// iOS 3.4 home: ONE persistent chat with 球球 (server: /api/chat*, chat.py), the same as the web main screen.
/// * history loads on open, scrolled to the bottom; older pages load when the top comes into view
/// * replies stream over SSE (POST /api/chat/send); no thinking and no agent steps are ever shown here
/// * a task is a compact card (title, status, step) that follows the task live (/ws/events kind "chat");
///   tapping it pushes the existing task view (ChatView: the computer on top, the steps below); back keeps the scroll
/// * questions / confirmations come in as messages with buttons; 我来接管桌面 opens the task view in takeover
/// * a reply that needs the computer has 交给电脑去做 (POST /api/chat/messages/{id}/to_task)
/// * 搜索 (top right, v3.9.14) searches the chat, tasks and files; 新建任务 (in the "+" menu) sends the next message
///   straight to the computer (mode "task")
/// * 3.5 (v2 mockup, HomeV2.swift): with nothing new, 球球 and its briefing (GET /api/greeting) instead of the chat;
///   amber / dark bubbles with a small 球球, thinking 球球 + 停止, "/" skill picker, "+" sheet with camera / video / library
///   (videos too) / files / 新建任务 / 链接 / 技能, uploads through POST /api/uploads/raw
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
    @Published var handoffThread: String? = nil  // 3.9.10: a message just handed to the computer: open its task page
    // v2 (3.5): with nothing new the home shows 球球 and its briefing (GET /api/greeting) instead of the chat
    @Published var staged = false
    @Published var greet: [JSON] = []
    @Published var chips: [JSON] = []
    @Published var greetAt: Date? = nil
    var stageOff = false

    @MainActor func index(_ id: String) -> Int? { msgs.firstIndex { $0.s("id") == id } }
    /// v3.9.14: the message a search hit asked for (HomeChatView.main scrolls to it, then clears it)
    @Published var jumpTo: String? = nil
    @Published var flash: String? = nil
    @MainActor func loadUntil(_ id: String) async -> Bool {
        if !loaded { await load() }
        for _ in 0..<40 {
            if index(id) != nil { return true }
            guard hasMore else { return false }
            if await older() == nil { return index(id) != nil }
        }
        return index(id) != nil
    }

    @MainActor func load() async {
        do {
            let r = try await API.shared.obj("GET", "/api/chat?limit=60")
            msgs = r.a("messages"); hasMore = r.b("has_more"); loaded = true; err = ""
            if !r.s("agent_name").isEmpty { agent = r.s("agent_name") }
            let readSeq = r.o("read").d("seq")
            if !stageOff, let last = msgs.last(where: { $0.d("seq") > 0 }) {   // something new since last time: show the chat
                stageOff = last.d("seq") > readSeq && last.s("role") != "user"
            }
            staged = !stageOff
            bottomTick += 1
            markRead()
            if staged { await loadGreeting() }
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
    @MainActor func loadGreeting(force: Bool = false) async {
        if !force, let at = greetAt, Date().timeIntervalSince(at) < 60, !greet.isEmpty { return }
        do {
            let g = try await API.shared.obj("GET", "/api/greeting")
            greet = g.a("items"); chips = g.a("chips")
            if greet.isEmpty { greet = [["text": "有什么要我做的，直接说就行。", "mood": "02", "action": ["type": "focus"]]] }
        } catch {   // never silent: say it, and still greet
            greet = [["text": "播报没取到（\(error.localizedDescription)）。有什么要我做的，直接说就行。", "mood": "34", "action": ["type": "focus"]]]
            chips = [["label": "💬 随便聊聊", "action": ["type": "focus"]]]
        }
        greetAt = Date()
    }
    /// 3.8.2: set when "↑ 聊天记录" (or anything) leaves the stage, so the chat opens at the newest message
    var openAtEnd = false
    @MainActor func leaveStage() { stageOff = true; if staged { openAtEnd = true; staged = false; bottomTick += 1 } }
    /// 停止 while 球球 is answering (POST /api/chat/cancel keeps what streamed so far).
    @MainActor func cancel() async {
        do {
            let r = try await API.shared.obj("POST", "/api/chat/cancel")
            if !r.b("cancelled") { err = "这条已经交给电脑了：要停的话，点任务卡片进去停止" }
        } catch { err = "停止失败：\(error.localizedDescription)" }
    }
    static func order(_ m: JSON) -> Double { let s = m.d("seq"); return s > 0 ? s : 1e15 }   // unsent ones last
    @MainActor func sortMsgs() { msgs.sort { HomeChat.order($0) < HomeChat.order($1) } }

    @MainActor func upsert(_ m: JSON, stick: Bool = false) {
        guard !m.s("id").isEmpty else { return }
        if let i = index(m.s("id")) {
            let moved = msgs[i].d("seq") != m.d("seq")
            msgs[i] = m
            if moved { sortMsgs(); bottomTick += 1 }   // 3.8.8: a continued task's card moves to the bottom
            return
        }
        if m.s("role") == "user" { msgs.removeAll { $0.b("tmp") && $0.s("text") == m.s("text") } }
        if m.s("role") != "user" && m.s("kind") == "text" { stream = nil }
        if staged { leaveStage() }   // something new: the chat takes over from 球球's briefing
        msgs.append(m); sortMsgs()
        bottomTick += 1
        markRead()
    }
    @MainActor func onEvent(_ e: JSON) {
        if e.s("kind") == "chat" && e.s("op") == "delete" { remove(e.o("message").s("id")); return }
        if e.s("kind") == "chat" { upsert(e.o("message")) }
    }
    @MainActor func remove(_ id: String) { if !id.isEmpty { msgs.removeAll { $0.s("id") == id } } }
    /// 3.8.2: delete one message (DELETE /api/chat/messages/{id}); a queued task's card also leaves the queue.
    @MainActor func deleteMessage(_ id: String) async {
        do {
            _ = try await API.shared.call("DELETE", "/api/chat/messages/\(id)")
            remove(id)
        } catch { err = "没能删除：\(error.localizedDescription)" }
    }
    @MainActor func markRead() {
        guard let last = msgs.last(where: { $0.d("seq") > 0 }) else { return }
        let seq = Int(last.d("seq"))
        Task { _ = try? await API.shared.call("POST", "/api/chat/read", ["seq": seq]) }
    }

    // 3.8 outbox: a message sent while 球球 is still answering waits here (shown as 排队中 under the chat) and goes
    // out by itself when the reply ends. Before 3.8 the send button was 停止 during a reply and send() returned at
    // once when a reply was running, so a message typed meanwhile could never be sent.
    @Published var outbox: [JSON] = []

    @MainActor func send(_ text: String, attachments: [JSON], mode: String) async -> Bool {
        if sending {
            outbox.append(["id": "q-\(UUID().uuidString.prefix(8))", "text": text, "attachments": attachments, "mode": mode])
            leaveStage(); bottomTick += 1
            return true
        }
        let ok = await sendNow(text, attachments: attachments, mode: mode)
        await flushOutbox()
        return ok
    }
    /// The queued messages, oldest first, one after the other (the server answers one message at a time).
    @MainActor func flushOutbox() async {
        while !sending, !outbox.isEmpty {
            let q = outbox.removeFirst()
            if !(await sendNow(q.s("text"), attachments: q.a("attachments"), mode: q.s("mode"))) {
                outbox.insert(q, at: 0); break   // the error is shown; it stays queued (立即发送 retries it)
            }
        }
    }
    /// 立即发送 on a queued message: it goes first; a reply still being written is stopped (what streamed is kept).
    @MainActor func sendQueuedNow(_ id: String) async {
        guard let i = outbox.firstIndex(where: { $0.s("id") == id }) else { return }
        let q = outbox.remove(at: i); outbox.insert(q, at: 0)
        if sending { await cancel() } else { await flushOutbox() }
    }
    @MainActor func dropQueued(_ id: String) { outbox.removeAll { $0.s("id") == id } }

    /// POST /api/chat/send as an event stream: user → route → delta… → message | task → done.
    @MainActor func sendNow(_ text: String, attachments: [JSON], mode: String) async -> Bool {
        guard !sending else { return false }
        sending = true; err = ""
        leaveStage()
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
        // v3.9.20: leaving the app right after 发送 used to cut the stream and show 发送失败 although the server
        // already had the message (it finishes the reply on its own). Ask iOS for background time, and on a
        // dropped stream check the server before calling it a failure.
        var bg = UIBackgroundTaskIdentifier.invalid
        bg = UIApplication.shared.beginBackgroundTask(withName: "aiw-chat-send") {
            UIApplication.shared.endBackgroundTask(bg); bg = .invalid
        }
        defer { if bg != .invalid { UIApplication.shared.endBackgroundTask(bg) } }
        var accepted = false
        let sentAt = Date()
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
                case "user": accepted = true; upsert(obj, stick: true)
                case "route": if obj.s("route") == "task" { streamNote = obj.b("skill") ? "正在把技能交给电脑…" : "正在交给电脑…" }
                case "status": if acc.isEmpty { streamNote = obj.s("text") }
                case "delta": acc += obj.s("text"); stream = acc; bottomTick += 1
                case "message", "task":
                    stream = nil; upsert(obj, stick: true)
                    if obj.s("kind") == "task" { noteHandoff(obj) }
                case "done": break
                default: break
                }
                event = "message"
            }
            return true
        } catch {
            if accepted || (error as? APIError) == nil, await recoverAfterDrop(text, since: sentAt, known: accepted) {
                return true
            }
            msgs.removeAll { $0.b("tmp") }
            err = "发送失败：\(error.localizedDescription)"
            return false
        }
    }
    /// v3.9.20: the stream broke (app sent to the background, network switch). If the server has the message,
    /// pull history instead of 发送失败; the reply is stored server-side and shows up by sync / the socket.
    @MainActor func recoverAfterDrop(_ text: String, since: Date, known: Bool) async -> Bool {
        let want = text.isEmpty ? "（附件）" : text
        for attempt in 0..<4 {
            if attempt > 0 { try? await Task.sleep(nanoseconds: 1_500_000_000) }
            guard let r = try? await API.shared.obj("GET", "/api/chat?limit=20") else { continue }
            let rows = r.a("messages")
            let mine = rows.last { $0.s("role") == "user" && $0.s("text") == want
                && (Date(timeIntervalSince1970: $0.d("created_at")) >= since.addingTimeInterval(-600) || $0.d("created_at") == 0) }
            if known || mine != nil {
                msgs.removeAll { $0.b("tmp") }
                for m in rows { if let i = index(m.s("id")) { msgs[i] = m } else { msgs.append(m) } }
                sortMsgs(); bottomTick += 1; err = ""
                return true
            }
        }
        return false
    }

    @MainActor func answer(_ id: String, _ body: JSON) async -> JSON? {
        do {
            let r = try await API.shared.obj("POST", "/api/chat/messages/\(id)/answer", body)
            upsert(r.o("message"))
            return r
        } catch { err = error.localizedDescription; await sync(); return nil }
    }
    /// 3.9.10: the task card a hand-off just made -> its thread opens (HomeChatView watches handoffThread)
    @MainActor func noteHandoff(_ card: JSON) {
        let t = card.o("data").s("thread_id")
        if !t.isEmpty && t != "<null>" { handoffThread = t }
    }
    @MainActor func toTask(_ id: String) async {
        do {
            let card = try await API.shared.obj("POST", "/api/chat/messages/\(id)/to_task", ["machine_id": API.shared.machine])
            upsert(card, stick: true)
            if card.s("kind") == "task" { noteHandoff(card) }
            if let i = index(id) {
                var m = msgs[i]; var d = m.o("data"); d["task_started"] = card.s("id"); m["data"] = d; msgs[i] = m
            }
        } catch { err = "没能交给电脑：\(error.localizedDescription)" }
    }
    /// 3.8.8: 继续 on a finished / stopped card: the SAME task (POST /api/tasks/{id}/continue); its card re-arms.
    @MainActor func continueTask(_ taskId: String) async {
        do { _ = try await API.shared.obj("POST", "/api/tasks/\(taskId)/continue", ["text": "继续"]); err = "" }
        catch { err = "没能继续：\(error.localizedDescription)" }
    }
    /// 3.8.8: 修改后重新发布 (POST /edit on the task's first run) or 补充 (POST /instruct) from a card.
    @MainActor func cardText(_ a: CardTextAsk, _ text: String) async -> String? {
        do {
            if a.ask.kind == .edit { _ = try await API.shared.obj("POST", "/api/tasks/\(a.rootId)/edit", ["prompt": text]) }
            else { _ = try await API.shared.obj("POST", "/api/tasks/\(a.taskId)/instruct", ["text": text]) }
            return nil
        } catch { return error.localizedDescription }
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
    @State private var photos: [PhotosPickerItem] = []
    @State private var camera: CamMode? = nil
    @State private var showAttach = false
    @State private var pending: AttachKind? = nil
    @State private var showLink = false
    @State private var linkText = ""
    @State private var skills: [JSON] = []
    @State private var picked: [String] = []
    @State private var pct: Double = 0
    @State private var showPhotos = false
    @State private var showFiles = false
    @State private var preview: Preview?
    @StateObject private var pin = BottomPin()
    @Environment(\.colorScheme) private var cs
    @State private var focused = false   // v3.9.17: IMEField (UITextView) takes a plain Bool binding
    @Environment(\.scenePhase) private var phase
    @AppStorage("hdraft") private var draft = ""
    @StateObject private var voice = SpeechInput()
    @State private var queuedPick: JSON? = nil
    @State private var deletePick: JSON? = nil
    @State private var cardAsk: CardTextAsk? = nil     // 3.8.8: 修改后重新发布 / 补充 on a task card
    /// 3.8.2: older pages load only after the opening jump to the newest message (the "加载更早的消息" row is on screen
    /// while the list still sits at the top, and its onAppear used to load a page and pin the view to it)
    @State private var olderOK = false
    /// v3.9.2: files / text shared from other apps (Import.swift) become pending attachments of this composer
    @ObservedObject private var inbox = ImportCenter.shared
    @State private var importing = false

    var liveStatus: String {
        m.msgs.last(where: { $0.s("kind") == "task" && isLive($0.o("data").s("status")) })?.o("data").s("status") ?? ""
    }

    // 3.8.2: split so the type checker copes (one body with the dialogs timed out in CI)
    var body: some View { withDialogs(main) }

    var queuedNote: String { m.sending ? "球球还在回复上一条，回复完会自动发出。" : "马上就发。" }
    func deleteTitle(_ d: JSON) -> String {
        d.s("kind") == "task" && d.o("data").s("status") == "queued" ? "删除（这个排队的任务不再执行）" : "删除"
    }
    func deleteNote(_ d: JSON) -> String {
        let st = d.o("data").s("status")
        if d.s("kind") == "task" && isLive(st) && st != "queued" { return "这个任务还在进行，要先进去停止它才能删除。" }
        return "只删除这一条，其他聊天记录不受影响。"
    }
    /// opening the chat (or leaving 球球's stage) starts at the newest message; back from a task keeps the place you were
    /// reading unless you were at the end. 3.8.2: "↑ 聊天记录" always lands on the newest message.
    func openedChat() {
        if !pin.opened || pin.pinned || m.openAtEnd {
            m.openAtEnd = false
            olderOK = false
            pin.toEnd(follow: 2.5)
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { olderOK = true }
        } else { olderOK = true }
    }
    func withDialogs<C: View>(_ c: C) -> some View {
        c
        // 3.8.2: one dialog for the queued message you tapped (a dialog per row inside the lazy list did not always
        // run its 删除), and one for deleting a chat message
        .confirmationDialog("这条还在排队", isPresented: Binding(get: { queuedPick != nil }, set: { if !$0 { queuedPick = nil } }),
                            titleVisibility: .visible, presenting: queuedPick) { q in
            Button(m.sending ? "立即发送（停止当前回复）" : "立即发送") { let id = q.s("id"); queuedPick = nil; Task { await m.sendQueuedNow(id) } }
            Button("删除", role: .destructive) { let id = q.s("id"); queuedPick = nil; m.dropQueued(id) }
            Button("取消", role: .cancel) { queuedPick = nil }
        } message: { _ in Text(queuedNote) }
        .confirmationDialog("删除这条消息？", isPresented: Binding(get: { deletePick != nil }, set: { if !$0 { deletePick = nil } }),
                            titleVisibility: .visible, presenting: deletePick) { d in
            Button(deleteTitle(d), role: .destructive) { let id = d.s("id"); deletePick = nil; Task { await m.deleteMessage(id) } }
            Button("取消", role: .cancel) { deletePick = nil }
        } message: { d in Text(deleteNote(d)) }
    }

    var main: some View {
        ScrollViewReader { proxy in
          Group {
            if m.staged {
                HomeStage(m: m, typing: typing, act: runAction, showChat: { m.leaveStage() })
            } else {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    if m.hasMore {
                        HStack { Spacer(); if m.loadingOlder { ProgressView() } else { Text("加载更早的消息").font(.caption).foregroundColor(.secondary) }; Spacer() }
                            .padding(.vertical, 6).id("older")
                            .onAppear { Task {
                                guard olderOK else { return }
                                if let keep = await m.older() { proxy.scrollTo(keep, anchor: .top) }
                            } }
                    } else if !m.msgs.isEmpty {
                        Text("· 聊天从这里开始 ·").font(.caption2).foregroundColor(.secondary).frame(maxWidth: .infinity)
                    }
                    if m.loaded && m.msgs.isEmpty { HomeChatEmpty(agent: m.agent) { text = $0; focused = true } }
                    messageRows
                    if !m.err.isEmpty { Text(m.err).font(.footnote).foregroundColor(.red) }
                    if !voice.error.isEmpty { Text(voice.error).font(.footnote).foregroundColor(.orange) }
                    Color.clear.frame(height: 1).id("end")
                }.padding(.horizontal, 14).padding(.vertical, 10)
                .reportsScrollMetrics()
            }
            .scrollDismissesKeyboard(.interactively)
            .onPreferenceChange(ScrollMetricsKey.self) { mm in pin.metrics(mm, live: m.sending) }
            // 3.6: new messages / streaming keep the chat at the newest message unless you scrolled up (then ↓ 最新消息)
            .onChange(of: m.bottomTick) { _ in pin.newContent() }
            .onChange(of: m.loaded) { ok in if ok { pin.toEnd(follow: 2.5) } }
            .onChange(of: m.jumpTo) { id in
                guard let id = id else { return }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
                    withAnimation(.easeOut(duration: 0.25)) { proxy.scrollTo(id, anchor: .center) }
                    m.flash = id; m.jumpTo = nil
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { if m.flash == id { m.flash = nil } }
                }
            }
            .onAppear {
                pin.jumpAction = { animated in
                    if animated { withAnimation(.easeOut(duration: 0.25)) { proxy.scrollTo("end", anchor: .bottom) } }
                    else { proxy.scrollTo("end", anchor: .bottom) }
                }
                openedChat()
            }
            }
          }
          // 3.7: the pill floats over the bottom of the chat, inside the safe area that the composer (its real height)
          // and the keyboard leave, so it always sits clearly above the input bar. 3.6 hung it on the composer's top
          // edge with an alignment guide, which iOS 17 did not honour inside an overlay: it covered the input.
          .overlay(alignment: .bottomTrailing) {
              if !m.staged && pin.showJump {
                  JumpToLatest { pin.toEnd(animated: true) }.padding(.trailing, 16).padding(.bottom, 12)
              }
          }
          .animation(.easeOut(duration: 0.18), value: pin.showJump)
          .safeAreaInset(edge: .bottom) {
            composer
                .reportsComposerTop()
          }
          .onPreferenceChange(ComposerTopKey.self) { pin.composer($0) }
        }
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .navigationBarLeading) {
                Button(action: openSettings) { Image(systemName: "gearshape").font(.system(size: 17)) }.accessibilityLabel("设置")
            }
            ToolbarItem(placement: .principal) { MateTitle(title: m.agent, status: liveStatus, threadId: nil, showMate: !m.staged) }
            ToolbarItemGroup(placement: .navigationBarTrailing) {
                // v3.9.14: 搜索 replaces the pencil (新建任务 is still the first item of the "+" menu)
                Button { openSheet(.search) } label: { Image(systemName: "magnifyingglass").font(.system(size: 17)) }
                    .accessibilityLabel("搜索")
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
        .task { if !m.loaded { await m.load() }; if text.isEmpty { text = draft }; await takeImports() }
        .onChange(of: inbox.tick) { _ in Task { await takeImports() } }
        .onDisappear { voice.stop() }
        .task { skills = (try? await API.shared.list("/api/skills")) ?? [] }
        .refreshable { await m.load() }
        .onReceive(hub.events) { e in m.onEvent(e) }
        // 3.9.10: 交给电脑 succeeded (the button, or a send routed to a task): straight to that task's page
        .onChange(of: m.handoffThread) { t in
            guard let t = t, !t.isEmpty else { return }
            m.handoffThread = nil
            path.append(.task(t, takeover: false))
        }
        // 3.8 aiw://chat (a push): the chat itself, at the newest message
        .onReceive(NotificationCenter.default.publisher(for: .aiwShowChat)) { n in
            let target = (n.userInfo?["m"] as? String) ?? ""
            Task {
                await m.sync(); m.leaveStage()
                if target.isEmpty { pin.toEnd(follow: 2); return }
                // v3.9.14 a search hit: older pages until the message is loaded, then scroll to it (main's onChange)
                if await m.loadUntil(target) { m.jumpTo = target } else { pin.toEnd(follow: 2) }
            }
        }
        .onChange(of: phase) { p in   // back in the foreground: what came in meanwhile, then the newest message
            if p != .active { voice.stop() }
            if p == .active { Task { await m.sync(); if !m.staged { pin.toEnd(follow: 2) } } }
        }
        .onChange(of: text) { t in draft = t; onText(t) }
        .onChange(of: m.staged) { on in if on { Task { await m.loadGreeting() } } }
        // 3.9.0: a reply / new task finished streaming: the rows above were swapped (tmp message, 正在想…, task
        // card), so land on the real end again
        .onChange(of: m.sending) { on in if !on && !m.staged { pin.toEnd(follow: 2) } }
        .onChange(of: photos) { items in
            guard !items.isEmpty else { return }
            let list = items; photos = []
            Task { for it in list { await addPicked(it) } }
        }
        .photosPicker(isPresented: $showPhotos, selection: $photos, maxSelectionCount: 9, matching: .any(of: [.images, .videos]))
        .sheet(isPresented: $showAttach, onDismiss: runPending) {
            AttachSheet { k in pending = k; showAttach = false }
                .presentationDetents([.height(AttachSheet.height)]).presentationDragIndicator(.hidden)
        }
        .fullScreenCover(item: $camera) { md in
            HomeCamera(mode: md) { u, img in camera = nil; Task { await gotCamera(u, img) } }.ignoresSafeArea()
        }
        .alert("添加链接", isPresented: $showLink) {
            TextField("https://…", text: $linkText).keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
            Button("添加") { addLink() }
            Button("取消", role: .cancel) {}
        } message: { Text("网页、视频或商品的链接") }
        .fileImporter(isPresented: $showFiles, allowedContentTypes: [.item], allowsMultipleSelection: true) { r in
            if case .success(let urls) = r { Task { for u in urls { await addFile(u) } } }
        }
        .sheet(item: $cardAsk) { a in
            TaskTextSheet(ask: a.ask) { text in await m.cardText(a, text) }
        }
        .sheet(item: $preview) { p in
            if isHtmlFile(p.url) { HtmlPreviewView(url: p.url, serverPath: p.path) }
            else if !textPreviewKind(p.url).isEmpty, textPreviewPage() != nil { FilePreviewView(url: p.url, kind: textPreviewKind(p.url)) }
            else { QuickLookView(url: p.url).ignoresSafeArea() }
        }
    }

    // MARK: rows
    /// 3.8.2: rows keyed by message id (index keys mixed rows up after a delete)
    @ViewBuilder var messageRows: some View {
        ForEach(m.msgs.indices.map { HRowKey(i: $0, id: m.msgs[$0].s("id")) }, id: \.id) { k in
            msgRow(m.msgs[k.i], id: k.id)
        }
        if let s = m.stream { streamRow(s) }
        ForEach(m.outbox.indices.map { HRowKey(i: $0, id: m.outbox[$0].s("id")) }, id: \.id) { k in
            outRow(m.outbox[k.i], id: k.id)
        }
    }
    func msgRow(_ msg: JSON, id: String) -> some View {
        row(msg).id(id).environment(\.aiwDeleteMessage, delAction(msg))
            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).stroke(amber, lineWidth: m.flash == id ? 2 : 0)
                        .animation(.easeOut(duration: 0.3), value: m.flash))
    }
    func outRow(_ q: JSON, id: String) -> some View {
        let drop: () -> Void = { Task { @MainActor in m.dropQueued(id) } }
        return queuedRow(q).id(id).environment(\.aiwDeleteMessage, drop)
    }
    @ViewBuilder func row(_ msg: JSON) -> some View {
        let d = msg.o("data")
        if msg.s("role") == "user" {
            HStack { Spacer(minLength: 48)
                VStack(alignment: .trailing, spacing: 3) {
                    UserBubbleV2(text: msg.s("text"), attachments: d.a("attachments"), skillTitle: skillTitle)
                    Text(msg.b("tmp") ? "发送中…" : when(msg.d("created_at"))).font(.caption2).foregroundColor(.secondary)
                }
            }
        } else {
            switch msg.s("kind") {
            case "task": withAvatar(meta: false) {
                TaskCardRow(msg: msg, model: m, open: { take in open(msg, takeover: take) }, retry: { Task { await m.retry(d.s("task_id")) } },
                            textAsk: { a in cardAsk = a }, openFile: { p, n in Task { await openFile(p, n) } })
            }
                .contextMenu { deleteItem(msg) }
            case "ask": withAvatar(meta: false) { AskRow(msg: msg, model: m, open: { take in open(msg, takeover: take) }) }
                .contextMenu { deleteItem(msg) }
            case "confirm": withAvatar(meta: false) { ConfirmRow(msg: msg, model: m, open: { open(msg, takeover: false) }) }
                .contextMenu { deleteItem(msg) }
            case "task_result": resultRow(msg)
            default: textRow(msg)
            }
        }
    }
    func delAction(_ msg: JSON) -> (() -> Void)? {
        if msg.b("tmp") { return nil }   // still sending: nothing on the server to delete yet
        return { deletePick = msg }
    }
    /// 3.8.2: 删除 in a card's long-press menu
    @ViewBuilder func deleteItem(_ msg: JSON) -> some View {
        Button(role: .destructive) { deletePick = msg } label: { Label("删除", systemImage: "trash") }
    }
    /// 球球's messages, task cards and questions carry the small 球球 head on the left (a still frame, see MateAvatar)
    @ViewBuilder func withAvatar<C: View>(meta: Bool, @ViewBuilder _ c: () -> C) -> some View {
        HStack(alignment: .bottom, spacing: 8) {
            MateAvatar().padding(.bottom, meta ? 18 : 2)
            c()
        }
    }
    @ViewBuilder func textRow(_ msg: JSON) -> some View {
        let d = msg.o("data")
        withAvatar(meta: true) {
            VStack(alignment: .leading, spacing: 6) {
                AiwBubble(text: msg.s("text"), error: d.b("error"))
                // v3.9.15: pictures the chat made itself (generate_image / edit_image)
                ForEach(d.a("files").indices, id: \.self) { i in
                    let f = d.a("files")[i]
                    Button { Task { await openFile(f.s("path"), f.s("name")) } } label: {
                        HStack(spacing: 10) {
                            Image(systemName: f.s("kind") == "image" ? "photo" : "doc").foregroundColor(amber)
                                .frame(width: 32, height: 32).background(amber.opacity(0.12)).clipShape(RoundedRectangle(cornerRadius: 8))
                            Text(f.s("name")).font(.callout.weight(.medium)).lineLimit(1).foregroundColor(.primary)
                            Spacer(minLength: 0)
                            Image(systemName: "chevron.right").font(.caption).foregroundColor(.secondary)
                        }.padding(8).background(Color(.secondarySystemBackground)).clipShape(RoundedRectangle(cornerRadius: 12))
                    }.buttonStyle(.plain)
                }
                if !d.s("offer_task").isEmpty {
                    if d.s("task_started").isEmpty {
                        Button { Task { await m.toTask(msg.s("id")) } } label: { Label("交给电脑去做", systemImage: "desktopcomputer") }
                            .buttonStyle(.borderedProminent).tint(amber).controlSize(.small)
                    } else {
                        Label("已交给电脑", systemImage: "checkmark").font(.caption).foregroundColor(.secondary)
                    }
                }
                Text(when(msg.d("created_at"))).font(.caption2).foregroundColor(.secondary)
            }
            Spacer(minLength: 24)
        }
    }
    @ViewBuilder func streamRow(_ s: String) -> some View {
        Group {
            if s.isEmpty {
                ThinkingRow(note: m.streamNote.isEmpty ? "正在想…" : m.streamNote)
            } else {
                withAvatar(meta: false) { AiwBubble(text: s); Spacer(minLength: 24) }
            }
        }.id("stream")
    }
    /// A message waiting for the reply to end (3.8): tap for 立即发送 / 删除.
    @ViewBuilder func queuedRow(_ q: JSON) -> some View {
        HStack { Spacer(minLength: 48)
            VStack(alignment: .trailing, spacing: 3) {
                Button { queuedPick = q } label: {
                    UserBubbleV2(text: q.s("text").isEmpty ? "（附件）" : q.s("text"), attachments: q.a("attachments"), skillTitle: skillTitle)
                        .opacity(0.6)
                }.buttonStyle(.plain)
                Button { queuedPick = q } label: {
                    Label("排队中 · 回复完自动发送 · 点一下立即发送或删除", systemImage: "clock").font(.caption2)
                }.buttonStyle(.plain).foregroundColor(hAmber)
            }
        }
    }
    @ViewBuilder func resultRow(_ msg: JSON) -> some View {
        let d = msg.o("data")
        let ok = d.s("status") == "succeeded"
        withAvatar(meta: true) {
            VStack(alignment: .leading, spacing: 6) {
                RichText(text: msg.s("text"), strong: hStrong(cs))
                    .foregroundColor(hBubbleText(cs))
                    .padding(.leading, 16).padding(.trailing, 13).padding(.vertical, 10)
                    .background(BubbleShape.aiw.fill(hBubbleFill(cs)).copyMenu(msg.s("text")))
                    .overlay(alignment: .leading) { Rectangle().fill(ok ? okGreen : Color.red).frame(width: 3) }
                    .clipShape(BubbleShape.aiw)
                    .overlay(BubbleShape.aiw.stroke(hAmber.opacity(cs == .dark ? 0.67 : 0.85), lineWidth: 1.5))
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
            }
            Spacer(minLength: 24)
        }
    }

    // MARK: composer
    var typing: Bool { !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !picked.isEmpty }
    var slashQ: String? { picked.count < 5 ? slashQuery(text) : nil }
    var placeholder: String { mode == "task" ? "要电脑做什么？" : picked.isEmpty ? "和我聊聊，或输入 / 选技能…" : "说说要用它做什么…" }
    var composer: some View {
        VStack(spacing: 8) {
            if let q = slashQ { SkillPicker(rows: skillRows(q), query: q) { chooseSkill($0) } }
            // 3.6: no quick-action chips above the input (tapping a briefing bubble already runs its action)
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
                        if uploading {
                            HStack(spacing: 6) { ProgressView(value: pct).frame(width: 60).tint(hAmber); Text("上传 \(Int(pct * 100))%").font(.caption2.monospacedDigit()) }
                                .padding(.horizontal, 8).padding(.vertical, 5).background(Color(.secondarySystemBackground)).clipShape(Capsule())
                        }
                        ForEach(attach.indices, id: \.self) { i in
                            HStack(spacing: 4) {
                                Image(systemName: attach[i].s("kind") == "video" ? "film" : fileIcon(attach[i].s("name"))); Text(attach[i].s("name")).lineLimit(1)
                                Button { attach.remove(at: i) } label: { Image(systemName: "xmark.circle.fill") }.accessibilityLabel("移除附件")
                            }.font(.caption).padding(.horizontal, 8).padding(.vertical, 5).background(Color(.secondarySystemBackground)).clipShape(Capsule())
                        }
                    }
                }
            }
            HStack(alignment: .bottom, spacing: 6) {
                Button { focused = false; showAttach = true } label: {
                    Image(systemName: "plus").font(.system(size: 22, weight: .light)).foregroundColor(hAmber).frame(width: 36, height: 36)
                }.accessibilityLabel("添加：拍照、视频、文件、技能")
                HStack(spacing: 6) {
                    ForEach(picked, id: \.self) { id in SkillToken(title: skillTitle(id)) { picked.removeAll { $0 == id } } }
                    IMEField(placeholder: placeholder, text: $text, focused: $focused, maxLines: 6)   // v3.9.17 pinyin
                }
                // 3.8: listening -> stop the mic; text -> send (queued while 球球 answers); a reply with nothing typed
                // -> 停止; otherwise the mic (voice input, Speech.swift)
                if voice.active {
                    MicButton(voice: voice) { voice.stop() }
                } else if canSend {
                    Button { Task { await send() } } label: {
                        Image(systemName: "arrow.up").font(.system(size: 16, weight: .bold)).foregroundColor(hInk)
                            .frame(width: 36, height: 36).background(hAmber).clipShape(Circle())
                    }.accessibilityLabel(m.sending ? "排队发送" : "发送")
                } else if m.sending {
                    Button { Task { await m.cancel() } } label: {
                        Image(systemName: "stop.fill").font(.system(size: 13, weight: .bold)).foregroundColor(hAmber)
                            .frame(width: 36, height: 36).background(Color(white: 0.2)).clipShape(Circle())
                    }.accessibilityLabel("停止")
                } else {
                    MicButton(voice: voice) { voice.toggle(current: text) { text = $0 } }
                }
            }
            .padding(.leading, 4).padding(.trailing, 6).padding(.vertical, 5)
            .background(Color(.systemBackground))
            .overlay(RoundedRectangle(cornerRadius: 28, style: .continuous).stroke(focused ? hAmber.opacity(0.6) : Color(.separator), lineWidth: 1))
            .clipShape(RoundedRectangle(cornerRadius: 28, style: .continuous))
        }
        .padding(.horizontal, 12).padding(.top, 6).padding(.bottom, 8)
        .background(.bar)
    }
    var canSend: Bool { !uploading && (!text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !attach.isEmpty) }

    // MARK: "/" skills (the same list and the same 【技能：…】 marker the old + menu used)
    func skillTitle(_ id: String) -> String {
        let s = skills.first { $0.s("id") == id }
        return (s?.s("title").isEmpty ?? true) ? id : s!.s("title")
    }
    func skillRows(_ q: String) -> [JSON] {
        let k = q.lowercased()
        let hit = skills.filter { s in k.isEmpty || [s.s("id"), s.s("title"), s.s("desc")].contains { $0.lowercased().contains(k) } }
        let first = hit.filter { $0.s("id").lowercased().hasPrefix(k) }, rest = hit.filter { !$0.s("id").lowercased().hasPrefix(k) }
        return Array((first + rest).prefix(30))
    }
    func chooseSkill(_ id: String) {
        if let r = text.range(of: "/[^\\s/]*$", options: .regularExpression) { text.removeSubrange(r) }
        if !picked.contains(id) { picked.append(id) }
        focused = true
    }
    /// Return while the picker is open picks its first row (a hardware keyboard or the on-screen one)
    func onText(_ t: String) {
        guard t.hasSuffix("\n") else { return }
        let before = String(t.dropLast())
        if let q = slashQuery(before), let first = skillRows(q).first { text = before; chooseSkill(first.s("id")) }
    }
    func insertSlash() {
        text += (text.isEmpty || text.hasSuffix(" ") || text.hasSuffix("\n")) ? "/" : " /"
        focused = true
    }

    // MARK: 球球's briefing actions (bubbles and chips)
    func runAction(_ a: JSON) {
        switch a.s("type") {
        case "thread":
            if !a.s("thread_id").isEmpty { path.append(.task(a.s("thread_id"), takeover: false)) }
        case "retry":
            Task {
                do {
                    let r = try await API.shared.obj("POST", "/api/tasks/\(a.s("task_id"))/retry")
                    let tid = r.s("thread_id").isEmpty ? a.s("thread_id") : r.s("thread_id")
                    m.greetAt = nil
                    if !tid.isEmpty { path.append(.task(tid, takeover: false)) }
                } catch { m.err = "没能接着做：\(error.localizedDescription)" }
            }
        case "view":
            switch a.s("view") {
            case "routines": openSheet(.routine)
            case "tracking": openSheet(.tracking)
            case "computer": openSheet(.computer)
            case "tasks": openSheet(.history)
            default: focused = true
            }
        case "prompt": text = a.s("text"); focused = true
        case "send": text = a.s("text"); Task { await send() }
        default: focused = true
        }
    }

    // MARK: "+" sheet
    func runPending() {
        guard let k = pending else { return }
        pending = nil
        switch k {
        case .photo, .video:
            if UIImagePickerController.isSourceTypeAvailable(.camera) { camera = k == .video ? .video : .photo }
            else { m.err = "这台设备没有可用的相机" }
        case .library: showPhotos = true
        case .file: showFiles = true
        case .task: mode = "task"; focused = true
        case .link:
            let pb = UIPasteboard.general.hasURLs ? (UIPasteboard.general.url?.absoluteString ?? "") : ""
            linkText = pb.isEmpty ? "https://" : pb
            showLink = true
        case .skill: insertSlash()
        }
    }
    func addLink() {
        let u = linkText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !u.isEmpty, u != "https://" else { return }
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        text = (t.isEmpty ? "" : t + " ") + u + " "
        focused = true
    }

    func send() async {
        if voice.active { voice.stop() }
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if body.isEmpty && attach.isEmpty { if !picked.isEmpty { m.err = "说说要用这个技能做什么" }; return }
        let sk = picked
        let t = sk.isEmpty ? body : "【技能：\(sk.joined(separator: "、"))】\n" + body
        let files = attach, md = mode
        text = ""; attach = []; mode = "auto"; draft = ""; picked = []
        pin.toEnd(follow: 2)   // your own message: always to the end
        if !(await m.send(t, attachments: files, mode: md)) { text = body; attach = files; mode = md; picked = sk }
    }
    func open(_ msg: JSON, takeover: Bool) {
        let tid = msg.o("data").s("thread_id")
        guard !tid.isEmpty else { m.err = "这个任务还没有开始"; return }
        path.append(.task(tid, takeover: takeover))
    }
    func openFile(_ path: String, _ name: String) async {
        guard let u = API.shared.artifactURL(path) else { return }
        guard let (tmp, _) = try? await URLSession.shared.download(from: u) else { m.err = "下载失败：\(name)"; return }
        let dest = FileManager.default.temporaryDirectory.appendingPathComponent(previewFileName(name, path: path))
        try? FileManager.default.removeItem(at: dest)
        try? FileManager.default.moveItem(at: tmp, to: dest)
        preview = Preview(url: dest, path: path)
    }

    // MARK: uploads (POST /api/uploads/raw: the file itself, so videos work; chat attachments stay on the API host)
    func gotCamera(_ u: URL?, _ img: UIImage?) async {
        if let u = u {
            await uploadFile(u, name: u.lastPathComponent, type: u.pathExtension.lowercased() == "mp4" ? "video/mp4" : "video/quicktime")
        } else if let img = img, let jpg = img.jpegData(compressionQuality: 0.85) {
            await uploadData(jpg, name: "photo-\(Int(Date().timeIntervalSince1970)).jpg", type: "image/jpeg")
        }
    }
    func addPicked(_ item: PhotosPickerItem) async {
        if item.supportedContentTypes.contains(where: { $0.conforms(to: .movie) || $0.conforms(to: .video) }) {
            do {
                guard let mv = try await item.loadTransferable(type: PickedMovie.self) else { m.err = "读不了这个视频"; return }
                let ext = mv.url.pathExtension.lowercased()
                await uploadFile(mv.url, name: "video-\(Int(Date().timeIntervalSince1970)).\(ext.isEmpty ? "mov" : ext)",
                                 type: ext == "mp4" ? "video/mp4" : "video/quicktime")
            } catch { m.err = "读不了这个视频：\(error.localizedDescription)" }
            return
        }
        guard let d = try? await item.loadTransferable(type: Data.self) else { m.err = "读不了这张照片"; return }
        let jpg = UIImage(data: d)?.jpegData(compressionQuality: 0.85) ?? d
        await uploadData(jpg, name: "photo-\(Int(Date().timeIntervalSince1970)).jpg", type: "image/jpeg")
    }
    func addFile(_ u: URL) async {
        let ok = u.startAccessingSecurityScopedResource()
        defer { if ok { u.stopAccessingSecurityScopedResource() } }
        let dst = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString.prefix(6))-\(u.lastPathComponent)")
        try? FileManager.default.removeItem(at: dst)
        do { try FileManager.default.copyItem(at: u, to: dst) } catch { m.err = "读不了 \(u.lastPathComponent)"; return }
        let type = UTType(filenameExtension: u.pathExtension)?.preferredMIMEType ?? "application/octet-stream"
        await uploadFile(dst, name: u.lastPathComponent, type: type)
    }
    /// v3.9.2: what other apps handed to Aiw (share sheet, 「用其他应用打开」) -> attachment chips in this composer,
    /// uploaded one by one through uploadFile like "+" › 文件. Nothing is sent: the user writes a message and sends it.
    @MainActor func takeImports() async {
        guard inbox.hasPending, !importing else { return }
        importing = true
        m.leaveStage()
        let got = inbox.take()
        if !got.texts.isEmpty {
            let t = got.texts.joined(separator: "\n")
            text = text.isEmpty ? t : text + "\n" + t
        }
        for f in got.files {
            let type = UTType(filenameExtension: f.pathExtension)?.preferredMIMEType ?? "application/octet-stream"
            await uploadFile(f, name: f.lastPathComponent, type: type)
            inbox.discard(f)
        }
        importing = false
        focused = true
        pin.toEnd(follow: 2)
        if inbox.hasPending { await takeImports() }   // more arrived while uploading
    }
    func uploadData(_ data: Data, name: String, type: String) async {
        let dst = FileManager.default.temporaryDirectory.appendingPathComponent(name)
        do { try data.write(to: dst) } catch { m.err = "存不了临时文件：\(error.localizedDescription)"; return }
        await uploadFile(dst, name: name, type: type)
    }
    func uploadFile(_ u: URL, name: String, type: String) async {
        let size = ((try? FileManager.default.attributesOfItem(atPath: u.path))?[.size] as? NSNumber)?.intValue ?? 0
        if size > 500 * 1024 * 1024 { m.err = "\(name) 超过 500MB"; return }
        uploading = true; pct = 0
        defer { uploading = false }
        let q = name.addingPercentEncoding(withAllowedCharacters: queryValueChars) ?? "file"
        let mid = API.shared.machine.addingPercentEncoding(withAllowedCharacters: queryValueChars) ?? ""
        do {
            var r = try await API.shared.postFile("/api/uploads/raw?name=\(q)&machine_id=\(mid)&local_only=true", file: u, contentType: type) { f in
                DispatchQueue.main.async { pct = f }
            }
            if r.s("kind").isEmpty && type.hasPrefix("video") { r["kind"] = "video" }
            attach.append(r)
        } catch { m.err = "上传失败：\(name)：\(error.localizedDescription)" }
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

// 3.8.8: TaskCardRow (one card per task, actions + result on it) lives in TaskCardV2.swift.

/// A task's question (escalation, takeover, form, free question) with buttons.
struct AskRow: View {
    let msg: JSON
    @ObservedObject var model: HomeChat
    let open: (Bool) -> Void
    @State private var reply = ""
    @State private var busy = false
    @Environment(\.colorScheme) private var cs
    var body: some View {
        let d = msg.o("data")
        let k = d.s("ask_kind").isEmpty ? "question" : d.s("ask_kind")
        let resolved = d.o("resolved")
        let done = !resolved.isEmpty
        let stale = d.b("stale") && !done
        VStack(alignment: .leading, spacing: 8) {
            Text(head(k) + (d.s("title").isEmpty ? "" : " · " + cleanTitle(d.s("title")))).font(.caption.weight(.bold)).foregroundColor(done || stale ? Color.secondary : hStrong(cs))
            Text(msg.s("text")).font(.callout.weight(.semibold)).foregroundColor(hBubbleText(cs)).textSelection(.enabled)
            if k == "takeover", !d.o("takeover").s("steps").isEmpty {
                Text(d.o("takeover").s("steps")).font(.footnote).foregroundColor(.secondary)
            }
            if done {
                Label(resolvedText(resolved), systemImage: "checkmark").font(.footnote).foregroundColor(okGreen)
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
                    IMEInput(placeholder: opts.isEmpty ? "回答…" : "或者直接告诉它…", text: $reply)   // v3.9.21 pinyin
                    Button("回复") { send() }.buttonStyle(.borderedProminent).tint(amber).controlSize(.small)
                        .disabled(reply.trimmingCharacters(in: .whitespaces).isEmpty || busy)
                }
            }
            if !d.s("thread_id").isEmpty { Button("查看任务 ›") { open(false) }.font(.caption).tint(hStrong(cs)) }
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
        .askCardStyle(cs, settled: done || stale)
        .padding(.trailing, 20)
    }
    func head(_ k: String) -> String {
        switch k { case "takeover": return "需要你接管"; case "form": return "需要你填写"; case "escalate": return "任务卡住了"; default: return "任务在问你" }
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
    @Environment(\.colorScheme) private var cs
    var body: some View {
        let d = msg.o("data"), s = d.o("summary")
        let resolved = d.o("resolved")
        let done = !resolved.isEmpty, stale = d.b("stale") && !done
        VStack(alignment: .leading, spacing: 8) {
            Text("需要你确认" + (d.s("title").isEmpty ? "" : " · " + cleanTitle(d.s("title")))).font(.caption.weight(.bold)).foregroundColor(done || stale ? Color.secondary : hStrong(cs))
            Text(s.s("title").isEmpty ? msg.s("text") : s.s("title")).font(.callout.weight(.semibold)).foregroundColor(hBubbleText(cs))
            if !s.s("what").isEmpty { Text(s.s("what")).font(.footnote).foregroundColor(.secondary) }
            let why = [s.s("risk_cn"), s.s("why")].filter { !$0.isEmpty }.joined(separator: " · ")
            if !why.isEmpty { Text(why).font(.footnote).foregroundColor(.secondary) }
            if done {
                Label(resolvedText(resolved), systemImage: "checkmark").font(.footnote).foregroundColor(okGreen)
            } else if stale {
                Text("已过期（任务已经不在等这个确认）").font(.footnote).foregroundColor(.secondary)
            } else {
                HStack {
                    Button("允许") { go(true) }.buttonStyle(.borderedProminent).tint(amber)
                    Button("不允许") { go(false) }.buttonStyle(.bordered)
                }.controlSize(.small).disabled(busy)
            }
            if !d.s("thread_id").isEmpty { Button("查看任务 ›", action: open).font(.caption).tint(hStrong(cs)) }
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
        .askCardStyle(cs, settled: done || stale)
        .padding(.trailing, 20)
    }
    func go(_ ok: Bool) { busy = true; Task { _ = await model.answer(msg.s("id"), ["approve": ok]); busy = false } }
}

/// 3.8.2: a row of the home chat keyed by its message id, with its index into the current array.
struct HRowKey: Hashable { let i: Int; let id: String }
