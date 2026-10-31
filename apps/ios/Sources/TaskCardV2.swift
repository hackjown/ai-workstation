import SwiftUI

/// 3.8.8: ONE card per task in the 小秘 chat (server chat.py, task card v2), updated in place over /ws/events:
/// * head: title, prompt summary, status chip (运行中 / 等你 / 已完成 / 失败 / 已停止), step, elapsed time, the latest
///   one-line progress; tap → the task view (full steps, screenshots and messages live only there)
/// * actions ON the card: takeover / slider, escalation and question buttons, a form, a confirmation
///   (POST /api/chat/messages/{card}/answer with action_id); answered ones collapse to a ✓ line
/// * the result: summary + a thumbnail of the output picture (or the file), 查看任务
/// * 继续 / 修改后重新发布 (or 补充 while it runs) attach to the SAME task (its card re-arms and moves down)
/// Cards from before 3.8.8 (no data.card) render the same way, minus actions and result (those were messages then).

func cardChip(_ st: String) -> String {
    switch st {
    case "running", "pending", "paused": return "运行中"
    case "takeover", "waiting_input", "waiting_confirm": return "等你"
    case "succeeded": return "已完成"
    case "failed", "interrupted": return "失败"
    case "stopped", "cancelled": return "已停止"
    case "queued": return "排队中"
    default: return statusText[st] ?? st
    }
}

func durText(_ s: Double) -> String {
    let n = max(0, Int(s.rounded()))
    if n < 60 { return "\(n) 秒" }
    if n < 3600 { return "\(n / 60) 分" + (n % 60 > 0 ? " \(n % 60) 秒" : "") }
    return "\(n / 3600) 小时 \((n % 3600) / 60) 分"
}

/// a plain string field, never "<null>" (a JSON null reads as NSNull)
func jstr(_ d: JSON, _ k: String) -> String { (d[k] as? String) ?? "" }

/// A follow-up typed on a card: 修改后重新发布 (edit) or 补充 (instruct).
struct CardTextAsk: Identifiable {
    let ask: TaskTextAsk
    let taskId: String
    let rootId: String
    var id: String { ask.id + taskId }
}

struct TaskCardRow: View {
    let msg: JSON
    @ObservedObject var model: HomeChat
    let open: (Bool) -> Void
    let retry: () -> Void
    let textAsk: (CardTextAsk) -> Void
    let openFile: (String, String) -> Void
    @Environment(\.colorScheme) private var cs

    var d: JSON { msg.o("data") }
    var st: String { d.s("status").isEmpty ? "running" : d.s("status") }
    var waiting: Bool { d.a("actions").contains { $0.o("resolved").isEmpty && !$0.b("stale") } }

    var body: some View {
        let tone = cardTone(st)
        VStack(alignment: .leading, spacing: 8) {
            Button { open(false) } label: { TaskCardHead(msg: msg) }.buttonStyle(.plain).accessibilityHint("打开任务")
            if isLive(st), !jstr(d, "progress").isEmpty {
                Text(jstr(d, "progress")).font(.caption).foregroundColor(.secondary).lineLimit(2).padding(.leading, 48)
            }
            if d.d("runs") > 1 {
                Text("↻ 接着做" + (jstr(d, "followup").isEmpty ? "" : "：" + jstr(d, "followup")))
                    .font(.caption).foregroundColor(hStrong(cs)).lineLimit(1).padding(.leading, 48)
            }
            ForEach(d.a("actions").indices, id: \.self) { i in
                CardActionView(card: msg, action: d.a("actions")[i], model: model, open: open)
            }
            if !isLive(st), !d.o("result").s("text").isEmpty {
                CardResultView(result: d.o("result"), openFile: openFile)
            }
            footer
        }
        .padding(.vertical, 11).padding(.leading, 14).padding(.trailing, 12)
        .taskCardStyle(waiting ? .live : tone, cs)
        .padding(.trailing, 20)
    }

