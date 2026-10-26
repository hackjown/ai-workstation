import SwiftUI

/// Opens straight into a fresh conversation; history and archive live in the drawer behind the top-left button.
struct RootView: View {
    @EnvironmentObject var api: API
    @State private var current: String? = nil
    @State private var fresh = UUID()
    @State private var drawer = false
    @State private var showSettings = false

    var body: some View {
        ZStack(alignment: .leading) {
            NavigationStack {
                ChatView(threadId: current, onThread: { current = $0 })
                    .id(fresh)   // a new key only when the user picks a thread or starts a new one
                    .toolbar {
                        ToolbarItem(placement: .navigationBarLeading) {
                            Button { withAnimation(.easeOut(duration: 0.22)) { drawer = true } } label: {
                                Image(systemName: "line.3.horizontal").font(.system(size: 17, weight: .semibold))
                            }
                        }
                        ToolbarItem(placement: .navigationBarTrailing) {
                            Button { newChat() } label: { Image(systemName: "square.and.pencil") }
                        }
                    }
            }
            if drawer {
                Color.black.opacity(0.28).ignoresSafeArea()
                    .onTapGesture { withAnimation(.easeOut(duration: 0.2)) { drawer = false } }
                    .transition(.opacity)
                HistoryDrawer(current: current, pick: { id in
                    current = id; fresh = UUID()
                    withAnimation(.easeOut(duration: 0.2)) { drawer = false }
                }, newChat: { newChat(); withAnimation { drawer = false } },
                   settings: { drawer = false; showSettings = true })
                    .frame(width: max(UIScreen.main.bounds.width * 0.5, 210))
                    .background(Color(.systemBackground).ignoresSafeArea())
                    .transition(.move(edge: .leading))
            }
        }
        .sheet(isPresented: $showSettings) { SettingsView() }
    }
    func newChat() { current = nil; fresh = UUID() }
}

struct HistoryDrawer: View {
    @EnvironmentObject var api: API
    @EnvironmentObject var hub: Hub
    let current: String?
    let pick: (String) -> Void
    let newChat: () -> Void
    let settings: () -> Void
    @State private var tab = 0
    @State private var threads: [JSON] = []
    @State private var err = ""
    @State private var query = ""

    var shown: [JSON] {
        query.isEmpty ? threads : threads.filter { ($0.s("title") + $0.s("last_prompt")).localizedCaseInsensitiveContains(query) }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                Image(systemName: "face.smiling").font(.system(size: 12, weight: .bold)).foregroundColor(.white)
                    .frame(width: 24, height: 24).background(amber).clipShape(RoundedRectangle(cornerRadius: 6))
                Text("Aiw").font(.headline)
                Spacer()
                Button(action: newChat) { Image(systemName: "square.and.pencil").font(.system(size: 15)) }
            }.padding(.horizontal, 12).padding(.top, 10).padding(.bottom, 8)
            Picker("", selection: $tab) { Text("历史对话").tag(0); Text("归档").tag(1) }
                .pickerStyle(.segmented).padding(.horizontal, 10)
            HStack {
                Image(systemName: "magnifyingglass").foregroundColor(.secondary)
                TextField("搜索", text: $query).font(.footnote)
            }.font(.footnote).padding(6).background(Color(.secondarySystemBackground)).clipShape(RoundedRectangle(cornerRadius: 10))
                .padding(.horizontal, 10).padding(.vertical, 6)
            List {
                if !err.isEmpty { Text(err).foregroundColor(.red).font(.footnote) }
                ForEach(shown.indices, id: \.self) { i in
                    let t = shown[i]
                    Button { pick(t.s("id")) } label: { ThreadRow(t: t) }
                        .listRowInsets(EdgeInsets(top: 4, leading: 12, bottom: 4, trailing: 10))
                        .listRowBackground(t.s("id") == current ? amber.opacity(0.12) : Color(.systemBackground))
                        .contextMenu {
                            Button { Task { await archive(t.s("id")) } } label: { Label(tab == 1 ? "恢复" : "归档", systemImage: "archivebox") }
                            Button(role: .destructive) { Task { await del(t.s("id")) } } label: { Label("删除", systemImage: "trash") }
                        }
                        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                            Button(role: .destructive) { Task { await del(t.s("id")) } } label: { Label("删除", systemImage: "trash") }
                            Button { Task { await archive(t.s("id")) } } label: { Label(tab == 1 ? "恢复" : "归档", systemImage: "archivebox") }.tint(.gray)
                        }
                }
                if shown.isEmpty && err.isEmpty {
                    Text(tab == 1 ? "没有归档的对话" : "还没有对话").foregroundColor(.secondary).font(.subheadline)
                }
            }.listStyle(.plain).refreshable { await load() }
            Divider()
            Button(action: settings) {
                HStack { Image(systemName: "gearshape"); Text("设置"); Spacer() }.font(.subheadline).foregroundColor(.primary)
                    .padding(.horizontal, 12).padding(.vertical, 12)
            }
        }
        .task { await load() }
        .onChange(of: tab) { _ in Task { await load() } }
        .onChange(of: hub.tick) { _ in if hub.lastEvent.s("kind") == "task" { Task { await load() } } }
    }

    func load() async {
        do { threads = try await api.list("/api/threads?archived=\(tab == 1)"); err = "" }
        catch { err = error.localizedDescription }
    }
    func del(_ id: String) async { _ = try? await api.call("DELETE", "/api/threads/\(id)"); await load() }
    func archive(_ id: String) async { _ = try? await api.call("PATCH", "/api/threads/\(id)", ["archived": tab != 1]); await load() }
}

struct ThreadRow: View {
    let t: JSON
    var body: some View {
        let st = t.s("last_status")
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text(cleanTitle(t.s("title"))).font(.footnote.weight(.semibold)).lineLimit(1).foregroundColor(.primary)
            }
            HStack(spacing: 5) {
                if isLive(st) { Circle().fill(st.hasPrefix("waiting") ? Color.orange : amber).frame(width: 6, height: 6) }
                Text(isLive(st) || st == "failed" ? (statusText[st] ?? st) + " · " + t.s("last_prompt") : t.s("last_prompt"))
                    .font(.caption2).foregroundColor(.secondary).lineLimit(1)
            }
            Text(when(t.d("updated_at"))).font(.system(size: 10)).foregroundColor(.secondary)
        }.padding(.vertical, 2)
    }
}

/// Drop the 【技能：…】 prefix a skill pick adds, so titles show what was asked.
func cleanTitle(_ t: String) -> String {
    var s = t
    if s.hasPrefix("【技能"), let r = s.range(of: "】") { s = String(s[r.upperBound...]) }
    s = s.trimmingCharacters(in: .whitespacesAndNewlines)
    return s.isEmpty ? "新对话" : s
}
