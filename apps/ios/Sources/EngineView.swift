import SwiftUI
import UIKit
import PhotosUI
import UniformTypeIdentifiers

// v3.9.59 引擎 on iOS (v3.9.61: 群聊 at the end of this file): the web page's 引擎 (bots.js, /api/bots*) as a sheet from the top-right "+" menu, next to
// 任务 / 电脑. The list of engine conversations (agy; v3.9.60 also Codex, Claude Code, CodeBuddy, Grok) -> one conversation: the engine's replies with
// their steps, quote (long press › 引用), the chat's "+" (拍照 / 照片 / 文件 / 技能) and "/" skills, model and strength,
// 交给管家 / 收回 / 停止. Like the web page it polls the conversation (2 s while it runs, 8 s otherwise).

/// v3.9.66: each engine's own colour, the same as the web page (bots.css, v3.9.63): its name above its replies.
func engineColor(_ bot: String, _ cs: ColorScheme) -> Color {
    switch bot {
    case "agy": return Color(red: 0xc0 / 255, green: 0x26 / 255, blue: 0xd3 / 255)     // #c026d3
    case "codex": return Color(red: 0x10 / 255, green: 0xa3 / 255, blue: 0x7f / 255)   // #10a37f
    case "claude": return Color(red: 0xd9 / 255, green: 0x77 / 255, blue: 0x57 / 255)  // #d97757
    case "codebuddy": return Color(red: 0x2b / 255, green: 0x6c / 255, blue: 0xff / 255) // #2b6cff
    case "grok": return cs == .dark ? Color(white: 0xe9 / 255) : Color(white: 0x11 / 255)
    case "muse": return Color(red: 0x6a / 255, green: 0x5c / 255, blue: 0xff / 255)      // #6a5cff
    case "cue": return Color(red: 0x10 / 255, green: 0xb9 / 255, blue: 0x81 / 255)       // #10b981
    default: return .secondary
    }
}

/// Renders cloud-engine generated images, videos, audio, and files (`data.media`) inline in engine chats & groups.
private struct EngineMediaView: View {
    let media: [JSON]
    var body: some View {
        let visual: [JSON] = media.compactMap { m in
            let p = m.s("path")
            let k = m.s("kind")
            guard !p.isEmpty, k == "image" || k == "video" else { return nil }
            return ["artifact": p, "name": m.s("name"), "kind": k]
        }
        let others: [JSON] = media.filter { m in
            let p = m.s("path")
            let k = m.s("kind")
            return p.isEmpty || (k != "image" && k != "video")
        }
        VStack(alignment: .leading, spacing: 6) {
            if !visual.isEmpty {
                AttachmentPreviews(attachments: visual)
            }
            ForEach(others.indices, id: \.self) { i in
                let m = others[i]
                let name = m.s("name").isEmpty ? "文件" : m.s("name")
                let link: URL? = !m.s("path").isEmpty ? API.shared.artifactURL(m.s("path"), download: false) : URL(string: m.s("url"))
                if let u = link {
                    Link(destination: u) {
                        HStack(spacing: 6) {
                            Image(systemName: m.s("kind") == "audio" ? "waveform" : fileIcon(name)).foregroundColor(amber)
                            Text(name).lineLimit(1).truncationMode(.middle)
                            Text("预览 / 下载").foregroundColor(.secondary)
                        }
                        .font(.caption).padding(.horizontal, 10).padding(.vertical, 6)
                        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color(.secondarySystemBackground)))
                    }
                } else {
                    Text(name + (m.s("error").isEmpty ? "" : "（\(m.s("error"))）"))
                        .font(.caption2).foregroundColor(.secondary)
                }
            }
        }
    }
}

/// v3.9.69: the engine's round avatar, the same as the web page: an amber ring like AIW's own, its real logo on white
/// (engines/eng-<id>.png in the bundle, copied by build-ipa.sh; LobeHub Icons, MIT). Unknown ids show their first letters.
struct EngineAvatar: View {
    let bot: String
    var size: CGFloat = 32
    @Environment(\.colorScheme) private var cs
    static var cache: [String: UIImage] = [:]
    static func logo(_ id: String) -> UIImage? {
        if let i = cache[id] { return i }
        guard let p = Bundle.main.path(forResource: "eng-" + id, ofType: "png", inDirectory: "engines"),
              let i = UIImage(contentsOfFile: p) else { return nil }
        cache[id] = i; return i
    }
    var body: some View {
        let id = bot.isEmpty ? "agy" : bot
        ZStack {
            Circle().fill(Color.white)
            if let img = EngineAvatar.logo(id) {
                Image(uiImage: img).resizable().interpolation(.high).scaledToFit().frame(width: size * 0.56, height: size * 0.56)
            } else {
                Text(String(id.prefix(3))).font(.system(size: size * 0.32, weight: .bold)).foregroundColor(.black)
            }
        }
        .frame(width: size, height: size)
        .overlay(Circle().strokeBorder(engineColor(id, cs), lineWidth: size >= 30 ? 1.5 : 1))   // v3.9.80: its own colour; amber is AIW's
        .accessibilityLabel(id)
    }
}

