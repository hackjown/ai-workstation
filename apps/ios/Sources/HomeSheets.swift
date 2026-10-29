import SwiftUI

// iOS v2 home sheets 追踪 / 例程 / 电脑. They only use endpoints the server already has:
//   追踪  GET/POST/PUT/DELETE /api/watches (+ /check)         — 商品价格 (mode shop) and 网页变化 (change/contains/price_below)
//   例程  GET/POST/PUT/DELETE /api/routines (+ /run), GET /api/xhs (小红书运营 is shown, edited in its own screen)
//   电脑  GET /api/machines (+ /{id} for status), PUT /api/machines/{id}/name, GET/PUT /api/browseros

/// A square tinted icon tile used by list rows.
struct IconTile: View {
    let symbol: String
    let color: Color
    var body: some View {
        Image(systemName: symbol).font(.system(size: 17, weight: .medium)).foregroundColor(color)
            .frame(width: 40, height: 40).background(color.opacity(0.12))
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}

/// "M/d HH:mm", "HH:mm" today, "明天 HH:mm".
func dayTime(_ ts: Double) -> String {
    guard ts > 0 else { return "" }
    let d = Date(timeIntervalSince1970: ts)
    let cal = Calendar.current
    let f = DateFormatter(); f.locale = Locale(identifier: "zh_CN")
    f.dateFormat = "HH:mm"
    if cal.isDateInToday(d) { return "今天 " + f.string(from: d) }
    if cal.isDateInTomorrow(d) { return "明天 " + f.string(from: d) }
    if cal.isDateInYesterday(d) { return "昨天 " + f.string(from: d) }
    f.dateFormat = "M/d HH:mm"
    return f.string(from: d)
}

func everyMinutes(_ m: Int) -> String {
    if m >= 1440 && m % 1440 == 0 { return m == 1440 ? "每天" : "每 \(m / 1440) 天" }
    if m >= 60 && m % 60 == 0 { return "每 \(m / 60) 小时" }
    return "每 \(m) 分钟"
}

// MARK: - 追踪

struct TrackingSheet: View {
    @EnvironmentObject var api: API
    let close: () -> Void
    @State private var items: [JSON] = []
    @State private var error = ""
    @State private var busy: String? = nil
    @State private var stage = ""   // 3.8.6: the running 立即检查 stage
    @State private var showShop = false
    @State private var showPage = false
    @State private var loaded = false
    @ObservedObject private var links = DeepLinks.shared

    var shop: [JSON] { items.filter { $0.s("mode") == "shop" } }
    /// 3.8 aiw://monitor/<id> (a price / page watch push): that watch on top with 立即重试
    var focused: JSON? { links.focusWatch.flatMap { id in items.first { $0.s("id") == id } } }
    var pages: [JSON] { items.filter { $0.s("mode") != "shop" } }

