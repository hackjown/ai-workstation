import Foundation
import SwiftUI
import UIKit
import EventKit
import Contacts
import HealthKit
import CoreLocation
import MessageUI
#if AIW_ALARMKIT && canImport(AlarmKit)
import AlarmKit
#endif

/// v3.9.27 device bridge: the server's agent runs tools on this phone (services/api/aiw_api/device_bridge.py).
///   WS /ws/device?token=…  hello{device_id, platform, capabilities, features} -> call{id, tool, args} -> result{id, ok, result|error}
/// Only capabilities the user switched on in 设置 › 设备能力 are announced. Write tools (calendar_create, reminders_*,
/// health_write, alarm_set, shortcut_run) are approved on the server's confirm card first (the same card this app shows).
/// The socket lives while the app is in the foreground; calls made meanwhile wait on the server (TTL) and a push
/// asks the user to open the app.
final class DeviceBridge: NSObject, ObservableObject {
    static let shared = DeviceBridge()
    static let allCaps: [(String, String)] = [
        ("calendar.read", "读取日历"), ("calendar.write", "新建日程"),
        ("reminders.read", "读取提醒事项"), ("reminders.write", "新建/完成/删除提醒事项"),
        ("contacts.read", "搜索通讯录"), ("phone.call", "打电话（拨号前确认）"), ("sms.send", "发短信（系统短信界面，你点发送）"),
        ("health.read", "读取健康（步数、睡眠、心率、体能训练）"), ("health.write", "写入健康（饮水、体重、正念）"),
        ("location.read", "当前位置"), ("alarm.write", "设闹钟（iOS 26+）"), ("shortcuts.run", "运行快捷指令"),
    ]
    @Published var online = false
    @Published var lastError = ""
    @Published var lastCall = ""
    private var task: URLSessionWebSocketTask?
    private var alive = false
    private var backoff: Double = 2
    let events = EKEventStore()
    let contacts = CNContactStore()
    let health = HKHealthStore()
    private var locMgr: CLLocationManager?
    private var locWait: [CheckedContinuation<CLLocation, Error>] = []

    var deviceId: String {
        if let v = UserDefaults.standard.string(forKey: "device_id") { return v }
        let v = UIDevice.current.identifierForVendor?.uuidString ?? UUID().uuidString
        UserDefaults.standard.set(v, forKey: "device_id"); return v
    }
    func enabled(_ cap: String) -> Bool { UserDefaults.standard.bool(forKey: "devcap_" + cap) }
    func setEnabled(_ cap: String, _ on: Bool) {
        UserDefaults.standard.set(on, forKey: "devcap_" + cap)
        if on { Task { await self.requestPermission(cap) } }
        if alive { sendHello() }
    }
    var alarmKitAvailable: Bool {
        #if AIW_ALARMKIT && canImport(AlarmKit)
        if #available(iOS 26.0, *) { return true }
        #endif
        return false
    }

    // ------------------------------------------------------------------ socket
    func start() {
        guard !alive, Self.allCaps.contains(where: { enabled($0.0) }) else { return }
        alive = true; backoff = 2; connect()
    }
    /// v3.9.28: a wake push (aiw://open) or coming back to the foreground: the server's fast path waits only ~8 s,
    /// so (re)connect right now instead of waiting out the backoff.
    func connectNow() {
        if !alive { start(); return }
        if online { return }
        backoff = 2; task?.cancel(with: .goingAway, reason: nil); task = nil; connect()
    }
    func stop() { alive = false; task?.cancel(with: .goingAway, reason: nil); task = nil; DispatchQueue.main.async { self.online = false } }
    func restart() { stop(); start() }