/// v3.9.67: files an engine names in its reply (file:///home/aiw/workspace/…, ~/workspace/…), at most 8, in order.
func engineFilePaths(_ text: String) -> [String] {
    let rx = try? NSRegularExpression(pattern: #"file://(/[^\s)\]<>"'`]+)|((?:~|/home/[\w.-]+)/workspace/[^\s)\]<>"'`，。；]+\.[A-Za-z0-9]{1,6})"#)
    let ns = text as NSString
    var out: [String] = []
    for m in rx?.matches(in: text, range: NSRange(location: 0, length: ns.length)) ?? [] {
        let r = m.range(at: 1).location != NSNotFound ? m.range(at: 1) : m.range(at: 2)
        var p = ns.substring(with: r)
        p = p.removingPercentEncoding ?? p
        while let l = p.last, ".,;:".contains(l) { p.removeLast() }
        if p.range(of: #"\.[A-Za-z0-9]{1,6}$"#, options: .regularExpression) != nil, !out.contains(p) { out.append(p) }
        if out.count >= 8 { break }
    }
    return out
}

/// v3.9.66: what you send in an engine conversation or group, as the chat draws it: select part of it and 复制.
func engineUserText(_ text: String) -> some View {
    SelectableText(text: uiAttributed(inlineMD(text), style: .callout, weight: .medium, color: UIColor(hInk), lineSpacing: 2),
                   whole: text, link: UIColor(hInk))
}

let engineEffortName: [String: String] = ["minimal": "最低", "low": "低", "medium": "中", "high": "高", "xhigh": "超高", "max": "最高"]
let engineEffortOrder = ["minimal", "low", "medium", "high", "xhigh", "max"]

/// The models this computer's engine account can use (GET /api/bots/engines/<bot>/models: groups of a model and its
/// strengths, plus the strengths the engine takes). v3.9.60: one list per engine; `bot` is the one on screen.
final class EngineModels: ObservableObject {
    static let shared = EngineModels()
    @Published var bot = "agy"
    @Published var groups: [JSON] = []
    @Published var engineEfforts: [String] = engineEffortOrder
    @Published var error = ""
    @Published var loading = false
    private var cache: [String: ([JSON], [String])] = [:]
    @MainActor func load(bot: String? = nil, refresh: Bool = false) async {
        let b = bot ?? self.bot
        if b != self.bot { self.bot = b; error = "" }
        if let hit = cache[b], !refresh { groups = hit.0; engineEfforts = hit.1; return }
        if !refresh { groups = []; engineEfforts = b == "agy" ? engineEffortOrder : [] }
        loading = true; defer { loading = false }
        do {
            let r = try await API.shared.obj("GET", "/api/bots/engines/\(b)/models" + (refresh ? "?refresh=1" : ""))
            let effs = (r["efforts"] as? [String]) ?? engineEffortOrder
            cache[b] = (r.a("groups"), effs)
            if self.bot == b { groups = r.a("groups"); engineEfforts = effs; error = "" }
        } catch let e {
            if self.bot == b { self.error = "读不到模型列表（\(e.localizedDescription)），先用 \(b) 的默认模型" }
        }
    }
    /// (group, strength) for a stored model: a group id, or a slug that names its strength
    func find(_ model: String) -> (JSON, String)? {
        for g in groups {
            if g.s("id") == model { return (g, "") }
            for (e, v) in g.o("slugs") where (v as? String) == model { return (g, e) }
        }
        return nil
    }
    func name(_ c: JSON) -> String {
        let model = c.s("model"), effort = c.s("effort")
        if model.isEmpty && effort.isEmpty { return "默认模型" }
        let hit = model.isEmpty ? nil : find(model)
        let n = hit.map { $0.0.s("label").isEmpty ? $0.0.s("id") : $0.0.s("label") } ?? (model.isEmpty ? "默认模型" : model)
        let e = (hit?.1.isEmpty == false ? hit!.1 : effort)
        return e.isEmpty ? n : n + " · " + (engineEffortName[e] ?? e)
    }
    /// the strengths a group offers (its slugs, else --effort low…max), and whether "默认" is allowed
    func efforts(_ gid: String) -> ([String], Bool) {
        let g = groups.first { $0.s("id") == gid }
        let own = (g?["efforts"] as? [String]) ?? []
        if let g = g, !own.isEmpty { return (own, g.b("plain")) }
        return (bot == "agy" ? engineEffortOrder.filter { $0 != "minimal" } : engineEfforts, true)
    }
    /// what create / PATCH take: a slug that names its strength goes alone
    func value(_ gid: String, _ eff: String) -> JSON {
        if let g = groups.first(where: { $0.s("id") == gid }), !eff.isEmpty, let slug = g.o("slugs")[eff] as? String {
            return ["model": slug, "effort": ""]
        }
        return ["model": gid, "effort": eff]
    }
}

/// 模型 + 强度 (a Section inside a Form)
struct EngineModelSection: View {
    @ObservedObject private var models = EngineModels.shared
    @Binding var group: String
    @Binding var effort: String
    init(group: Binding<String>, effort: Binding<String>) { _group = group; _effort = effort }
    var body: some View {
        Section(header: HStack {
            Text("模型和强度"); Spacer()
            Button("刷新") { Task { await models.load(refresh: true) } }.font(.caption)
        }.textCase(nil), footer: Text(models.error.isEmpty ? "强度越高越慢、越仔细。选「默认」就用 \(models.bot) 自己的设置。" : models.error)) {
            Picker("模型", selection: $group) {
                Text("默认（\(models.bot) 自己的设置）").tag("")
                ForEach(models.groups.indices, id: \.self) { i in
                    let g = models.groups[i]
                    Text(g.s("label").isEmpty ? g.s("id") : g.s("label")).tag(g.s("id"))
                }
                if !group.isEmpty && !models.groups.contains(where: { $0.s("id") == group }) { Text(group).tag(group) }
            }
            let ep = models.efforts(group)
            if !ep.0.isEmpty {
                Picker("强度", selection: $effort) {
                    if ep.1 { Text("默认").tag("") }
                    ForEach(ep.0, id: \.self) { e in Text("\(engineEffortName[e] ?? e)（\(e)）").tag(e) }
                }
            }
            if models.loading { HStack { ProgressView(); Text("读取模型…").font(.footnote).foregroundColor(.secondary) } }
        }
        .onChange(of: group) { _ in fixEffort() }
        .onChange(of: models.groups.count) { _ in fixEffort() }
        .onChange(of: models.engineEfforts) { _ in fixEffort() }
    }
    func fixEffort() {
        let (effs, plain) = models.efforts(group)
        if effs.isEmpty { effort = ""; return }
        if effs.contains(effort) || (plain && effort.isEmpty) { return }
        effort = plain ? "" : (effs.contains("high") ? "high" : (effs.last ?? ""))
    }
    /// the picker state for a stored model / effort
    static func split(_ model: String, _ effort: String) -> (String, String) {
        if let (g, e) = EngineModels.shared.find(model) { return (g.s("id"), e.isEmpty ? effort : e) }
        return (model, effort)
    }
}

// MARK: - the list

struct EngineSheet: View {
    let close: () -> Void
    @State private var convs: [JSON] = []
    @State private var engines: [JSON] = []
    @State private var error = ""
    @State private var showNew = false
    @State private var open: String? = nil
    @State private var loaded = false
    @State private var groups: [JSON] = []          // v3.9.61 引擎群聊
    @State private var openGroup: String? = nil
    @State private var showNewGroup = false
    @State private var cfgBot: EngineCfgTarget? = nil   // v3.9.80
    @ObservedObject private var links = DeepLinks.shared
    var body: some View {
        SheetChrome("引擎", close: close, trailing: {
            Menu {
                Button { showNew = true } label: { Label("新任务（一个引擎）", systemImage: "square.and.pencil") }
                Button { showNewGroup = true } label: { Label("新群聊任务（几个引擎分工）", systemImage: "person.3") }
            } label: { Image(systemName: "square.and.pencil").font(.system(size: 17)) }.accessibilityLabel("新建")
        }) {
            List {
                Section(footer: Text("点头像单聊，长按配置")) { avatarBar }   // v3.9.80
                if !error.isEmpty { Section { Text(error).foregroundColor(.red).font(.footnote) } }
                if !dms.isEmpty {
                    Section(header: Text("单聊")) {
                        ForEach(dms.indices, id: \.self) { i in
                            let c = dms[i]
                            NavigationLink(tag: c.s("id"), selection: $open) {
                                EngineConvView(cid: c.s("id"), engines: engines) { Task { await load() } }
                            } label: { row(c) }
                        }
                    }
                }
                if loaded && tasks.isEmpty && groups.isEmpty {
                    Section {
                        Text("还没有任务。点上面的头像直接和引擎单聊，或者新建一个任务交给它做。")
                            .font(.subheadline).foregroundColor(.secondary)
                        Button { showNew = true } label: { Label("新任务", systemImage: "plus") }.foregroundColor(amber)
                    }
                }
                if !groups.isEmpty {
                    Section(header: Text("群聊任务")) {
                        ForEach(groups.indices, id: \.self) { i in
                            let g = groups[i]
                            NavigationLink(tag: g.s("id"), selection: $openGroup) {
                                EngineGroupView(gid: g.s("id"), engines: engines) { Task { await load() } }
                            } label: { groupRow(g) }
                        }
                    }
                }
                Section(header: tasks.isEmpty ? nil : Text("任务"), footer: Text(soon)) {
                    ForEach(tasks.indices, id: \.self) { i in
                        let c = tasks[i]
                        NavigationLink(tag: c.s("id"), selection: $open) {
                            EngineConvView(cid: c.s("id"), engines: engines) { Task { await load() } }
                        } label: { row(c) }
                    }
                }
            }
            .listStyle(.insetGrouped)
            .refreshable { await load() }
        }
        .task { await load() }
        .onChange(of: links.focusEngineConv) { _ in Task { await applyDeepFocus() } }
        .onChange(of: links.focusEngineGroup) { _ in Task { await applyDeepFocus() } }
        .onChange(of: open) { id in
            if let id = id {
                if let idx = convs.firstIndex(where: { $0.s("id") == id }) {
                    convs[idx]["unread"] = 0
                    if convs[idx].s("kind") == "dm",
                       let ei = engines.firstIndex(where: { $0.s("id") == convs[idx].s("bot") }) {
                        engines[ei]["unread"] = 0
                    }
                }
            } else {
                Task { await load() }
            }
        }
        .sheet(item: $cfgBot) { t in
            EngineCfgSheet(engine: engines.first { $0.s("id") == t.id } ?? ["id": t.id, "title": t.id]) { Task { await load() } }
        }
        .sheet(isPresented: $showNewGroup) {
            EngineGroupNewSheet(engines: engines) { g in
                showNewGroup = false
                groups.insert(g, at: 0)
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { openGroup = g.s("id") }
            }
        }
        .sheet(isPresented: $showNew) {
            EngineNewSheet(engines: engines) { c in
                showNew = false
                convs.insert(c, at: 0)
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { open = c.s("id") }
            }
        }
    }
    var dms: [JSON] { convs.filter { $0.s("kind") == "dm" } }
    var tasks: [JSON] { convs.filter { $0.s("kind") != "dm" } }
    var avatarBar: some View {
        // v3.9.83: two rows (4 + 3), every engine visible without scrolling
        LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 6), count: 4), spacing: 8) {
            ForEach(engines.filter { $0.b("available") }.indices, id: \.self) { i in
                let e = engines.filter { $0.b("available") }[i]
                avatarCell(e)
            }
        }.padding(.vertical, 4)
    }
    func avatarCell(_ e: JSON) -> some View {
        let id = e.s("id")
        let busy = e.d("busy") > 0
        let unread = Int(e.d("unread"))
        let rd = e.s("ready").isEmpty ? ((e.b("cloud") && !e.b("configured")) ? "off" : "unknown") : e.s("ready")
        let dot: Color = busy ? amber : (rd == "ok" ? Color.green : Color.gray)   // v3.9.83: green only when it works
        return VStack(spacing: 3) {
            EngineAvatar(bot: id, size: 44)
                .saturation(busy || rd == "ok" ? 1 : 0.4).opacity(busy || rd == "ok" ? 1 : 0.75)
                .overlay(Circle().fill(dot).frame(width: 11, height: 11).overlay(Circle().stroke(Color(.systemBackground), lineWidth: 2)), alignment: .bottomTrailing)
                .overlay(alignment: .topTrailing) {
                    if unread > 0 {
                        Text(unread > 99 ? "99+" : "\(unread)")
                            .font(.system(size: 10, weight: .bold))
                            .foregroundColor(.white)
                            .padding(.horizontal, 5)
                            .frame(minWidth: 18, minHeight: 18)
                            .background(Color.red)
                            .clipShape(Capsule())
                            .overlay(Capsule().stroke(Color(.systemBackground), lineWidth: 1.5))
                            .offset(x: 5, y: -4)
                    }
                }
            Text(e.s("short").isEmpty ? e.s("title") : e.s("short")).font(.caption2).foregroundColor(.secondary).lineLimit(1)
        }
        .frame(width: 52)
        .contentShape(Rectangle())
        .onTapGesture { Task { await openDm(id) } }
        .contextMenu {
            Button { Task { await openDm(id) } } label: { Label("单聊", systemImage: "bubble.left") }
            Button { cfgBot = EngineCfgTarget(id: id) } label: { Label("配置", systemImage: "gearshape") }
        }
    }
    @MainActor func openDm(_ bot: String) async {
        if let e = engines.first(where: { $0.s("id") == bot }), e.b("cloud"), !e.b("configured") { cfgBot = EngineCfgTarget(id: bot); return }
        do {
            let c = try await API.shared.obj("POST", "/api/bots/dm/\(bot)")
            if !convs.contains(where: { $0.s("id") == c.s("id") }) { convs.insert(c, at: 0) }
            if let idx = engines.firstIndex(where: { $0.s("id") == bot }) { engines[idx]["unread"] = 0 }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { open = c.s("id") }
        } catch let e { error = "没能打开单聊：\(e.localizedDescription)" }
    }
    var soon: String {
        engines.filter { !$0.b("available") }.map { $0.s("title") + "：下一版接入" }.joined(separator: " · ")
    }
    func row(_ c: JSON) -> some View {
        let eng = engines.first { $0.s("id") == c.s("bot") }
        let last = c.o("last")
        let unread = Int(c.d("unread"))
        return HStack(alignment: .top, spacing: 10) {
        EngineAvatar(bot: c.s("bot"), size: 36)
            .overlay(alignment: .topTrailing) {
                if unread > 0 {
                    Text(unread > 99 ? "99+" : "\(unread)")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundColor(.white)
                        .padding(.horizontal, 5)
                        .frame(minWidth: 17, minHeight: 17)
                        .background(Color.red)
                        .clipShape(Capsule())
                        .overlay(Capsule().stroke(Color(.systemBackground), lineWidth: 1.5))
                        .offset(x: 6, y: -5)
                }
            }
            .padding(.top, 2)
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(c.s("title")).font(.subheadline.weight(.semibold)).lineLimit(1)
                if c.b("busy") || c.s("status") == "running" { ProgressView().controlSize(.mini) }
                if !c.o("takeover").isEmpty {
                    Text("管家").font(.caption2.weight(.bold)).foregroundColor(amber)
                        .padding(.horizontal, 5).padding(.vertical, 1).overlay(Capsule().stroke(amber))
                }
                Spacer()
                Text(when(c.d("updated_at"))).font(.caption2).foregroundColor(.secondary)
            }
            Text("\(eng?.s("title") ?? c.s("bot")) · \(EngineModels.shared.name(c))").font(.caption).foregroundColor(.secondary).lineLimit(1)
            if !last.isEmpty {
                Text((last.s("role") == "user" ? "我：" : "") + last.s("text")).font(.caption).foregroundColor(.secondary).lineLimit(2)
            }
        }
        }.padding(.vertical, 2)
    }
    func groupRow(_ g: JSON) -> some View {
        let names = g.a("members").map { m in engines.first { $0.s("id") == m.s("bot") }?.s("title") ?? m.s("bot") }
        let last = g.o("last")
        let st = engineGroupStatus[g.s("status")] ?? ""
        return VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Image(systemName: "person.3").font(.caption).foregroundColor(amber)
                Text(g.s("title")).font(.subheadline.weight(.semibold)).lineLimit(1)
                if g.b("busy") { ProgressView().controlSize(.mini) }
                Spacer()
                Text(when(g.d("updated_at"))).font(.caption2).foregroundColor(.secondary)
            }
            Text(names.joined(separator: "、") + (st.isEmpty ? "" : " · " + st)).font(.caption).foregroundColor(.secondary).lineLimit(1)
            if !last.isEmpty { Text(last.s("text")).font(.caption).foregroundColor(.secondary).lineLimit(2) }
        }.padding(.vertical, 2)
    }
    @MainActor func load() async {
        do {
            let r = try await API.shared.obj("GET", "/api/bots?check=1")
            engines = r.a("engines"); convs = r.a("convs"); error = ""
        } catch let e { self.error = "读不到引擎对话：\(e.localizedDescription)" }
        if let r = try? await API.shared.obj("GET", "/api/bots/groups") { groups = r.a("groups") }
        loaded = true
        // v3.9.68: 打开对话 on a permission card in the chat opens that conversation
        if let want = UserDefaults.standard.string(forKey: "aiw_open_engine_conv"), !want.isEmpty {
            UserDefaults.standard.removeObject(forKey: "aiw_open_engine_conv")
            if convs.contains(where: { $0.s("id") == want }) { open = want }
        }
        await applyDeepFocus(allowReload: false)
        await EngineModels.shared.load()
    }
    @MainActor func applyDeepFocus(allowReload: Bool = true) async {
        if let cid = links.focusEngineConv, !cid.isEmpty {
            links.focusEngineConv = nil
            if allowReload && !convs.contains(where: { $0.s("id") == cid }) {
                await load()
            }
            if convs.contains(where: { $0.s("id") == cid }) {
                openGroup = nil
                if open == cid { return }
                if open != nil {
                    open = nil
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { open = cid }
                } else {
                    open = cid
                }
            }
            return
        }
        if let gid = links.focusEngineGroup, !gid.isEmpty {
            links.focusEngineGroup = nil
            if allowReload && !groups.contains(where: { $0.s("id") == gid }) {
                await load()
            }
            if groups.contains(where: { $0.s("id") == gid }) {
                open = nil
                if openGroup == gid { return }
                if openGroup != nil {
                    openGroup = nil
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { openGroup = gid }
                } else {
                    openGroup = gid
                }
            }
        }
    }
}

