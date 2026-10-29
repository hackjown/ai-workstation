import SwiftUI

/// v6 (3.8.6): 立即检查 runs as a job on the server. POST /api/watches/{id}/check?async=1 answers at once with a job id;
/// GET /api/watches/{id}/check/{job} shows the stage (推送探针 → 连接浏览器 → 打开商品页 → 等待价格渲染 → 读取价格 → 完成)
/// until it is done. onStage is called on the main actor with each new stage text.
extension API {
    @MainActor
    func runWatchCheck(_ wid: String, onStage: @escaping (String) -> Void) async -> JSON {
        var job: JSON
        do {
            job = try await obj("POST", "/api/watches/\(wid)/check?async=1").o("job")
        } catch {
            return ["ok": false, "note": error.localizedDescription, "label": "没能开始：" + error.localizedDescription]
        }
        if job.s("id").isEmpty { return ["ok": false, "note": "服务器太旧，不支持检查进度（请更新服务器）", "label": "服务器太旧"] }
        onStage(job.s("label").isEmpty ? "开始检查" : job.s("label"))
        var i = 0
        while job.s("state") != "done" && i < 400 {
            i += 1
            try? await Task.sleep(nanoseconds: 1_200_000_000)
            do {
                job = try await obj("GET", "/api/watches/\(wid)/check/\(job.s("id"))")
                onStage(job.s("label").isEmpty ? job.s("stage") : job.s("label"))
            } catch {
                if error.localizedDescription.contains("没有这个检查") { break }
            }
        }
        var res = job.o("result")
        let label = job.s("label").replacingOccurrences(of: "完成：", with: "")
        res["label"] = job.s("state") == "done" ? label : "检查没有结束（服务器重启过？）"
        return res
    }
}

struct DiagTarget: Identifiable { let id: String }

/// 查看诊断: the last check's screenshot, final URL, page title, text near the price, probe version and stage timings.
struct ShopDiagView: View {
    @EnvironmentObject var api: API
    @Environment(\.dismiss) var dismiss
    let wid: String
    @State private var d: JSON = [:]
    @State private var error = ""

    var body: some View {
        NavigationStack {
            List {
                if !error.isEmpty { Section { Text(error).foregroundColor(.red).font(.footnote) } }
                if !d.isEmpty {
                    Section(header: Text("结论").textCase(nil)) {
                        line("状态", d.s("status"))
                        line("说明", d.s("note"))
                        ForEach(strings("hints"), id: \.self) { Text($0).font(.footnote).foregroundColor(amber) }
                        line("探针", "v\(d.s("probe_version"))（服务器带 v\(d.s("server_probe_version"))）· 服务器 \(d.s("server_version"))")
                    }
                    if d.b("screenshot"), let u = api.url("/api/watches/\(wid)/diag.jpg",
                                                          query: [URLQueryItem(name: "token", value: api.token),
                                                                  URLQueryItem(name: "t", value: String(Int(d.d("at"))))]) {
                        Section(header: Text("截图").textCase(nil)) {
                            AsyncImage(url: u) { img in img.resizable().scaledToFit() } placeholder: { ProgressView() }
                        }
                    }
                    Section(header: Text("页面").textCase(nil)) {
                        line("最终网址", d.s("final_url"))
                        line("页面标题", d.s("page_title"))
                        line("标题来自", d.s("title_from"))
                        line("页面状态", [d.s("ready"), d.s("visibility")].filter { !$0.isEmpty && $0 != "<null>" }.joined(separator: " · "))
                        line("元素数", d.s("n_elems"))
                        ForEach(strings("snippets"), id: \.self) { Text($0).font(.caption).textSelection(.enabled) }
                    }
                    Section(header: Text("价格来源").textCase(nil)) {
                        ForEach(d.a("net").indices, id: \.self) { i in
                            let n = d.a("net")[i]
                            Text(n.s("src") + " " + n.s("status") + (n.b("price") ? " ✓" : "") + " " + n.s("error")).font(.caption)
                        }
                        ForEach(d.a("api").indices, id: \.self) { i in
                            let a = d.a("api")[i]
                            Text(a.s("src") + " " + (a.s("status").isEmpty ? a.s("error") : a.s("status"))).font(.caption)
                        }
                        if !d.o("mobile").isEmpty {
                            line("移动版", d.o("mobile").s("skipped").isEmpty ? d.o("mobile").s("url") : "跳过：" + d.o("mobile").s("skipped"))
                        }
                    }
                    Section(header: Text("阶段耗时").textCase(nil)) {
                        let st = d.a("probe_stages").isEmpty ? d.a("stages") : d.a("probe_stages")
                        ForEach(st.indices, id: \.self) { i in
                            line(st[i].s("label"), st[i]["ms"] == nil ? "" : String(format: "%.1f 秒", st[i].d("ms") / 1000))
                        }
                    }
                }
            }
            .navigationTitle("诊断").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
            .task {
                do { d = try await api.obj("GET", "/api/watches/\(wid)/diag"); error = "" }
                catch { self.error = error.localizedDescription }
            }
        }
    }

    func strings(_ k: String) -> [String] { (d[k] as? [Any])?.map { "\($0)" } ?? [] }

    @ViewBuilder func line(_ k: String, _ v: String) -> some View {
        if !v.isEmpty && v != "<null>" {
            HStack(alignment: .top) {
                Text(k).font(.caption).foregroundColor(.secondary).frame(width: 72, alignment: .leading)
                Text(v).font(.caption).textSelection(.enabled)
            }
        }
    }
}
