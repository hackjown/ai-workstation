import SwiftUI

/// Plain-language approval and takeover cards (the server builds the summary in tool_router/explain.py).
/// A confirmation shows a bold title, 要做什么, 影响 chips, a colored 低/中/高 风险 tag and 为什么; the raw command
/// only sits under 查看技术细节. Shared with the web cards in apps/web/approval.js.

extension Notification.Name {
    /// A takeover card's 打开桌面接管: the desktop pane switches to that computer and opens the takeover screen.
    static let aiwOpenTakeover = Notification.Name("aiwOpenTakeover")
}

/// The takeover the agent is waiting on (so the full-screen desktop can show what to do and hand back properly).
final class TakeoverBus: ObservableObject {
    static let shared = TakeoverBus()
    @Published var pending: JSON? = nil      // the takeover form + "session_id"
    func set(_ form: JSON, session: String) { var f = form; f["session_id"] = session; pending = f }
    func clear(_ session: String) { if pending?.s("session_id") == session { pending = nil } }
    /// Tell the agent the user is done (or cancelled), from the card or the takeover screen.
    func answer(_ session: String, cancel: Bool) async {
        _ = try? await API.shared.call("POST", "/api/agent/answer", ["session_id": session, "text": cancel ? "__cancel__" : "done"])
        await MainActor.run { self.clear(session) }
    }
}

let approvalKindCN: [String: String] = ["computer": "电脑", "delete": "删除", "create": "新建", "write": "写入", "read": "读取",
                                        "move": "移动", "app": "应用", "site": "网站", "package": "软件包", "money": "金额", "place": "地址"]

func riskColor(_ r: String) -> Color { r == "high" ? .red : r == "low" ? .green : amber }
func riskCN(_ r: String) -> String { r == "high" ? "高风险" : r == "low" ? "低风险" : "中风险" }

struct RiskTag: View {
    let risk: String
    var body: some View {
        Text(riskCN(risk)).font(.caption.bold()).foregroundColor(riskColor(risk))
            .padding(.horizontal, 8).padding(.vertical, 3)
            .background(riskColor(risk).opacity(0.12)).clipShape(Capsule())
            .overlay(Capsule().stroke(riskColor(risk).opacity(0.35), lineWidth: 1))
    }
}

/// Chips that wrap onto new lines.
struct WrapLayout: Layout {
    var spacing: CGFloat = 6
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxW = proposal.width ?? 320
        var x: CGFloat = 0, y: CGFloat = 0, row: CGFloat = 0, widest: CGFloat = 0
        for s in subviews {
            let z = s.sizeThatFits(ProposedViewSize(width: maxW, height: nil))
            if x > 0 && x + z.width > maxW { y += row + spacing; x = 0; row = 0 }
            x += z.width + spacing; row = max(row, z.height); widest = max(widest, min(x, maxW))
        }
        return CGSize(width: proposal.width ?? widest, height: y + row)
    }
    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, row: CGFloat = 0
        for s in subviews {
            let z = s.sizeThatFits(ProposedViewSize(width: bounds.width, height: nil))
            if x > bounds.minX && x + z.width > bounds.maxX { y += row + spacing; x = bounds.minX; row = 0 }
            s.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(width: min(z.width, bounds.width), height: z.height))
            x += z.width + spacing; row = max(row, z.height)
        }
    }
}

struct ImpactChips: View {
    let items: [JSON]
    var body: some View {
        WrapLayout {
            ForEach(items.indices, id: \.self) { i in
                let it = items[i]
                let k = it.s("kind")
                HStack(spacing: 4) {
                    Text(approvalKindCN[k] ?? k).font(.caption.bold())
                        .foregroundColor(k == "delete" || k == "money" ? .red : (k == "create" || k == "write") ? amber : (k == "computer" || k == "site") ? .green : .secondary)
                    Text(it.s("label")).font(.caption).foregroundColor(.primary).lineLimit(2)
                }
                .padding(.horizontal, 8).padding(.vertical, 4)
                .background(Color(.tertiarySystemBackground)).clipShape(RoundedRectangle(cornerRadius: 6))
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color(.separator), lineWidth: 0.5))
            }
        }
    }
}

/// A summary from an older server (only tool / payload / reason): still readable, raw only in the details.
func approvalSummary(_ c: JSON) -> JSON {
    let s = c.o("summary")
    if !s.isEmpty { return s }
    let p = c.o("payload")
    var tech: JSON = ["tool": c.s("tool"), "reason": c.s("reason")]
    if !p.s("command").isEmpty { tech["command"] = p.s("command") } else { tech["payload"] = p }
    return ["title": "确认：\(toolName(c.s("tool")))（\(c.s("reason"))）", "what": "Aiw 想执行「\(toolName(c.s("tool")))」这一步。",
            "impact": [JSON](), "risks": c.s("reason").isEmpty ? [String]() : [c.s("reason")], "risk": "medium", "tech": tech]
}