    @ViewBuilder var footer: some View {
        let tid = d.s("task_id").isEmpty ? msg.s("task_id") : d.s("task_id")
        let root = d.s("root_id").isEmpty ? tid : d.s("root_id")
        HStack(spacing: 10) {
            Button("查看任务 ›") { open(false) }.font(.caption).tint(hStrong(cs))
            Spacer(minLength: 4)
            if d.d("card") > 0 {
                if isLive(st) && st != "queued" {
                    Button("补充") { textAsk(CardTextAsk(ask: TaskTextAsk(kind: .instruct, text: ""), taskId: tid, rootId: root)) }
                        .font(.caption.weight(.semibold)).buttonStyle(.bordered).tint(hAmber).controlSize(.mini)
                } else if !isLive(st) && !tid.isEmpty {
                    Button("修改后重新发布") {
                        textAsk(CardTextAsk(ask: TaskTextAsk(kind: .edit, text: jstr(d, "prompt").isEmpty ? jstr(d, "title") : jstr(d, "prompt")),
                                            taskId: tid, rootId: root))
                    }.font(.caption.weight(.semibold)).buttonStyle(.bordered).tint(hAmber).controlSize(.mini)
                    Button("继续") { Task { await model.continueTask(tid) } }
                        .font(.caption.weight(.semibold)).buttonStyle(.borderedProminent).tint(hAmber).controlSize(.mini)
                }
            } else if cardTone(st) == .failed && !tid.isEmpty {
                Button("重试", action: retry).font(.caption.weight(.semibold)).buttonStyle(.bordered).tint(hAmber).controlSize(.mini)
            }
        }
    }
}

/// icon, title, prompt, status chip, step · elapsed, progress bar
struct TaskCardHead: View {
    let msg: JSON
    @Environment(\.colorScheme) private var cs
    var body: some View {
        let d = msg.o("data")
        let st = d.s("status").isEmpty ? "running" : d.s("status")
        let tone = cardTone(st)
        HStack(alignment: .top, spacing: 12) {
            CardIcon(st: st)
            VStack(alignment: .leading, spacing: 3) {
                Text(cleanTitle(d.s("title").isEmpty ? msg.s("text") : d.s("title"))).font(.callout.weight(.semibold)).lineLimit(2)
                    .foregroundColor(cardTitle(cs))
                if !jstr(d, "prompt").isEmpty && jstr(d, "prompt") != d.s("title") {
                    Text(jstr(d, "prompt")).font(.caption).foregroundColor(.secondary).lineLimit(1)
                }
                CardSubLine(msg: msg).foregroundColor(cardAccent(tone, cs))
                if !d.s("machine_name").isEmpty {   // aiw-roles-1: the computer (and its role) this task runs on
                    Label(d.s("machine_name"), systemImage: "desktopcomputer").font(.caption2).foregroundColor(.secondary).lineLimit(1)
                }
                if isLive(st) { CardProgress(frac: frac(d, st), tone: tone).padding(.top, 3) }
            }
            Spacer(minLength: 4)
            Chip(text: cardChip(st), color: tone == .failed ? cardRed : (cs == .dark ? hAmber : hHLLight), dot: true)
        }
        .contentShape(Rectangle())
    }
    func frac(_ d: JSON, _ st: String) -> Double {
        let total = d.d("plan_total")
        if total > 0 { return min(1, d.d("plan_done") / total) }
        if d.d("max_steps") > 0 { return min(1, d.d("step") / d.d("max_steps")) }
        return 0.04
    }
}

