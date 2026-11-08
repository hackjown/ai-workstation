import SwiftUI
import Contacts
import EventKit

/// v3.9.28 手机快通道, on-device fast answers in the home chat (HomeChat.swift):
///   打电话给X / 给X打电话 / 拨打X / call X     -> contacts looked up here; several numbers -> a picker sheet; always a
///                                              local confirm before tel:
///   查看(手机)提醒事项 / 我有什么提醒            -> EventKit reminders, answered here
///   今天日程 / 今天有什么安排                    -> EventKit events of today, answered here
/// The answer shows as a bubble at once and goes into the chat history with POST /api/chat/device_result (client_id
/// = a UUID, so a retry is not written twice). No match, or the iOS permission is not granted yet -> the message
/// goes to the server as before (its own fast path / the computer).
enum PhoneFastIntent: Equatable {
    case call(String)        // a name or a number
    case reminders
    case calendarToday
}

struct FastCandidate: Identifiable {
    let id = UUID()
    let name: String
    let number: String
    let label: String
    let user: String         // what the user typed
}

struct FastPick: Identifiable {
    let id = UUID()
    let user: String
    let query: String
    let list: [FastCandidate]
}

enum PhoneFast {
    private static let callRes = [
        #"^(?:请|帮我|麻烦|麻烦你)?(?:打电话|打个电话|拨电话|拨个电话|拨打|呼叫)给?\s*(.+)$"#,
        #"^(?:请|帮我|麻烦|麻烦你)?给\s*(.+?)\s*(?:打|拨)(?:个|一个|一下)?电话(?:吧)?$"#,
        #"^(?:please\s+)?(?:call|dial|phone)\s+(.+)$"#,
    ]
    private static let reminderRes = [
        #"^(?:请|帮我)?(?:查看|看看|看一下|查一下|查查|列出|显示|打开)?(?:一下)?(?:我的)?(?:手机)?(?:上|里)?的?(?:提醒事项|提醒)(?:列表)?(?:有哪些|有什么)?(?:吗|呢)?$"#,
        #"^我(?:现在|还)?有(?:什么|哪些|啥)(?:提醒事项|提醒|待办)(?:吗|呢)?$"#,
        #"^(?:show|list|what are)\s+(?:my\s+)?reminders$"#,
    ]
    private static let calendarRes = [
        #"^(?:请|帮我)?(?:查看|看看|看一下|查一下)?(?:我)?今天(?:的)?(?:日程|安排|行程|日历)(?:是什么|有哪些|有什么|呢)?$"#,
        #"^(?:我)?今天(?:都)?有(?:什么|哪些|啥)(?:安排|日程|行程|会|会议|事)(?:吗|呢)?$"#,
        #"^(?:what'?s|show)\s+(?:on\s+)?(?:my\s+)?(?:calendar|schedule|agenda)(?:\s+for)?\s+today$"#,
    ]

    static func match(_ raw: String) -> PhoneFastIntent? {
        let t = raw.trimmingCharacters(in: .whitespacesAndNewlines)
            .trimmingCharacters(in: CharacterSet(charactersIn: "。.!！?？~～ ，,"))
        guard !t.isEmpty, t.count <= 40, !t.contains("\n"), !t.hasPrefix("【") else { return nil }
        for p in callRes {
            if let who = capture(p, t) {
                let w = who.trimmingCharacters(in: CharacterSet(charactersIn: " 。.!！吧呀啊"))
                if !w.isEmpty && w.count <= 20 && !w.contains("电话") { return .call(w) }
            }
        }
        if reminderRes.contains(where: { capture($0, t) != nil }) { return .reminders }
        if calendarRes.contains(where: { capture($0, t) != nil }) { return .calendarToday }
        return nil
    }