func techText(_ s: JSON) -> String {
    let t = s.o("tech")
    if !t.s("command").isEmpty { return t.s("command") }
    if let d = try? JSONSerialization.data(withJSONObject: t.o("payload"), options: [.prettyPrinted, .sortedKeys]),
       let x = String(data: d, encoding: .utf8) { return x }
    return ""
}

struct ApprovalCard: View {
    let c: JSON
    let decide: (Bool, Bool) async -> Void      // (approve, whole task)
    @State private var showTech = false
    @State private var busy = false

    var body: some View {
        let s = approvalSummary(c)
        let risk = s.s("risk").isEmpty ? "medium" : s.s("risk")
        let risks = (s["risks"] as? [String]) ?? []
        return VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 6) {
                Image(systemName: "hand.raised.fill").foregroundColor(amber)
                Text("需要你确认").font(.footnote.weight(.semibold)).foregroundColor(.secondary)
                Spacer()
                RiskTag(risk: risk)
            }
            Text(s.s("title")).font(.headline).fixedSize(horizontal: false, vertical: true)
            if !s.s("what").isEmpty { Text(s.s("what")).font(.subheadline).fixedSize(horizontal: false, vertical: true) }
            if c.s("tool") == "xhs_review" { XhsReviewList(payload: c.o("payload")) }
            if !s.a("impact").isEmpty {
                section("影响") { ImpactChips(items: s.a("impact")) }
            }
            if !risks.isEmpty {
                section("风险") {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(risks, id: \.self) { r in
                            HStack(alignment: .top, spacing: 5) { Text("•"); Text(r).fixedSize(horizontal: false, vertical: true) }.font(.subheadline)
                        }
                    }
                }
            }
            if !s.s("why").isEmpty {
                section("为什么") { Text(s.s("why")).font(.footnote).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true) }
            }
            HStack(spacing: 8) {
                Button { go(true, false) } label: { Text("允许").frame(maxWidth: .infinity) }.buttonStyle(.borderedProminent).tint(amber)
                Button { go(false, false) } label: { Text("拒绝").frame(maxWidth: .infinity) }.buttonStyle(.bordered)
            }.disabled(busy)
            if c.b("task_allow") {
                Button("本任务内都允许（付款、发布除外）") { go(true, true) }.font(.footnote).disabled(busy)
            }
            DisclosureGroup(isExpanded: $showTech) {
                Text(techText(s)).font(.system(.caption2, design: .monospaced)).foregroundColor(.secondary)
                    .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading).padding(8)
                    .background(Color(.systemBackground)).clipShape(RoundedRectangle(cornerRadius: 8))
            } label: { Text("查看技术细节").font(.caption).foregroundColor(.secondary) }
        }
        .padding(12)
        .background(Color(.systemBackground).opacity(0.6))
        .overlay(alignment: .leading) { Rectangle().fill(riskColor(risk)).frame(width: 4) }
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color(.separator), lineWidth: 0.5))
    }

    func go(_ ok: Bool, _ all: Bool) {
        busy = true
        Task { await decide(ok, all); await MainActor.run { busy = false } }
    }

    @ViewBuilder func section<V: View>(_ label: String, @ViewBuilder _ content: () -> V) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label).font(.caption.bold()).foregroundColor(.secondary)
            content()
        }
    }
}

struct TakeoverCard: View {
    let form: JSON
    let session: String
    @State private var busy = false