struct EngineNewSheet: View {
    let engines: [JSON]
    let done: (JSON) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var bot = "agy"
    @State private var title = ""
    @State private var workdir = ""
    @State private var machine = "auto"          // v3.9.73
    @State private var machines: [JSON] = []
    @State private var group = ""
    @State private var effort = ""
    @State private var busy = false
    @State private var error = ""
    var body: some View {
        NavigationStack {
            Form {
                Section(header: Text("引擎")) {
                    Picker("引擎", selection: $bot) {
                        ForEach(engines.indices, id: \.self) { i in
                            let e = engines[i]
                            Label { Text(e.s("title") + (e.b("available") ? "" : "（即将支持）")) } icon: { EngineAvatar(bot: e.s("id"), size: 22) }.tag(e.s("id"))
                        }
                    }
                }
                Section(header: Text("标题")) { TextField("例如 ldok 重建", text: $title) }
                Section(header: Text("电脑"), footer: Text("自动：画 CAD、渲染或剪辑视频交给云电脑，写代码和其他事留在本地电脑。选云电脑时，它上面要装好并登录引擎。")) {
                    Picker("电脑", selection: $machine) {
                        Text("自动（按任务选）").tag("auto")
                        ForEach(machines.indices, id: \.self) { i in
                            Text(machineName(machines[i])).tag(machines[i].s("id"))
                        }
                    }
                }
                Section(header: Text("工作文件夹"), footer: Text("电脑上的文件夹，它就在这里改文件；空着就新建一个。")) {
                    TextField("~/workspace/ldok-rebuild", text: $workdir).textInputAutocapitalization(.never).autocorrectionDisabled()
                }
                EngineModelSection(group: $group, effort: $effort)
                if !error.isEmpty { Section { Text(error).foregroundColor(.red).font(.footnote) } }
            }
            .navigationTitle("新对话").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    if busy { ProgressView() } else { Button("开始") { Task { await create() } }.bold() }
                }
            }
        }
        .task {
            machines = (try? await API.shared.list("/api/machines")) ?? []
            await EngineModels.shared.load(bot: bot)
        }
        .onChange(of: bot) { b in group = ""; effort = ""; Task { await EngineModels.shared.load(bot: b) } }
    }
    func machineName(_ m: JSON) -> String {
        let n: String = m.s("name")
        return n.isEmpty ? m.s("id") : n
    }
    @MainActor func create() async {
        if let e = engines.first(where: { $0.s("id") == bot }), !e.b("available") { error = "\(e.s("title")) 还没接入"; return }
        busy = true; defer { busy = false }
        var req: JSON = ["bot": bot, "title": title, "workdir": workdir, "machine_id": machine]
        for (k, v) in EngineModels.shared.value(group, effort) { req[k] = v }
        do { done(try await API.shared.obj("POST", "/api/bots/convs", req)) }
        catch { self.error = "没能新建：\(error.localizedDescription)" }
    }
}

// MARK: - one conversation

struct EngineQuote: Equatable { let id: String; let who: String; let text: String }

struct EngineConvView: View {
    let cid: String
    let engines: [JSON]
    let changed: () -> Void
    @Environment(\.dismiss) private var dismiss
    @Environment(\.colorScheme) private var cs
    @State private var conv: JSON = [:]
    @State private var msgs: [JSON] = []
    @State private var err = ""
    @State private var text = ""
    @State private var focused = false
    @State private var sending = false
    @State private var quote: EngineQuote? = nil
    @State private var attach: [JSON] = []
    @State private var uploading = false
    @State private var pct: Double = 0
    @State private var skills: [JSON] = []
    @State private var picked: [String] = []
    @State private var showAttach = false
    @State private var pending: AttachKind? = nil
    @State private var camera: CamMode? = nil
    @State private var photos: [PhotosPickerItem] = []
    @State private var showPhotos = false
    @State private var showFiles = false
    @State private var showTake = false
    @State private var showModel = false
    @State private var showRename = false
    @State private var newTitle = ""
    @State private var confirmDelete = false
    @State private var lastCount = 0
    @State private var showAllow = false        // v3.9.67: 允许这类命令再试 (the rule, editable)
    @State private var allowRule = ""
    @State private var confirmOnce = false      // v3.9.67: 这一轮放开权限重跑
    @State private var preview: Preview? = nil  // v3.9.67: a file the engine made, copied from the computer
    @State private var fetching = ""

