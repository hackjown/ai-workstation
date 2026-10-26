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
                    .frame(width: min(UIScreen.main.bounds.width * 0.84, 340))
                    .background(Color(.systemBackground).ignoresSafeArea())
                    .transition(.move(edge: .leading))
                    .gesture(DragGesture().onEnded { g in if g.translation.width < -60 { withAnimation { drawer = false } } })
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
                Image(systemName: "face.smiling").font(.system(size: 15, weight: .bold)).foregroundColor(.white)
                    .frame(width: 30, height: 30).background(amber).clipShape(RoundedRectangle(cornerRadius: 8))
                Text("Aiw").font(.title3.bold())
                Spacer()
                Button(action: newChat) { Image(systemName: "square.and.pencil").font(.system(size: 18)) }
            }.padding(.horizontal, 16).padding(.top, 12).padding(.bottom, 10)
            Picker("", selection: $tab) { Text("历史对话").tag(0); Text("归档").tag(1) }
                .pickerStyle(.segmented).padding(.horizontal, 16)
            HStack {
                Image(systemName: "magnifyingglass").foregroundColor(.secondary)
                TextField("搜索", text: $query)
            }.padding(8).background(Color(.secondarySystemBackground)).clipShape(RoundedRectangle(cornerRadius: 10))
                .padding(.horizontal, 16).padding(.vertical, 8)
            List {
                if !err.isEmpty { Text(err).foregroundColor(.red).font(.footnote) }
                ForEach(shown.indices, id: \.self) { i in
                    let t = shown[i]
                    Button { pick(t.s("id")) } label: { ThreadRow(t: t) }
                        .listRowBackground(t.s("id") == current ? amber.opacity(0.12) : Color.clear)
                        .swipeActions(edge: .trailing) {
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
                HStack { Image(systemName: "gearshape"); Text("设置"); Spacer() }.foregroundColor(.primary)
                    .padding(.horizontal, 16).padding(.vertical, 14)
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
                Text(t.s("title").isEmpty ? "新对话" : t.s("title")).font(.subheadline.weight(.semibold)).lineLimit(1).foregroundColor(.primary)
                Spacer()
                Text(when(t.d("updated_at"))).font(.caption2).foregroundColor(.secondary)
            }
            HStack(spacing: 5) {
                if isLive(st) { Circle().fill(st.hasPrefix("waiting") ? Color.orange : amber).frame(width: 6, height: 6) }
                Text(isLive(st) || st == "failed" ? (statusText[st] ?? st) + " · " + t.s("last_prompt") : t.s("last_prompt"))
                    .font(.caption).foregroundColor(.secondary).lineLimit(1)
            }
        }.padding(.vertical, 3)
    }
}
