import SwiftUI
import UIKit

/// 京东/淘宝价格监控: list with current price, last check and status, plus a form to add one.
/// Watches with mode "shop" from /api/watches; the server does the checking and the ordering.
struct ShopWatchView: View {
    @EnvironmentObject var api: API
    @State private var items: [JSON] = []
    @State private var showAdd = false
    @State private var error = ""
    @State private var busy: String? = nil
    @State private var machines: [JSON] = []   // 3.8.2: each watch says (and can change) which computer it runs on
    @State private var stage: [String: String] = [:]   // 3.8.6: the running 立即检查 stage per watch
    @State private var lastRun: [String: String] = [:] // 3.8.6: the result of the last 立即检查, with its time
    @State private var diag: DiagTarget? = nil
    @State private var confirmDelete: DiagTarget? = nil   // 3.8.9: 删除 is a button on the card, asked once

    var body: some View {
        List {
            // 3.8.9: each watch is its own inset card (full width on the phone), the explanation below them
            if items.isEmpty { Section { Text("还没有价格监控").foregroundColor(.secondary) } }
            ForEach(items.indices, id: \.self) { i in
                Section {
                    row(items[i])
                        .swipeActions(edge: .trailing) {
                            Button(role: .destructive) { Task { await delete(items[i]) } } label: { Label("删除", systemImage: "trash") }
                            Button { Task { await toggle(items[i]) } } label: {
                                Label(items[i].b("enabled") ? "暂停" : "启用", systemImage: items[i].b("enabled") ? "pause" : "play")
                            }.tint(.gray)
                        }
                        .swipeActions(edge: .leading) {
                            Button { Task { await check(items[i]) } } label: { Label("立即检查", systemImage: "arrow.clockwise") }.tint(amber)
                        }
                }
            }
            Section(footer: Text("在 AIW 电脑上已登录的 Chrome 里读价格（含到手价/券后价）和库存。开了“自动下单”会停在付款前，推送给你确认，你允许后才提交；支付密码由你在接管桌面里输入。也可以在对话里说：帮我盯着这个链接，低于 199 就下单。")) { EmptyView() }
            if !error.isEmpty { Section { Text(error).foregroundColor(.red).font(.footnote) } }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("京东/淘宝价格监控").navigationBarTitleDisplayMode(.inline)
        .toolbar { ToolbarItem(placement: .primaryAction) { Button { showAdd = true } label: { Image(systemName: "plus") } } }
        .sheet(isPresented: $showAdd) { AddShopWatchView { Task { await load() } }.environmentObject(api) }
        .sheet(item: $diag) { t in ShopDiagView(wid: t.id).environmentObject(api) }
        .confirmationDialog("删除这个价格监控？", isPresented: Binding(get: { confirmDelete != nil }, set: { if !$0 { confirmDelete = nil } }),
                            titleVisibility: .visible) {
            Button("删除", role: .destructive) {
                if let t = confirmDelete, let w = items.first(where: { $0.s("id") == t.id }) { Task { await delete(w) } }
                confirmDelete = nil
            }
            Button("取消", role: .cancel) { confirmDelete = nil }
        }
        .refreshable { await load() }
        .task { await load() }
    }

    /// 3.8.9: one full-width card per watch: title (2 lines), chips that wrap, price, meta, note / error box, the
    /// latest 立即检查 line, the computer (full name, a menu) and the actions in a row that wraps.
    @ViewBuilder func row(_ w: JSON) -> some View {
        let site = siteName(w)
        let id = w.s("id")
        let err = shopCN(str(w, "last_error"))
        let st = w.s("shop_status")
        VStack(alignment: .leading, spacing: 8) {
            Text(watchTitle(w)).font(.subheadline.weight(.semibold)).lineLimit(2)
                .fixedSize(horizontal: false, vertical: true).frame(maxWidth: .infinity, alignment: .leading)
            WrapLayout(spacing: 6) {
                chip(site + " ≤ ¥" + str(w, "value"), .secondary)
                if w.b("auto_order") { chip("自动下单", amber) }
                if let ss = shopStatus[st] { chip(ss.0, ss.1) }
                if let os = orderStatus[w.s("order_status")] { chip(os.0, os.1) }
                if !w.b("enabled") { chip("已暂停", Color.secondary) }
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(priceLine(w)).font(.footnote).fixedSize(horizontal: false, vertical: true)
                Text(timeLine(w)).font(.caption).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if let note = statusNote(w, site) {
                box(note, amber)
            } else if !err.isEmpty {
                box(err + "（已失败 \(Int(w.d("fail_count"))) 次）", .red)
            }
            if busy == id {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.mini)
                    Text("正在检查：" + (stage[id] ?? "开始检查")).font(.caption).foregroundColor(amber)
                }
            } else if let l = lastRun[id] {
                Text(shopCN(l)).font(.caption).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Divider()
            Menu {
                ForEach(machines.indices, id: \.self) { j in
                    Button { Task { await setMachine(w, machines[j].s("id")) } } label: {
                        if machines[j].s("id") == w.s("machine_id") { Label(machineName(machines[j].s("id")), systemImage: "checkmark") }
                        else { Text(machineName(machines[j].s("id"))) }
                    }
                }
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "desktopcomputer")
                    Text("检查电脑：").foregroundColor(.secondary)
                    Text(machineName(w.s("machine_id"))).foregroundColor(.primary).lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true).multilineTextAlignment(.leading)
                    Spacer(minLength: 4)
                    Image(systemName: "chevron.up.chevron.down").font(.caption2).foregroundColor(.secondary)
                }.font(.caption)
            }.buttonStyle(.borderless)
            WrapLayout(spacing: 8) {
                Button(busy == id ? "检查中…" : "立即检查") { Task { await check(w) } }.disabled(busy != nil)
                if w.d("diag_at") > 0 { Button("查看诊断") { diag = DiagTarget(id: id) } }
                Button(w.b("enabled") ? "暂停" : "启用") { Task { await toggle(w) } }
                if let u = URL(string: w.s("url")) { Link("打开商品", destination: u) }
                Button("删除", role: .destructive) { confirmDelete = DiagTarget(id: id) }
            }
            .buttonStyle(.bordered).controlSize(.small).font(.caption)
        }.padding(.vertical, 6)
    }

    func box(_ text: String, _ c: Color) -> some View {
        Text(text).font(.caption).foregroundColor(c == amber ? .primary : c).fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 10).padding(.vertical, 7)
            .background(c.opacity(0.10)).overlay(RoundedRectangle(cornerRadius: 8).stroke(c.opacity(0.35)))
            .clipShape(RoundedRectangle(cornerRadius: 8))
    }

    func statusNote(_ w: JSON, _ site: String) -> String? {
        let m = machineName(w.s("machine_id"))
        switch w.s("shop_status") {
        case "login": return "这台电脑的 Chrome 还没登录\(site)：在桌面里点接管，登录一次（扫码即可）。"
        case "captcha": return "「\(m)」上遇到滑块验证：打开这台电脑的桌面点接管，完成一次验证。"
        case "need_login": return shopCN(str(w, "status_note")) + "：在「\(m)」的桌面里接管，登录京东一次（扫码即可）。"
        case "risk_limit": return shopCN(str(w, "status_note")) + "。这不是验证码，检查已自动放慢。"
        case "machine_busy", "machine_offline", "no_machine": return shopCN(str(w, "status_note"))
        default: return nil
        }
    }

    /// Never JD's site-wide title (a product page that did not render): the user's own name instead.
    func watchTitle(_ w: JSON) -> String {
        let t = str(w, "item_title").trimmingCharacters(in: .whitespaces)
        return t.isEmpty || isGenericJDTitle(t) ? str(w, "name") : t
    }

    func machineName(_ id: String) -> String {
        if id.isEmpty { return "未指定" }
        let m = machines.first { $0.s("id") == id }
        return (m?.s("name").isEmpty ?? true) ? id : m!.s("name")
    }
    func setMachine(_ w: JSON, _ mid: String) async {
        var body: JSON = [:]
        for k in ["name", "url", "mode", "value", "sku", "address_note"] { if let v = w[k] as? String { body[k] = v } }
        body["value"] = str(w, "value")
        body["interval_min"] = Int(w.d("interval_min")); body["quantity"] = max(1, Int(w.d("quantity")))
        body["auto_order"] = w.b("auto_order"); body["enabled"] = w.b("enabled"); body["machine_id"] = mid
        do { _ = try await api.obj("PUT", "/api/watches/\(w.s("id"))", body) } catch { self.error = error.localizedDescription }
        await load()
    }
    func str(_ w: JSON, _ k: String) -> String {
        if let v = w[k] as? String { return v }
        if let v = w[k] as? NSNumber { return v.stringValue }
        return ""
    }
    func siteName(_ w: JSON) -> String {
        let names: [String: String] = ["jd": "京东", "taobao": "淘宝", "tmall": "天猫"]
        return names[str(w, "site")] ?? "电商"
    }
    func timeLine(_ w: JSON) -> String {
        let last = when(w.d("last_checked"))
        var s = "每 \(str(w, "interval_min")) 分钟 · 上次 " + (last.isEmpty ? "—" : last)
        if w.d("triggered_at") > 0 { s += " · 触发于 " + when(w.d("triggered_at")) }
        return s
    }

    func chip(_ t: String, _ c: Color) -> some View {
        Text(t).font(.caption2).padding(.horizontal, 6).padding(.vertical, 2)
            .background(c.opacity(0.15)).foregroundColor(c).clipShape(Capsule())
    }

    func yuan(_ v: Any?) -> String {
        guard let n = v as? NSNumber else { return "—" }
        let d = n.doubleValue
        return d == d.rounded() ? "¥\(Int(d))" : String(format: "¥%.2f", d)
    }

    func priceLine(_ w: JSON) -> String {
        let qty = max(1, Int(w.d("quantity")))
        if w.d("last_checked") <= 0 { return "等待首次检查 · 数量 \(qty)" }
        var s: String
        if w["final_price"] is NSNumber {
            s = "到手 " + yuan(w["final_price"]) + "（标价 " + yuan(w["price"]) + "）"
        } else {
            s = "现价 " + yuan(w["price"])
        }
        if let st = w["in_stock"] as? NSNumber { s += st.intValue != 0 ? " · 有货" : " · 无货" }
        s += " · 数量 \(qty)"
        let sku = str(w, "sku")
        if !sku.isEmpty { s += " · " + sku }
        return s
    }

    let shopStatus: [String: (String, Color)] = ["ok": ("正常", Color.green), "login": ("需要登录", amber), "captcha": ("需要验证", amber),
        "off_shelf": ("已下架", Color.red), "no_price": ("读不到价格", amber), "error": ("出错", Color.red), "no_browser": ("浏览器未就绪", amber),
        "machine_busy": ("等电脑空闲", amber), "machine_offline": ("等电脑上线", amber), "no_machine": ("电脑不在了", Color.red),
        "need_login": ("需要登录京东", amber), "risk_limit": ("京东风控限制", amber), "price_pending": ("未开价", amber)]
    let orderStatus: [String: (String, Color)] = ["running": ("下单中", amber), "waiting_confirm": ("等你确认付款", amber),
        "submitted": ("已提交订单", Color.green), "rejected": ("已拒绝", Color.secondary), "timeout": ("超时已取消", Color.secondary),
        "above_target": ("实付超目标", amber), "submit_failed": ("提交失败", Color.red), "failed": ("下单失败", Color.red)]

    func load() async {
        if machines.isEmpty { machines = (try? await api.list("/api/machines")) ?? [] }
        do { items = try await api.list("/api/watches").filter { $0.s("mode") == "shop" }; error = "" }
        catch { self.error = error.localizedDescription }
    }
    func check(_ w: JSON) async {
        let id = w.s("id")
        if busy != nil { return }
        busy = id
        stage[id] = "开始检查"
        let res = await api.runWatchCheck(id) { s in stage[id] = s }
        let f = DateFormatter(); f.dateFormat = "HH:mm:ss"
        lastRun[id] = "刚才 \(f.string(from: Date())) 立即检查：" + shopCN(res.s("label").replacingOccurrences(of: "完成：", with: ""))
        busy = nil
        await load()
    }
    func toggle(_ w: JSON) async {
        var body: JSON = [:]
        for k in ["name", "url", "mode", "value", "sku", "address_note", "machine_id"] { if let v = w[k] as? String { body[k] = v } }
        body["value"] = str(w, "value")   // a number in the row would otherwise be dropped
        body["interval_min"] = Int(w.d("interval_min")); body["quantity"] = max(1, Int(w.d("quantity")))
        body["auto_order"] = w.b("auto_order"); body["enabled"] = !w.b("enabled")
        do { _ = try await api.obj("PUT", "/api/watches/\(w.s("id"))", body) } catch { self.error = error.localizedDescription }
        await load()
    }
    func delete(_ w: JSON) async {
        do { _ = try await api.call("DELETE", "/api/watches/\(w.s("id"))") } catch { self.error = error.localizedDescription }
        await load()
    }
}