    var engine: JSON { engines.first { $0.s("id") == conv.s("bot") } ?? ["title": "Antigravity", "short": "agy"] }
    var takeover: JSON { (conv["takeover"] as? JSON) ?? [:] }
    var live: Bool { conv.b("busy") || conv.s("status") == "running" }
    var slashQ: String? { slashQuery(text) }
    // v3.9.74: built outside body; the inline ternary made body too slow to type-check
    var emptyHint: String {
        let label: String = conv.s("machine_label")
        let place: String = label.isEmpty ? "电脑" : " \(label) "
        let wd: String = conv.s("workdir")
        return "直接说要它做什么。它在\(place)的 \(wd) 里干活，改动不会自动推送。"
    }
    var navSubtitle: String {
        var parts: [String] = []
        let title = engine.s("title")
        if !title.isEmpty { parts.append(title) }
        if conv.s("kind") == "dm" { parts.append("单聊") }
        let ml = conv.s("machine_label")
        if !ml.isEmpty { parts.append(ml) }
        let mn = EngineModels.shared.name(conv)
        if !mn.isEmpty && mn != "默认" && !engine.b("cloud") { parts.append(mn) }
        else if parts.count <= 1 && !mn.isEmpty { parts.append(mn) }
        return parts.joined(separator: " · ")
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    if msgs.isEmpty && !conv.isEmpty {
                        Text(emptyHint)
                            .font(.footnote).foregroundColor(.secondary).frame(maxWidth: .infinity).padding(.top, 40)
                    }
                    ForEach(msgs.indices.map { HRowKey(i: $0, id: msgs[$0].s("id")) }, id: \.id) { k in row(msgs[k.i]) }
                    if !err.isEmpty { Text(err).font(.footnote).foregroundColor(.red) }
                    Color.clear.frame(height: 1).id("end")
                }.padding(.horizontal, 14).padding(.vertical, 10)
            }
            .scrollDismissesKeyboard(.interactively)
            .safeAreaInset(edge: .top) { if !takeover.isEmpty { takeBar } }
            .safeAreaInset(edge: .bottom) { composer }
            .onChange(of: msgs.count) { n in
                if n != lastCount { lastCount = n; withAnimation { proxy.scrollTo("end", anchor: .bottom) } }
            }
            .onChange(of: msgs.last?.s("text") ?? "") { _ in proxy.scrollTo("end", anchor: .bottom) }
        }
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) {
                VStack(spacing: 0) {
                    Text(conv.s("title").isEmpty ? "引擎" : conv.s("title")).font(.subheadline.weight(.bold)).lineLimit(1)
                    Text(navSubtitle).font(.caption2).foregroundColor(.secondary).lineLimit(1)
                }.onTapGesture { showModel = true }
            }
            ToolbarItem(placement: .navigationBarTrailing) { menu }
        }
        .task { await poll() }
        .task { skills = (try? await API.shared.list("/api/skills")) ?? [] }
        .onChange(of: text) { t in onText(t) }
        .onChange(of: photos) { items in
            guard !items.isEmpty else { return }
            let list = items; photos = []
            Task { for it in list { await addPicked(it) } }
        }
        .photosPicker(isPresented: $showPhotos, selection: $photos, maxSelectionCount: 9, matching: .any(of: [.images, .videos]))
        .sheet(isPresented: $showAttach, onDismiss: runPending) {
            AttachSheet(kinds: [.photo, .video, .library, .file, .skill]) { k in pending = k; showAttach = false }
                .presentationDetents([.height(AttachSheet.height)]).presentationDragIndicator(.hidden)
        }
        .fullScreenCover(item: $camera) { md in
            HomeCamera(mode: md) { u, img in camera = nil; Task { await gotCamera(u, img) } }.ignoresSafeArea()
        }
        .fileImporter(isPresented: $showFiles, allowedContentTypes: [.item], allowsMultipleSelection: true) { r in
            if case .success(let urls) = r { Task { for u in urls { await addFile(u) } } }
        }
        .sheet(isPresented: $showTake) {
            EngineTakeSheet(engine: engine.s("title")) { goal, rounds in
                do {
                    _ = try await API.shared.obj("POST", "/api/bots/convs/\(cid)/takeover", ["goal": goal, "rounds": rounds])
                    await load(); return nil
                } catch { return "没能交给管家：\(error.localizedDescription)" }
            }
        }
        .sheet(isPresented: $showModel) {
            EngineModelSheet(model: conv.s("model"), effort: conv.s("effort")) { v in
                do {
                    _ = try await API.shared.obj("PATCH", "/api/bots/convs/\(cid)", v)
                    await load(); changed(); return nil
                } catch { return "没改成：\(error.localizedDescription)" }
            }
        }
        .environment(\.aiwOpenFile, { p in Task { await openFile(p) } })
        .sheet(item: $preview) { p in
            if isHtmlFile(p.url) { HtmlPreviewView(url: p.url, serverPath: p.path) }
            else if !textPreviewKind(p.url).isEmpty, textPreviewPage() != nil { FilePreviewView(url: p.url, kind: textPreviewKind(p.url)) }
            else { QuickLookView(url: p.url).ignoresSafeArea() }
        }
        .alert("允许这类命令", isPresented: $showAllow) {
            TextField("规则", text: $allowRule)
            Button("允许并再试") { Task { await act("POST", "/permit", ["mode": "allow", "rule": allowRule]) } }
            Button("取消", role: .cancel) {}
        } message: { Text("这条规则会写进 agy 的权限设置，以后也算数。可以改得更窄一点。") }
        .confirmationDialog("这一轮放开权限重跑？", isPresented: $confirmOnce, titleVisibility: .visible) {
            Button("放开这一轮") { Task { await act("POST", "/permit", ["mode": "once"]) } }
        } message: { Text("这一轮不做权限检查，让它把被拦的那一步做完；下一轮恢复原来的规则。") }
        .alert("对话名称", isPresented: $showRename) {
            TextField("名称", text: $newTitle)
            Button("保存") { Task { await act("PATCH", "", ["title": newTitle]); changed() } }
            Button("取消", role: .cancel) {}
        }
        .confirmationDialog("删除这个对话？", isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("删除", role: .destructive) {
                Task {
                    do { _ = try await API.shared.call("DELETE", "/api/bots/convs/\(cid)"); changed(); dismiss() }
                    catch { err = error.localizedDescription }
                }
            }
        } message: { Text("电脑上的文件夹和文件不会删。") }
    }

    // MARK: header
    var menu: some View {
        Menu {
            if takeover.isEmpty {
                Button { showTake = true } label: { Label("交给管家", systemImage: "person.2") }
            } else {
                Button { Task { await act("DELETE", "/takeover") } } label: { Label("收回", systemImage: "hand.raised") }
            }
            if live { Button { Task { await act("POST", "/stop") } } label: { Label("停止", systemImage: "stop.circle") } }
            Button { showModel = true } label: { Label("模型和强度", systemImage: "cpu") }
            if conv.s("kind") == "dm" {   // v3.9.80: the engine's own chat keeps its context until a new topic
                Button { Task { await act("POST", "/new_topic") } } label: { Label("新话题", systemImage: "arrow.counterclockwise") }
            } else {
                Button { newTitle = conv.s("title"); showRename = true } label: { Label("改名", systemImage: "pencil") }
                Button(role: .destructive) { confirmDelete = true } label: { Label("删除对话", systemImage: "trash") }
            }
        } label: { Image(systemName: "ellipsis.circle").font(.system(size: 18)) }.accessibilityLabel("更多")
    }
    var takeBar: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("AIW 管家接管中（第 \(Int(takeover.d("rounds")))/\(Int(takeover.d("max"))) 轮）").font(.caption.weight(.bold)).foregroundColor(amber)
            Text(takeover.s("goal")).font(.caption).lineLimit(2)
            Text("你随时可以插话，它会先发你的话再继续；菜单里「收回」自己接手。").font(.caption2).foregroundColor(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 12).padding(.vertical, 8).background(amber.opacity(0.1)).background(.bar)
    }

    // MARK: rows
    @ViewBuilder func row(_ m: JSON) -> some View {
        switch m.s("role") {
        case "system":
            Text(m.s("text")).font(.caption).foregroundColor(.secondary).multilineTextAlignment(.center).frame(maxWidth: .infinity)
        case "user", "aiw": mine(m)
        default: bot(m)
        }
    }
    func mine(_ m: JSON) -> some View {
        let d = m.o("data"), aiw = m.s("role") == "aiw"
        let atts = d.a("attachments"), sk = (d["skills"] as? [String]) ?? []
        let bad = atts.filter { $0.s("vm_path").isEmpty }.map { $0.s("name") }
        return VStack(alignment: .trailing, spacing: 4) {
            if aiw { Text("AIW 管家" + (d["report"] is String ? " · " + d.s("report") : "")).font(.caption2.weight(.semibold)).foregroundColor(.secondary) }
            if !d.o("quote").isEmpty { quoteView(d.o("quote").s("who"), d.o("quote").s("text")) }
            if !sk.isEmpty {
                HStack(spacing: 4) { ForEach(sk, id: \.self) { s in Text("✨ " + skillTitle(s)).font(.caption2.weight(.semibold)).foregroundColor(hHL)
                    .padding(.horizontal, 6).padding(.vertical, 2).background(Capsule().fill(hTok)) } }
            }
            if !m.s("text").isEmpty {
                Group {
                    if d.b("report") { RichText(text: m.s("text")) } else { engineUserText(m.s("text")) }
                }
                .font(.callout).foregroundColor(aiw ? .primary : hInk)
                .padding(.horizontal, 12).padding(.vertical, 8)
                .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(aiw ? amber.opacity(0.15) : hAmber))
            }
            ForEach(atts.indices, id: \.self) { i in
                Label(atts[i].s("name"), systemImage: atts[i].s("kind") == "video" ? "film" : fileIcon(atts[i].s("name")))
                    .font(.caption).lineLimit(1).padding(.horizontal, 8).padding(.vertical, 4)
                    .background(Capsule().fill(Color(.secondarySystemBackground)))
            }
            if !bad.isEmpty { Text("有附件没能放到电脑上：" + bad.joined(separator: "、")).font(.caption2).foregroundColor(.red) }
            Text((d.b("queued") ? "排队中，它回完这一轮就发 · " : "") + when(m.d("created_at"))).font(.caption2).foregroundColor(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .trailing).padding(.leading, 40)
        .contextMenu { msgMenu(m) }
    }
    func bot(_ m: JSON) -> some View {
        let d = m.o("data"), st = d.s("status"), running = st == "running"
        let steps = d.a("steps"), denied = d.a("denied"), files = (d.o("changes")["files"] as? [String]) ?? []
        let said = ((d["said"] as? [String]) ?? []).first ?? ""
        return HStack(alignment: .top, spacing: 8) {
        EngineAvatar(bot: conv.s("bot"), size: 30)
        VStack(alignment: .leading, spacing: 5) {
            Text(engine.s("title") + (st == "blocked" ? " · 被权限挡住" : st == "stopped" ? " · 已停止" : ""))
                .font(.caption2.weight(.semibold)).foregroundColor(engineColor(conv.s("bot").isEmpty ? "agy" : conv.s("bot"), cs))
            if running && m.s("text").isEmpty {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text(said.isEmpty ? "在电脑上干活…" : String(said.prefix(80))).font(.footnote).foregroundColor(.secondary).lineLimit(2)
                }
                .padding(.horizontal, 12).padding(.vertical, 8)
                .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(Color(.secondarySystemBackground)))
            } else {
                RichText(text: m.s("text"))
                    .padding(.horizontal, 12).padding(.vertical, 8)
                    .background(RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .fill(["error", "failed", "blocked"].contains(st) ? Color.red.opacity(0.1) : Color(.secondarySystemBackground)))
            }
            let media = d.a("media")
            if !running && !media.isEmpty {
                EngineMediaView(media: media)
            }
            if !steps.isEmpty {
                DisclosureGroup("\(running ? "正在做" : "做了") \(stepCount(d, steps.count)) 步") {
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(Array(steps.suffix(15).enumerated()), id: \.offset) { _, s in
                            (Text(s.s("tool")).bold() + Text(" " + s.s("arg")) + Text(s.s("error").isEmpty ? "" : "  " + s.s("error")).foregroundColor(.red))
                                .font(.system(.caption2, design: .monospaced)).lineLimit(3)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(.top, 4)
                }.font(.caption).tint(.secondary)
            }
            if !denied.isEmpty {
                Text("被拒绝的操作：" + denied.map { "\($0.s("tool")) \($0.s("target"))（\($0.s("why").isEmpty ? $0.s("action") : $0.s("why"))）" }.joined(separator: "；"))
                    .font(.caption2).foregroundColor(.orange)
            }
            if !running {                           // v3.9.67: files it made on the computer: tap to preview / share
                let paths = engineFilePaths(m.s("text"))
                if !paths.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(paths, id: \.self) { p in
                            Button { Task { await openFile(p) } } label: {
                                HStack(spacing: 6) {
                                    Image(systemName: fetching == p ? "hourglass" : fileIcon(p)).foregroundColor(amber)
                                    Text((p as NSString).lastPathComponent).lineLimit(1).truncationMode(.middle)
                                    Text("预览 / 下载").foregroundColor(.secondary)
                                }.font(.caption)
                                .padding(.horizontal, 10).padding(.vertical, 6)
                                .background(RoundedRectangle(cornerRadius: 10).fill(Color(.secondarySystemBackground)))
                            }.buttonStyle(.plain).disabled(!fetching.isEmpty)
                        }
                    }
                }
            }
            if canPermit(m) {                       // v3.9.67: the user decides; AIW never widens the rules itself
                HStack(spacing: 8) {
                    Button {
                        allowRule = (denied.last ?? [:]).s("rule")
                        if allowRule.isEmpty { err = "没抓到被拦的具体命令，用「这一轮放开权限重跑」" } else { showAllow = true }
                    } label: { Text("允许这类命令再试").font(.caption.weight(.semibold)) }
                        .buttonStyle(.borderedProminent).tint(amber)
                    Button { confirmOnce = true } label: { Text("这一轮放开权限重跑").font(.caption.weight(.semibold)) }
                        .buttonStyle(.bordered).tint(amber)
                    Button { Task { await act("POST", "/permit", ["mode": "deny"]) } } label: { Text("拒绝").font(.caption.weight(.semibold)) }
                        .buttonStyle(.bordered)        // v3.9.68
                }.controlSize(.small)
            }
            if !files.isEmpty {
                Text("改动：" + files.prefix(8).joined(separator: "，") + (files.count > 8 ? " …" : "")).font(.caption2).foregroundColor(.secondary)
            }
            Text(when(m.d("created_at"))).font(.caption2).foregroundColor(.secondary)
        }
        }
        .frame(maxWidth: .infinity, alignment: .leading).padding(.trailing, 24)
        .contextMenu { if !running { msgMenu(m) } }
    }
    /// v3.9.77: the turn's whole step count (the message keeps only the newest 40 steps to show)
    func stepCount(_ d: JSON, _ shown: Int) -> Int {
        let n: Int = Int(d.s("step_count")) ?? 0
        return max(n, shown)
    }
    /// v3.9.67: copy the file from the computer (GET …/file), download it, preview it (QuickLook shares / saves)
    @MainActor func openFile(_ raw: String) async {
        guard fetching.isEmpty else { return }
        fetching = raw; defer { fetching = "" }
        do {
            let q = raw.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? raw
            let r = (try await API.shared.call("GET", "/api/bots/convs/\(cid)/file?path=\(q)", nil, timeout: 900) as? JSON) ?? [:]
            guard let u = API.shared.artifactURL(r.s("path")) else { return }
            let (tmp, _) = try await URLSession.shared.download(from: u)
            let dest = FileManager.default.temporaryDirectory.appendingPathComponent(previewFileName(r.s("name"), path: r.s("path")))
            try? FileManager.default.removeItem(at: dest)
            try FileManager.default.moveItem(at: tmp, to: dest)
            preview = Preview(url: dest, path: r.s("path"))
        } catch { err = "打不开这个文件：\(error.localizedDescription)" }
    }
    /// v3.9.67: the newest agy reply that the permission rules stopped, not yet answered, nothing running
    func canPermit(_ m: JSON) -> Bool {
        let d = m.o("data")
        guard d.s("status") == "blocked", d.s("permitted").isEmpty, !live, (conv.s("bot").isEmpty ? "agy" : conv.s("bot")) == "agy" else { return false }
        return msgs.last(where: { $0.s("role") == "bot" })?.s("id") == m.s("id")
    }
    @ViewBuilder func msgMenu(_ m: JSON) -> some View {
        Button { setQuote(m) } label: { Label("引用", systemImage: "quote.bubble") }
        Button { UIPasteboard.general.string = m.s("text") } label: { Label("复制", systemImage: "doc.on.doc") }
    }
    func setQuote(_ m: JSON) {
        let who = m.s("role") == "bot" ? engine.s("title") : (m.s("role") == "aiw" ? "AIW 管家" : "我")
        quote = EngineQuote(id: m.s("id"), who: who, text: m.s("text"))
        focused = true
    }
    func quoteView(_ who: String, _ t: String) -> some View {
        HStack(spacing: 6) {
            Rectangle().fill(hAmber).frame(width: 2)
            Text((who.isEmpty ? "" : who + "：") + t).font(.caption).foregroundColor(.secondary).lineLimit(2)
        }.fixedSize(horizontal: false, vertical: true)
    }

    // MARK: composer (the chat's: "+" sheet, attachment chips, "/" skills, IMEField)
    var composer: some View {
        VStack(spacing: 8) {
            if let q = slashQ { SkillPicker(rows: skillRows(q), query: q) { chooseSkill($0) } }
            if let q = quote {
                HStack(spacing: 6) {
                    quoteView(q.who, q.text)
                    Spacer(minLength: 4)
                    Button { quote = nil } label: { Image(systemName: "xmark.circle.fill").foregroundColor(.secondary) }.accessibilityLabel("取消引用")
                }
                .padding(.horizontal, 10).padding(.vertical, 6).background(Color(.secondarySystemBackground)).clipShape(RoundedRectangle(cornerRadius: 10))
            }
            if !attach.isEmpty || uploading {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack {
                        if uploading {
                            HStack(spacing: 6) { ProgressView(value: pct).frame(width: 60).tint(hAmber); Text("上传 \(Int(pct * 100))%").font(.caption2.monospacedDigit()) }
                                .padding(.horizontal, 8).padding(.vertical, 5).background(Color(.secondarySystemBackground)).clipShape(Capsule())
                        }
                        ForEach(attach.indices, id: \.self) { i in
                            HStack(spacing: 4) {
                                Image(systemName: attach[i].s("kind") == "video" ? "film" : fileIcon(attach[i].s("name"))); Text(attach[i].s("name")).lineLimit(1)
                                Button { attach.remove(at: i) } label: { Image(systemName: "xmark.circle.fill") }.accessibilityLabel("移除附件")
                            }.font(.caption).padding(.horizontal, 8).padding(.vertical, 5).background(Color(.secondarySystemBackground)).clipShape(Capsule())
                        }
                    }
                }
            }
            HStack(alignment: .bottom, spacing: 6) {
                Button { focused = false; showAttach = true } label: {
                    Image(systemName: "plus").font(.system(size: 22, weight: .light)).foregroundColor(hAmber).frame(width: 36, height: 36)
                }.accessibilityLabel("添加：拍照、照片、文件、技能")
                HStack(spacing: 6) {
                    ForEach(picked, id: \.self) { id in SkillToken(title: skillTitle(id)) { picked.removeAll { $0 == id } } }
                    IMEField(placeholder: takeover.isEmpty ? "和 \(engine.s("title")) 说…（/ 选技能）" : "插话，管家会先发你的话", text: $text, focused: $focused, maxLines: 6)
                }
                if canSend {
                    Button { Task { await send() } } label: {
                        Image(systemName: "arrow.up").font(.system(size: 16, weight: .bold)).foregroundColor(hInk)
                            .frame(width: 36, height: 36).background(hAmber).clipShape(Circle())
                    }.disabled(sending).accessibilityLabel(live ? "排队发送" : "发送")
                } else if live {
                    Button { Task { await act("POST", "/stop") } } label: {
                        Image(systemName: "stop.fill").font(.system(size: 13, weight: .bold)).foregroundColor(hAmber)
                            .frame(width: 36, height: 36).background(Color(white: 0.2)).clipShape(Circle())
                    }.accessibilityLabel("停止")
                }
            }
            .padding(.leading, 4).padding(.trailing, 6).padding(.vertical, 5)
            .background(Color(.systemBackground))
            .overlay(RoundedRectangle(cornerRadius: 28, style: .continuous).stroke(focused ? hAmber.opacity(0.6) : Color(.separator), lineWidth: 1))
            .clipShape(RoundedRectangle(cornerRadius: 28, style: .continuous))
        }
        .padding(.horizontal, 12).padding(.top, 6).padding(.bottom, 8)
        .background(.bar)
    }
    var canSend: Bool { !uploading && (!text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !attach.isEmpty) }

    // MARK: skills (the chat's "/" picker)
    func skillTitle(_ id: String) -> String {
        let s = skills.first { $0.s("id") == id }
        return (s?.s("title").isEmpty ?? true) ? id : s!.s("title")
    }
    func skillRows(_ q: String) -> [JSON] {
        let k = q.lowercased()
        let hit = skills.filter { s in k.isEmpty || [s.s("id"), s.s("title"), s.s("desc")].contains { $0.lowercased().contains(k) } }
        let first = hit.filter { $0.s("id").lowercased().hasPrefix(k) }, rest = hit.filter { !$0.s("id").lowercased().hasPrefix(k) }
        return Array((first + rest).prefix(30))
    }
    func chooseSkill(_ id: String) {
        if let r = text.range(of: "/[^\\s/]*$", options: .regularExpression) { text.removeSubrange(r) }
        if !picked.contains(id) { picked.append(id) }
        focused = true
    }
    func onText(_ t: String) {
        guard t.hasSuffix("\n") else { return }
        let before = String(t.dropLast())
        if let q = slashQuery(before), let first = skillRows(q).first { text = before; chooseSkill(first.s("id")) }
    }
    func runPending() {
        guard let k = pending else { return }
        pending = nil
        switch k {
        case .photo, .video:
            if UIImagePickerController.isSourceTypeAvailable(.camera) { camera = k == .video ? .video : .photo }
            else { err = "这台设备没有可用的相机" }
        case .library: showPhotos = true
        case .file: showFiles = true
        case .skill:
            text += (text.isEmpty || text.hasSuffix(" ") || text.hasSuffix("\n")) ? "/" : " /"
            focused = true
        default: focused = true
        }
    }

    // MARK: server
    @MainActor func load() async {
        do {
            let r = try await API.shared.obj("GET", "/api/bots/convs/\(cid)?after=0")
            conv = r.o("conv"); msgs = r.a("messages")
            if err.hasPrefix("读不到") { err = "" }
        } catch { err = "读不到这个对话：\(error.localizedDescription)" }
    }
    @MainActor func poll() async {
        await load()
        await EngineModels.shared.load(bot: conv.s("bot").isEmpty ? "agy" : conv.s("bot"))
        while !Task.isCancelled {
            await load()
            let busy = live || !takeover.isEmpty || conv.d("queued") > 0
            try? await Task.sleep(nanoseconds: (busy ? 2 : 8) * 1_000_000_000)
        }
    }
    @MainActor func act(_ method: String, _ path: String, _ body: JSON? = nil) async {
        do { _ = try await API.shared.call(method, "/api/bots/convs/\(cid)\(path)", body); await load() }
        catch { err = error.localizedDescription }
    }
    @MainActor func send() async {
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if uploading { err = "附件还在上传，传完再发"; return }
        if body.isEmpty && attach.isEmpty { if !picked.isEmpty { err = "说说要用这个技能做什么" }; return }
        sending = true; defer { sending = false }
        var req: JSON = ["text": body, "attachments": attach, "skills": picked]
        if let q = quote { req["quote"] = q.id }
        let keep = (text, attach, picked, quote)
        text = ""; attach = []; picked = []; quote = nil; err = ""
        do { _ = try await API.shared.call("POST", "/api/bots/convs/\(cid)/send", req, timeout: 180); await load() }
        catch {
            (text, attach, picked, quote) = keep
            err = "没发出去：\(error.localizedDescription)"
        }
    }

    // MARK: uploads (POST /api/uploads/raw, local_only: the server copies them into the conversation's folder on send)
    func gotCamera(_ u: URL?, _ img: UIImage?) async {
        if let u = u {
            await uploadFile(u, name: u.lastPathComponent, type: u.pathExtension.lowercased() == "mp4" ? "video/mp4" : "video/quicktime")
        } else if let img = img, let jpg = img.jpegData(compressionQuality: 0.85) {
            await uploadData(jpg, name: "photo-\(Int(Date().timeIntervalSince1970)).jpg", type: "image/jpeg")
        }
    }
    func addPicked(_ item: PhotosPickerItem) async {
        if item.supportedContentTypes.contains(where: { $0.conforms(to: .movie) || $0.conforms(to: .video) }) {
            do {
                guard let mv = try await item.loadTransferable(type: PickedMovie.self) else { err = "读不了这个视频"; return }
                let ext = mv.url.pathExtension.lowercased()
                await uploadFile(mv.url, name: "video-\(Int(Date().timeIntervalSince1970)).\(ext.isEmpty ? "mov" : ext)",
                                 type: ext == "mp4" ? "video/mp4" : "video/quicktime")
            } catch { err = "读不了这个视频：\(error.localizedDescription)" }
            return
        }
        guard let d = try? await item.loadTransferable(type: Data.self) else { err = "读不了这张照片"; return }
        let stamp = Int(Date().timeIntervalSince1970)
        if UIImage(data: d) == nil, let vx = videoExt(d) {
            await uploadData(d, name: "video-\(stamp).\(vx)", type: vx == "mp4" ? "video/mp4" : "video/quicktime")
            return
        }
        let img = UIImage(data: d)
        let jpg = img?.jpegData(compressionQuality: 0.85) ?? d
        await uploadData(jpg, name: img == nil ? "file-\(stamp)" : "photo-\(stamp).jpg", type: img == nil ? "application/octet-stream" : "image/jpeg")
    }
    func addFile(_ u: URL) async {
        let ok = u.startAccessingSecurityScopedResource()
        defer { if ok { u.stopAccessingSecurityScopedResource() } }
        let dst = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString.prefix(6))-\(u.lastPathComponent)")
        try? FileManager.default.removeItem(at: dst)
        do { try FileManager.default.copyItem(at: u, to: dst) } catch { err = "读不了 \(u.lastPathComponent)"; return }
        let type = UTType(filenameExtension: u.pathExtension)?.preferredMIMEType ?? "application/octet-stream"
        await uploadFile(dst, name: u.lastPathComponent, type: type)
    }
    func uploadData(_ data: Data, name: String, type: String) async {
        let dst = FileManager.default.temporaryDirectory.appendingPathComponent(name)
        do { try data.write(to: dst) } catch { err = "存不了临时文件：\(error.localizedDescription)"; return }
        await uploadFile(dst, name: name, type: type)
    }
    func uploadFile(_ u: URL, name: String, type: String) async {
        let size = ((try? FileManager.default.attributesOfItem(atPath: u.path))?[.size] as? NSNumber)?.intValue ?? 0
        if size > 500 * 1024 * 1024 { err = "\(name) 超过 500MB"; return }
        uploading = true; pct = 0
        defer { uploading = false }
        let q = name.addingPercentEncoding(withAllowedCharacters: queryValueChars) ?? "file"
        let mid = API.shared.machine.addingPercentEncoding(withAllowedCharacters: queryValueChars) ?? ""
        do {
            var r = try await API.shared.postFile("/api/uploads/raw?name=\(q)&machine_id=\(mid)&local_only=true", file: u, contentType: type) { f in
                DispatchQueue.main.async { pct = f }
            }
            if r.s("kind").isEmpty && type.hasPrefix("video") { r["kind"] = "video" }
            attach.append(r)
        } catch { err = "上传失败：\(name)：\(error.localizedDescription)" }
    }
}

