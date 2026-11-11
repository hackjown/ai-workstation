import SwiftUI

/// v3.9.24 设置 › 用量统计: tokens per day and per model (/api/usage), the same numbers as the web page.
struct UsageView: View {
    @EnvironmentObject var api: API
    @State private var days = 7
    @State private var data: JSON = [:]
    @State private var err = ""
    @State private var loaded = false

    static func fmt(_ n: Double) -> String {
        if n >= 1_000_000 { return String(format: "%.2fM", n / 1_000_000) }
        if n >= 10_000 { return String(format: "%.1fk", n / 1_000) }
        return String(Int(n))
    }

    var body: some View {
        List {
            Section {
                Picker("范围", selection: $days) {
                    Text("今天").tag(1); Text("7 天").tag(7); Text("30 天").tag(30); Text("90 天").tag(90)
                }.pickerStyle(.segmented)
            }
            if !err.isEmpty { Section { Text(err).foregroundColor(.red).font(.footnote) } }
            let tot = data.o("total")
            Section(header: Text("合计"), footer: Text(data.b("recording") || !loaded ? "数字来自网关返回的 usage，按北京时间分天。" : "服务器已停止记录（AIW_USAGE=0）。")) {
                LabeledContent("总 token", value: Self.fmt(tot.d("total")))
                LabeledContent("输入", value: Self.fmt(tot.d("input")))
                LabeledContent("输出", value: Self.fmt(tot.d("output")))
                LabeledContent("调用次数", value: String(Int(tot.d("calls"))))
            }
            Section("每天") {
                let rows = data.a("per_day")
                let mx = max(1, rows.map { $0.d("total") }.max() ?? 1)
                if loaded && rows.isEmpty { Text("这段时间没有记录").foregroundColor(.secondary) }
                ForEach(rows.indices, id: \.self) { i in
                    let d = rows[i]
                    VStack(alignment: .leading, spacing: 4) {
                        HStack { Text(d.s("day")).font(.subheadline.monospacedDigit()); Spacer(); Text(Self.fmt(d.d("total"))).font(.subheadline.monospacedDigit()) }
                        GeometryReader { g in
                            RoundedRectangle(cornerRadius: 3).fill(amber).frame(width: max(2, g.size.width * d.d("total") / mx), height: 6)
                        }.frame(height: 6)
                        ForEach(d.a("models").indices, id: \.self) { j in
                            let m = d.a("models")[j]
                            HStack { Text(m.s("model")).font(.caption).foregroundColor(.secondary).lineLimit(1); Spacer()
                                Text(Self.fmt(m.d("total"))).font(.caption.monospacedDigit()).foregroundColor(.secondary) }
                        }
                    }.padding(.vertical, 2)
                }
            }
            Section("按模型") {
                ForEach(data.a("per_model").indices, id: \.self) { i in
                    let m = data.a("per_model")[i]
                    LabeledContent(m.s("model"), value: "\(Self.fmt(m.d("input") + m.d("output"))) · \(Int(m.d("calls"))) 次")
                }
            }
            Section("按角色") {
                ForEach(data.a("per_role").indices, id: \.self) { i in
                    let m = data.a("per_role")[i]
                    LabeledContent(roleName(m.s("role")), value: Self.fmt(m.d("input") + m.d("output")))
                }
            }
        }
        .navigationTitle("用量统计").navigationBarTitleDisplayMode(.inline)
        .task(id: days) { await load() }
        .refreshable { await load() }
    }

    func roleName(_ r: String) -> String {
        ["main": "任务主模型", "chat": "聊天", "small": "小模型", "vision": "看图", "subagent": "子代理", "review": "审核", "geometry": "定位", "call": "通话"][r] ?? r
    }

    func load() async {
        do {
            data = (try await api.call("GET", "/api/usage?days=\(days)")) as? JSON ?? [:]
            err = ""
        } catch { err = "\(error.localizedDescription)" }
        loaded = true
    }
}

/// v3.9.24 设置 › 自动签到: the sites, their last result, 立即签到. Adding sites and the history are on the web page.
struct CheckinView: View {
    @EnvironmentObject var api: API
    @State private var sites: [JSON] = []
    @State private var err = ""
    @State private var busy: String? = nil

    var body: some View {
        List {
            Section(footer: Text("第一次由 Agent 签到并录下配方，之后每天自动重放；遇到验证码会请你接管。添加网站请在网页「设置 › 进阶功能 › 自动签到」。")) {
                if sites.isEmpty { Text("还没有网站").foregroundColor(.secondary) }
                ForEach(sites.indices, id: \.self) { i in
                    let s = sites[i]
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(s.s("name")).font(.body.weight(.medium))
                            Spacer()
                            Text(status(s.s("last_status"))).font(.caption).foregroundColor(.secondary)
                        }
                        Text(s.s("last_message")).font(.caption).foregroundColor(.secondary).lineLimit(2)
                        Button(busy == s.s("id") ? "签到中…" : "立即签到") { Task { await run(s.s("id")) } }
                            .font(.caption).disabled(busy != nil)
                    }
                }
            }
            if !err.isEmpty { Section { Text(err).foregroundColor(.red).font(.footnote) } }
        }
        .navigationTitle("自动签到").navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
    }

    func status(_ s: String) -> String {
        ["ok": "✅ 成功", "already": "☑️ 已签过", "failed": "❌ 失败", "replay_failed": "⚠️ 重放失败", "human": "🖐 需要你", "pending": "⏳ 处理中"][s] ?? ""
    }

    func load() async {
        do {
            let r = (try await api.call("GET", "/api/checkin/sites")) as? JSON ?? [:]
            sites = r.a("sites"); err = ""
        } catch { err = "\(error.localizedDescription)" }
    }

    func run(_ id: String) async {
        busy = id
        defer { busy = nil }
        do { _ = try await api.call("POST", "/api/checkin/sites/\(id)/run", [String: Any](), timeout: 180) } catch { err = "\(error.localizedDescription)" }
        await load()
    }
}