    private func connect() {
        guard alive, let u = API.shared.wsURL("/ws/device") else { return }
        let t = URLSession.shared.webSocketTask(with: u)
        task = t; t.resume()
        sendHello(); receive(t)
    }
    private func sendHello() {
        let caps = Self.allCaps.map { $0.0 }.filter { enabled($0) && ($0 != "alarm.write" || alarmKitAvailable) }
        send(["type": "hello", "device_id": deviceId, "platform": "ios", "name": UIDevice.current.name,
              "capabilities": caps, "features": ["alarmkit": alarmKitAvailable,
                                                 "ios": UIDevice.current.systemVersion] as [String: Any]])
    }
    private func send(_ obj: [String: Any]) {
        guard let d = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: d, encoding: .utf8) else { return }
        task?.send(.string(s)) { _ in }
    }
    private func receive(_ t: URLSessionWebSocketTask) {
        t.receive { [weak self] res in
            guard let self = self, t === self.task else { return }
            switch res {
            case .success(let m):
                var data: Data?
                if case .string(let s) = m { data = s.data(using: .utf8) }
                if case .data(let d) = m { data = d }
                if let d = data, let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] { self.handle(o) }
                self.receive(t)
            case .failure(let e):
                DispatchQueue.main.async { self.online = false; self.lastError = e.localizedDescription }
                let wait = self.backoff; self.backoff = min(self.backoff * 2, 60)
                DispatchQueue.main.asyncAfter(deadline: .now() + wait) { [weak self] in self?.connect() }
            }
        }
    }
    private func handle(_ m: [String: Any]) {
        switch m["type"] as? String {
        case "welcome": backoff = 2; DispatchQueue.main.async { self.online = true; self.lastError = "" }
        case "call":
            guard let id = m["id"] as? String, let tool = m["tool"] as? String else { return }
            // v3.9.28: a call the server already gave up on (fast path ~8 s TTL) is dropped, never run late
            if let exp = Self.number(m["expires_at"]), exp > 0, exp < Date().timeIntervalSince1970 { return }
            let args = m["args"] as? [String: Any] ?? [:]
            DispatchQueue.main.async { self.lastCall = tool }
            Task {
                do {
                    let r = try await self.run(tool, args)
                    self.send(["type": "result", "id": id, "ok": true, "result": r])
                } catch {
                    self.send(["type": "result", "id": id, "ok": false, "error": "\(error.localizedDescription)"])
                }
            }
        default: break
        }
    }

    static func number(_ v: Any?) -> Double? {
        if let d = v as? Double { return d }
        if let i = v as? Int { return Double(i) }
        if let n = v as? NSNumber { return n.doubleValue }
        if let s = v as? String { return Double(s) }
        return nil
    }

    // ------------------------------------------------------------------ tools
    struct BridgeError: LocalizedError { let msg: String; var errorDescription: String? { msg } }
    private func need(_ cap: String) throws { if !enabled(cap) { throw BridgeError(msg: "用户没有在手机上打开「\(cap)」") } }
    private func date(_ v: Any?) -> Date? {
        guard let s = v as? String, !s.isEmpty else { return nil }
        let f = ISO8601DateFormatter()
        for o in [ISO8601DateFormatter.Options.withInternetDateTime, [.withInternetDateTime, .withFractionalSeconds],
                  [.withFullDate, .withTime, .withColonSeparatorInTime], [.withFullDate]] as [ISO8601DateFormatter.Options] {
            f.formatOptions = o
            if o == [.withFullDate, .withTime, .withColonSeparatorInTime] { f.timeZone = .current }
            if let d = f.date(from: s) { return d }
        }
        return nil
    }
    private func iso(_ d: Date?) -> String { d.map { ISO8601DateFormatter().string(from: $0) } ?? "" }

    /// `local`: the user asked for it in this app's own chat (v3.9.28 on-device fast answers), so the 设备能力 switch
    /// is not required; iOS permissions still are.
    func run(_ tool: String, _ a: [String: Any], local: Bool = false) async throws -> [String: Any] {
        if local {
            switch tool {
            case "reminders_list": return try await remindersList(a)
            case "calendar_today": return try await calendarToday(a)
            case "phone_call": return try await phoneCall(a)
            case "contacts_search": return try await contactsSearch(a)
            default: break
            }
        }
        switch tool {
        case "calendar_list": try need("calendar.read"); return try await calendarList(a)
        case "calendar_create": try need("calendar.write"); return try await calendarCreate(a)
        case "reminders_list": try need("reminders.read"); return try await remindersList(a)
        case "reminders_create": try need("reminders.write"); return try await remindersCreate(a)
        case "reminders_complete": try need("reminders.write"); return try await remindersComplete(a)
        case "reminders_delete": try need("reminders.write"); return try await remindersDelete(a)
        case "calendar_today": try need("calendar.read"); return try await calendarToday(a)
        case "phone_call": try need("phone.call"); return try await phoneCall(a)
        case "sms_send": try need("sms.send"); return try await smsSend(a)
        case "contacts_search": try need("contacts.read"); return try await contactsSearch(a)
        case "health_read": try need("health.read"); return try await healthRead(a)
        case "health_write": try need("health.write"); return try await healthWrite(a)
        case "location_get": try need("location.read"); return try await locationGet()
        case "alarm_set": try need("alarm.write"); return try await alarmSet(a)
        case "shortcut_run": try need("shortcuts.run"); return try await shortcutRun(a)
        default: throw BridgeError(msg: "这台手机不支持 \(tool)")
        }
    }

    // MARK: permissions
    func requestPermission(_ cap: String) async {
        switch cap {
        case "calendar.read", "calendar.write": _ = try? await calendarAccess()
        case "reminders.read", "reminders.write": _ = try? await remindersAccess()
        case "contacts.read": _ = try? await contacts.requestAccess(for: .contacts)
        case "health.read", "health.write": try? await healthAccess()
        case "location.read": await MainActor.run { self.ensureLocMgr(); self.locMgr?.requestWhenInUseAuthorization() }
        case "alarm.write": _ = try? await alarmAccess()
        default: break
        }
    }
    private func calendarAccess() async throws -> Bool {
        if #available(iOS 17.0, *) { return try await events.requestFullAccessToEvents() }
        return try await events.requestAccess(to: .event)
    }
    private func remindersAccess() async throws -> Bool {
        if #available(iOS 17.0, *) { return try await events.requestFullAccessToReminders() }
        return try await events.requestAccess(to: .reminder)
    }

    // MARK: EventKit
    private func calendarList(_ a: [String: Any]) async throws -> [String: Any] {
        guard try await calendarAccess() else { throw BridgeError(msg: "没有日历权限（设置 › 隐私 › 日历 › AIW）") }
        let start = date(a["start"]) ?? Date(), end = date(a["end"]) ?? start.addingTimeInterval(86400 * 7)
        let pred = events.predicateForEvents(withStart: start, end: end, calendars: nil)
        let q = (a["query"] as? String ?? "").lowercased()
        let list = events.events(matching: pred).filter { q.isEmpty || ($0.title ?? "").lowercased().contains(q) }.prefix(100)
        return ["events": list.map { ["id": $0.eventIdentifier ?? "", "title": $0.title ?? "", "start": iso($0.startDate),
                                       "end": iso($0.endDate), "all_day": $0.isAllDay, "location": $0.location ?? "",
                                       "calendar": $0.calendar?.title ?? ""] as [String: Any] }]
    }
    private func calendarCreate(_ a: [String: Any]) async throws -> [String: Any] {
        guard try await calendarAccess() else { throw BridgeError(msg: "没有日历权限") }
        guard let s = date(a["start"]), let e = date(a["end"]) else { throw BridgeError(msg: "start / end 不是 ISO 时间") }
        let ev = EKEvent(eventStore: events)
        ev.title = a["title"] as? String ?? "日程"; ev.startDate = s; ev.endDate = e
        ev.isAllDay = a["all_day"] as? Bool ?? false
        ev.location = a["location"] as? String; ev.notes = a["notes"] as? String
        if let name = a["calendar"] as? String, let c = events.calendars(for: .event).first(where: { $0.title == name && $0.allowsContentModifications }) {
            ev.calendar = c
        } else { ev.calendar = events.defaultCalendarForNewEvents }
        try events.save(ev, span: .thisEvent, commit: true)
        return ["created": true, "id": ev.eventIdentifier ?? "", "calendar": ev.calendar?.title ?? ""]
    }
    private func remindersList(_ a: [String: Any]) async throws -> [String: Any] {
        guard try await remindersAccess() else { throw BridgeError(msg: "没有提醒事项权限") }
        var cals: [EKCalendar]? = nil
        if let name = a["list"] as? String, !name.isEmpty { cals = events.calendars(for: .reminder).filter { $0.title == name } }
        let all = a["include_completed"] as? Bool ?? false
        let pred = all ? events.predicateForReminders(in: cals)
            : events.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil, calendars: cals)
        let items: [EKReminder] = await withCheckedContinuation { c in events.fetchReminders(matching: pred) { c.resume(returning: $0 ?? []) } }
        return ["reminders": items.prefix(200).map { r in
            ["id": r.calendarItemIdentifier, "title": r.title ?? "", "done": r.isCompleted, "list": r.calendar?.title ?? "",
             "due": r.dueDateComponents?.date.map { self.iso($0) } ?? "", "priority": r.priority] as [String: Any] }]
    }
    private func remindersCreate(_ a: [String: Any]) async throws -> [String: Any] {
        guard try await remindersAccess() else { throw BridgeError(msg: "没有提醒事项权限") }
        let r = EKReminder(eventStore: events)
        r.title = a["title"] as? String ?? "提醒"; r.notes = a["notes"] as? String
        r.priority = a["priority"] as? Int ?? 0
        if let name = a["list"] as? String, let c = events.calendars(for: .reminder).first(where: { $0.title == name }) { r.calendar = c }
        else { r.calendar = events.defaultCalendarForNewReminders() }
        if let d = date(a["due"]) {
            r.dueDateComponents = Calendar.current.dateComponents([.year, .month, .day, .hour, .minute], from: d)
            r.addAlarm(EKAlarm(absoluteDate: d))
        }
        try events.save(r, commit: true)
        return ["created": true, "id": r.calendarItemIdentifier]
    }
    private func remindersComplete(_ a: [String: Any]) async throws -> [String: Any] {
        guard try await remindersAccess() else { throw BridgeError(msg: "没有提醒事项权限") }
        guard let id = a["id"] as? String, let r = events.calendarItem(withIdentifier: id) as? EKReminder else { throw BridgeError(msg: "找不到这条提醒") }
        r.isCompleted = true
        try events.save(r, commit: true)
        return ["completed": true, "id": id]
    }
    private func remindersDelete(_ a: [String: Any]) async throws -> [String: Any] {
        guard try await remindersAccess() else { throw BridgeError(msg: "没有提醒事项权限") }
        guard let id = a["id"] as? String, let r = events.calendarItem(withIdentifier: id) as? EKReminder else { throw BridgeError(msg: "找不到这条提醒") }
        try events.remove(r, commit: true)
        return ["deleted": true, "id": id]
    }
    /// v3.9.28 calendar_today: today's events in the phone's own time zone
    func calendarToday(_ a: [String: Any]) async throws -> [String: Any] {
        guard try await calendarAccess() else { throw BridgeError(msg: "没有日历权限（设置 › 隐私 › 日历 › AIW）") }
        let start = Calendar.current.startOfDay(for: Date())
        let end = Calendar.current.date(byAdding: .day, value: 1, to: start) ?? start.addingTimeInterval(86400)
        let pred = events.predicateForEvents(withStart: start, end: end, calendars: nil)
        let list = events.events(matching: pred).sorted { $0.startDate < $1.startDate }.prefix(100)
        let df = DateFormatter(); df.calendar = Calendar(identifier: .gregorian); df.locale = Locale(identifier: "en_US_POSIX")
        df.timeZone = .current; df.dateFormat = "yyyy-MM-dd"
        return ["events": list.map { ["id": $0.eventIdentifier ?? "", "title": $0.title ?? "", "start": iso($0.startDate),
                                       "end": iso($0.endDate), "all_day": $0.isAllDay, "location": $0.location ?? "",
                                       "calendar": $0.calendar?.title ?? ""] as [String: Any] },
                "date": df.string(from: start), "tz": TimeZone.current.identifier]
    }

    // MARK: Phone / SMS (v3.9.28)
    static func cleanNumber(_ s: String) -> String {
        s.filter { ($0.isASCII && $0.isNumber) || $0 == "+" || $0 == "*" || $0 == "#" || $0 == "," }
    }
    private func phoneCall(_ a: [String: Any]) async throws -> [String: Any] {
        let num = Self.cleanNumber(a["number"] as? String ?? "")
        guard num.filter(\.isNumber).count >= 3, let u = URL(string: "tel:" + num) else { throw BridgeError(msg: "号码不对") }
        let active = await MainActor.run { UIApplication.shared.applicationState == .active }
        guard active else { throw BridgeError(msg: "App 不在前台，不能拨号") }
        let opened: Bool = await withCheckedContinuation { cc in DispatchQueue.main.async { UIApplication.shared.open(u) { cc.resume(returning: $0) } } }
        if !opened { throw BridgeError(msg: "这台设备不能打电话") }
        var out: [String: Any] = ["started": true, "number": num]
        if let n = a["name"] as? String, !n.isEmpty { out["name"] = n }
        return out
    }
    private var smsWait: CheckedContinuation<MessageComposeResult?, Never>?
    private func smsSend(_ a: [String: Any]) async throws -> [String: Any] {
        let to = (a["to"] as? String ?? "").split(whereSeparator: { $0 == "," || $0 == "，" || $0 == ";" })
            .map { Self.cleanNumber(String($0)) }.filter { !$0.isEmpty }
        guard !to.isEmpty else { throw BridgeError(msg: "缺少收件人号码") }
        let body = a["body"] as? String ?? ""
        let can = await MainActor.run { MFMessageComposeViewController.canSendText() && UIApplication.shared.applicationState == .active }
        guard can else { throw BridgeError(msg: "这台设备现在不能发短信（App 需在前台）") }
        let res: MessageComposeResult? = await withCheckedContinuation { (c: CheckedContinuation<MessageComposeResult?, Never>) in
            DispatchQueue.main.async {
                guard self.smsWait == nil, let top = Self.topController() else { c.resume(returning: nil); return }
                let vc = MFMessageComposeViewController()
                vc.recipients = to; vc.body = body; vc.messageComposeDelegate = self
                self.smsWait = c
                top.present(vc, animated: true)
            }
        }
        guard let result = res else { throw BridgeError(msg: "短信界面已打开或无法显示") }
        if result == .failed { throw BridgeError(msg: "短信发送失败") }
        return ["sent": result == .sent, "presented": true, "to": to.joined(separator: ",")]
    }
    static func topController() -> UIViewController? {
        let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        let win = scenes.flatMap { $0.windows }.first { $0.isKeyWindow } ?? scenes.first?.windows.first
        var top = win?.rootViewController
        while let p = top?.presentedViewController { top = p }
        return top
    }

    // MARK: Contacts
    private func contactsSearch(_ a: [String: Any]) async throws -> [String: Any] {
        guard try await contacts.requestAccess(for: .contacts) else { throw BridgeError(msg: "没有通讯录权限") }
        let q = (a["query"] as? String ?? "").trimmingCharacters(in: .whitespaces)
        let limit = a["limit"] as? Int ?? 20
        let keys: [CNKeyDescriptor] = [CNContactGivenNameKey, CNContactFamilyNameKey, CNContactOrganizationNameKey,
                                       CNContactPhoneNumbersKey, CNContactEmailAddressesKey] as [CNKeyDescriptor]
        var found: [CNContact] = []
        if q.rangeOfCharacter(from: .decimalDigits) != nil && q.filter(\.isNumber).count >= 3 {
            found = (try? contacts.unifiedContacts(matching: CNContact.predicateForContacts(matching: CNPhoneNumber(stringValue: q)), keysToFetch: keys)) ?? []
        } else if q.contains("@") {
            found = (try? contacts.unifiedContacts(matching: CNContact.predicateForContacts(matchingEmailAddress: q), keysToFetch: keys)) ?? []
        } else {
            found = (try? contacts.unifiedContacts(matching: CNContact.predicateForContacts(matchingName: q), keysToFetch: keys)) ?? []
        }
        return ["contacts": found.prefix(limit).map { c in
            ["name": "\(c.familyName)\(c.givenName)".isEmpty ? c.organizationName : "\(c.familyName)\(c.givenName)",
             "org": c.organizationName, "phones": c.phoneNumbers.map { $0.value.stringValue },
             "emails": c.emailAddresses.map { $0.value as String }] as [String: Any] }]
    }

    // MARK: HealthKit
    private var readTypes: Set<HKObjectType> {
        var s: Set<HKObjectType> = [HKObjectType.workoutType()]
        if let t = HKObjectType.quantityType(forIdentifier: .stepCount) { s.insert(t) }
        if let t = HKObjectType.quantityType(forIdentifier: .heartRate) { s.insert(t) }
        if let t = HKObjectType.categoryType(forIdentifier: .sleepAnalysis) { s.insert(t) }
        return s
    }
    private var writeTypes: Set<HKSampleType> {
        var s: Set<HKSampleType> = []
        if let t = HKObjectType.quantityType(forIdentifier: .dietaryWater) { s.insert(t) }
        if let t = HKObjectType.quantityType(forIdentifier: .bodyMass) { s.insert(t) }
        if let t = HKObjectType.categoryType(forIdentifier: .mindfulSession) { s.insert(t) }
        return s
    }
    private func healthAccess() async throws {
        guard HKHealthStore.isHealthDataAvailable() else { throw BridgeError(msg: "这台设备没有健康数据") }
        try await health.requestAuthorization(toShare: enabled("health.write") ? writeTypes : [], read: readTypes)
    }
    private func samples(_ type: HKSampleType, _ start: Date, _ end: Date, limit: Int = 500) async throws -> [HKSample] {
        try await withCheckedThrowingContinuation { c in
            let q = HKSampleQuery(sampleType: type, predicate: HKQuery.predicateForSamples(withStart: start, end: end),
                                  limit: limit, sortDescriptors: [NSSortDescriptor(key: HKSampleSortIdentifierStartDate, ascending: true)]) { _, r, e in
                if let e = e { c.resume(throwing: e) } else { c.resume(returning: r ?? []) }
            }
            health.execute(q)
        }
    }
    private func healthRead(_ a: [String: Any]) async throws -> [String: Any] {
        try await healthAccess()
        let start = date(a["start"]) ?? Calendar.current.startOfDay(for: Date()), end = date(a["end"]) ?? Date()
        switch a["kind"] as? String ?? "steps" {
        case "steps":
            guard let t = HKQuantityType.quantityType(forIdentifier: .stepCount) else { break }
            let total: Double = try await withCheckedThrowingContinuation { c in
                let q = HKStatisticsQuery(quantityType: t, quantitySamplePredicate: HKQuery.predicateForSamples(withStart: start, end: end),
                                          options: .cumulativeSum) { _, s, e in
                    if let e = e as? HKError, e.code == .errorNoData { c.resume(returning: 0) }
                    else if let e = e { c.resume(throwing: e) }
                    else { c.resume(returning: s?.sumQuantity()?.doubleValue(for: .count()) ?? 0) }
                }
                health.execute(q)
            }
            return ["kind": "steps", "start": iso(start), "end": iso(end), "steps": Int(total)]
        case "heart_rate":
            guard let t = HKQuantityType.quantityType(forIdentifier: .heartRate) else { break }
            let unit = HKUnit.count().unitDivided(by: .minute())
            let s = try await samples(t, start, end).compactMap { $0 as? HKQuantitySample }
            let v = s.map { $0.quantity.doubleValue(for: unit) }
            return ["kind": "heart_rate", "count": v.count, "min": v.min() ?? 0, "max": v.max() ?? 0,
                    "avg": v.isEmpty ? 0 : v.reduce(0, +) / Double(v.count),
                    "recent": s.suffix(20).map { ["at": iso($0.startDate), "bpm": Int($0.quantity.doubleValue(for: unit))] as [String: Any] }]
        case "sleep":
            guard let t = HKCategoryType.categoryType(forIdentifier: .sleepAnalysis) else { break }
            let s = try await samples(t, start, end).compactMap { $0 as? HKCategorySample }
            let asleep = s.filter { $0.value != HKCategoryValueSleepAnalysis.inBed.rawValue && $0.value != HKCategoryValueSleepAnalysis.awake.rawValue }
            let mins = asleep.reduce(0.0) { $0 + $1.endDate.timeIntervalSince($1.startDate) } / 60
            return ["kind": "sleep", "asleep_minutes": Int(mins), "segments": s.suffix(60).map { ["start": iso($0.startDate), "end": iso($0.endDate), "value": $0.value] as [String: Any] }]
        case "workouts":
            let s = try await samples(HKObjectType.workoutType(), start, end, limit: 50).compactMap { $0 as? HKWorkout }
            return ["kind": "workouts", "workouts": s.map { w in
                ["start": iso(w.startDate), "minutes": Int(w.duration / 60), "type": Int(w.workoutActivityType.rawValue),
                 "kcal": Int(w.totalEnergyBurned?.doubleValue(for: .kilocalorie()) ?? 0),
                 "meters": Int(w.totalDistance?.doubleValue(for: .meter()) ?? 0)] as [String: Any] }]
        default: break
        }
        throw BridgeError(msg: "不支持的健康数据类型")
    }
    private func healthWrite(_ a: [String: Any]) async throws -> [String: Any] {
        try await healthAccess()
        let at = date(a["at"]) ?? Date()
        let v = (a["value"] as? Double) ?? Double(a["value"] as? Int ?? 0)
        let sample: HKSample
        switch a["kind"] as? String ?? "" {
        case "water":
            guard let t = HKQuantityType.quantityType(forIdentifier: .dietaryWater), v > 0, v < 5000 else { throw BridgeError(msg: "饮水量应在 0–5000 ml") }
            sample = HKQuantitySample(type: t, quantity: HKQuantity(unit: .literUnit(with: .milli), doubleValue: v), start: at, end: at)
        case "weight":
            guard let t = HKQuantityType.quantityType(forIdentifier: .bodyMass), v > 2, v < 400 else { throw BridgeError(msg: "体重应在 2–400 kg") }
            sample = HKQuantitySample(type: t, quantity: HKQuantity(unit: .gramUnit(with: .kilo), doubleValue: v), start: at, end: at)
        case "mindful":
            guard let t = HKCategoryType.categoryType(forIdentifier: .mindfulSession), v > 0, v <= 240 else { throw BridgeError(msg: "正念 1–240 分钟") }
            sample = HKCategorySample(type: t, value: HKCategoryValue.notApplicable.rawValue, start: at, end: at.addingTimeInterval(v * 60))
        default: throw BridgeError(msg: "只能写入 water / weight / mindful")
        }
        try await health.save(sample)
        return ["saved": true, "kind": a["kind"] as? String ?? "", "value": v]
    }

    // MARK: CoreLocation
    private func ensureLocMgr() {   // always called on the main thread
        if locMgr == nil { let m = CLLocationManager(); m.delegate = self; m.desiredAccuracy = kCLLocationAccuracyHundredMeters; locMgr = m }
    }
    private func locationGet() async throws -> [String: Any] {
        let loc: CLLocation = try await withCheckedThrowingContinuation { c in
            DispatchQueue.main.async {
                self.ensureLocMgr()
                self.locWait.append(c)
                let st = self.locMgr!.authorizationStatus
                if st == .notDetermined { self.locMgr!.requestWhenInUseAuthorization() }
                else if st == .denied || st == .restricted {
                    self.locWait.forEach { $0.resume(throwing: BridgeError(msg: "没有定位权限")) }; self.locWait = []
                } else { self.locMgr!.requestLocation() }
            }
        }
        var out: [String: Any] = ["lat": loc.coordinate.latitude, "lng": loc.coordinate.longitude,
                                  "accuracy_m": loc.horizontalAccuracy, "at": iso(loc.timestamp)]
        if let p = try? await CLGeocoder().reverseGeocodeLocation(loc).first {
            out["address"] = [p.administrativeArea, p.locality, p.subLocality, p.thoroughfare, p.name].compactMap { $0 }.joined(separator: " ")
        }
        return out
    }

    // MARK: AlarmKit (iOS 26+, build with -D AIW_ALARMKIT; needs NSAlarmKitUsageDescription)
    private func alarmAccess() async throws -> Bool {
        #if AIW_ALARMKIT && canImport(AlarmKit)
        if #available(iOS 26.0, *) {
            let st = try await AlarmManager.shared.requestAuthorization()
            return st == .authorized
        }
        #endif
        return false
    }
    private func alarmSet(_ a: [String: Any]) async throws -> [String: Any] {
        #if AIW_ALARMKIT && canImport(AlarmKit)
        if #available(iOS 26.0, *) {
            guard try await alarmAccess() else { throw BridgeError(msg: "没有闹钟权限（设置 › AIW › 闹钟）") }
            let ts = a["time"] as? String ?? ""
            var fire = date(ts)
            if fire == nil, let m = ts.range(of: #"^(\d{1,2}):(\d{2})$"#, options: .regularExpression).map({ String(ts[$0]) }) {
                let p = m.split(separator: ":").compactMap { Int($0) }
                var dc = Calendar.current.dateComponents([.year, .month, .day], from: Date()); dc.hour = p[0]; dc.minute = p[1]
                fire = Calendar.current.date(from: dc).map { $0 < Date() ? $0.addingTimeInterval(86400) : $0 }
            }
            guard let at = fire else { throw BridgeError(msg: "time 要是 HH:MM 或 ISO 时间") }
            let title = (a["label"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "aiw 闹钟"
            let alert = AlarmPresentation.Alert(title: LocalizedStringResource(stringLiteral: title),
                                                stopButton: AlarmButton(text: "停止", textColor: .white, systemImageName: "stop.circle"))
            let attrs = AlarmAttributes<AiwAlarmMeta>(presentation: AlarmPresentation(alert: alert), metadata: AiwAlarmMeta(), tintColor: .orange)
            let id = UUID()
            _ = try await AlarmManager.shared.schedule(id: id, configuration: .alarm(schedule: .fixed(at), attributes: attrs))
            return ["scheduled": true, "id": id.uuidString, "at": iso(at)]
        }
        #endif
        throw BridgeError(msg: alarmKitAvailable ? "闹钟设置失败" : "这台手机或这个安装包不支持 AlarmKit（需要 iOS 26+ 且用 AIW_ALARMKIT 编译、带闹钟权限）")
    }

    // MARK: Shortcuts (URL scheme; the shortcut's output is not returned)
    private func shortcutRun(_ a: [String: Any]) async throws -> [String: Any] {
        guard let name = a["name"] as? String, !name.isEmpty else { throw BridgeError(msg: "缺少快捷指令名称") }
        var c = URLComponents(string: "shortcuts://run-shortcut")!
        c.queryItems = [URLQueryItem(name: "name", value: name)]
        if let t = a["input"] as? String, !t.isEmpty { c.queryItems! += [URLQueryItem(name: "input", value: "text"), URLQueryItem(name: "text", value: t)] }
        guard let u = c.url else { throw BridgeError(msg: "名称无效") }
        let ok = await MainActor.run { UIApplication.shared.applicationState == .active }
        guard ok else { throw BridgeError(msg: "App 不在前台，不能打开快捷指令") }
        let opened: Bool = await withCheckedContinuation { cc in DispatchQueue.main.async { UIApplication.shared.open(u) { cc.resume(returning: $0) } } }
        if !opened { throw BridgeError(msg: "打不开快捷指令 App") }
        return ["launched": true, "name": name, "note": "快捷指令已启动，它的输出不会返回给 Agent"]
    }
}