// MARK: - 交给管家 / 模型和强度

struct EngineTakeSheet: View {
    let engine: String
    let go: (String, Int) async -> String?
    @Environment(\.dismiss) private var dismiss
    @State private var goal = ""
    @State private var rounds = 8
    @State private var busy = false
    @State private var error = ""
    var body: some View {
        NavigationStack {
            Form {
                Section(header: Text("目标和验收标准"), footer: Text("管家会替你和 \(engine) 来回沟通，直到目标完成、需要你决定或轮数用完。它不会让对方推送、合并或删除东西。")) {
                    TextEditor(text: $goal).frame(minHeight: 110)
                }
                Section { Stepper("最多来回 \(rounds) 轮", value: $rounds, in: 1...30) }
                if !error.isEmpty { Section { Text(error).foregroundColor(.red).font(.footnote) } }
            }
            .navigationTitle("交给 AIW 管家").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    if busy { ProgressView() } else {
                        Button("交给管家") {
                            Task {
                                busy = true
                                let e = await go(goal.trimmingCharacters(in: .whitespacesAndNewlines), rounds)
                                busy = false
                                if let e = e { error = e } else { dismiss() }
                            }
                        }.bold().disabled(goal.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    }
                }
            }
        }
    }
}

struct EngineModelSheet: View {
    let save: (JSON) async -> String?
    @Environment(\.dismiss) private var dismiss
    @State private var group: String
    @State private var effort: String
    @State private var busy = false
    @State private var error = ""
    init(model: String, effort: String, save: @escaping (JSON) async -> String?) {
        self.save = save
        let (g, e) = EngineModelSection.split(model, effort)
        _group = State(initialValue: g); _effort = State(initialValue: e)
    }
    var body: some View {
        NavigationStack {
            Form {
                Section { Text("从下一句起生效：引擎会带着原会话接着聊，前面聊的都还在。").font(.footnote).foregroundColor(.secondary) }
                EngineModelSection(group: $group, effort: $effort)
                if !error.isEmpty { Section { Text(error).foregroundColor(.red).font(.footnote) } }
            }
            .navigationTitle("模型和强度").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    if busy { ProgressView() } else {
                        Button("保存") {
                            Task {
                                busy = true
                                let e = await save(EngineModels.shared.value(group, effort))
                                busy = false
                                if let e = e { error = e } else { dismiss() }
                            }
                        }.bold()
                    }
                }
            }
        }
    }
}


