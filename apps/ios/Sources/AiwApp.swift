import SwiftUI
import Combine
import UserNotifications

@main
struct AiwApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate   // orientation: OrientationLock (DesktopView.swift)
    @StateObject private var api = API.shared
    @StateObject private var hub = Hub.shared
    @AppStorage("appearance") private var appearance = "system"
    @Environment(\.scenePhase) private var phase
    var body: some Scene {
        WindowGroup {
            Group {
                if api.configured { RootView() } else { ConnectView() }
            }
            .environmentObject(api).environmentObject(hub)
            .tint(amber)
            .preferredColorScheme(appearance == "light" ? .light : appearance == "dark" ? .dark : nil)
            .onAppear {
                UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
                UNUserNotificationCenter.current().delegate = NotifyDelegate.shared
                if api.configured { hub.start() }
            }
            .onChange(of: api.token) { _ in api.configured ? hub.start() : hub.stop() }
            // 微信「用其他应用打开」/ Open In delivers a file URL; the share extension opens aiw://import (Import.swift)
            .onOpenURL { ImportCenter.shared.handle($0) }
            .onChange(of: phase) { p in if p == .active { ImportCenter.shared.collectShared() } }
        }
    }
}

final class NotifyDelegate: NSObject, UNUserNotificationCenterDelegate {
    static let shared = NotifyDelegate()
    func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification,
                                withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
        done([.banner, .sound])
    }
}

/// The console-wide stream (/ws/events): task status changes and notifications. Views listen to `tick`.
final class Hub: ObservableObject {
    static let shared = Hub()
    @Published var tick = 0
    @Published var lastEvent: JSON = [:]
    /// Every event, one by one (onChange(of: tick) can coalesce a burst; the chat must not miss a message)
    let events = PassthroughSubject<JSON, Never>()
    private var sock: Socket?
    func start() {
        stop()
        guard let u = API.shared.wsURL("/ws/events") else { return }
        let s = Socket(u)
        s.onMessage = { [weak self] m in
            guard let self = self else { return }
            self.lastEvent = m
            self.tick += 1
            self.events.send(m)
            if m.s("kind") == "notification" {
                let n = m.o("notification")
                let c = UNMutableNotificationContent()
                c.title = n.s("title"); c.body = n.s("body"); c.sound = .default
                UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: n.s("id"), content: c, trigger: nil))
            }
        }
        s.start(); sock = s
    }
    func stop() { sock?.stop(); sock = nil }
}

struct ConnectView: View {
    @EnvironmentObject var api: API
    @State private var server = API.shared.server
    @State private var token = ""
    @State private var busy = false
    @State private var err = ""
    var body: some View {
        NavigationStack {
            Form {
                Section {
                    HStack(spacing: 14) {
                        Image(systemName: "face.smiling").font(.system(size: 30, weight: .semibold)).foregroundColor(.white)
                            .frame(width: 56, height: 56).background(amber).clipShape(RoundedRectangle(cornerRadius: 14))
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Aiw").font(.title2.bold())
                            Text("连接你的 AI 工作站").font(.subheadline).foregroundColor(.secondary)
                        }
                    }.padding(.vertical, 6)
                }
                Section(header: Text("服务器"), footer: Text("例如 http://192.168.50.188:18000")) {
                    TextField("地址", text: $server).keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                    SecureField("Admin Token", text: $token)
                }
                if !err.isEmpty { Section { Text(err).foregroundColor(.red) } }
                Section {
                    Button { Task { await connect() } } label: {
                        HStack { Spacer(); if busy { ProgressView() } else { Text("连接").bold() }; Spacer() }
                    }.disabled(server.isEmpty || token.isEmpty || busy)
                }
            }.navigationTitle("登录")
        }
    }
    func connect() async {
        busy = true; err = ""
        let old = (api.server, api.token)
        api.save(server: server, token: token)
        do { _ = try await api.list("/api/threads") } catch {
            err = error.localizedDescription; api.save(server: old.0, token: old.1)
        }
        busy = false
    }
}