    var body: some View {
        SheetChrome("追踪", close: close, trailing: {
            Menu {
                Button { showShop = true } label: { Label("商品价格（京东/淘宝）", systemImage: "cart") }
                Button { showPage = true } label: { Label("网页变化", systemImage: "globe") }
            } label: { Label("添加", systemImage: "plus").font(.subheadline.weight(.semibold)) }
        }) {
            List {
                if !error.isEmpty { Section { Text(error).foregroundColor(.red).font(.footnote) } }
                if let w = focused {
                    Section(header: Text("通知里的这一项").textCase(nil),
                            footer: Text(w.s("last_error").isEmpty ? "" : w.s("last_error")).foregroundColor(.red)) {
                        row(w).listRowBackground(amber.opacity(0.10))
                        Button { Task { await check(w) } } label: { Label("立即重试", systemImage: "arrow.clockwise") }
                            .disabled(busy == w.s("id"))
                        if let u = URL(string: w.s("url")), !w.s("url").isEmpty { Link(destination: u) { Label("打开商品 / 网页", systemImage: "safari") } }
                    }
                }
                Section(header: Text("商品价格").textCase(nil)) {
                    if shop.isEmpty && loaded { Text("还没有价格监控，点右上角「添加」").font(.subheadline).foregroundColor(.secondary) }
                    ForEach(shop.indices, id: \.self) { i in row(shop[i]) }
                }
                Section(header: Text("网页变化").textCase(nil),
                        footer: Text("价格从电脑上已登录的浏览器读取，到目标价时推送通知；网页按间隔检查，有变化时通知你。左滑可立即检查或删除。")) {
                    if pages.isEmpty && loaded { Text("还没有网页追踪").font(.subheadline).foregroundColor(.secondary) }
                    ForEach(pages.indices, id: \.self) { i in row(pages[i]) }
                }
                Section { NavigationLink { ShopWatchView() } label: { Label("价格监控详情与下单状态", systemImage: "tag") } }
            }
            .listStyle(.insetGrouped)
            .refreshable { await load() }
        }
        .task { await load() }
        .onDisappear { links.focusWatch = nil }
        .sheet(isPresented: $showShop) { AddShopWatchView { Task { await load() } } }
        .sheet(isPresented: $showPage) { AddPageWatchView { Task { await load() } } }
    }

    @ViewBuilder func row(_ w: JSON) -> some View {
        let isShop = w.s("mode") == "shop"
        HStack(spacing: 12) {
            IconTile(symbol: isShop ? "cart" : "globe", color: isShop ? (w.s("site") == "jd" ? .red : .orange) : .blue)
            VStack(alignment: .leading, spacing: 4) {
                Text(title(w)).font(.subheadline.weight(.semibold)).lineLimit(1)
                subtitle(w)
                HStack(spacing: 6) {
                    Chip(text: everyMinutes(max(1, Int(w.d("interval_min")))), color: amber)
                    Chip(text: checkText(w), color: w.s("last_error").isEmpty ? .secondary : .red)
                    if busy == w.s("id") { ProgressView().controlSize(.mini) }
                }
                if busy == w.s("id") && !stage.isEmpty { Text(stage).font(.caption).foregroundColor(amber).lineLimit(1) }
            }
            Spacer(minLength: 6)
            Toggle("", isOn: Binding(get: { w.b("enabled") }, set: { on in Task { await setEnabled(w, on) } }))
                .labelsHidden().tint(.green)
        }
        .padding(.vertical, 3)
        .swipeActions(edge: .trailing) {
            Button(role: .destructive) { Task { await delete(w) } } label: { Label("删除", systemImage: "trash") }
            Button { Task { await check(w) } } label: { Label("立即检查", systemImage: "arrow.clockwise") }.tint(amber)
        }
        .contextMenu { if let u = URL(string: w.s("url")), !w.s("url").isEmpty { Link(destination: u) { Label("打开网页", systemImage: "safari") } } }
    }

    func title(_ w: JSON) -> String {
        if w.s("mode") == "shop" {
            let site = ["jd": "京东", "taobao": "淘宝", "tmall": "天猫"][w.s("site")] ?? "电商"
            let t = w.s("item_title").isEmpty ? w.s("name") : w.s("item_title")
            return site + " · " + (t.isEmpty ? "商品" : t)
        }
        return w.s("name").isEmpty ? w.s("url") : w.s("name")
    }
    func num(_ v: Any?) -> Double? { (v as? NSNumber)?.doubleValue ?? (v as? String).flatMap(Double.init) }
    func yuan(_ d: Double) -> String { d == d.rounded() ? "¥\(Int(d))" : String(format: "¥%.2f", d) }