// MARK: - v3.9.80 an engine's settings (long-press its avatar): model and strength, computer and folder for its own chat
struct EngineCfgTarget: Identifiable { let id: String }

struct EngineCfgSheet: View {
    let engine: JSON
    let changed: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var group = ""
    @State private var effort = ""
    @State private var machine = ""
    @State private var workdir = ""
    @State private var machines: [JSON] = []
    @State private var busy = false
    @State private var error = ""
    var bot: String { engine.s("id") }
    var cloud: Bool { engine.b("cloud") }
    var body: some View {
        NavigationStack {
            Form {
                Section {
                    HStack(spacing: 10) {
                        EngineAvatar(bot: bot, size: 40)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(engine.s("title")).font(.headline)
                            Text(engine.s("desc")).font(.caption).foregroundColor(.secondary)
                        }
                    }
                    if cloud {
                        Text(engine.b("configured") ? "已配置凭据" : "还没配置凭据，请在网页的 引擎 › 点名字 › 配置凭据 里填。").font(.footnote).foregroundColor(.secondary)
                    } else {
                        Text("登录：" + (engine.s("login").isEmpty ? "在电脑上登录这个 CLI" : engine.s("login"))).font(.footnote).foregroundColor(.secondary)
                    }
                }
                EngineModelSection(group: $group, effort: $effort)
                if !cloud {
                    Section(header: Text("电脑")) {
                        Picker("电脑", selection: $machine) {
                            Text("默认").tag("")
                            ForEach(machines.indices, id: \.self) { i in
                                Text(machines[i].s("name").isEmpty ? machines[i].s("id") : machines[i].s("name")).tag(machines[i].s("id"))
                            }
                        }
                    }
                    Section(header: Text("单聊的工作文件夹"), footer: Text("单聊在这里干活；空着用 ~/workspace/\(bot)-chat/main。")) {
                        TextField("~/workspace/\(bot)-chat/main", text: $workdir).textInputAutocapitalization(.never).autocorrectionDisabled()
                    }
                }
                if !error.isEmpty { Section { Text(error).foregroundColor(.red).font(.footnote) } }
            }
            .navigationTitle("配置").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    if busy { ProgressView() } else { Button("保存") { Task { await save() } }.bold() }
                }
            }
        }
        .task {
            await EngineModels.shared.load(bot: bot)
            if let r = try? await API.shared.obj("GET", "/api/bots/engines/\(bot)/config") {
                let c = r.o("cfg")
                let (g, e) = EngineModelSection.split(c.s("model"), c.s("effort"))
                group = g; effort = e; machine = c.s("machine_id"); workdir = c.s("workdir")
            }
            if !cloud { machines = (try? await API.shared.list("/api/machines")) ?? [] }
        }
    }
    @MainActor func save() async {
        busy = true; defer { busy = false }
        var req: JSON = ["model": "", "effort": ""]
        for (k, v) in EngineModels.shared.value(group, effort) { req[k] = v }
        if !cloud { req["machine_id"] = machine; req["workdir"] = workdir }
        do { _ = try await API.shared.obj("PUT", "/api/bots/engines/\(bot)/config", req); changed(); dismiss() }
        catch { self.error = "没保存：\(error.localizedDescription)" }
    }
}


