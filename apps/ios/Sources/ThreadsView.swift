import SwiftUI
import AVFoundation

/// iOS v2 home: always a fresh conversation at launch (past ones live in 历史对话). No drawer: the gear opens 设置,
/// the "+" opens a native popover menu; every item but 新建对话 opens a large sheet with × and a centred title.
enum HomeSheet: String, Identifiable {
    case history, archive, tracking, routine, computer
    var id: String { rawValue }
    var title: String {
        switch self {
        case .history: return "历史对话"
        case .archive: return "归档"
        case .tracking: return "追踪"
        case .routine: return "例程"
        case .computer: return "电脑"
        }
    }
}

struct RootView: View {
    @EnvironmentObject var api: API
    @State private var current: String? = nil      // never restored: the app opens on 新对话
    @State private var fresh = UUID()
    @State private var sheet: HomeSheet?
    @State private var showSettings = false

    var body: some View {
        NavigationStack {
            ChatView(threadId: current, onThread: { current = $0 })
                .id(fresh)   // a new key only when the user picks a thread or starts a new one
                .toolbar {
                    ToolbarItem(placement: .navigationBarLeading) {
                        Button { showSettings = true } label: {
                            Image(systemName: "gearshape").font(.system(size: 17, weight: .regular))
                        }.accessibilityLabel("设置")
                    }
                    ToolbarItem(placement: .navigationBarTrailing) { homeMenu }
                }
        }
        .sheet(isPresented: $showSettings) { SettingsView() }
        .sheet(item: $sheet) { s in
            sheetView(s)
                .presentationDetents([.large])
                .presentationDragIndicator(.hidden)
        }
    }

    /// Native menu (material, label on the left, SF Symbol on the right, separators), as in IMG_2914.
    var homeMenu: some View {
        Menu {
            Button { newChat() } label: { Label("新建对话", systemImage: "square.and.pencil") }
            Button { sheet = .history } label: { Label("历史对话", systemImage: "clock.arrow.circlepath") }
            Button { sheet = .archive } label: { Label("归档", systemImage: "archivebox") }
            Button { sheet = .tracking } label: { Label("追踪", systemImage: "scope") }
            Button { sheet = .routine } label: { Label("例程", systemImage: "arrow.triangle.2.circlepath") }
            Button { sheet = .computer } label: { Label("电脑", systemImage: "desktopcomputer") }
        } label: {
            Image(systemName: "plus").font(.system(size: 19, weight: .medium))
        }.accessibilityLabel("菜单")
    }

    @ViewBuilder func sheetView(_ s: HomeSheet) -> some View {
        let close = { sheet = nil }
        switch s {
        case .history: HistorySheet(archived: false, current: current, open: openThread, close: close)
        case .archive: HistorySheet(archived: true, current: current, open: openThread, close: close)
        case .tracking: TrackingSheet(close: close)
        case .routine: RoutineSheet(open: openThread, close: close)
        case .computer: ComputerSheet(close: close)
        }
    }

    func openThread(_ id: String) { current = id; fresh = UUID(); sheet = nil }
    func newChat() { current = nil; fresh = UUID() }
}

/// Sheet chrome: × at the top-left, a bold centred title, an optional action on the right.
struct SheetChrome<Content: View, Trailing: View>: View {
    let title: String
    let close: () -> Void
    let trailing: Trailing
    let content: Content
    init(_ title: String, close: @escaping () -> Void, @ViewBuilder trailing: () -> Trailing, @ViewBuilder content: () -> Content) {
        self.title = title; self.close = close; self.trailing = trailing(); self.content = content()
    }
    var body: some View {
        NavigationStack {
            content
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .navigationBarLeading) {
                        Button(action: close) {
                            Image(systemName: "xmark").font(.system(size: 16, weight: .semibold)).foregroundColor(.primary)
                        }.accessibilityLabel("关闭")
                    }
                    ToolbarItem(placement: .principal) { Text(title).font(.headline.weight(.bold)) }
                    ToolbarItem(placement: .navigationBarTrailing) { trailing }
                }
        }
    }
}

extension SheetChrome where Trailing == EmptyView {
    init(_ title: String, close: @escaping () -> Void, @ViewBuilder content: () -> Content) {
        self.init(title, close: close, trailing: { EmptyView() }, content: content)
    }
}