    @ViewBuilder func subtitle(_ w: JSON) -> some View {
        if w.s("mode") == "shop" {
            let target = num(w["value"])
            let cur = num(w["final_price"]) ?? num(w["price"])
            if let c = cur, let t = target, c <= t {
                (Text("已到目标价 ").foregroundColor(okGreen) + Text(yuan(c) + (w.d("triggered_at") > 0 ? " · 已推送通知" : "")).foregroundColor(.secondary))
                    .font(.caption).lineLimit(1)
            } else {
                Text((cur.map { "当前 " + yuan($0) } ?? "等待首次检查") + (target.map { " · 目标 ≤ " + yuan($0) } ?? ""))
                    .font(.caption).foregroundColor(.secondary).lineLimit(1)
            }
        } else {
            let mode = w.s("mode")
            let txt = mode == "contains" ? "页面出现「\(w.s("value"))」时通知我" : mode == "price_below" ? "价格低于 \(w.s("value")) 时通知我" : "内容有变化时通知我"
            Text(txt).font(.caption).foregroundColor(.secondary).lineLimit(1)
        }
    }
    func checkText(_ w: JSON) -> String {
        if !w.b("enabled") { return "已暂停" }
        if !w.s("last_error").isEmpty { return "检查出错" }
        let last = w.d("last_checked")
        if last <= 0 { return "未检查" }
        if Date().timeIntervalSince1970 - last < 120 { return "刚刚检查" }
        return when(last) + " 检查"
    }

    func load() async {
        do { items = try await api.list("/api/watches"); error = "" } catch { self.error = error.localizedDescription }
        loaded = true
    }
    func check(_ w: JSON) async {
        let id = w.s("id")
        if busy != nil { return }
        busy = id
        stage = "开始检查"
        let res = await api.runWatchCheck(id) { s in stage = s }   // 3.8.6: live stage under the row
        stage = ""
        if !res.b("ok") { self.error = res.s("label").isEmpty ? res.s("note") : res.s("label") } else { self.error = "" }
        busy = nil
        await load()
    }
    /// PUT takes the whole watch: send it back with only `enabled` changed (same as ShopWatchView).
    func setEnabled(_ w: JSON, _ on: Bool) async {
        var body: JSON = [:]
        for k in ["name", "url", "mode", "value", "sku", "address_note", "machine_id"] { if let v = w[k] as? String { body[k] = v } }
        body["interval_min"] = Int(w.d("interval_min")); body["enabled"] = on
        if w.s("mode") == "shop" { body["quantity"] = max(1, Int(w.d("quantity"))); body["auto_order"] = w.b("auto_order") }
        do { _ = try await api.obj("PUT", "/api/watches/\(w.s("id"))", body) } catch { self.error = error.localizedDescription }
        await load()
    }
    func delete(_ w: JSON) async {
        do { _ = try await api.call("DELETE", "/api/watches/\(w.s("id"))") } catch { self.error = error.localizedDescription }
        await load()
    }
}

struct AddPageWatchView: View {
    @EnvironmentObject var api: API
    @Environment(\.dismiss) var dismiss
    var done: () -> Void
    init(done: @escaping () -> Void) { self.done = done }
    @State private var name = ""
    @State private var url = ""
    @State private var mode = "change"
    @State private var value = ""
    @State private var interval = 60
    @State private var error = ""
    @State private var saving = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("名称，如 InstaBox 价格页", text: $name)
                    TextField("网址 https://…", text: $url).keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                }
                Section(header: Text("什么时候通知")) {
                    Picker("条件", selection: $mode) {
                        Text("内容有变化").tag("change"); Text("出现某段文字").tag("contains"); Text("价格低于").tag("price_below")
                    }
                    if mode != "change" { TextField(mode == "contains" ? "要出现的文字" : "价格", text: $value).keyboardType(mode == "price_below" ? .decimalPad : .default) }
                    Picker("间隔", selection: $interval) {
                        Text("15 分钟").tag(15); Text("30 分钟").tag(30); Text("1 小时").tag(60); Text("6 小时").tag(360); Text("每天").tag(1440)
                    }
                }
                if !error.isEmpty { Section { Text(error).foregroundColor(.red).font(.footnote) } }
            }
            .navigationTitle("添加网页追踪").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "保存中…" : "添加") { Task { await save() } }
                        .disabled(name.isEmpty || url.isEmpty || (mode != "change" && value.isEmpty) || saving)
                }
            }
        }
    }
    func save() async {
        saving = true; defer { saving = false }
        let body: JSON = ["name": name, "url": url.trimmingCharacters(in: .whitespacesAndNewlines), "mode": mode, "value": value, "interval_min": interval]
        do { _ = try await api.obj("POST", "/api/watches", body); done(); dismiss() } catch { self.error = error.localizedDescription }
    }
}

