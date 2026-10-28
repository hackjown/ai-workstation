import SwiftUI

// Readable chat text (iOS 3.3). The agent's replies are Markdown-ish plain text: paragraphs, "- " lists, numbered
// steps, headings, code fences, long ~/workspace/... paths. SwiftUI's Text(.init(s)) only does inline Markdown, so a
// list showed its raw "- " marks and every path broke across lines. RichText renders the blocks natively with real
// spacing, bullets with a hanging indent, a bold lead-in for "名称：说明" items, and long file paths shortened to the
// file name in a code chip (the full path is on the file card and under 查看步骤).

/// One block of a reply.
enum RichBlock {
    case para(String)
    case heading(String, Int)
    case bullet(String, level: Int)
    case number(String, String)          // marker ("1."), text
    case quote(String)
    case code(String)
    case table([String])
}

func richBlocks(_ src: String) -> [RichBlock] {
    var out: [RichBlock] = []
    var para: [String] = []
    var code: [String]? = nil
    var table: [String] = []
    func flushPara() { if !para.isEmpty { out.append(.para(para.joined(separator: "\n"))); para = [] } }
    func flushTable() { if !table.isEmpty { out.append(.table(table)); table = [] } }
    for raw in src.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n") {
        let line = raw.trimmingCharacters(in: .whitespaces)
        if var c = code {
            if line.hasPrefix("```") { out.append(.code(c.joined(separator: "\n"))); code = nil } else { c.append(raw); code = c }
            continue
        }
        if line.hasPrefix("```") { flushPara(); flushTable(); code = []; continue }
        if line.hasPrefix("|") { flushPara(); table.append(line); continue } else { flushTable() }
        if line.isEmpty { flushPara(); continue }
        let indent = raw.prefix { $0 == " " || $0 == "\t" }.count
        if let h = line.firstIndex(where: { $0 != "#" }), line.hasPrefix("#"), line[h] == " " {
            flushPara(); out.append(.heading(String(line[h...]).trimmingCharacters(in: .whitespaces), line.distance(from: line.startIndex, to: h))); continue
        }
        if let f = line.first, "-*•·+".contains(f), line.count > 1, line[line.index(after: line.startIndex)] == " " {
            flushPara(); out.append(.bullet(String(line.dropFirst(2)), level: indent >= 2 ? 1 : 0)); continue
        }
        if let r = line.range(of: #"^(\d{1,2}|[一二三四五六七八九十]{1,2})[.、)）]\s*"#, options: .regularExpression), r.upperBound < line.endIndex {
            flushPara()
            let mark = line[r].trimmingCharacters(in: .whitespaces)
            out.append(.number(mark, String(line[r.upperBound...]))); continue
        }
        if line.hasPrefix(">") { flushPara(); out.append(.quote(String(line.dropFirst()).trimmingCharacters(in: .whitespaces))); continue }
        if line.allSatisfy({ "-_*=".contains($0) }) && line.count >= 3 { flushPara(); continue }   // --- rules
        para.append(line)
    }
    if let c = code { out.append(.code(c.joined(separator: "\n"))) }
    flushPara(); flushTable()
    return out
}

