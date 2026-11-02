import SwiftUI
import UserNotifications

// iOS 3.8 deep links: a Bark push (url=aiw://…), an ntfy click, a local notification from /ws/events or any link opens
// Aiw on the item it is about. The server builds the path (services/api/aiw_api/notify.py deep_path):
//   aiw://task/<task id>                    the task detail page (its thread: computer on top, steps below)
//   aiw://takeover/<machine>?task=<task id> that task in takeover (no task: that computer's desktop, takeover)
//   aiw://monitor/<watch id>                追踪 with that price / page watch highlighted, 立即重试 on top
//   aiw://chat[?m=<message id>]             the home chat at the newest message, or scrolled to that message (v3.9.14)
// aiw://import… stays the share extension's (Import.swift). A cold launch works the same way: onOpenURL (or the
// notification tap) stores the target here, and RootView takes it as soon as it is on screen (after sign-in).

enum DeepTarget: Equatable {
    case task(String)
    case takeover(machine: String, task: String?)
    case monitor(String)
    case chat(message: String?)

    init?(_ url: URL) {
        guard url.scheme?.lowercased() == "aiw" else { return nil }
        // aiw://task/ID -> host "task", path "/ID"
        let host = (url.host ?? "").lowercased()
        let parts = url.path.split(separator: "/").map { String($0).removingPercentEncoding ?? String($0) }
        let q = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func param(_ k: String) -> String? { q.first { $0.name == k }?.value.flatMap { $0.isEmpty ? nil : $0 } }
        switch host {
        case "task":
            guard let id = parts.first, !id.isEmpty else { return nil }
            self = .task(id)
        case "takeover":
            self = .takeover(machine: parts.first ?? "", task: param("task"))
        case "monitor":
            guard let id = parts.first, !id.isEmpty else { return nil }
            self = .monitor(id)
        case "chat":
            self = .chat(message: param("m") ?? parts.first)
        default:
            return nil
        }
    }
}

final class DeepLinks: ObservableObject {
    static let shared = DeepLinks()
    @Published var pending: DeepTarget?
    /// the watch 追踪 should highlight (set with .monitor, read by TrackingSheet)
    @Published var focusWatch: String?

    /// true when the URL was a deep link (else it is an import / file for ImportCenter)
    @discardableResult
    func handle(_ url: URL) -> Bool {
        guard let t = DeepTarget(url) else { return false }
        DispatchQueue.main.async { self.pending = t }
        return true
    }
    func take() -> DeepTarget? { let t = pending; pending = nil; return t }
}

extension Notification.Name {
    /// the home chat: leave 球球's briefing and jump to the newest message (aiw://chat)
    static let aiwShowChat = Notification.Name("aiwShowChat")
}

/// A local notification (Hub, from /ws/events) carries the same path as the push: tapping it opens the item.
func deepURL(_ path: String) -> URL? { path.isEmpty ? nil : URL(string: "aiw://" + path) }
