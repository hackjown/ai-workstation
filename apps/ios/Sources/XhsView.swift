import SwiftUI

/// 小红书运营: settings, today's counts, pause state and the approval queue (edit / approve / skip, batch approve).
/// Everything is stored and enforced on the server (/api/xhs); this view only reads and writes it.
private let xhsKinds: [(String, String)] = [("like", "点赞"), ("comment", "评论别人"), ("reply", "回复评论"), ("post", "发布笔记")]
private func xhsKindName(_ k: String) -> String { xhsKinds.first { $0.0 == k }?.1 ?? k }

struct XhsView: View {
    @EnvironmentObject var api: API
    @State private var data: JSON = [:]
    @State private var enabled = false
    @State private var machineId = ""
    @State private var persona = ""
    @State private var keywords = ""
    @State private var tone = ""
    @State private var caps: [String: Int] = ["like": 30, "comment": 8, "reply": 20, "post": 1]
    @State private var acts: [String: Bool] = ["like": true, "comment": true, "reply": true, "post": true]
    @State private var autoReply = false
    @State private var start = Date()
    @State private var end = Date()
    @State private var picked: Set<String> = []
    @State private var edits: [String: String] = [:]
    @State private var error = ""
    @State private var saved = false

    var body: some View {
        Form {
            let pause = data.o("pause")
            if !pause.isEmpty {
                Section {
                    VStack(alignment: .leading, spacing: 6) {
                        Label("已暂停到 \(when(pause.d("until")))", systemImage: "exclamationmark.octagon.fill").foregroundColor(.red).font(.subheadline.bold())
                        Text("页面出现了\(pause.s("reason"))。所有自动操作已停止 24 小时；验证码不会自动处理，需要时请在桌面里接管自己处理。").font(.footnote)
                        Button("解除暂停") { Task { await resume() } }.font(.footnote)
                    }
                }
            }
            Section(header: Text("今天"), footer: Text(nextLine)) {
                let c = data.o("counts")
                HStack {
                    ForEach(xhsKinds, id: \.0) { k in
                        VStack { Text("\(Int(c.d(k.0)))/\(caps[k.0] ?? 0)").font(.headline); Text(k.1).font(.caption2).foregroundColor(.secondary) }
                            .frame(maxWidth: .infinity)
                    }
                }
                Button { Task { await run() } } label: { Label("现在跑一次", systemImage: "play.fill") }
            }
            pendingSection
            Section(header: Text("设置"), footer: Text(machineHint)) {
                Toggle("自动运营（活跃时段内随机时间）", isOn: $enabled)
                Picker("电脑", selection: $machineId) {
                    ForEach(data.a("machines").indices, id: \.self) { i in
                        let m = data.a("machines")[i]
                        Text(m.s("name") + (m.b("cloud") ? "（云 · 机房 IP）" : "（本地 · 家庭 IP）")).tag(m.s("id"))
                    }
                }
            }
            Section(header: Text("人设与兴趣")) {
                TextField("人设，例如：杭州上班族，周末露营、手冲咖啡", text: $persona, axis: .vertical).lineLimit(2...4)
                TextField("兴趣关键词，逗号分隔", text: $keywords)
                TextField("语气", text: $tone)
            }
            Section(header: Text("每日上限"), footer: Text("点赞不审核但受上限限制；上限在服务器端按天计数。")) {
                ForEach(xhsKinds, id: \.0) { k in
                    Stepper("\(k.1)：\(caps[k.0] ?? 0)", value: Binding(get: { caps[k.0] ?? 0 }, set: { caps[k.0] = $0 }), in: 0...100)
                }
            }
            Section(header: Text("活跃时段与动作")) {
                DatePicker("开始", selection: $start, displayedComponents: .hourAndMinute)
                DatePicker("结束", selection: $end, displayedComponents: .hourAndMinute)
                ForEach(xhsKinds, id: \.0) { k in
                    Toggle(k.1, isOn: Binding(get: { acts[k.0] ?? false }, set: { acts[k.0] = $0 }))
                }
                Toggle("回复我笔记下的评论不用审核", isOn: $autoReply)
            }
            Section(footer: Text("自动化操作可能被小红书限流或封号。Aiw 会模仿真人节奏，但不能保证不被识别；遇到任何验证或警告会立刻停下并暂停 24 小时。")) {
                Button(saved ? "已保存" : "保存设置") { Task { await save() } }
            }
            if !error.isEmpty { Section { Text(error).foregroundColor(.red).font(.footnote) } }
        }
        .navigationTitle("小红书运营").navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task { await load() }
    }