/// A small coloured capsule (skill, status, schedule…).
struct Chip: View {
    let text: String
    var color: Color = amber
    var dot = false
    var body: some View {
        HStack(spacing: 4) {
            if dot { Circle().fill(color).frame(width: 5, height: 5) }
            Text(text).lineLimit(1)
        }
        .font(.caption.weight(.medium)).foregroundColor(color)
        .padding(.horizontal, 7).padding(.vertical, 3)
        .background(color.opacity(0.13)).clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
    }
}

let okGreen = Color(red: 0.16, green: 0.6, blue: 0.3)

// MARK: - 历史对话 / 归档

struct HistorySheet: View {
    @EnvironmentObject var api: API
    @EnvironmentObject var hub: Hub
    let archived: Bool
    let current: String?
    let open: (String) -> Void
    let close: () -> Void
    @State private var threads: [JSON] = []
    @State private var skills: [String: String] = [:]
    @State private var err = ""
    @State private var query = ""
    @State private var filter = 0            // 0 全部 · 1 运行中 · 2 失败 · 3 有文件
    @State private var loaded = false
    @State private var renaming: JSON? = nil
    @State private var newTitle = ""
    @AppStorage("pinnedThreads") private var pinnedRaw = ""   // 置顶 is kept on this phone (no server field)

    var pinned: [String] { pinnedRaw.split(separator: ",").map(String.init) }

    var shown: [JSON] {
        threads.filter { t in
            let st = t.s("last_status")
            switch filter {
            case 1: if !isLive(st) { return false }
            case 2: if st != "failed" { return false }
            case 3: if t.d("file_count") <= 0 { return false }
            default: break
            }
            if query.isEmpty { return true }
            let hay = [t.s("title"), t.s("last_prompt"), t.s("last_result"), (t.s("last_file") as NSString).lastPathComponent].joined(separator: " ")
            return hay.localizedCaseInsensitiveContains(query)
        }
    }

    /// 置顶 first, then 今天 / 昨天 / 近 7 天 / 更早 (archive: one list).
    struct DayGroup: Identifiable { let id: String; let items: [JSON] }
    var groups: [DayGroup] {
        let list = shown
        if archived { return list.isEmpty ? [] : [DayGroup(id: "\(list.count) 个已归档对话", items: list)] }
        let pins = list.filter { pinned.contains($0.s("id")) }
        let rest = list.filter { !pinned.contains($0.s("id")) }
        let cal = Calendar.current
        var today: [JSON] = [], yday: [JSON] = [], week: [JSON] = [], older: [JSON] = []
        let weekAgo = cal.date(byAdding: .day, value: -7, to: cal.startOfDay(for: Date())) ?? Date()
        for t in rest {
            let d = Date(timeIntervalSince1970: t.d("updated_at"))
            if cal.isDateInToday(d) { today.append(t) } else if cal.isDateInYesterday(d) { yday.append(t) }
            else if d >= weekAgo { week.append(t) } else { older.append(t) }
        }
        return [DayGroup(id: "置顶", items: pins), DayGroup(id: "今天", items: today), DayGroup(id: "昨天", items: yday),
                DayGroup(id: "近 7 天", items: week), DayGroup(id: "更早", items: older)].filter { !$0.items.isEmpty }
    }