/// status line · n/m 项 · 第 n 步 · elapsed (ticks every second while the task runs)
struct CardSubLine: View {
    let msg: JSON
    var body: some View {
        let d = msg.o("data")
        let st = d.s("status").isEmpty ? "running" : d.s("status")
        TimelineView(.periodic(from: .now, by: 1)) { ctx in
            Text((isLive(st) ? "● " : "") + line(d, st, ctx.date)).font(.caption).lineLimit(1)
        }
    }
    func line(_ d: JSON, _ st: String, _ now: Date) -> String {
        var parts: [String] = []
        let l = jstr(d, "status_line")
        if !l.isEmpty && l != statusText[st] && l != cardChip(st) { parts.append(l) }
        if d.d("plan_total") > 0 { parts.append("\(Int(d.d("plan_done")))/\(Int(d.d("plan_total"))) 项") }
        if d.d("step") > 0 { parts.append("第 \(Int(d.d("step")))\(d.d("max_steps") > 0 ? "/\(Int(d.d("max_steps")))" : "") 步") }
        let start = d.d("started_at") > 0 ? d.d("started_at") : (d.d("card") > 0 ? 0 : msg.d("created_at"))
        if start > 0 && st != "queued" {
            let end = d.d("ended_at") > 0 ? d.d("ended_at") : (isLive(st) ? now.timeIntervalSince1970 : msg.d("updated_at"))
            parts.append(durText(end - start))
        }
        if st == "queued" { parts.append("点开可立即发送或删除") }
        return parts.isEmpty ? "交给电脑的任务" : parts.joined(separator: " · ")
    }
}

/// One thing the task needs from you, on its card. Answered → a ✓ line; stale → a grey line.
struct CardActionView: View {
    let card: JSON
    let action: JSON
    @ObservedObject var model: HomeChat
    let open: (Bool) -> Void
    @State private var reply = ""
    @State private var busy = false
    @Environment(\.colorScheme) private var cs

    var kind: String { action.s("kind") == "confirm" ? "confirm" : (action.s("ask_kind").isEmpty ? "question" : action.s("ask_kind")) }
    var head: String {
        switch kind {
        case "confirm": return "需要你确认"
        case "takeover": return "需要你接管"
        case "form": return "需要你填写"
        case "escalate": return "任务卡住了"
        default: return "任务在问你"
        }
    }
    var question: String {
        kind == "confirm" && !action.o("summary").s("title").isEmpty ? action.o("summary").s("title") : jstr(action, "question")
    }

    var body: some View {
        let resolved = action.o("resolved")
        if !resolved.isEmpty {
            Label(head + "：" + resolvedText(resolved), systemImage: "checkmark").font(.footnote).foregroundColor(okGreen).lineLimit(1)
        } else if action.b("stale") {
            Text(head + " · 已过期").font(.footnote).foregroundColor(.secondary).lineLimit(1)
        } else {
            openBox
        }
    }

    @ViewBuilder var openBox: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(head).font(.caption.weight(.bold)).foregroundColor(hStrong(cs))
            Text(question).font(.callout.weight(.semibold)).foregroundColor(hBubbleText(cs)).textSelection(.enabled)
            detail
            buttons
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(hAmber.opacity(cs == .dark ? 0.12 : 0.09)))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).stroke(hAmber.opacity(0.6), lineWidth: 1))
    }

    @ViewBuilder var detail: some View {
        if kind == "confirm" {
            let s = action.o("summary")
            if !s.s("what").isEmpty { Text(s.s("what")).font(.footnote).foregroundColor(.secondary) }
            let why = [s.s("risk_cn"), s.s("why")].filter { !$0.isEmpty }.joined(separator: " · ")
            if !why.isEmpty { Text(why).font(.footnote).foregroundColor(.secondary) }
        } else if kind == "takeover" {
            let t = action.o("takeover")
            let steps = jstr(t, "steps").isEmpty ? jstr(t, "reason") : jstr(t, "steps")
            if !steps.isEmpty { Text(steps).font(.footnote).foregroundColor(.secondary) }
        }
    }

    @ViewBuilder var buttons: some View {
        switch kind {
        case "confirm":
            HStack {
                Button("允许") { go(["approve": true]) }.buttonStyle(.borderedProminent).tint(amber)
                Button("不允许") { go(["approve": false]) }.buttonStyle(.bordered)
            }.controlSize(.small).disabled(busy)
        case "takeover":
            HStack {
                Button { open(true) } label: { Label("接管桌面", systemImage: "hand.raised.fill") }.buttonStyle(.borderedProminent).tint(amber)
                Button("完成，继续") { go(["choice": "done"]) }.buttonStyle(.bordered)
            }.controlSize(.small).disabled(busy)
        case "form":
            Button("去填写") { open(false) }.buttonStyle(.borderedProminent).tint(amber).controlSize(.small)
        default:
            questionButtons
        }
    }

    @ViewBuilder var questionButtons: some View {
        let opts = (action["options"] as? [Any])?.map { "\($0)" } ?? []
        if !opts.isEmpty { FlowButtons(options: opts) { o in go(["choice": o]) }.disabled(busy) }
        HStack {
            TextField(opts.isEmpty ? "回答…" : "或者直接告诉它…", text: $reply).textFieldStyle(.roundedBorder).submitLabel(.send)
                .onSubmit { send() }
            Button("回复") { send() }.buttonStyle(.borderedProminent).tint(amber).controlSize(.small)
                .disabled(reply.trimmingCharacters(in: .whitespaces).isEmpty || busy)
        }
    }

    func send() {
        let s = reply.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return }
        go(["text": s]); reply = ""
    }
    func go(_ body: JSON) {
        busy = true
        var b = body
        b["action_id"] = action.s("id")
        Task {
            let r = await model.answer(card.s("id"), b)
            busy = false
            if let r = r, r.b("open_task") { open(true) }   // 我来接管桌面 → the task view, straight into the desktop
        }
    }
}