    var body: some View {
        let title = form.s("title").hasPrefix("需要你接管") ? form.s("title") : "需要你接管：" + (form.s("title").isEmpty ? "在桌面上处理一下" : form.s("title"))
        var where_: [JSON] = []
        if !form.s("machine").isEmpty { where_.append(["kind": "computer", "label": form.s("machine")]) }
        if !form.s("site").isEmpty {
            let host = form.s("site_host")
            where_.append(["kind": "site", "label": form.s("site") + (host.isEmpty || host == form.s("site") ? "" : " · " + host)])
        } else if !form.s("page_title").isEmpty { where_.append(["kind": "site", "label": form.s("page_title")]) }
        let done = form.s("done_label").isEmpty ? "完成，交还给 Aiw" : form.s("done_label")
        return VStack(alignment: .leading, spacing: 9) {
            Label("Aiw 在等你", systemImage: "hand.raised.fill").font(.footnote.bold()).foregroundColor(.red)
            Text(title).font(.headline).fixedSize(horizontal: false, vertical: true)
            if !form.s("steps").isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    Text("你要做的").font(.caption.bold()).foregroundColor(amber)
                    Text(form.s("steps")).font(.subheadline).fixedSize(horizontal: false, vertical: true)
                }.padding(10).frame(maxWidth: .infinity, alignment: .leading)
                .background(amber.opacity(0.1)).clipShape(RoundedRectangle(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(amber.opacity(0.4), lineWidth: 1))
            }
            if !where_.isEmpty { ImpactChips(items: where_) }
            if !form.s("why").isEmpty { Text("为什么：" + form.s("why")).font(.footnote).foregroundColor(.secondary) }
            Button {
                NotificationCenter.default.post(name: .aiwOpenTakeover, object: nil, userInfo: ["machine": form.s("machine_id"), "session": session])
            } label: { Label("打开桌面接管", systemImage: "hand.raised.fill").frame(maxWidth: .infinity) }
                .buttonStyle(.borderedProminent).tint(amber)
            HStack(spacing: 8) {
                Button { Task { busy = true; await TakeoverBus.shared.answer(session, cancel: false); busy = false } } label: {
                    Text(done).frame(maxWidth: .infinity)
                }.buttonStyle(.bordered)
                Button(role: .cancel) { Task { busy = true; await TakeoverBus.shared.answer(session, cancel: true); busy = false } } label: {
                    Text("取消")
                }.buttonStyle(.bordered)
            }.disabled(busy)
            Text("接管时 Aiw 会暂停；处理好点「\(done)」，它从当前页面接着做。不要在聊天里发密码或验证码。")
                .font(.caption2).foregroundColor(.secondary)
        }
        .padding(12)
        .background(Color(.systemBackground).opacity(0.6))
        .overlay(alignment: .leading) { Rectangle().fill(Color.red).frame(width: 4) }
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color(.separator), lineWidth: 0.5))
        .onAppear { TakeoverBus.shared.set(form, session: session) }
    }
}

/// Past confirmations / takeovers of a task with the user's decision (the timeline).
struct ApprovalLog: View {
    let items: [JSON]
    @State private var showOlder = false   // v3.9.19: same as the web: over 3 records, only the newest 2 stay open
    private var okCount: Int { items.filter { $0.s("kind") != "takeover" && $0.b("approve") }.count }
    private var noCount: Int { items.filter { $0.s("kind") != "takeover" && !$0.b("approve") }.count }
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text("确认记录").font(.caption.bold()).foregroundColor(.secondary)
                if items.count > 3 {
                    Text("允许 \(okCount) · 未允许 \(noCount)").font(.caption2).foregroundColor(.secondary)
                }
            }
            if items.count > 3 {
                let older = items.count - 2
                DisclosureGroup(isExpanded: $showOlder) {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(0..<older, id: \.self) { i in row(items[i]) }
                    }
                } label: {
                    Text("更早的 \(older) 条").font(.caption).foregroundColor(.secondary)
                }
                ForEach(older..<items.count, id: \.self) { i in row(items[i]) }
            } else {
                ForEach(items.indices, id: \.self) { i in row(items[i]) }
            }
        }
    }
    @ViewBuilder func row(_ d: JSON) -> some View {
        if d.s("kind") == "takeover" {
            HStack(alignment: .top, spacing: 6) {
                Text(d.b("cancelled") ? "已取消接管" : d.b("timeout") ? "没有接管，已超时" : "你已处理并交还")
                    .font(.caption.bold()).foregroundColor(d.b("cancelled") || d.b("timeout") ? .red : .green)
                Text(d.s("title")).font(.caption)
            }
        } else {
            let s = d.o("summary").isEmpty ? ["title": d.s("title"), "risk": d.s("risk")] as JSON : d.o("summary")
            let ok = d.b("approve")
            let by = d.s("by")
            let dec = ok ? (by == "task" ? "本任务内自动允许" : by == "user_task" ? "已允许（本任务内都允许）" : "已允许")
                : (by == "timeout" ? "没有确认，已跳过" : by == "stop" ? "任务已停止" : "已拒绝")
            DisclosureGroup {
                VStack(alignment: .leading, spacing: 4) {
                    if !s.s("what").isEmpty { Text(s.s("what")).font(.caption).foregroundColor(.secondary) }
                    if !s.a("impact").isEmpty { ImpactChips(items: s.a("impact")) }
                    if !techText(s).isEmpty {
                        Text(techText(s)).font(.system(.caption2, design: .monospaced)).foregroundColor(.secondary).textSelection(.enabled)
                    }
                }
            } label: {
                HStack(alignment: .top, spacing: 6) {
                    Text(dec).font(.caption.bold()).foregroundColor(ok ? .green : .red)
                    RiskTag(risk: s.s("risk").isEmpty ? "medium" : s.s("risk")).scaleEffect(0.85)
                    Text(s.s("title")).font(.caption).foregroundColor(.primary).multilineTextAlignment(.leading)
                }
            }
        }
    }
}
