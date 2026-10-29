import SwiftUI

/// 设置 › Aiw 学到的经验 (3.8.5): what Aiw learned from earlier tasks (/api/lessons, lessons.py). Each lesson says
/// where it applies (电脑 / 网站 / 技能 / 工具 / 通用), the problem and the fix that worked; 「用户纠正过」 marks the
/// user's own corrections. View, search, delete one (swipe / long press) or clear all.
let lessonScopes: [(String, String)] = [("machine", "电脑"), ("site", "网站"), ("skill", "技能"), ("tool", "工具"), ("general", "通用")]

struct LessonsView: View {
    @EnvironmentObject var api: API
    @State private var items: [JSON] = []
    @State private var scope = ""
    @State private var query = ""
    @State private var err = ""
    @State private var loaded = false
    @State private var askClear = false

    var shown: [JSON] {
        items.filter { l in
            (scope.isEmpty || l.s("scope") == scope) &&
            (query.isEmpty || "\(l.s("where")) \(l.s("problem")) \(l.s("fix"))".localizedCaseInsensitiveContains(query))
        }
    }

    var body: some View {
        List {
            Section(footer: Text("任务做完后，Aiw 会把「遇到什么问题 → 怎么解决的」记下来，下次直接用；出错时也会按报错去找。你纠正过的做法最优先。")) {
                Picker("分类", selection: $scope) {
                    Text("全部").tag("")
                    ForEach(lessonScopes, id: \.0) { s in Text(s.1).tag(s.0) }
                }.pickerStyle(.segmented)
            }
            if !err.isEmpty { Section { Text(err).foregroundColor(.red).font(.footnote) } }
            Section {
                if loaded && shown.isEmpty {
                    Text(query.isEmpty && scope.isEmpty ? "还没有经验。Aiw 做完任务后会自己总结。" : "没有匹配的经验").foregroundColor(.secondary)
                }
                ForEach(shown.indices, id: \.self) { i in
                    let l = shown[i]
                    LessonRow(l: l)
                        .swipeActions(edge: .trailing) {
                            Button(role: .destructive) { Task { await delete(l) } } label: { Label("删除", systemImage: "trash") }
                        }
                        .contextMenu {
                            Button { UIPasteboard.general.string = "\(l.s("problem")) → \(l.s("fix"))" } label: { Label("复制", systemImage: "doc.on.doc") }
                            Button(role: .destructive) { Task { await delete(l) } } label: { Label("删除", systemImage: "trash") }
                        }
                }
            }
        }
        .searchable(text: $query, prompt: "搜索经验")
        .navigationTitle("Aiw 学到的经验").navigationBarTitleDisplayMode(.inline)
        .toolbar { ToolbarItem(placement: .navigationBarTrailing) {
            Button("清空") { askClear = true }.disabled(items.isEmpty)
        } }
        .confirmationDialog("清空全部经验？", isPresented: $askClear, titleVisibility: .visible) {
            Button("清空", role: .destructive) { Task { await clear() } }
            Button("取消", role: .cancel) {}
        }
        .task { await load() }
        .refreshable { await load() }
    }

    func load() async {
        do { items = try await api.list("/api/lessons"); err = ""; loaded = true }
        catch { err = error.localizedDescription }
    }
    func delete(_ l: JSON) async {
        do { _ = try await api.call("DELETE", "/api/lessons/\(l.s("id"))"); await load() } catch { err = error.localizedDescription }
    }
    func clear() async {
        do { _ = try await api.call("DELETE", "/api/lessons"); await load() } catch { err = error.localizedDescription }
    }
}

struct LessonRow: View {
    let l: JSON
    var user: Bool { l.s("source") == "user" }
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Chip(text: l.s("scope_cn").isEmpty ? "通用" : l.s("scope_cn"), color: user ? okGreen : amber)
            VStack(alignment: .leading, spacing: 3) {
                Text((user ? "用户纠正过：" : "") + l.s("problem")).font(.callout.weight(.medium)).textSelection(.enabled)
                Text("→ " + l.s("fix")).font(.callout).textSelection(.enabled)
                Text(meta).font(.caption2).foregroundColor(.secondary)
            }
        }.padding(.vertical, 2)
    }
    var meta: String {
        let used = "用过 \(Int(l.d("hits"))) 次"
        let at = l.d("last_used") > 0 ? "上次用 " + when(l.d("last_used")) : "记于 " + when(l.d("created_at"))
        return [l.s("where"), used, at].filter { !$0.isEmpty }.joined(separator: " · ")
    }
}