// MARK: - v3.9.61 引擎群聊: several engines on one git repository, each on its own branch; AIW 管家 splits and merges

let engineGroupStatus: [String: String] = ["working": "在干活", "merging": "管家在合并", "fixing": "在解决冲突"]

struct EngineGroupNewSheet: View {
    let engines: [JSON]
    let done: (JSON) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var picked: Set<String> = ["codex", "claude"]
    @State private var title = ""
    @State private var repo = ""
    @State private var base = ""
    @State private var autoMerge = true
    @State private var goal = ""
    @State private var busy = false
    @State private var error = ""
    var body: some View {
        NavigationStack {
            Form {
                Section(header: Text("成员"), footer: Text("每个引擎在自己的分支和工作树里并行干，做完由 AIW 管家合并到一个新分支。不推送，也不动基线分支。")) {
                    ForEach(engines.filter { $0.b("available") }.indices, id: \.self) { i in
                        let e = engines.filter { $0.b("available") }[i]
                        let id = e.s("id")
                        Toggle(e.s("title"), isOn: Binding(get: { picked.contains(id) },
                                                            set: { on in if on { picked.insert(id) } else { picked.remove(id) } }))
                    }
                }
                Section(header: Text("标题")) { TextField("例如 登录页改版", text: $title) }
                Section(header: Text("git 仓库"), footer: Text("电脑上的文件夹；空着就新建一个仓库。")) {
                    TextField("~/workspace/ldok", text: $repo).textInputAutocapitalization(.never).autocorrectionDisabled()
                    TextField("基线分支（空着用当前分支）", text: $base).textInputAutocapitalization(.never).autocorrectionDisabled()
                }
                Section { Toggle("都做完后管家自动合并", isOn: $autoMerge) }
                Section(header: Text("要做什么（可以先空着）")) {
                    TextField("例如：做一个登录页，后端加接口，前端做页面，跑通测试", text: $goal, axis: .vertical).lineLimit(2...6)
                }
                if !error.isEmpty { Section { Text(error).foregroundColor(.red).font(.footnote) } }
            }
            .navigationTitle("新群聊").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    if busy { ProgressView() } else { Button("建群") { Task { await create() } }.bold() }
                }
            }
        }
    }
    @MainActor func create() async {
        if picked.isEmpty { error = "至少选一个引擎"; return }
        busy = true; defer { busy = false }
        let order = engines.map { $0.s("id") }.filter { picked.contains($0) }
        let req: JSON = ["title": title, "repo": repo, "base": base, "members": order, "auto_merge": autoMerge, "goal": goal]
        do { done(try await API.shared.obj("POST", "/api/bots/groups", req)) }
        catch let e { error = "没能建群：\(e.localizedDescription)" }
    }
}

struct EngineGroupView: View {
    let gid: String
    let engines: [JSON]
    let changed: () -> Void
    @Environment(\.dismiss) private var dismiss
    @Environment(\.colorScheme) private var cs
    @State private var group: JSON = [:]
    @State private var msgs: [JSON] = []
    @State private var text = ""
    @State private var err = ""
    @State private var sending = false
    @State private var confirmDelete = false
    @State private var openConv: String? = nil
    @State private var askDeliver: String? = nil     // v3.9.86: push / merge wait for a second tap

