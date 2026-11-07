import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var api: API
    @Environment(\.dismiss) var dismiss
    @State private var machines: [JSON] = []
    @AppStorage("appearance") private var appearance = "system"
    @AppStorage("mate") private var mate = true
    // 3.8.9: 主模型思考 = the server's models.thinking ("on" | "off"), the same setting as the web 深度思考 select
    @State private var thinking = false
    @State private var thinkingLoaded = false
    @State private var thinkingSaving = false
    @State private var thinkingError = ""
    var body: some View {
        NavigationStack {
            Form {
                Section("服务器") {
                    LabeledContent("地址", value: api.server)
                    Button("退出登录", role: .destructive) { api.signOut(); dismiss() }
                }
                Section(header: Text("在哪台电脑上做"), footer: Text("新任务用这台电脑")) {
                    if machines.isEmpty { Text(api.machine).foregroundColor(.secondary) }
                    ForEach(machines.indices, id: \.self) { i in
                        let mm = machines[i]
                        Button { api.setMachine(mm.s("id")) } label: {
                            HStack {
                                Image(systemName: "desktopcomputer")
                                Text(mm.s("name").isEmpty ? mm.s("id") : mm.s("name")).foregroundColor(.primary)
                                Spacer()
                                if api.machine == mm.s("id") { Image(systemName: "checkmark").foregroundColor(amber) }
                            }
                        }
                    }
                }
                Section(header: Text("模型"), footer: Text(thinkingError.isEmpty
                        ? "打开后主模型每一步先深度思考再操作，更稳但慢很多；关闭更快（推荐）。和网页设置里的「深度思考」是同一个开关，下一个任务开始生效。"
                        : thinkingError).foregroundColor(thinkingError.isEmpty ? .secondary : .red)) {
                    Toggle(isOn: Binding(get: { thinking }, set: { v in Task { await setThinking(v) } })) {
                        HStack(spacing: 6) {
                            Text("主模型思考")
                            if thinkingSaving || !thinkingLoaded { ProgressView().controlSize(.mini) }
                        }
                    }
                    .tint(amber).disabled(!thinkingLoaded || thinkingSaving)
                }
                Section(footer: Text("球球记得的你：画像、事实和决定，聊天和每个任务都会参考；可以查看、修改、删除")) {
                    NavigationLink { MemoryView() } label: { Label("记忆", systemImage: "brain.head.profile") }
                }
                Section(footer: Text("做过的任务里遇到的问题和解决办法，下次直接用；不对的可以删掉")) {
                    NavigationLink { LessonsView() } label: { Label("Aiw 学到的经验", systemImage: "lightbulb") }
                }
                Section(footer: Text("降价提醒，可自动下单到付款前，等你确认")) {
                    NavigationLink { ShopWatchView() } label: { Label("京东/淘宝价格监控", systemImage: "tag") }
                }
                Section(footer: Text("点赞、回复评论、给兴趣相近的笔记写评论、发笔记；评论和笔记先给你审核")) {
                    NavigationLink { XhsView() } label: { Label("小红书运营", systemImage: "heart.text.square") }
                }
                Section(footer: Text("每天、每个模型用了多少 token（v3.9.24）")) {
                    NavigationLink { UsageView() } label: { Label("用量统计", systemImage: "chart.bar") }
                    NavigationLink { CheckinView() } label: { Label("自动签到", systemImage: "checkmark.seal") }
                }
                Section(footer: Text("让 Agent 用这台手机的日历、提醒事项、通讯录、健康、定位、闹钟和快捷指令；写操作先确认（v3.9.27）")) {
                    NavigationLink { DeviceCapsView() } label: { Label("设备能力", systemImage: "iphone.gen3.radiowaves.left.and.right") }
                }
                Section(footer: Text("对话标题旁的球球随任务状态变表情：接收、思考、操作、等你、完成、出错（形象仅供个人学习研究，非商用）")) {
                    Toggle("表情助手", isOn: $mate)
                }
                Section(header: Text("外观"), footer: Text("跟随系统时，白天浅色、晚上深色，随 iPhone 的设置自动切换")) {
                    Picker("外观", selection: $appearance) {
                        Text("跟随系统").tag("system"); Text("浅色").tag("light"); Text("深色").tag("dark")
                    }.pickerStyle(.segmented)
                }
                Section(footer: Text("模型、连接器、运维主机等完整设置在网页里")) {
                    if let u = api.url("/") { Link(destination: u) { Label("打开网页设置", systemImage: "safari") } }
                }
                Section { LabeledContent("版本", value: Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "") }
            }
            .navigationTitle("设置").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { dismiss() } } }
            .task {
                machines = (try? await api.list("/api/machines")) ?? []
                await loadThinking()
            }
        }
    }

    func loadThinking() async {
        do {
            let m = try await api.obj("GET", "/api/settings/models")
            thinking = m.s("thinking") == "on"
            thinkingLoaded = true
            thinkingError = ""
        } catch { thinkingError = "读不到模型设置：" + error.localizedDescription }
    }

    func setThinking(_ on: Bool) async {
        let before = thinking
        thinking = on
        thinkingSaving = true
        defer { thinkingSaving = false }
        do {
            // PATCH changes only this key of the server's models settings (PUT would replace them all)
            let m = try await api.obj("PATCH", "/api/settings/models", ["thinking": on ? "on" : "off"])
            thinking = m.s("thinking") == "on"
            thinkingError = ""
        } catch {
            thinking = before
            thinkingError = "没能保存：" + error.localizedDescription
        }
    }
}