    @ViewBuilder var pendingSection: some View {
        let pending = data.a("pending")
        Section(header: HStack {
            Text("待审核 \(pending.count)")
            Spacer()
            if !pending.isEmpty {
                Button(picked.count == pending.count ? "取消全选" : "全选") {
                    picked = picked.count == pending.count ? [] : Set(pending.map { $0.s("id") })
                }.font(.caption)
            }
        }, footer: Text(pending.isEmpty ? "没有待审核的草稿。新草稿会推送通知给你。" : "可以先改文字，再通过；勾选多条后批量通过或跳过。")) {
            ForEach(pending.indices, id: \.self) { i in draftRow(pending[i]) }
            if !picked.isEmpty {
                HStack {
                    Button("通过所选 \(picked.count)") { Task { await batch("approve") } }.buttonStyle(.borderedProminent).tint(amber)
                    Button("跳过所选") { Task { await batch("skip") } }.buttonStyle(.bordered)
                }
            }
        }
        let approved = data.a("approved")
        if !approved.isEmpty {
            Section(header: Text("已批准，等下次会话发出 \(approved.count)")) {
                ForEach(approved.indices, id: \.self) { i in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(xhsKindName(approved[i].s("kind")) + " · " + approved[i].s("target_title")).font(.caption).foregroundColor(.secondary)
                        Text(approved[i].s("text")).font(.footnote)
                    }
                }
            }
        }
    }

    @ViewBuilder func draftRow(_ d: JSON) -> some View {
        let id = d.s("id")
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top) {
                Button { if picked.contains(id) { picked.remove(id) } else { picked.insert(id) } } label: {
                    Image(systemName: picked.contains(id) ? "checkmark.circle.fill" : "circle").foregroundColor(amber)
                }.buttonStyle(.plain)
                VStack(alignment: .leading, spacing: 2) {
                    Text(xhsKindName(d.s("kind"))).font(.caption.bold()).foregroundColor(amber)
                    if d.s("kind") == "post" {
                        Text(d.s("title").isEmpty ? "（无标题）" : d.s("title")).font(.subheadline.bold())
                    } else if let u = URL(string: d.s("target_url")), !d.s("target_url").isEmpty {
                        Link(d.s("target_title").isEmpty ? "目标笔记" : d.s("target_title"), destination: u).font(.subheadline)
                    }
                }
            }
            if !d.s("target_excerpt").isEmpty {
                Text(d.s("target_excerpt")).font(.caption).foregroundColor(.secondary).lineLimit(3)
                    .padding(.leading, 8).overlay(Rectangle().fill(amber.opacity(0.6)).frame(width: 3), alignment: .leading)
            }
            TextField("草稿", text: Binding(get: { edits[id] ?? d.s("text") }, set: { edits[id] = $0 }), axis: .vertical)
                .lineLimit(2...6).textFieldStyle(.roundedBorder)
            HStack {
                Button("通过") { Task { await decide(id, "approve") } }.buttonStyle(.borderedProminent).tint(amber)
                if let e = edits[id], e != d.s("text") { Button("保存修改") { Task { await decide(id, "edit") } }.buttonStyle(.bordered) }
                Button("跳过") { Task { await decide(id, "skip") } }.buttonStyle(.bordered)
            }.font(.footnote)
        }.padding(.vertical, 4)
    }

    var nextLine: String {
        let runs = (data["next_runs"] as? [Double]) ?? []
        if !enabled { return "未开启自动运营。" }
        return runs.isEmpty ? "今天的时段已过或额度已满。" : "今天还会在 " + runs.map { when($0) }.joined(separator: "、") + " 左右各跑一次（时间随机）。"
    }
    var machineHint: String {
        let cloud = data.a("machines").first { $0.s("id") == machineId }?.b("cloud") ?? false
        return (cloud ? "⚠️ 云电脑是机房 IP，更容易被风控，建议用本地虚拟机（家庭网络 IP）。" : "推荐本地虚拟机（如 104）：家庭网络 IP，比云电脑更不容易被风控。")
            + "这台电脑的 Chrome 要登录一次小红书：没登录时会通知你，在桌面里接管后用小红书 App 扫码。"
    }

    func hm(_ s: String) -> Date {
        let p = s.split(separator: ":").compactMap { Int($0) }
        return Calendar.current.date(bySettingHour: p.first ?? 9, minute: p.count > 1 ? p[1] : 0, second: 0, of: Date()) ?? Date()
    }
    func hmString(_ d: Date) -> String {
        let c = Calendar.current.dateComponents([.hour, .minute], from: d)
        return String(format: "%02d:%02d", c.hour ?? 0, c.minute ?? 0)
    }

    func load() async {
        do {
            let o = try await api.obj("GET", "/api/xhs")
            await MainActor.run {
                data = o
                let s = o.o("settings")
                enabled = s.b("enabled"); machineId = o.s("machine_id"); persona = s.s("persona"); tone = s.s("tone")
                keywords = ((s["keywords"] as? [String]) ?? []).joined(separator: "，")
                for k in xhsKinds.map({ $0.0 }) { caps[k] = Int(s.o("caps").d(k)); acts[k] = s.o("actions").b(k) }
                autoReply = s.b("auto_reply")
                start = hm(s.o("window").s("start")); end = hm(s.o("window").s("end"))
                let ids = Set(o.a("pending").map { $0.s("id") }); picked = picked.intersection(ids)
                error = ""
            }
        } catch { await MainActor.run { self.error = error.localizedDescription } }
    }
    func save() async {
        let body: JSON = ["enabled": enabled, "machine_id": machineId, "persona": persona, "keywords": keywords, "tone": tone,
                          "caps": caps, "actions": acts, "auto_reply": autoReply,
                          "window": ["start": hmString(start), "end": hmString(end)]]
        do { _ = try await api.obj("PUT", "/api/xhs/settings", body); await MainActor.run { saved = true }; await load() }
        catch { await MainActor.run { self.error = error.localizedDescription } }
    }
    func decide(_ id: String, _ action: String) async {
        var body: JSON = ["action": action]
        if let e = edits[id] { body["text"] = e }
        do { _ = try await api.obj("POST", "/api/xhs/drafts/\(id)", body); await MainActor.run { edits[id] = nil; picked.remove(id) }; await load() }
        catch { await MainActor.run { self.error = error.localizedDescription } }
    }
    func batch(_ action: String) async {
        let ids = Array(picked)
        if action == "approve" {
            for id in ids { if let e = edits[id] { _ = try? await api.obj("POST", "/api/xhs/drafts/\(id)", ["action": "edit", "text": e] as JSON) } }
        }
        do { _ = try await api.obj("POST", "/api/xhs/drafts/batch", ["ids": ids, "action": action] as JSON); await MainActor.run { picked = []; edits = [:] }; await load() }
        catch { await MainActor.run { self.error = error.localizedDescription } }
    }
    func run() async {
        do { _ = try await api.obj("POST", "/api/xhs/run"); await MainActor.run { error = "已开始一次会话，可在对话里看过程" } }
        catch { await MainActor.run { self.error = error.localizedDescription } }
    }
    func resume() async { _ = try? await api.call("DELETE", "/api/xhs/pause"); await load() }
}

/// The drafts inside an xhs_review confirmation card (target post, excerpt, draft text).
struct XhsReviewList: View {
    let payload: JSON
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(payload.a("drafts").indices, id: \.self) { i in
                let d = payload.a("drafts")[i]
                VStack(alignment: .leading, spacing: 3) {
                    Text(xhsKindName(d.s("kind")) + (d.s("target_title").isEmpty ? "" : " · " + d.s("target_title")))
                        .font(.caption.bold()).foregroundColor(amber)
                    if !d.s("target_excerpt").isEmpty { Text(d.s("target_excerpt")).font(.caption2).foregroundColor(.secondary).lineLimit(2) }
                    Text(d.s("text")).font(.footnote)
                }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color(.systemBackground)).clipShape(RoundedRectangle(cornerRadius: 8))
            }
            NavigationLink { XhsView().environmentObject(API.shared) } label: { Label("逐条编辑 / 跳过", systemImage: "square.and.pencil").font(.caption.bold()) }
            Text("允许 = 全部按原文通过；拒绝 = 全部跳过").font(.caption2).foregroundColor(.secondary)
        }
    }
}