    var body: some View {
        SheetChrome(archived ? "归档" : "历史对话", close: close) {
            List {
                if archived {
                    Label("左滑卡片 · 点「恢复」放回历史对话", systemImage: "arrow.uturn.backward")
                        .font(.subheadline).foregroundColor(amber)
                        .padding(.horizontal, 14).padding(.vertical, 11)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(amber.opacity(0.09)).clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                        .listRowInsets(EdgeInsets(top: 4, leading: 16, bottom: 4, trailing: 16))
                        .listRowSeparator(.hidden).listRowBackground(Color.clear)
                } else {
                    filterChips
                        .listRowInsets(EdgeInsets(top: 2, leading: 16, bottom: 6, trailing: 16))
                        .listRowSeparator(.hidden).listRowBackground(Color.clear)
                }
                if !err.isEmpty {
                    Text(err).font(.footnote).foregroundColor(.red).listRowSeparator(.hidden).listRowBackground(Color.clear)
                }
                ForEach(groups) { g in
                    Section {
                        ForEach(g.items.indices, id: \.self) { i in card(g.items[i]) }
                    } header: {
                        Text(g.id).font(.subheadline).foregroundColor(.secondary).textCase(nil)
                    }
                }
                if loaded && groups.isEmpty && err.isEmpty {
                    Text(archived ? "没有归档的对话" : query.isEmpty && filter == 0 ? "还没有对话" : "没有符合的对话")
                        .foregroundColor(.secondary).font(.subheadline).frame(maxWidth: .infinity).padding(.top, 40)
                        .listRowSeparator(.hidden).listRowBackground(Color.clear)
                }
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .background(Color(.systemGroupedBackground))
            .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always), prompt: archived ? "搜索归档" : "搜索对话、文件")
            .refreshable { await load() }
        }
        .task {
            await load()
            let list = (try? await api.list("/api/skills")) ?? []
            var m: [String: String] = [:]
            for s in list { m[s.s("id")] = s.s("title") }
            skills = m
        }
        .onChange(of: hub.tick) { _ in if hub.lastEvent.s("kind") == "task" { Task { await load() } } }
        .alert("重命名", isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
            TextField("对话标题", text: $newTitle)
            Button("取消", role: .cancel) { renaming = nil }
            Button("保存") { if let t = renaming { Task { await rename(t.s("id"), newTitle) } }; renaming = nil }
        }
    }

    var filterChips: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(Array(["全部", "运行中", "失败", "有文件"].enumerated()), id: \.offset) { i, name in
                    Button { filter = i } label: {
                        Text(name).font(.subheadline.weight(.medium))
                            .foregroundColor(filter == i ? .white : .primary)
                            .padding(.horizontal, 14).padding(.vertical, 7)
                            .background(filter == i ? amber : Color(.secondarySystemGroupedBackground))
                            .clipShape(Capsule())
                            .overlay(Capsule().stroke(Color(.separator).opacity(filter == i ? 0 : 0.6), lineWidth: 0.5))
                    }.buttonStyle(.plain)
                }
            }
        }
    }

    @ViewBuilder func card(_ t: JSON) -> some View {
        let id = t.s("id")
        Button { open(id) } label: {
            ThreadCard(t: t, skills: skills, archived: archived, pinned: pinned.contains(id), current: id == current)
        }
        .buttonStyle(.plain)
        .listRowInsets(EdgeInsets(top: 5, leading: 16, bottom: 5, trailing: 16))
        .listRowSeparator(.hidden)
        .listRowBackground(Color.clear)
        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
            if archived {
                Button { Task { await setArchived(id, false) } } label: { Label("恢复", systemImage: "arrow.uturn.backward") }.tint(amber)
                Button(role: .destructive) { Task { await del(id) } } label: { Label("删除", systemImage: "trash") }
            } else {
                Button(role: .destructive) { Task { await del(id) } } label: { Label("删除", systemImage: "trash") }
                Button { Task { await setArchived(id, true) } } label: { Label("归档", systemImage: "archivebox") }.tint(amber)
            }
        }
        .contextMenu {
            Button { newTitle = cleanTitle(t.s("title")); renaming = t } label: { Label("重命名", systemImage: "pencil") }
            Button { togglePin(id) } label: {
                pinned.contains(id) ? Label("取消置顶", systemImage: "pin.slash") : Label("置顶", systemImage: "pin")
            }
        }
    }

    func load() async {
        do { threads = try await api.list("/api/threads?archived=\(archived)"); err = "" }
        catch { err = error.localizedDescription }
        loaded = true
    }
    func del(_ id: String) async { _ = try? await api.call("DELETE", "/api/threads/\(id)"); await load() }
    func setArchived(_ id: String, _ a: Bool) async { _ = try? await api.call("PATCH", "/api/threads/\(id)", ["archived": a]); await load() }
    func rename(_ id: String, _ title: String) async {
        let s = title.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return }
        // keep a picked skill in the stored title so the skill chip survives the rename
        let old = threads.first { $0.s("id") == id }?.s("title") ?? ""
        let prefix = skillPrefix(old)
        _ = try? await api.call("PATCH", "/api/threads/\(id)", ["title": prefix + s])
        await load()
    }
    func togglePin(_ id: String) {
        var p = pinned
        if let i = p.firstIndex(of: id) { p.remove(at: i) } else { p.insert(id, at: 0) }
        pinnedRaw = p.joined(separator: ",")
    }
}