/// The task's result on its card: the summary, a thumbnail of the output picture, the files.
struct CardResultView: View {
    let result: JSON
    let openFile: (String, String) -> Void
    @Environment(\.colorScheme) private var cs
    var body: some View {
        let ok = result.s("status") == "succeeded"
        let files = result.a("files")
        let pics = files.filter { $0.s("kind") == "image" }
        VStack(alignment: .leading, spacing: 8) {
            RichText(text: result.s("text"), strong: hStrong(cs)).foregroundColor(hBubbleText(cs))
            if let p = pics.first { thumb(p, more: pics.count - 1) }
            ForEach(files.indices, id: \.self) { i in
                if files[i].s("kind") != "image" { fileRow(files[i]) }
            }
        }
        .padding(.leading, 10).padding(.vertical, 4)
        .overlay(alignment: .leading) { Rectangle().fill(ok ? okGreen : cardRed).frame(width: 3) }
    }
    @ViewBuilder func thumb(_ f: JSON, more: Int) -> some View {
        Button { openFile(f.s("path"), f.s("name")) } label: {
            ZStack(alignment: .bottomTrailing) {
                AsyncImage(url: API.shared.artifactURL(f.s("path"), download: false)) { img in
                    img.resizable().scaledToFill()
                } placeholder: {
                    ZStack { Color(.secondarySystemBackground); Image(systemName: "photo").foregroundColor(.secondary) }
                }
                .frame(width: 180, height: 120).clipped()
                if more > 0 {
                    Text("+\(more)").font(.caption2.weight(.bold)).foregroundColor(.white)
                        .padding(.horizontal, 6).padding(.vertical, 3).background(Color.black.opacity(0.6)).clipShape(Capsule()).padding(6)
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        }.buttonStyle(.plain).accessibilityLabel("打开图片 \(f.s("name"))")
    }
    @ViewBuilder func fileRow(_ f: JSON) -> some View {
        Button { openFile(f.s("path"), f.s("name")) } label: {
            HStack(spacing: 10) {
                Image(systemName: f.s("kind") == "video" ? "film" : "doc").foregroundColor(amber)
                    .frame(width: 32, height: 32).background(amber.opacity(0.12)).clipShape(RoundedRectangle(cornerRadius: 8))
                Text(f.s("name")).font(.callout.weight(.medium)).lineLimit(1).foregroundColor(.primary)
                Spacer(minLength: 0)
                Image(systemName: "chevron.right").font(.caption).foregroundColor(.secondary)
            }.padding(8).background(Color(.secondarySystemBackground)).clipShape(RoundedRectangle(cornerRadius: 12))
        }.buttonStyle(.plain)
    }
}