// MARK: - 例程

/// "daily 09:00" / "every 30m" / "every 2h" / 5-field cron -> 每天 09:00, 每 30 分钟, 每周五 18:00, 工作日 09:00…
func scheduleText(_ s: String) -> String {
    let t = s.trimmingCharacters(in: .whitespaces)
    let low = t.lowercased()
    if low.hasPrefix("daily ") { return "每天 " + t.dropFirst(6) }
    if low.hasPrefix("every ") {
        let v = low.dropFirst(6)
        if v.hasSuffix("m"), let n = Int(v.dropLast()) { return everyMinutes(n) }
        if v.hasSuffix("h"), let n = Int(v.dropLast()) { return "每 \(n) 小时" }
    }
    let f = t.split(separator: " ").map(String.init)
    guard f.count == 5, let mi = Int(f[0]), let h = Int(f[1]), f[2] == "*", f[3] == "*" else { return t }
    let hm = String(format: "%02d:%02d", h, mi)
    let days = ["日", "一", "二", "三", "四", "五", "六", "日"]
    switch f[4] {
    case "*": return "每天 " + hm
    case "1-5": return "工作日 " + hm
    case "0,6", "6,0", "6-7": return "周末 " + hm
    default:
        if let d = Int(f[4]), d >= 0, d <= 7 { return "每周" + days[d] + " " + hm }
        return t
    }
}

struct RoutineSheet: View {
    @EnvironmentObject var api: API
    let open: (String) -> Void
    let close: () -> Void
    @State private var items: [JSON] = []
    @State private var xhs: JSON = [:]
    @State private var error = ""
    @State private var showAdd = false
    @State private var running: String? = nil
    @State private var loaded = false

    var on: [JSON] { items.filter { $0.b("enabled") } }
    var off: [JSON] { items.filter { !$0.b("enabled") } }

    var body: some View {
        SheetChrome("例程", close: close, trailing: {
            Button { showAdd = true } label: { Label("新建", systemImage: "plus").font(.subheadline.weight(.semibold)) }
        }) {
            List {
                if !error.isEmpty { Section { Text(error).foregroundColor(.red).font(.footnote) } }
                Section(header: Text("已启用").textCase(nil)) {
                    if on.isEmpty && loaded { Text("还没有启用的例程，点右上角「新建」").font(.subheadline).foregroundColor(.secondary) }
                    ForEach(on.indices, id: \.self) { i in row(on[i]) }
                    if !xhs.isEmpty && xhs.o("settings").b("enabled") { xhsRow }
                }
                if !off.isEmpty || (!xhs.isEmpty && !xhs.o("settings").b("enabled")) {
                    Section(header: Text("已暂停").textCase(nil)) {
                        ForEach(off.indices, id: \.self) { i in row(off[i]) }
                        if !xhs.isEmpty && !xhs.o("settings").b("enabled") { xhsRow }
                    }
                }
                Section(footer: Text("例程按时在选定电脑上新建任务，结果进入历史对话。点一条打开它最近的对话，左滑可立即运行或删除。")) { EmptyView() }
            }
            .listStyle(.insetGrouped)
            .refreshable { await load() }
        }
        .task { await load() }
        .sheet(isPresented: $showAdd) { AddRoutineView { Task { await load() } } }
    }