    var project: JSON { group.o("project") }
    var live: Bool { group.b("busy") || (!group.s("status").isEmpty && group.s("status") != "idle") }
    func title(_ bot: String) -> String { engines.first { $0.s("id") == bot }?.s("title") ?? bot }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    if msgs.isEmpty && !group.isEmpty {
                        Text("说要做什么：不 @ 就由 AIW 管家分工；@codex 只发给它。每个引擎在自己的分支上改，做完管家合并。\n以「项目规划：」开头发一份规划，就按 讨论 → 设计 → 开发 → 评审 → 验收 → 实测截图 → 交付 走完。")
                            .font(.footnote).foregroundColor(.secondary).frame(maxWidth: .infinity).padding(.top, 40)
                    }
                    ForEach(msgs.indices, id: \.self) { i in row(msgs[i]) }
                    if !err.isEmpty { Text(err).font(.footnote).foregroundColor(.red) }
                    Color.clear.frame(height: 1).id("end")
                }.padding(.horizontal, 14).padding(.vertical, 10)
            }
            .scrollDismissesKeyboard(.interactively)
            .safeAreaInset(edge: .top) { statusBar }
            .safeAreaInset(edge: .bottom) { composer }
            .onChange(of: msgs.count) { _ in withAnimation { proxy.scrollTo("end", anchor: .bottom) } }
        }
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) {
                VStack(spacing: 0) {
                    Text(group.s("title").isEmpty ? "群聊" : group.s("title")).font(.subheadline.weight(.bold)).lineLimit(1)
                    Text(group.s("repo") + " · " + group.s("base")).font(.caption2).foregroundColor(.secondary).lineLimit(1)
                }
            }
            ToolbarItem(placement: .navigationBarTrailing) {
                Menu {
                    Button { Task { await act("POST", "/merge") } } label: { Label("合并", systemImage: "arrow.triangle.merge") }
                    if live { Button { Task { await act("POST", "/stop") } } label: { Label("停止", systemImage: "stop.circle") } }
                    ForEach(group.a("members").indices, id: \.self) { i in
                        let m = group.a("members")[i]
                        Button { openConv = m.s("conv") } label: { Label("\(title(m.s("bot"))) 的对话", systemImage: "bubble.left") }
                    }
                    Button(role: .destructive) { confirmDelete = true } label: { Label("删除群聊", systemImage: "trash") }
                } label: { Image(systemName: "ellipsis.circle").font(.system(size: 18)) }.accessibilityLabel("更多")
            }
        }
        .navigationDestination(isPresented: Binding(get: { openConv != nil }, set: { on in if !on { openConv = nil } })) {
            EngineConvView(cid: openConv ?? "", engines: engines) { changed() }
        }
        .confirmationDialog("删除这个群聊？", isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("删除", role: .destructive) {
                Task {
                    do { _ = try await API.shared.call("DELETE", "/api/bots/groups/\(gid)"); changed(); dismiss() }
                    catch let e { err = e.localizedDescription }
                }
            }
        } message: { Text("电脑上的仓库、分支和工作树不会删。") }
        .confirmationDialog(askDeliver == "push" ? "把分支推送到 origin？" : "把成果合入 \(group.s("base"))？",
                            isPresented: Binding(get: { askDeliver != nil }, set: { on in if !on { askDeliver = nil } }), titleVisibility: .visible) {
            Button(askDeliver == "push" ? "推送" : "合入") { let v = askDeliver ?? ""; Task { await projectAct("deliver", v) } }
        } message: { Text(askDeliver == "push" ? "推送 aiw/\(gid)/merge，不会动 \(group.s("base"))。" : "只有你的仓库正停在 \(group.s("base")) 且没有未提交改动时才会合。") }
        .task { await poll() }
    }

    @ViewBuilder var statusBar: some View {
        VStack(spacing: 0) {
            if !project.s("phase").isEmpty { projectBar }
            workingBar
        }
    }

    /// v3.9.86 项目模式: which stage the project is at, the design picked, rework rounds, what was delivered
    var projectBar: some View {
        let order = ["discuss", "design", "dev", "accept", "run", "deliver"]
        let names = ["discuss": "讨论", "design": "设计", "pick": "选设计", "confirm": "确认", "dev": "开发", "rework": "返工",
                     "accept": "验收", "fix": "返修", "run": "实测", "deliver": "交付", "stopped": "已停止"]
        let alias = ["pick": "design", "confirm": "design", "rework": "dev", "fix": "accept"]
        let ph = project.s("phase"), cur = order.firstIndex(of: alias[ph] ?? ph) ?? -1
        let done = ((project["delivered"] as? [String]) ?? []).map { ["patch": "补丁", "push": "推送", "merge": "合入"][$0] ?? $0 }
        var extra: [String] = []
        if ph == "stopped" { extra.append("已停止") } else if !order.contains(ph), let n = names[ph] { extra.append("现在：" + n) }
        if !project.s("pick").isEmpty { extra.append("设计：方案 " + project.s("pick")) }
        if project.d("rework") > 0 { extra.append("返工 \(Int(project.d("rework"))) 轮") }
        if project.d("fixes") > 0 { extra.append("验收返修 \(Int(project.d("fixes"))) 次") }
        if !done.isEmpty { extra.append("已交付：" + done.joined(separator: "、")) }
        return VStack(alignment: .leading, spacing: 4) {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 4) {
                    ForEach(order.indices, id: \.self) { i in
                        if i > 0 { Text("›").font(.caption2).foregroundColor(.secondary) }
                        Text(names[order[i]] ?? "").font(.caption2.weight(i == cur ? .bold : .regular))
                            .foregroundColor(i == cur ? Color(red: 0.16, green: 0.10, blue: 0.02) : .primary)
                            .opacity(i <= cur ? 1 : 0.55)
                            .padding(.horizontal, 8).padding(.vertical, 3)
                            .background(Capsule().fill(i == cur ? amber : Color.secondary.opacity(0.12)))
                    }
                }
            }
            if !extra.isEmpty { Text(extra.joined(separator: " · ")).font(.caption2).foregroundColor(.secondary).lineLimit(1) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 12).padding(.vertical, 6).background(.bar)
    }

    /// the buttons on AIW 管家's newest message that asks something (开工 / 用方案 X / 交付), live only at that stage
    @ViewBuilder func actions(_ m: JSON) -> some View {
        let acts = m.o("data").a("actions")
        let last = msgs.last { !$0.o("data").a("actions").isEmpty }
        if !acts.isEmpty, let l = last, l.s("id") == m.s("id") {
            let live = ["go": "confirm", "pick": "pick", "deliver": "deliver"]
            HStack(spacing: 8) {
                ForEach(acts.indices, id: \.self) { i in
                    let a = acts[i], on = project.s("phase") == live[a.s("id")]
                    let primary = a.s("id") != "deliver" || a.s("value") == "patch"
                    Button {
                        if a.s("id") == "deliver" && a.s("value") != "patch" { askDeliver = a.s("value") }
                        else { Task { await projectAct(a.s("id"), a.s("value")) } }
                    } label: { Text(a.s("label")).font(.caption.weight(.semibold)) }
                        .buttonStyle(.bordered).tint(primary ? amber : Color.secondary).disabled(!on)
                }
            }
        }
    }

    @ViewBuilder var workingBar: some View {
        let pending = (group["pending"] as? [String]) ?? []
        if !pending.isEmpty || group.s("status") == "merging" {
            VStack(alignment: .leading, spacing: 2) {
                Text(engineGroupStatus[group.s("status")] ?? "在干活").font(.caption.weight(.bold)).foregroundColor(amber)
                if !pending.isEmpty {
                    Text("等 " + pending.map { title($0) }.joined(separator: "、") + " 回复" + (group.b("auto_merge") ? "，都回复后管家自动合并" : ""))
                        .font(.caption2).foregroundColor(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 12).padding(.vertical, 8).background(amber.opacity(0.1)).background(.bar)
        }
    }

    var composer: some View {
        HStack(alignment: .bottom, spacing: 8) {
            TextField("说要做什么，@muse @cue @codex @claude @grok @cbc @agy 指定谁做…", text: $text, axis: .vertical)
                .lineLimit(1...5).padding(.horizontal, 12).padding(.vertical, 8)
                .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Color(.secondarySystemBackground)))
            Button { Task { await send() } } label: {
                Image(systemName: sending ? "hourglass" : "arrow.up.circle.fill").font(.system(size: 30)).foregroundColor(amber)
            }.disabled(sending || text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
        }
        .padding(.horizontal, 12).padding(.vertical, 8).background(.bar)
    }

    @ViewBuilder func row(_ m: JSON) -> some View {
        let role = m.s("role")
        if role == "system" {
            Text(m.s("text")).font(.caption).foregroundColor(.secondary).multilineTextAlignment(.center).frame(maxWidth: .infinity)
        } else if role == "user" || role == "aiw" {
            VStack(alignment: .trailing, spacing: 4) {
                if role == "aiw" { Text("AIW 管家").font(.caption2.weight(.semibold)).foregroundColor(.secondary) }
                Group {
                    if role == "aiw" { RichText(text: m.s("text")) } else { engineUserText(m.s("text")) }
                }
                .font(.callout).foregroundColor(role == "aiw" ? .primary : hInk)
                .padding(.horizontal, 12).padding(.vertical, 8)
                .background(RoundedRectangle(cornerRadius: 16, style: .continuous).fill(role == "aiw" ? amber.opacity(0.15) : hAmber))
                if !m.o("data").a("media").isEmpty { EngineMediaList(media: m.o("data").a("media")) }
                actions(m)
                Text(when(m.d("created_at"))).font(.caption2).foregroundColor(.secondary)
            }.frame(maxWidth: .infinity, alignment: .trailing)
        } else {
            let d = m.o("data")
            let bad = ["error", "failed", "blocked"].contains(d.s("status"))
            let files = (d.o("changes")["files"] as? [String]) ?? []
            HStack(alignment: .top, spacing: 8) {
            EngineAvatar(bot: m.s("who"), size: 30)
            VStack(alignment: .leading, spacing: 4) {
                Text(title(m.s("who")) + (bad ? " · 没做成" : "")).font(.caption2.weight(.semibold)).foregroundColor(engineColor(m.s("who"), cs))
                RichText(text: m.s("text").isEmpty ? "（没有文字）" : m.s("text"))
                    .font(.callout).padding(.horizontal, 12).padding(.vertical, 8)
                    .background(RoundedRectangle(cornerRadius: 16, style: .continuous)
                        .fill(bad ? Color.red.opacity(0.12) : Color(.secondarySystemBackground)))
                if !d.a("media").isEmpty { EngineMediaList(media: d.a("media")) }
                if !files.isEmpty {
                    Text("改动：" + files.prefix(8).joined(separator: "，")).font(.caption2).foregroundColor(.secondary).lineLimit(2)
                }
                HStack(spacing: 8) {
                    Text(when(m.d("created_at"))).font(.caption2).foregroundColor(.secondary)
                    if !d.s("conv").isEmpty { Button("看它的对话 ›") { openConv = d.s("conv") }.font(.caption2) }
                }
            }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @MainActor func load() async {
        do {
            let r = try await API.shared.obj("GET", "/api/bots/groups/\(gid)?after=0")
            group = r.o("group"); msgs = r.a("messages")
            if err.hasPrefix("读不到") { err = "" }
        } catch let e { err = "读不到这个群聊：\(e.localizedDescription)" }
    }
    @MainActor func poll() async {
        while !Task.isCancelled {
            await load()
            try? await Task.sleep(nanoseconds: (live ? 2 : 8) * 1_000_000_000)
        }
    }
    @MainActor func send() async {
        let body = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if body.isEmpty { return }
        sending = true; defer { sending = false }
        do {
            _ = try await API.shared.obj("POST", "/api/bots/groups/\(gid)/send", ["text": body])
            text = ""; err = ""
            await load()
        } catch let e { err = "没发出去：\(e.localizedDescription)" }
    }
    @MainActor func projectAct(_ action: String, _ value: String) async {
        do {
            _ = try await API.shared.obj("POST", "/api/bots/groups/\(gid)/project", ["action": action, "value": value])
            err = ""; await load()
        } catch let e { err = e.localizedDescription }
    }
    @MainActor func act(_ method: String, _ path: String) async {
        do { _ = try await API.shared.obj(method, "/api/bots/groups/\(gid)\(path)"); await load(); changed() }
        catch let e { err = e.localizedDescription }
    }
}