/// One conversation as a card: clean title, skill + status chips, one-line summary, time, file thumbnail, progress.
struct ThreadCard: View {
    let t: JSON
    let skills: [String: String]
    let archived: Bool
    let pinned: Bool
    let current: Bool

    var status: String { t.s("last_status") }
    var running: Bool { isLive(status) }
    var skillChips: [String] {
        var ids = skillIds(t.s("title"))
        if ids.isEmpty { ids = skillIds(t.s("last_prompt")) }
        var out = ids.map { skills[$0] ?? $0 }
        let src: [String: String] = ["watch_order": "价格监控", "routine": "例程", "xhs": "小红书", "watch": "追踪"]
        if let s = src[t.s("last_source")], !out.contains(s) { out.insert(s, at: 0) }
        return Array(out.prefix(2))
    }
    var summary: String {
        let r = oneLine(t.s("last_result"))
        if !r.isEmpty && !running { return r }
        let p = oneLine(cleanTitle(t.s("last_prompt")))
        if running { return (statusText[status] ?? "进行中") + (p.isEmpty ? "" : " · " + p) }
        return p.isEmpty ? (r.isEmpty ? "—" : r) : p
    }
    var statusChip: (String, Color)? {
        switch status {
        case "succeeded": return ("完成", okGreen)
        case "failed": return ("失败", .red)
        case "stopped", "interrupted": return (statusText[status] ?? status, .secondary)
        case "": return nil
        default: return running ? (statusText[status] ?? "运行中", status.hasPrefix("waiting") ? .orange : amber) : (status, .secondary)
        }
    }
    var timeText: String {
        let ts = t.d("updated_at")
        if running && Date().timeIntervalSince1970 - ts < 90 { return "刚刚" }
        let d = Date(timeIntervalSince1970: ts)
        let f = DateFormatter(); f.locale = Locale(identifier: "zh_CN")
        let cal = Calendar.current
        f.dateFormat = cal.isDateInToday(d) ? "HH:mm" : cal.isDateInYesterday(d) ? "昨天 HH:mm" : "M/d HH:mm"
        return (archived ? "更新于 " : "") + f.string(from: d)
    }

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 5) {
                    if pinned { Image(systemName: "pin.fill").font(.caption2).foregroundColor(amber) }
                    Text(cleanTitle(t.s("title"))).font(.body.weight(.semibold)).foregroundColor(.primary).lineLimit(1)
                }
                Text(summary).font(.subheadline).foregroundColor(status == "failed" ? .red : .secondary).lineLimit(1)
                HStack(spacing: 6) {
                    ForEach(skillChips, id: \.self) { Chip(text: $0, color: amber) }
                    if let c = statusChip { Chip(text: c.0, color: c.1, dot: running) }
                    Spacer(minLength: 4)
                    Text(timeText).font(.caption).foregroundColor(.secondary).lineLimit(1)
                }
            }
            if t.d("file_count") > 0 {
                FileThumb(path: t.s("last_file"), count: Int(t.d("file_count")))
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemGroupedBackground))
        .overlay(alignment: .bottom) { if running { RunBar(progress: t["progress"] as? Double) } }
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous).stroke(amber.opacity(current ? 0.7 : 0), lineWidth: 1.5))
        .contentShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
    }
}

/// The thin bar under a running card: the plan's share done, or a sliding segment when there is no plan.
struct RunBar: View {
    let progress: Double?
    @State private var slide = false
    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Rectangle().fill(amber.opacity(0.15))
                if let p = progress {
                    Rectangle().fill(amber).frame(width: max(6, g.size.width * min(1, max(0.04, p))))
                } else {
                    Rectangle().fill(amber).frame(width: g.size.width * 0.3)
                        .offset(x: slide ? g.size.width * 0.75 : -g.size.width * 0.05)
                        .onAppear { withAnimation(.easeInOut(duration: 1.4).repeatForever(autoreverses: true)) { slide = true } }
                }
            }
        }.frame(height: 3)
    }
}

/// Output file preview on a card: picture thumbnail, video frame, or a file-type tile; "+N" for more files.
struct FileThumb: View {
    let path: String
    let count: Int
    @State private var image: UIImage?
    static let cache = NSCache<NSString, UIImage>()