struct AddShopWatchView: View {
    @EnvironmentObject var api: API
    @Environment(\.dismiss) var dismiss
    var done: () -> Void
    init(done: @escaping () -> Void) { self.done = done }
    @State private var url = ""
    @State private var target = ""
    @State private var quantity = 1
    @State private var sku = ""
    @State private var address = ""
    @State private var interval = 30
    @State private var autoOrder = false
    @State private var machine = ""
    @State private var machines: [JSON] = []
    @State private var error = ""
    @State private var saving = false

    var body: some View {
        NavigationStack {
            Form {
                Section(header: Text("商品")) {
                    TextField("商品链接或分享文本", text: $url, axis: .vertical).lineLimit(1...3)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                    if url.isEmpty {
                        Button { if let s = UIPasteboard.general.string { url = s } } label: { Label("从剪贴板粘贴", systemImage: "doc.on.clipboard") }
                    }
                    TextField("目标单价（元）", text: $target).keyboardType(.decimalPad)
                    Stepper("数量 \(quantity)", value: $quantity, in: 1...99)
                    TextField("规格（可空），如 黑色 256GB", text: $sku)
                    TextField("收货地址（可空 = 默认地址）", text: $address)
                }
                Section(header: Text("检查")) {
                    Picker("间隔", selection: $interval) {
                        Text("10 分钟").tag(10); Text("15 分钟").tag(15); Text("30 分钟").tag(30)
                        Text("1 小时").tag(60); Text("2 小时").tag(120); Text("6 小时").tag(360)
                    }
                    Picker("电脑", selection: $machine) {
                        ForEach(machines.indices, id: \.self) { i in
                            Text(machines[i].s("name").isEmpty ? machines[i].s("id") : machines[i].s("name")).tag(machines[i].s("id"))
                        }
                    }
                }
                Section(footer: Text("降到目标价时，AIW 会把商品放进订单、停在付款前，推送给你确认；你允许后才提交订单。实付超过目标价不会下单。")) {
                    Toggle("降价后自动下单", isOn: $autoOrder).tint(amber)
                }
                if !error.isEmpty { Section { Text(error).foregroundColor(.red).font(.footnote) } }
            }
            .navigationTitle("添加价格监控").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "保存中…" : "添加") { Task { await save() } }.disabled(url.isEmpty || target.isEmpty || saving)
                }
            }
            .task {
                machines = (try? await api.list("/api/machines")) ?? []
                // 3.8.2: default to a local computer (home IP, logged-in Chrome), not whichever one is selected now
                if machine.isEmpty {
                    machine = machines.first { $0.s("provider") != "instabox" && !$0.s("id").hasPrefix("cloud-") }?.s("id") ?? api.machine
                }
            }
        }
    }

    func save() async {
        saving = true
        defer { saving = false }
        let body: JSON = ["mode": "shop", "url": url.trimmingCharacters(in: .whitespacesAndNewlines), "value": target,
                          "quantity": quantity, "sku": sku, "address_note": address, "interval_min": interval,
                          "auto_order": autoOrder, "machine_id": machine]
        do { _ = try await api.obj("POST", "/api/watches", body); done(); dismiss() }
        catch { self.error = error.localizedDescription }
    }
}

