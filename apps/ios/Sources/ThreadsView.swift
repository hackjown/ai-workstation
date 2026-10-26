import SwiftUI

struct ThreadsView: View {
    @EnvironmentObject var api: API
    @EnvironmentObject var hub: Hub
    @State private var threads: [JSON] = []
    @State private var archived = false
    @State private var path: [String] = []
    @State private var err = ""
    @State private var showSettings = false

    var body: some View {
        NavigationStack(path: $path) {
            List {
                if !err.isEmpty { Text(err).foregroundColor(.red).font(.footnote) }
                ForEach(threads.indices, id: \.self) { i in
                    let t = threads[i]
                    NavigationLink(value: t.s("id")) { ThreadRow(t: t) }
                        .swipeActions(edge: .trailing) {
                            Button(role: .destructive) { Task { await del(t.s("id")) } } label: { Label("删除", systemImage: "trash") }
                            Button { Task { await archive(t.s("id")) } } label: { Label(archived ? "恢复" : "归档", systemImage: "archivebox") }.tint(.gray)
                        }
                }
                if threads.isEmpty && err.isEmpty {
                    Text(archived ? "没有归档的对话" : "还没有对话，点右上角开始").foregroundColor(.secondary)
                }
            }
            .listStyle(.plain)
            .refreshable { await load() }
            .navigationTitle(archived ? "归档" : "Aiw")
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) {
                    Menu {
                        Button { archived.toggle(); Task { await load() } } label: { Label(archived ? "全部对话" : "归档", systemImage: "archivebox") }
                        Button { showSettings = true } label: { Label("设置", systemImage: "gearshape") }
                    } label: { Image(systemName: "line.3.horizontal") }
                }
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button { path.append("new") } label: { Image(systemName: "square.and.pencil") }
                }
            }
            .navigationDestination(for: String.self) { id in ChatView(threadId: id == "new" ? nil : id) }
            .sheet(isPresented: $showSettings) { SettingsView() }
        }
        .task { await load() }
        .onChange(of: hub.tick) { _ in
            if hub.lastEvent.s("kind") == "task" { Task { await load() } }
        }
    }

    func load() async {
        do { threads = try await api.list("/api/threads?archived=\(archived)"); err = "" }
        catch { err = error.localizedDescription }
    }
    func del(_ id: String) async { _ = try? await api.call("DELETE", "/api/threads/\(id)"); await load() }
    func archive(_ id: String) async { _ = try? await api.call("PATCH", "/api/threads/\(id)", ["archived": !archived]); await load() }
}

struct ThreadRow: View {
    let t: JSON
    var body: some View {
        let st = t.s("last_status")
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(t.s("title").isEmpty ? "新对话" : t.s("title")).font(.headline).lineLimit(1)
                Spacer()
                Text(when(t.d("updated_at"))).font(.caption).foregroundColor(.secondary)
            }
            HStack(spacing: 6) {
                if isLive(st) { Circle().fill(st.hasPrefix("waiting") ? Color.orange : amber).frame(width: 7, height: 7) }
                Text(t.s("last_prompt")).font(.subheadline).foregroundColor(.secondary).lineLimit(2)
            }
            if isLive(st) || st == "failed" {
                Text(statusText[st] ?? st).font(.caption2.bold()).foregroundColor(st == "failed" ? .red : amber)
            }
        }.padding(.vertical, 4)
    }
}