    /// nil = no match; "" = matched without a group
    static func capture(_ pattern: String, _ t: String) -> String? {
        guard let re = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) else { return nil }
        guard let m = re.firstMatch(in: t, range: NSRange(t.startIndex..., in: t)) else { return nil }
        if m.numberOfRanges > 1, m.range(at: 1).location != NSNotFound, let r = Range(m.range(at: 1), in: t) { return String(t[r]) }
        return ""
    }

    // MARK: permissions (already granted only; asking is the 设备能力 screen's job, else the server handles it)
    static func granted(_ type: EKEntityType) -> Bool {
        let st = EKEventStore.authorizationStatus(for: type)
        if #available(iOS 17.0, *) { if st == .fullAccess { return true } }
        return st == .authorized
    }
    static var contactsGranted: Bool { CNContactStore.authorizationStatus(for: .contacts) == .authorized }

    /// A typed number (mostly digits) is dialled as it is.
    static func directNumber(_ who: String) -> String? {
        let digits = who.filter { $0.isASCII && $0.isNumber }
        guard digits.count >= 3 else { return nil }
        let other = who.filter { !($0.isASCII && $0.isNumber) && !" +-()*#".contains($0) }
        return other.isEmpty ? DeviceBridge.cleanNumber(who) : nil
    }

    /// Every phone number of every contact whose name matches (one candidate per number).
    static func lookup(_ name: String, user: String) -> [FastCandidate] {
        let store = CNContactStore()
        let keys: [CNKeyDescriptor] = [CNContactGivenNameKey, CNContactFamilyNameKey, CNContactOrganizationNameKey,
                                       CNContactNicknameKey, CNContactPhoneNumbersKey] as [CNKeyDescriptor]
        let found = (try? store.unifiedContacts(matching: CNContact.predicateForContacts(matchingName: name), keysToFetch: keys)) ?? []
        var out: [FastCandidate] = []
        for c in found.prefix(20) {
            let full = "\(c.familyName)\(c.givenName)"
            let shown = full.isEmpty ? (c.nickname.isEmpty ? c.organizationName : c.nickname) : full
            for p in c.phoneNumbers {
                let label = p.label.map { CNLabeledValue<CNPhoneNumber>.localizedString(forLabel: $0) } ?? ""
                out.append(FastCandidate(name: shown, number: p.value.stringValue, label: label, user: user))
            }
        }
        return out
    }

    // MARK: answers
    static func hm(_ iso: String) -> String {
        let f = ISO8601DateFormatter()
        guard let d = f.date(from: iso) else { return "" }
        let o = DateFormatter(); o.locale = Locale(identifier: "zh_CN"); o.timeZone = .current; o.dateFormat = "HH:mm"
        return o.string(from: d)
    }
    static func remindersText(_ r: [String: Any]) -> String {
        let items = (r["reminders"] as? [[String: Any]]) ?? []
        if items.isEmpty { return "手机上没有未完成的提醒事项。" }
        var lines = ["你有 \(items.count) 条未完成的提醒事项："]
        for it in items.prefix(30) {
            var extra: [String] = []
            let due = it["due"] as? String ?? ""
            if !due.isEmpty, let d = ISO8601DateFormatter().date(from: due) {
                let o = DateFormatter(); o.locale = Locale(identifier: "zh_CN"); o.timeZone = .current; o.dateFormat = "M月d日 HH:mm"
                extra.append(o.string(from: d))
            }
            if let l = it["list"] as? String, !l.isEmpty { extra.append(l) }
            lines.append("• \(it["title"] as? String ?? "")" + (extra.isEmpty ? "" : "（\(extra.joined(separator: " · "))）"))
        }
        if items.count > 30 { lines.append("……还有 \(items.count - 30) 条") }
        return lines.joined(separator: "\n")
    }
    static func calendarText(_ r: [String: Any]) -> String {
        let evs = (r["events"] as? [[String: Any]]) ?? []
        if evs.isEmpty { return "今天（\(r["date"] as? String ?? "")）日历上没有安排。" }
        var lines = ["今天（\(r["date"] as? String ?? "")）有 \(evs.count) 项安排："]
        for e in evs {
            let when = (e["all_day"] as? Bool ?? false) ? "全天" : "\(hm(e["start"] as? String ?? ""))–\(hm(e["end"] as? String ?? ""))"
            let loc = e["location"] as? String ?? ""
            lines.append("• \(when) \(e["title"] as? String ?? "")" + (loc.isEmpty ? "" : " @ \(loc)"))
        }
        return lines.joined(separator: "\n")
    }
}

extension HomeChat {
    /// Show the exchange at once, then write it into the chat history (POST /api/chat/device_result). The server's
    /// message replaces the local bubbles when it lands (it quotes what was asked).
    @MainActor func fastLocal(user: String, reply: String, tool: String, ok: Bool, args: JSON, result: JSON?, error: String?) {
        let cid = UUID().uuidString
        let now = Date().timeIntervalSince1970
        let uid = "local-u-\(cid)", aid = "local-a-\(cid)"
        leaveStage()
        msgs.append(["id": uid, "role": "user", "kind": "text", "text": user, "data": [:] as JSON, "created_at": now])
        msgs.append(["id": aid, "role": "assistant", "kind": "text", "text": reply,
                     "data": ["device_result": true, "tool": tool] as JSON, "created_at": now])
        bottomTick += 1
        var body: JSON = ["tool": tool, "ok": ok, "args": args, "text": "「\(user)」\n\(reply)",
                          "device_id": DeviceBridge.shared.deviceId, "client_id": cid]
        if let r = result { body["result"] = r }
        if let e = error { body["error"] = e }
        Task { @MainActor in
            for attempt in 0..<3 {
                if let r = try? await API.shared.obj("POST", "/api/chat/device_result", body), !r.o("message").isEmpty {
                    self.remove(uid); self.remove(aid); self.upsert(r.o("message"), stick: true)
                    return
                }
                try? await Task.sleep(nanoseconds: UInt64(attempt + 1) * 1_500_000_000)
            }
        }
    }
}
