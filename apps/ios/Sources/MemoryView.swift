import SwiftUI

/// 设置 › 记忆: what 球球 remembers about you (/api/memories*, memory.py). 画像 / 事实 / 决定; replaced memories are
/// hidden unless 显示被替换的. Passwords, codes and tokens are refused by the server.
let memoryKinds: [(String, String)] = [("profile", "画像"), ("fact", "事实"), ("decision", "决定")]
func memoryKindName(_ k: String) -> String { memoryKinds.first { $0.0 == k }?.1 ?? "事实" }

struct MemoryView: View {
    @EnvironmentObject var api: API
    @State private var items: [JSON] = []
    @State private var kind = ""
    @State private var showOld = false
    @State private var query = ""
    @State private var err = ""
    @State private var loaded = false
    @State private var editing: JSON? = nil
    @State private var adding = false

    var shown: [JSON] {
        items.filter { (kind.isEmpty || $0.s("kind") == kind) && (query.isEmpty || $0.s("text").localizedCaseInsensitiveContains(query)) }
    }

    var body: some View {
        List {
            Section(footer: Text("聊天和每个任务都会参考：你的画像，以及和当前请求最相关的 8 条事实和决定。在聊天里说「记住…」会立刻记下，「忘掉…」会删掉。密码、验证码和 token 不会被记下。")) {
                Picker("类型", selection: $kind) {
                    Text("全部").tag("")
                    ForEach(memoryKinds, id: \.0) { k in Text(k.1).tag(k.0) }
                }.pickerStyle(.segmented)
                Toggle("显示被替换的", isOn: $showOld)
            }
            if !err.isEmpty { Section { Text(err).foregroundColor(.red).font(.footnote) } }
            Section {
                if loaded && shown.isEmpty {
                    Text(query.isEmpty && kind.isEmpty ? "还没有记忆。在聊天里说「记住…」试试。" : "没有匹配的记忆").foregroundColor(.secondary)
                }
                ForEach(shown.indices, id: \.self) { i in
                    let mm = shown[i]
                    row(mm)
                        .swipeActions(edge: .trailing) {
                            Button(role: .destructive) { Task { await delete(mm) } } label: { Label("删除", systemImage: "trash") }
                            if mm.s("superseded").isEmpty {
                                Button { editing = mm } label: { Label("编辑", systemImage: "pencil") }.tint(amber)
                            }
                        }
                        .contextMenu {
                            if mm.s("superseded").isEmpty { Button { editing = mm } label: { Label("编辑", systemImage: "pencil") } }
                            else { Button { Task { await restore(mm) } } label: { Label("恢复", systemImage: "arrow.uturn.backward") } }
                            Button(role: .destructive) { Task { await delete(mm) } } label: { Label("删除", systemImage: "trash") }
                        }
                }
            }
        }
        .searchable(text: $query, prompt: "搜索记忆")
        .navigationTitle("记忆").navigationBarTitleDisplayMode(.inline)
        .toolbar { ToolbarItem(placement: .navigationBarTrailing) {
            Button { adding = true } label: { Image(systemName: "plus") }.accessibilityLabel("添加记忆")
        } }
        .task { await load() }
        .refreshable { await load() }
        .onChange(of: showOld) { _ in Task { await load() } }
        .sheet(isPresented: $adding) { MemoryEditor(title: "添加记忆", text: "", kind: "fact") { t, k in await save(nil, t, k) } }
        .sheet(item: Binding(get: { editing.map { MemEdit(m: $0) } }, set: { if $0 == nil { editing = nil } })) { e in
            MemoryEditor(title: "编辑记忆", text: e.m.s("text"), kind: e.m.s("kind")) { t, k in await save(e.m.s("id"), t, k) }
        }
    }

    @ViewBuilder func row(_ m: JSON) -> some View {
        let old = !m.s("superseded").isEmpty
        HStack(alignment: .top, spacing: 10) {
            Chip(text: memoryKindName(m.s("kind")), color: kindColor(m.s("kind")))
            VStack(alignment: .leading, spacing: 3) {
                Text(m.s("text")).font(.callout).strikethrough(old).foregroundColor(old ? Color.secondary : Color.primary).textSelection(.enabled)
                Text(source(m) + " · " + when(m.d("updated_at") > 0 ? m.d("updated_at") : m.d("created_at")) + (old ? " · 已被新的记忆替换" : ""))
                    .font(.caption2).foregroundColor(.secondary)
            }
        }.padding(.vertical, 2)
    }
    func kindColor(_ k: String) -> Color { k == "profile" ? amber : k == "decision" ? okGreen : Color.secondary }
    func source(_ m: JSON) -> String {
        let s = m.s("source")
        if s == "user" { return "你添加" }
        if s.hasPrefix("msg-") { return "从聊天里记下" }
        if s.hasPrefix("task-") { return "任务里记下" }
        return "球球记下"
    }
    func load() async {
        do { items = try await api.list("/api/memories" + (showOld ? "?all=true" : "")); err = ""; loaded = true }
        catch { err = error.localizedDescription }
    }
    /// Returns an error message for the editor, or nil when saved.
    func save(_ id: String?, _ text: String, _ kind: String) async -> String? {
        do {
            if let id = id { _ = try await api.obj("PUT", "/api/memories/\(id)", ["text": text, "kind": kind]) }
            else { _ = try await api.obj("POST", "/api/memories", ["text": text, "kind": kind]) }
            await load(); return nil
        } catch { return error.localizedDescription }
    }
    func delete(_ m: JSON) async {
        do { _ = try await api.call("DELETE", "/api/memories/\(m.s("id"))"); await load() } catch { err = error.localizedDescription }
    }
    func restore(_ m: JSON) async {
        do { _ = try await api.call("POST", "/api/memories/\(m.s("id"))/restore"); await load() } catch { err = error.localizedDescription }
    }
}

struct MemEdit: Identifiable { let m: JSON; var id: String { m.s("id") } }

struct MemoryEditor: View {
    let title: String
    @State var text: String
    @State var kind: String
    let save: (String, String) async -> String?
    @Environment(\.dismiss) private var dismiss
    @State private var err = ""
    @State private var busy = false
    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Picker("类型", selection: $kind) { ForEach(memoryKinds, id: \.0) { k in Text(k.1).tag(k.0) } }.pickerStyle(.segmented)
                    TextField("例如：周报放在 ~/workspace/reports，用 Word 格式", text: $text, axis: .vertical).lineLimit(2...6)
                }
                if !err.isEmpty { Section { Text(err).foregroundColor(.red).font(.footnote) } }
            }
            .navigationTitle(title).navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("保存") {
                        busy = true
                        Task {
                            let e = await save(text.trimmingCharacters(in: .whitespacesAndNewlines), kind)
                            busy = false
                            if let e = e { err = e } else { dismiss() }
                        }
                    }.disabled(busy || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
        .presentationDetents([.medium])
    }
}