    var xhsRow: some View {
        let s = xhs.o("settings"), w = s.o("window")
        return NavigationLink { XhsView() } label: {
            HStack(spacing: 12) {
                IconTile(symbol: "sparkle", color: .red)
                VStack(alignment: .leading, spacing: 4) {
                    Text("小红书运营").font(.subheadline.weight(.semibold))
                    Text("浏览、点赞，评论先进审核队列").font(.caption).foregroundColor(.secondary).lineLimit(1)
                    HStack(spacing: 6) {
                        Chip(text: "每天 \(w.s("start"))–\(w.s("end"))", color: amber)
                        Chip(text: s.b("enabled") ? "随机开始" : "已关闭", color: .secondary)
                    }
                }
            }.padding(.vertical, 3)
        }
    }

    @ViewBuilder func row(_ r: JSON) -> some View {
        let tid = r.s("thread_id")
        Button { if !tid.isEmpty && tid != "<null>" { open(tid) } } label: {
            HStack(spacing: 12) {
                IconTile(symbol: icon(r), color: r.b("enabled") ? amber : .secondary)
                VStack(alignment: .leading, spacing: 4) {
                    Text(r.s("name")).font(.subheadline.weight(.semibold)).foregroundColor(.primary).lineLimit(1)
                    Text(cleanTitle(r.s("prompt"))).font(.caption).foregroundColor(.secondary).lineLimit(1)
                    HStack(spacing: 6) {
                        Chip(text: scheduleText(r.s("schedule")), color: amber)
                        Chip(text: r.b("enabled") ? (r.d("next_run") > 0 ? "下次 " + dayTime(r.d("next_run")) : "待排期") : "已暂停", color: .secondary)
                        if running == r.s("id") { ProgressView().controlSize(.mini) }
                    }
                }
                Spacer(minLength: 6)
                Toggle("", isOn: Binding(get: { r.b("enabled") }, set: { v in Task { await setEnabled(r, v) } }))
                    .labelsHidden().tint(.green)
            }.padding(.vertical, 3)
        }
        .buttonStyle(.plain)
        .swipeActions(edge: .trailing) {
            Button(role: .destructive) { Task { await delete(r) } } label: { Label("删除", systemImage: "trash") }
            Button { Task { await run(r) } } label: { Label("立即运行", systemImage: "play") }.tint(amber)
        }
    }

    func icon(_ r: JSON) -> String {
        let t = r.s("name") + r.s("prompt")
        if t.contains("报") || t.contains("新闻") { return "doc.text" }
        if t.contains("清理") || t.contains("删除") { return "trash" }
        if t.contains("备份") { return "externaldrive" }
        return "arrow.triangle.2.circlepath"
    }

    func load() async {
        do { items = try await api.list("/api/routines"); error = "" } catch { self.error = error.localizedDescription }
        xhs = (try? await api.obj("GET", "/api/xhs")) ?? [:]
        loaded = true
    }
    func setEnabled(_ r: JSON, _ on: Bool) async {
        var body: JSON = ["name": r.s("name"), "prompt": r.s("prompt"), "schedule": r.s("schedule"), "enabled": on]
        let m = r.s("machine_id"); if !m.isEmpty && m != "<null>" { body["machine_id"] = m }
        do { _ = try await api.obj("PUT", "/api/routines/\(r.s("id"))", body) } catch { self.error = error.localizedDescription }
        await load()
    }
    func run(_ r: JSON) async {
        running = r.s("id")
        do {
            let res = try await api.obj("POST", "/api/routines/\(r.s("id"))/run")
            running = nil
            let tid = res.s("thread_id")
            if !tid.isEmpty { open(tid); return }
        } catch { self.error = error.localizedDescription }
        running = nil
        await load()
    }
    func delete(_ r: JSON) async {
        do { _ = try await api.call("DELETE", "/api/routines/\(r.s("id"))") } catch { self.error = error.localizedDescription }
        await load()
    }
}

struct AddRoutineView: View {
    @EnvironmentObject var api: API
    @Environment(\.dismiss) var dismiss
    var done: () -> Void
    init(done: @escaping () -> Void) { self.done = done }
    @State private var name = ""
    @State private var prompt = ""
    @State private var preset = "daily"
    @State private var time = Calendar.current.date(bySettingHour: 8, minute: 0, second: 0, of: Date()) ?? Date()
    @State private var weekday = 5
    @State private var custom = ""
    @State private var machine = ""
    @State private var machines: [JSON] = []
    @State private var error = ""
    @State private var saving = false