private let pathRx = try! NSRegularExpression(
    pattern: #"(?<![\w:/.`])(?:~?/)?(?:[\w.\-~\p{Han}]+/)+([^\s/`：:，,。；;、）)（(】\]]+\.[A-Za-z0-9]{1,6})(?![\w/])"#)

/// Long file paths -> `file.ext` (a code chip); URLs are left alone.
func shortPaths(_ s: String) -> String {
    let ns = s as NSString
    var out = s
    for m in pathRx.matches(in: s, range: NSRange(location: 0, length: ns.length)).reversed() {
        let whole = ns.substring(with: m.range), name = ns.substring(with: m.range(at: 1))
        if !whole.contains("/") { continue }
        if let r = Range(m.range, in: out) { out.replaceSubrange(r, with: "`\(name)`") }
    }
    return out
}

/// Inline Markdown (bold, italic, code, links) for one block, with paths shortened.
func inlineMD(_ s: String, leadBold: Bool = false) -> AttributedString {
    var t = shortPaths(s)
    // "瘦脸：在脸部…" -> "**瘦脸**：在脸部…" for list items whose short lead-in names the point
    if leadBold, !t.hasPrefix("**"), let r = t.range(of: "：") ?? t.range(of: ": "),
       (1...10).contains(t.distance(from: t.startIndex, to: r.lowerBound)), !t[..<r.lowerBound].contains("`") {
        t = "**" + String(t[..<r.lowerBound]) + "**" + String(t[r.lowerBound...])
    }
    let opt = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
    return (try? AttributedString(markdown: t, options: opt)) ?? AttributedString(t)
}

/// A whole reply, rendered block by block. 3.8: every text block is a SelectableText (partial selection + 复制 /
/// 复制全文 in the edit menu; links still open); tables stay SwiftUI text (whole-cell copy).
struct RichText: View {
    let text: String
    var font: Font = .callout
    /// v2 home: 球球's bubbles show bold / highlighted text in light amber (#ffc35c)
    var strong: Color? = nil
    var style: UIFont.TextStyle = .callout
    /// the text colour (UIKit draws it, so a .foregroundColor around RichText does not reach it); nil = the bubble text
    var color: Color? = nil
    @Environment(\.colorScheme) private var cs
    func md(_ s: String, leadBold: Bool = false) -> AttributedString {
        var a = inlineMD(s, leadBold: leadBold)
        guard let c = strong else { return a }
        for run in a.runs where run.inlinePresentationIntent?.contains(.stronglyEmphasized) == true {
            a[run.range].foregroundColor = c
        }
        return a
    }
    var ink: UIColor { UIColor(color ?? hBubbleText(cs)) }
    func sel(_ s: String, leadBold: Bool = false, style st: UIFont.TextStyle? = nil, weight: UIFont.Weight = .regular,
             tint: UIColor? = nil) -> some View {
        SelectableText(text: uiAttributed(inlineMD(s, leadBold: leadBold), style: st ?? style, weight: weight, color: tint ?? ink,
                                          strong: strong.map { UIColor($0) }),
                       whole: text, link: strong.map { UIColor($0) } ?? UIColor(amber))
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            ForEach(Array(richBlocks(text).enumerated()), id: \.offset) { _, b in block(b) }
        }
        .font(font)
        .lineSpacing(3)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder func block(_ b: RichBlock) -> some View {
        switch b {
        case .para(let s):
            sel(s)
        case .heading(let s, let lvl):
            sel(s, style: lvl <= 2 ? .headline : .subheadline, weight: .semibold).padding(.top, 2)
        case .bullet(let s, let lvl):
            HStack(alignment: .top, spacing: 7) {   // .top: a UIKit text view has no SwiftUI text baseline
                Text(lvl == 0 ? "•" : "◦").fontWeight(.heavy).foregroundColor(lvl == 0 ? amber : .secondary)
                sel(s, leadBold: true)
            }.padding(.leading, lvl == 0 ? 2 : 16)
        case .number(let n, let s):
            HStack(alignment: .top, spacing: 6) {
                Text(n).foregroundColor(amber).fontWeight(.semibold).monospacedDigit()
                sel(s, leadBold: true)
            }
        case .quote(let s):
            HStack(spacing: 8) {
                RoundedRectangle(cornerRadius: 1.5).fill(amber.opacity(0.6)).frame(width: 3)
                sel(s, tint: .secondaryLabel)
            }
        case .code(let s):
            SelectableText(text: NSAttributedString(string: s, attributes: [
                .font: UIFont.monospacedSystemFont(ofSize: UIFont.preferredFont(forTextStyle: .footnote).pointSize, weight: .regular),
                .foregroundColor: ink]), whole: text)
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color(.tertiarySystemBackground)).clipShape(RoundedRectangle(cornerRadius: 8))
        case .table(let rows):
            ScrollView(.horizontal, showsIndicators: false) {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(rows.filter { !$0.replacingOccurrences(of: "|", with: "").allSatisfy { "-: ".contains($0) } }.enumerated()), id: \.offset) { i, r in
                        let cells = r.trimmingCharacters(in: CharacterSet(charactersIn: "| ")).components(separatedBy: "|").map { $0.trimmingCharacters(in: .whitespaces) }
                        HStack(spacing: 14) {
                            ForEach(Array(cells.enumerated()), id: \.offset) { _, c in
                                Text(md(c)).font(i == 0 ? .footnote.weight(.semibold) : .footnote).frame(minWidth: 60, alignment: .leading)
                            }
                        }
                    }
                }.padding(10)
            }.background(Color(.tertiarySystemBackground)).clipShape(RoundedRectangle(cornerRadius: 8))
        }
    }
}

/// The user's message split into what they asked, the skills they picked and the files they attached.
struct PromptParts {
    var text: String
    var skills: [String]
    var files: [String]
    init(_ p: String) {
        skills = skillIds(p)
        var s = p
        if s.hasPrefix("【技能"), let r = s.range(of: "】") { s = String(s[r.upperBound...]) }
        files = []
        if let r = s.range(of: "附件（已放到电脑上）：") {
            files = s[r.upperBound...].split(whereSeparator: \.isNewline).map {
                $0.trimmingCharacters(in: CharacterSet(charactersIn: "-•* ")) }.filter { !$0.isEmpty }
                .map { ($0 as NSString).lastPathComponent }
            s = String(s[..<r.lowerBound])
        }
        text = s.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

/// The user's bubble in the task view (3.7): the same as the chat's (UserBubbleV2: amber fill, dark text, skill chips,
/// attachment lines), so the task view and the chat look alike.
struct UserBubble: View {
    let prompt: String
    var body: some View {
        let p = PromptParts(prompt)
        UserBubbleV2(text: prompt, attachments: p.files.map { ["name": $0] as JSON }, skillTitle: { $0 })
            .frame(maxWidth: .infinity, alignment: .trailing)
    }
}

func fileIcon(_ name: String) -> String {
    switch (name as NSString).pathExtension.lowercased() {
    case "jpg", "jpeg", "png", "heic", "webp", "gif": return "photo"
    case "mp4", "mov", "m4v": return "film"
    case "pdf": return "doc.richtext"
    case "xlsx", "xls", "csv": return "tablecells"
    case "md", "txt", "json": return "doc.text"
    default: return "doc"
    }
}
