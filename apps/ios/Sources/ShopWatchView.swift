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

    var body: some View {
        List {
            Section(footer: Text("在 AIW 电脑上已登录的 Chrome 里读价格（含到手价/券后价）和库存。开了“自动下单”会停在付款前，推送给你确认，你允许后才提交；支付密码由你在接管桌面里输入。也可以在对话里说：帮我盯着这个链接，低于 199 就下单。")) {
                if items.isEmpty { Text("还没有价格监控").foregroundColor(.secondary) }
                ForEach(items.indices, id: \.self) { i in
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
            if !error.isEmpty { Section { Text(error).foregroundColor(.red).font(.footnote) } }
        }
        .navigationTitle("京东/淘宝价格监控").navigationBarTitleDisplayMode(.inline)
        .toolbar { ToolbarItem(placement: .primaryAction) { Button { showAdd = true } label: { Image(systemName: "plus") } } }
        .sheet(isPresented: $showAdd) { AddShopWatchView { Task { await load() } }.environmentObject(api) }
        .refreshable { await load() }
        .task { await load() }
    }

    @ViewBuilder func row(_ w: JSON) -> some View {
        let site = siteName(w)
        let title = str(w, "item_title").isEmpty ? str(w, "name") : str(w, "item_title")
        let checkLine = timeLine(w)
        let err = str(w, "last_error")
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(title).font(.subheadline.weight(.semibold)).lineLimit(2)
                Spacer(minLength: 4)
                if busy == w.s("id") { ProgressView().scaleEffect(0.7) }
            }
            HStack(spacing: 6) {
                chip(site + " ≤ ¥" + str(w, "value"), .secondary)
                if w.b("auto_order") { chip("自动下单", amber) }
                if let st = shopStatus[w.s("shop_status")] { chip(st.0, st.1) }
                if let os = orderStatus[w.s("order_status")] { chip(os.0, os.1) }
                if !w.b("enabled") { chip("已暂停", Color.secondary) }
            }
            Text(priceLine(w)).font(.footnote)
            Text(checkLine).font(.caption).foregroundColor(.secondary)
            if w.s("shop_status") == "login" {
                Text("这台电脑的 Chrome 还没登录\(site)：在桌面里点接管，登录一次（扫码即可）。").font(.caption).foregroundColor(amber)
            } else if w.s("shop_status") == "captcha" {
                Text("遇到滑块验证：在桌面里点接管，完成一次验证。").font(.caption).foregroundColor(amber)
            } else if !err.isEmpty {
                Text(err).font(.caption).foregroundColor(.red).lineLimit(2)
            }
            if let u = URL(string: w.s("url")) { Link("打开商品", destination: u).font(.caption) }
        }.padding(.vertical, 2)
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
        "off_shelf": ("已下架", Color.red), "no_price": ("读不到价格", amber), "error": ("出错", Color.red), "no_browser": ("浏览器未就绪", amber)]
    let orderStatus: [String: (String, Color)] = ["running": ("下单中", amber), "waiting_confirm": ("等你确认付款", amber),
        "submitted": ("已提交订单", Color.green), "rejected": ("已拒绝", Color.secondary), "timeout": ("超时已取消", Color.secondary),
        "above_target": ("实付超目标", amber), "submit_failed": ("提交失败", Color.red), "failed": ("下单失败", Color.red)]

    func load() async {
        do { items = try await api.list("/api/watches").filter { $0.s("mode") == "shop" }; error = "" }
        catch { self.error = error.localizedDescription }
    }
    func check(_ w: JSON) async {
        busy = w.s("id")
        do { _ = try await api.obj("POST", "/api/watches/\(w.s("id"))/check") } catch { self.error = error.localizedDescription }
        busy = nil
        await load()
    }
    func toggle(_ w: JSON) async {
        var body: JSON = [:]
        for k in ["name", "url", "mode", "value", "sku", "address_note", "machine_id"] { if let v = w[k] as? String { body[k] = v } }
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
                if machine.isEmpty { machine = api.machine }
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