    var schedule: String {
        let c = Calendar.current.dateComponents([.hour, .minute], from: time)
        let h = c.hour ?? 8, m = c.minute ?? 0
        switch preset {
        case "daily": return String(format: "daily %02d:%02d", h, m)
        case "workday": return "\(m) \(h) * * 1-5"
        case "weekly": return "\(m) \(h) * * \(weekday)"
        case "hourly": return "every 1h"
        default: return custom
        }
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("名称，如 每日早报", text: $name)
                    TextField("让 Aiw 做什么", text: $prompt, axis: .vertical).lineLimit(2...6)
                }
                Section(header: Text("时间"), footer: Text("自定义支持 5 段 cron（0 9 * * 1-5）、every 30m、daily 09:00；按北京时间。")) {
                    Picker("重复", selection: $preset) {
                        Text("每天").tag("daily"); Text("工作日").tag("workday"); Text("每周").tag("weekly"); Text("每小时").tag("hourly"); Text("自定义").tag("custom")
                    }
                    if preset == "weekly" {
                        Picker("星期", selection: $weekday) {
                            ForEach(Array(["日", "一", "二", "三", "四", "五", "六"].enumerated()), id: \.offset) { i, d in Text("周" + d).tag(i) }
                        }
                    }
                    if ["daily", "workday", "weekly"].contains(preset) { DatePicker("时间", selection: $time, displayedComponents: .hourAndMinute) }
                    if preset == "custom" { TextField("0 9 * * 1-5", text: $custom).textInputAutocapitalization(.never).autocorrectionDisabled() }
                }
                Section(header: Text("电脑")) {
                    Picker("在哪台电脑上做", selection: $machine) {
                        ForEach(machines.indices, id: \.self) { i in
                            Text(machines[i].s("name").isEmpty ? machines[i].s("id") : machines[i].s("name")).tag(machines[i].s("id"))
                        }
                    }
                }
                if !error.isEmpty { Section { Text(error).foregroundColor(.red).font(.footnote) } }
            }
            .navigationTitle("新建例程").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "保存中…" : "添加") { Task { await save() } }.disabled(name.isEmpty || prompt.isEmpty || schedule.isEmpty || saving)
                }
            }
            .task {
                machines = (try? await api.list("/api/machines")) ?? []
                if machine.isEmpty { machine = api.machine }
            }
        }
    }
    func save() async {
        saving = true; defer { saving = false }
        var body: JSON = ["name": name, "prompt": prompt, "schedule": schedule, "enabled": true]
        if !machine.isEmpty { body["machine_id"] = machine }
        do { _ = try await api.obj("POST", "/api/routines", body); done(); dismiss() } catch { self.error = error.localizedDescription }
    }
}

// MARK: - 电脑

struct ComputerSheet: View {
    @EnvironmentObject var api: API
    let close: () -> Void
    @State private var machines: [JSON] = []
    @State private var detail: [String: JSON] = [:]
    @State private var bos: [JSON] = []
    @State private var error = ""
    @State private var renaming: JSON? = nil
    @State private var viewing: DiagTarget? = nil   // 3.8.6: 查看 = this computer's desktop full screen, no task
    @State private var newName = ""

    var locals: [JSON] { machines.filter { $0.s("provider") != "instabox" } }
    var clouds: [JSON] { machines.filter { $0.s("provider") == "instabox" }.sorted { $0.d("order") < $1.d("order") } }
    var bosShown: [JSON] { bos.filter { $0.o("seen").b("installed") } }

