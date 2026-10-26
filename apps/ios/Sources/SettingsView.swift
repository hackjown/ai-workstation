import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var api: API
    @Environment(\.dismiss) var dismiss
    @State private var machines: [JSON] = []
    @AppStorage("appearance") private var appearance = "system"
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
                Section(footer: Text("降价提醒，可自动下单到付款前，等你确认")) {
                    NavigationLink { ShopWatchView() } label: { Label("京东/淘宝价格监控", systemImage: "tag") }
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
            .task { machines = (try? await api.list("/api/machines")) ?? [] }
        }
    }
}