/// 3.8.9: JD's site-wide title ('京东(JD.COM)-正品低价、品质保障、配送及时、轻松购物!') is never a product name.
func isGenericJDTitle(_ t: String) -> Bool {
    let s = t.trimmingCharacters(in: .whitespaces)
    if s.isEmpty { return true }
    if s.contains("正品低价、品质保障、配送及时、轻松购物") || s.contains("多快好省") { return true }
    return ["京东", "京东(JD.COM)", "京东（JD.COM）", "JD.COM", "京东商城"].contains(s)
}

/// 3.8.9: errors stored by older servers in English, in plain Chinese (new servers translate them already).
func shopCN(_ t: String) -> String {
    var s = t
    let rules: [(String, String)] = [
        ("probe failed: timeout after (\\d+)s?", "检查超时（$1 秒没有结束）"),
        ("timeout after (\\d+(?:\\.\\d+)?)s?", "超时（$1 秒）"),
        ("^probe failed:\\s*", "检查失败："),
        ("NO_CDP:?\\s*", "连不上浏览器调试端口："),
        ("Failed to fetch", "请求被浏览器拦下")]
    for (p, r) in rules {
        s = s.replacingOccurrences(of: p, with: r, options: [.regularExpression, .caseInsensitive])
    }
    return s
}
