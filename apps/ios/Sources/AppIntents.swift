import AppIntents
import Foundation

/// v3.9.27: Siri / 快捷指令 / 动作按钮 entry points (idea from Rakazo's RakazoIntents.swift, written for aiw).
/// NOTE: the Shortcuts app discovers App Intents from metadata that Xcode's build step (appintentsmetadataprocessor)
/// writes into the bundle; the plain swiftc build in build-ipa.sh does not run it — see the v3.9.27 report.
@available(iOS 16.0, *)
struct AskAiwIntent: AppIntent {
    static var title: LocalizedStringResource = "让 aiw 去做"
    static var description = IntentDescription("把一句话交给 aiw 作为任务执行")
    @Parameter(title: "要做什么") var text: String
    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard API.shared.configured else { return .result(dialog: "先在 aiw App 里连接服务器") }
        let r = try await API.shared.obj("POST", "/api/tasks", ["prompt": text, "machine_id": API.shared.machine])
        return .result(dialog: "已交给 aiw：\(r.s("id").isEmpty ? text : r.s("id"))")
    }
}

/// 反向桥：个人自动化（每天 22:00、到家……）里运行它，手机把今天的步数/睡眠上报给 aiw 记忆
@available(iOS 16.0, *)
struct ReportHealthIntent: AppIntent {
    static var title: LocalizedStringResource = "向 aiw 上报今日健康"
    static var description = IntentDescription("读取今天的步数和昨晚睡眠，发给 aiw")
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let b = DeviceBridge.shared
        guard b.enabled("health.read") else { return .result(dialog: "先在 aiw › 设置 › 设备能力 里打开「读取健康」") }
        let start = Calendar.current.startOfDay(for: Date())
        let f = ISO8601DateFormatter()
        let steps = try await b.run("health_read", ["kind": "steps", "start": f.string(from: start), "end": f.string(from: Date())])
        let sleep = try await b.run("health_read", ["kind": "sleep", "start": f.string(from: start.addingTimeInterval(-6 * 3600)),
                                                    "end": f.string(from: start.addingTimeInterval(12 * 3600))])
        let line = "今日步数 \(steps["steps"] ?? 0)，昨晚睡眠 \(sleep["asleep_minutes"] ?? 0) 分钟"
        _ = try? await API.shared.obj("POST", "/api/memories", ["text": "[健康上报] " + line])
        return .result(dialog: "\(line)")
    }
}

@available(iOS 16.0, *)
struct AiwShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: AskAiwIntent(), phrases: ["用 \(.applicationName) 做件事", "让 \(.applicationName) 去做"])
        AppShortcut(intent: ReportHealthIntent(), phrases: ["向 \(.applicationName) 上报健康"])
    }
}