    var body: some View {
        SheetChrome("电脑", close: close) {
            List {
                if !error.isEmpty { Section { Text(error).foregroundColor(.red).font(.footnote) } }
                if !locals.isEmpty {
                    Section(header: Label("本地", systemImage: "desktopcomputer").textCase(nil)) {
                        ForEach(locals.indices, id: \.self) { i in row(locals[i]) }
                    }
                }
                Section(header: HStack {
                    Label("InstaBox 云电脑", systemImage: "cloud"); Spacer(); if !clouds.isEmpty { Text("\(clouds.count) 台") }
                }.textCase(nil)) {
                    if clouds.isEmpty { Text("还没有云电脑").font(.subheadline).foregroundColor(.secondary) }
                    ForEach(clouds.indices, id: \.self) { i in row(clouds[i]) }
                }
                if !bosShown.isEmpty {
                    Section(header: Label("BrowserOS", systemImage: "globe").textCase(nil)) {
                        ForEach(bosShown.indices, id: \.self) { i in bosRow(bosShown[i]) }
                    }
                }
                Section(footer: Text("云电脑空闲 30 分钟自动休眠，发任务时自动唤醒。名称在网页和 iOS 通用，留空恢复默认名。")) {
                    if let u = api.url("/") {
                        Link(destination: u) {
                            HStack { Spacer(); Label("添加云电脑（网页设置 › 电脑）", systemImage: "plus"); Spacer() }
                        }.foregroundColor(amber)
                    }
                }
            }
            .listStyle(.insetGrouped)
            .refreshable { await load() }
        }
        .task { await load() }
        .fullScreenCover(item: $viewing) { t in
            TakeoverView(liveSession: nil, machineId: t.id, wake: true, title: machineTitle(t.id)) { viewing = nil }
                .environmentObject(api)
        }
        .alert("重命名电脑", isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
            TextField(renaming?.s("default_name") ?? "名称", text: $newName)
            Button("取消", role: .cancel) { renaming = nil }
            Button("保存") { if let m = renaming { Task { await rename(m.s("id"), newName) } }; renaming = nil }
        } message: {
            Text("留空恢复默认名「\(renaming?.s("default_name") ?? "")」")
        }
    }

    enum Conn { case checking, online, asleep, offline }
    func conn(_ m: JSON) -> Conn {
        guard let d = detail[m.s("id")] else { return .checking }
        let a = d.o("agent")
        if a.b("ok") { return .online }
        if a.b("paused") { return .asleep }
        return .offline
    }