    var ext: String { (path as NSString).pathExtension.lowercased() }
    var isImage: Bool { ["png", "jpg", "jpeg", "webp", "gif", "heic"].contains(ext) }
    var isVideo: Bool { ["mp4", "mov", "m4v", "webm"].contains(ext) }

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Group {
                if let img = image {
                    Image(uiImage: img).resizable().scaledToFill()
                        .overlay { if isVideo { Image(systemName: "play.circle.fill").font(.system(size: 22)).foregroundColor(.white.opacity(0.92)) } }
                } else if isVideo {
                    LinearGradient(colors: [Color(red: 0.45, green: 0.3, blue: 0.85), Color(red: 0.85, green: 0.35, blue: 0.6)],
                                   startPoint: .topLeading, endPoint: .bottomTrailing)
                        .overlay(Image(systemName: "play.circle.fill").font(.system(size: 24)).foregroundColor(.white.opacity(0.92)))
                } else {
                    Color.blue.opacity(0.09).overlay(
                        VStack(spacing: 3) {
                            Image(systemName: isImage ? "photo" : "doc").font(.system(size: 22)).foregroundColor(.blue)
                            if !ext.isEmpty { Text(ext.uppercased()).font(.system(size: 10, weight: .bold)).foregroundColor(.blue).lineLimit(1) }
                        })
                }
            }
            .frame(width: 64, height: 64)
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            if count > 1 {
                Text("+\(count - 1)").font(.system(size: 10, weight: .bold)).foregroundColor(.primary)
                    .padding(.horizontal, 5).padding(.vertical, 1).background(.thinMaterial).clipShape(Capsule()).padding(4)
            }
        }
        .task(id: path) { await loadThumb() }
        .accessibilityLabel((path as NSString).lastPathComponent)
    }

    func loadThumb() async {
        guard isImage || isVideo, !path.isEmpty else { return }
        if let c = FileThumb.cache.object(forKey: path as NSString) { image = c; return }
        guard let u = API.shared.artifactURL(path, download: false) else { return }
        var img: UIImage?
        if isImage {
            var r = URLRequest(url: u, timeoutInterval: 20)
            r.setValue("Bearer \(API.shared.token)", forHTTPHeaderField: "Authorization")
            if let (d, _) = try? await URLSession.shared.data(for: r), let full = UIImage(data: d) {
                img = full.preparingThumbnail(of: CGSize(width: 160, height: 160)) ?? full
            }
        } else {
            let g = AVAssetImageGenerator(asset: AVURLAsset(url: u))
            g.appliesPreferredTrackTransform = true
            g.maximumSize = CGSize(width: 200, height: 200)
            if let r = try? await g.image(at: CMTime(seconds: 0.5, preferredTimescale: 600)) { img = UIImage(cgImage: r.image) }
        }
        if let i = img { FileThumb.cache.setObject(i, forKey: path as NSString); image = i }
    }
}

/// Drop the 【技能：…】 prefix a skill pick adds, so titles show what was asked.
func cleanTitle(_ t: String) -> String {
    var s = t
    if s.hasPrefix("【技能"), let r = s.range(of: "】") { s = String(s[r.upperBound...]) }
    s = s.trimmingCharacters(in: .whitespacesAndNewlines)
    return s.isEmpty ? "新对话" : s
}

/// The skill ids of a 【技能：a、b】 prefix.
func skillIds(_ t: String) -> [String] {
    guard t.hasPrefix("【技能"), let r = t.range(of: "】") else { return [] }
    var inner = String(t[t.startIndex..<r.lowerBound]).replacingOccurrences(of: "【技能", with: "")
    inner = inner.trimmingCharacters(in: CharacterSet(charactersIn: "：: "))
    return inner.split(whereSeparator: { "、,，".contains($0) }).map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
}

/// The 【技能：…】 prefix itself (kept when a conversation is renamed).
func skillPrefix(_ t: String) -> String {
    guard t.hasPrefix("【技能"), let r = t.range(of: "】") else { return "" }
    return String(t[t.startIndex..<r.upperBound])
}

/// First meaningful line of a result, without Markdown marks.
func oneLine(_ s: String) -> String {
    var line = s.split(whereSeparator: \.isNewline).map { $0.trimmingCharacters(in: .whitespaces) }.first { !$0.isEmpty } ?? ""
    while let c = line.first, "#>-*+ ".contains(c) { line.removeFirst() }
    line = line.replacingOccurrences(of: "**", with: "").replacingOccurrences(of: "__", with: "").replacingOccurrences(of: "`", with: "")
    return line.trimmingCharacters(in: .whitespaces)
}