#if AIW_ALARMKIT && canImport(AlarmKit)
@available(iOS 26.0, *)
struct AiwAlarmMeta: AlarmMetadata {}
#endif

extension DeviceBridge: MFMessageComposeViewControllerDelegate {
    func messageComposeViewController(_ controller: MFMessageComposeViewController, didFinishWith result: MessageComposeResult) {
        controller.dismiss(animated: true)
        let w = smsWait; smsWait = nil
        w?.resume(returning: result)
    }
}

extension DeviceBridge: CLLocationManagerDelegate {
    func locationManager(_ m: CLLocationManager, didUpdateLocations locs: [CLLocation]) {
        guard let l = locs.last else { return }
        locWait.forEach { $0.resume(returning: l) }; locWait = []
    }
    func locationManager(_ m: CLLocationManager, didFailWithError e: Error) {
        locWait.forEach { $0.resume(throwing: e) }; locWait = []
    }
    func locationManagerDidChangeAuthorization(_ m: CLLocationManager) {
        guard !locWait.isEmpty else { return }
        switch m.authorizationStatus {
        case .authorizedWhenInUse, .authorizedAlways: m.requestLocation()
        case .denied, .restricted: locWait.forEach { $0.resume(throwing: BridgeError(msg: "没有定位权限")) }; locWait = []
        default: break
        }
    }
}