    @ViewBuilder func row(_ m: JSON) -> some View {
        let id = m.s("id"), cur = id == api.machine, c = conn(m)
        let cloud = m.s("provider") == "instabox"
        HStack(alignment: .top, spacing: 12) {
            MateView(status: "", threadId: nil, bus: false, emotion: c == .online ? "02" : "00", animate: c == .online)
                .frame(width: 44, height: 44).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 6) {
                    Text(m.s("name")).font(.body.weight(.semibold)).lineLimit(1)
                    if cur {
                        Text("当前").font(.caption2.weight(.bold)).foregroundColor(.white)
                            .padding(.horizontal, 6).padding(.vertical, 2).background(amber).clipShape(RoundedRectangle(cornerRadius: 5))
                    }
                    Spacer(minLength: 4)
                    connChip(c)
                }
                HStack(spacing: 8) {
                    Text(spec(m, cloud)).font(.subheadline).foregroundColor(.secondary).lineLimit(1)
                    Spacer(minLength: 4)
                    if cloud || hasDesktop(m) {
                        Button("查看") { viewing = DiagTarget(id: id) }
                            .buttonStyle(.bordered).controlSize(.small).tint(amber)
                    }
                    Button(cur ? "使用中" : "使用") { use(id) }
                        .buttonStyle(.bordered).controlSize(.small).tint(amber).disabled(cur)
                    Button("重命名") { newName = m.s("custom_name") == "<null>" ? "" : m.s("custom_name"); renaming = m }
                        .buttonStyle(.bordered).controlSize(.small).tint(amber)
                }
                Text(longId(m, cloud)).font(.system(size: 11, design: .monospaced)).foregroundColor(Color(.tertiaryLabel))
                    .lineLimit(1).truncationMode(.middle).textSelection(.enabled)
            }
        }
        .padding(.vertical, 4)
        .listRowBackground(cur ? amber.opacity(0.07) : Color(.secondarySystemGroupedBackground))
    }

    /// a local VM has a desktop when the server knows its noVNC address (desktop_endpoint)
    func hasDesktop(_ m: JSON) -> Bool {
        let d = m.s("desktop_endpoint")
        return !d.isEmpty && d != "<null>"
    }
    func machineTitle(_ id: String) -> String {
        let m = machines.first { $0.s("id") == id } ?? [:]
        return m.s("name").isEmpty ? id : m.s("name")
    }

    @ViewBuilder func connChip(_ c: Conn) -> some View {
        switch c {
        case .checking: Chip(text: "检查中", color: .secondary)
        case .online: Chip(text: "已连接", color: okGreen, dot: true)
        case .asleep: Chip(text: "休眠中", color: .secondary, dot: true)
        case .offline: Chip(text: "未连接", color: .red, dot: true)
        }
    }

    func spec(_ m: JSON, _ cloud: Bool) -> String {
        let sp = detail[m.s("id")]?.o("vm").o("spec") ?? [:]
        let cpu = Int(sp.d("vcpus")), mem = Int((sp.d("memMiB") / 1024).rounded())
        let res = cpu > 0 ? "\(cpu) 核 \(mem > 0 ? "\(mem)G" : "")" : ""
        if cloud { return res.isEmpty ? "4 核 8G" : res }
        return "Proxmox" + (res.isEmpty ? "" : " · " + res)
    }
    func longId(_ m: JSON, _ cloud: Bool) -> String {
        let vm = m.s("provider_vm_id")
        if cloud { return vm.isEmpty || vm == "<null>" ? m.s("id") : vm }
        return m.s("id") + (vm.isEmpty || vm == "<null>" ? "" : " · VM \(vm)")
    }

    @ViewBuilder func bosRow(_ b: JSON) -> some View {
        HStack(spacing: 12) {
            IconTile(symbol: "globe", color: amber)
            VStack(alignment: .leading, spacing: 3) {
                Text("BrowserOS").font(.subheadline.weight(.semibold))
                Text(b.s("name") + " · " + (b.b("enabled") ? "已开启" : "未开启")).font(.caption).foregroundColor(.secondary)
            }
            Spacer()
            Toggle("", isOn: Binding(get: { b.b("enabled") }, set: { v in Task { await bosSet(b.s("id"), v) } })).labelsHidden().tint(.green)
        }
    }

    func use(_ id: String) {
        api.setMachine(id)
        NotificationCenter.default.post(name: .aiwMachinesChanged, object: nil)
    }

    func load() async {
        do { machines = try await api.list("/api/machines"); error = "" } catch { self.error = error.localizedDescription; return }
        bos = ((try? await api.obj("GET", "/api/browseros")) ?? [:]).a("machines")
        for m in machines {
            let id = m.s("id")
            Task {
                if let d = try? await api.obj("GET", "/api/machines/\(id)") { detail[id] = d }
                else { detail[id] = ["agent": ["ok": false]] }
            }
        }
    }
    func rename(_ id: String, _ name: String) async {
        do {
            _ = try await api.obj("PUT", "/api/machines/\(id)/name", ["name": name.trimmingCharacters(in: .whitespacesAndNewlines)])
            NotificationCenter.default.post(name: .aiwMachinesChanged, object: nil)
            machines = try await api.list("/api/machines")
        } catch { self.error = error.localizedDescription }
    }
    func bosSet(_ id: String, _ on: Bool) async {
        do { _ = try await api.obj("PUT", "/api/browseros/\(id)", ["enabled": on]) } catch { self.error = error.localizedDescription }
        bos = ((try? await api.obj("GET", "/api/browseros")) ?? [:]).a("machines")
    }
}
