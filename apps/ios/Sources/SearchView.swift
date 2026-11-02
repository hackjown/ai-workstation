import SwiftUI

/// v3.9.14 in-app search over the chat, tasks and files (GET /api/search, server: search.py). It opens from the
/// magnifier at the top right of the home chat (where 新建任务 was; 新建任务 stays in the "+" menu). A hit opens through
/// the deep links: a chat message scrolls the chat to it (aiw://chat?m=<id>), a task or a task's file opens the task.
struct SearchSheet: View {
    let close: () -> Void
    @State private var q = ""
    @State private var kind = ""
    @State private var results: [JSON] = []
    @State private var total = 0
    @State private var loading = false
    @State private var error = ""
    @State private var job: Task<Void, Never>? = nil
    @FocusState private var focused: Bool

    private let kinds: [(String, String)] = [("", "全部"), ("chat", "对话"), ("task", "任务"), ("file", "文件")]
    private let status = ["succeeded": "完成", "failed": "失败", "running": "进行中", "cancelled": "已取消", "waiting": "等待中"]

    var body: some View {
        SheetChrome("搜索", close: close) {
            VStack(spacing: 0) {
                HStack(spacing: 8) {
                    Image(systemName: "magnifyingglass").foregroundColor(.secondary)
                    TextField("搜对话、任务和文件", text: $q)
                        .focused($focused).submitLabel(.search)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .onSubmit { run(now: true) }
                    if !q.isEmpty {
                        Button { q = "" } label: { Image(systemName: "xmark.circle.fill").foregroundColor(.secondary) }
                            .accessibilityLabel("清空")
                    }
                }
                .padding(.horizontal, 12).padding(.vertical, 9)
                .background(Color(.secondarySystemBackground)).clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                .padding(.horizontal, 16).padding(.top, 6)
                Picker("类别", selection: $kind) {
                    ForEach(kinds, id: \.0) { k in Text(k.1).tag(k.0) }
                }
                .pickerStyle(.segmented).padding(.horizontal, 16).padding(.vertical, 8)
                list
            }
        }
        .onAppear { DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { focused = true } }
        .onChange(of: q) { _ in run(now: false) }
        .onChange(of: kind) { _ in run(now: true) }
    }

    @ViewBuilder var list: some View {
        if q.trimmingCharacters(in: .whitespaces).isEmpty {
            hint("输入关键词，中文两个字以上")
        } else if !error.isEmpty {
            hint("搜索失败：" + error)
        } else if results.isEmpty {
            if loading { ProgressView().frame(maxWidth: .infinity).padding(.top, 40); Spacer() }
            else { hint("没有搜到「\(q)」，换个说法试试") }
        } else {
            List {
                ForEach(groups, id: \.0) { g in
                    Section(header: Text(g.1).textCase(nil)) {
                        ForEach(g.2.indices, id: \.self) { i in
                            let r = g.2[i]
                            Button { pick(r) } label: { row(r) }.buttonStyle(.plain)
                        }
                    }
                }
                if total > results.count {
                    Text("共 \(total) 条，只显示最相关的 \(results.count) 条").font(.caption).foregroundColor(.secondary)
                }
            }
            .listStyle(.insetGrouped)
            .scrollDismissesKeyboard(.immediately)
        }
    }

    /// in "全部" the hits are grouped by kind, in server order inside each group
    private var groups: [(String, String, [JSON])] {
        if !kind.isEmpty { return [(kind, kinds.first { $0.0 == kind }?.1 ?? "", results)] }
        var out: [(String, String, [JSON])] = []
        for r in results {
            let k = r.s("kind")
            if let i = out.firstIndex(where: { $0.0 == k }) { out[i].2.append(r) }
            else { out.append((k, r.s("kind_cn"), [r])) }
        }
        return out
    }

    func hint(_ s: String) -> some View {
        VStack { Text(s).font(.subheadline).foregroundColor(.secondary).multilineTextAlignment(.center).padding(.top, 40); Spacer() }
            .frame(maxWidth: .infinity)
    }

    func row(_ r: JSON) -> some View {
        let k = r.s("kind")
        let title = k == "chat" ? (r.s("role") == "user" ? "我说" : "Aiw 说") : r.s("title")
        var meta = [when(r.d("ts"))]
        if k == "task", let st = status[r.s("status")] ?? (r.s("status").isEmpty ? nil : r.s("status")) { meta.append(st) }
        if k == "file", !r.s("path").isEmpty { meta.append(r.s("path")) }
        return VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Image(systemName: k == "chat" ? "bubble.left" : k == "task" ? "checklist" : "doc")
                    .font(.caption).foregroundColor(.secondary)
                Text(highlight(title)).font(.subheadline.weight(.semibold)).lineLimit(1)
            }
            if k != "file" { Text(highlight(r.s("snippet"))).font(.footnote).foregroundColor(.secondary).lineLimit(3) }
            Text(meta.filter { !$0.isEmpty }.joined(separator: " · ")).font(.caption2).foregroundColor(.secondary).lineLimit(1)
        }
        .padding(.vertical, 2).contentShape(Rectangle())
    }

    /// the search words in bold amber
    func highlight(_ text: String) -> AttributedString {
        var a = AttributedString(text)
        let words = q.split(whereSeparator: { " ,，、".contains($0) }).map(String.init).filter { !$0.isEmpty }.prefix(6)
        for w in words {
            var start = a.startIndex
            while start < a.endIndex, let r = a[start...].range(of: w, options: .caseInsensitive) {
                a[r].foregroundColor = amber
                a[r].font = .body.weight(.semibold)
                start = r.upperBound
            }
        }
        return a
    }

    func when(_ ts: Double) -> String {
        guard ts > 0 else { return "" }
        let d = Date(timeIntervalSince1970: ts), f = DateFormatter()
        f.locale = Locale(identifier: "zh_CN")
        let cal = Calendar.current
        f.dateFormat = cal.isDateInToday(d) ? "今天 HH:mm" : cal.isDate(d, equalTo: Date(), toGranularity: .year) ? "M月d日 HH:mm" : "yyyy-M-d HH:mm"
        return f.string(from: d)
    }

    func run(now: Bool) {
        job?.cancel()
        let text = q.trimmingCharacters(in: .whitespaces), k = kind
        guard !text.isEmpty else { results = []; total = 0; error = ""; return }
        job = Task { @MainActor in
            if !now { try? await Task.sleep(nanoseconds: 250_000_000) }
            if Task.isCancelled { return }
            loading = true
            defer { loading = false }
            var comps = URLComponents()
            comps.queryItems = [URLQueryItem(name: "q", value: text), URLQueryItem(name: "limit", value: "40")]
                + (k.isEmpty ? [] : [URLQueryItem(name: "kind", value: k)])
            do {
                let r = try await API.shared.obj("GET", "/api/search?" + (comps.percentEncodedQuery ?? ""))
                if Task.isCancelled { return }
                results = r.a("results"); total = Int(r.d("total")); error = ""
            } catch {
                if Task.isCancelled { return }
                self.error = error.localizedDescription
            }
        }
    }

    func pick(_ r: JSON) {
        let open = r.s("open").isEmpty ? "chat" : r.s("open")
        close()
        if let u = URL(string: "aiw://" + open) {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { DeepLinks.shared.handle(u) }
        }
    }
}