/// 设置 › 设备能力: which phone features the agent may use. Off by default; switching one on asks iOS for permission.
struct DeviceCapsView: View {
    @ObservedObject var bridge = DeviceBridge.shared
    @State private var tick = 0
    var body: some View {
        Form {
            Section(footer: Text(bridge.online ? "已连上服务器，Agent 可以调用下面打开的能力" : (bridge.lastError.isEmpty ? "打开任意一项后连接；App 在后台时调用会排队，推送提醒你打开 App" : "未连接：\(bridge.lastError)"))) {
                HStack { Text("状态"); Spacer(); Text(bridge.online ? "在线" : "离线").foregroundStyle(bridge.online ? .green : .secondary) }
                if !bridge.lastCall.isEmpty { LabeledContent("最近一次", value: bridge.lastCall) }
            }
            Section(header: Text("允许 Agent 使用"), footer: Text("新建日程、提醒事项、写健康、设闹钟、运行快捷指令前，都会先弹确认卡片（网页或手机上都能点）。")) {
                ForEach(DeviceBridge.allCaps, id: \.0) { cap in
                    Toggle(cap.1, isOn: Binding(get: { _ = tick; return bridge.enabled(cap.0) },
                                                set: { bridge.setEnabled(cap.0, $0); tick += 1; bridge.start() }))
                        .disabled(cap.0 == "alarm.write" && !bridge.alarmKitAvailable)
                }
            }
        }
        .navigationTitle("设备能力").navigationBarTitleDisplayMode(.inline)
    }
}
