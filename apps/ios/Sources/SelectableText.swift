import SwiftUI
import UIKit

// iOS 3.8: chat text you can select part of and copy. SwiftUI's .textSelection(.enabled) on iOS only offers 复制 for
// the WHOLE Text (no handles, no partial selection), so the bubbles (user, 球球, task results, the task view) render
// their text blocks through a non-editable, selectable UITextView instead: long-press a word, drag the handles,
// 复制 / 全选 / 复制全文 in the system edit menu. Links stay tappable (they open as before). Sizing: the view's
// height is measured with the same TextKit 1 layout it draws with (no insets, no line fragment padding), at the width
// SwiftUI proposes, so the bubble hugs short text and wraps long (Chinese) text exactly; scrolling is off.

/// SwiftUI AttributedString (inline Markdown: bold, italic, code, links) -> NSAttributedString for UIKit.
func uiAttributed(_ a: AttributedString, style: UIFont.TextStyle, weight: UIFont.Weight = .regular, color: UIColor,
                  strong: UIColor? = nil, lineSpacing: CGFloat = 3) -> NSAttributedString {
    let base = UIFont.preferredFont(forTextStyle: style)
    let size = base.pointSize
    func font(bold: Bool, italic: Bool, code: Bool) -> UIFont {
        if code { return .monospacedSystemFont(ofSize: size * 0.9, weight: .regular) }
        var f = UIFont.systemFont(ofSize: size, weight: bold ? .semibold : weight)
        if italic, let d = f.fontDescriptor.withSymbolicTraits(.traitItalic) { f = UIFont(descriptor: d, size: size) }
        return f
    }
    let para = NSMutableParagraphStyle()
    para.lineSpacing = lineSpacing
    para.lineBreakMode = .byWordWrapping
    let out = NSMutableAttributedString()
    for run in a.runs {
        let s = String(a[run.range].characters)
        let intent = run.inlinePresentationIntent ?? []
        let bold = intent.contains(.stronglyEmphasized), code = intent.contains(.code)
        var attrs: [NSAttributedString.Key: Any] = [
            .font: font(bold: bold, italic: intent.contains(.emphasized), code: code),
            .foregroundColor: bold ? (strong ?? color) : color,
            .paragraphStyle: para,
        ]
        if code { attrs[.backgroundColor] = color.withAlphaComponent(0.10) }
        if intent.contains(.strikethrough) { attrs[.strikethroughStyle] = NSUnderlineStyle.single.rawValue }
        if let l = run.link { attrs[.link] = l }
        out.append(NSAttributedString(string: s, attributes: attrs))
    }
    return out
}

/// Measures a string the way SelectableText lays it out (TextKit 1, zero insets): the size it needs at `width`.
func selectableSize(_ s: NSAttributedString, width: CGFloat) -> CGSize {
    guard s.length > 0 else { return .zero }
    let w = max(1, width)
    let r = s.boundingRect(with: CGSize(width: w, height: .greatestFiniteMagnitude),
                           options: [.usesLineFragmentOrigin, .usesFontLeading], context: nil)
    return CGSize(width: min(w, ceil(r.width) + 1), height: ceil(r.height) + 1)
}

final class AiwTextView: UITextView, UITextViewDelegate {
    var whole: String = ""
    private var storage: NSTextStorage?
    /// An explicit TextKit 1 stack: the same layout boundingRect measures (iOS 16 would pick TextKit 2 by default).
    convenience init() {
        let ts = NSTextStorage(), lm = NSLayoutManager()
        ts.addLayoutManager(lm)
        let tc = NSTextContainer(size: CGSize(width: 0, height: CGFloat.greatestFiniteMagnitude))
        tc.widthTracksTextView = true
        lm.addTextContainer(tc)
        self.init(frame: .zero, textContainer: tc)
        storage = ts
    }
    override init(frame: CGRect, textContainer: NSTextContainer?) {
        super.init(frame: frame, textContainer: textContainer)
        isEditable = false
        isSelectable = true
        isScrollEnabled = false
        backgroundColor = .clear
        textContainerInset = .zero
        self.textContainer.lineFragmentPadding = 0
        self.textContainer.widthTracksTextView = true
        adjustsFontForContentSizeCategory = true
        dataDetectorTypes = []
        delegate = self
        setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        setContentHuggingPriority(.defaultHigh, for: .vertical)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    /// The system edit menu (long-press / selection) gets 复制全文 next to 复制 and 全选.
    @available(iOS 16.0, *)
    func textView(_ textView: UITextView, editMenuForTextIn range: NSRange, suggestedActions: [UIMenuElement]) -> UIMenu? {
        let all = whole.isEmpty ? (textView.text ?? "") : whole
        let copyAll = UIAction(title: "复制全文", image: UIImage(systemName: "doc.on.doc")) { _ in UIPasteboard.general.string = all }
        return UIMenu(children: suggestedActions + [copyAll])
    }
}

/// One block of selectable chat text. `whole` is the full message (复制全文); the block itself may be a part of it.
struct SelectableText: UIViewRepresentable {
    let text: NSAttributedString
    var whole: String = ""
    var link: UIColor? = nil

    func makeUIView(context: Context) -> AiwTextView {
        let v = AiwTextView()
        v.attributedText = text
        v.whole = whole
        if let l = link { v.linkTextAttributes = [.foregroundColor: l, .underlineStyle: NSUnderlineStyle.single.rawValue] }
        return v
    }
    func updateUIView(_ v: AiwTextView, context: Context) {
        if !(v.attributedText?.isEqual(to: text) ?? false) { v.attributedText = text }
        v.whole = whole
        if let l = link { v.linkTextAttributes = [.foregroundColor: l, .underlineStyle: NSUnderlineStyle.single.rawValue] }
    }
    @available(iOS 16.0, *)
    func sizeThatFits(_ proposal: ProposedViewSize, uiView: AiwTextView, context: Context) -> CGSize? {
        let w = proposal.width ?? (UIScreen.main.bounds.width - 120)
        guard w.isFinite, w > 0 else { return selectableSize(text, width: UIScreen.main.bounds.width - 120) }
        return selectableSize(text, width: w)
    }
}

/// 复制 the whole message on long-press of the bubble's own area (padding / background). The text itself keeps the
/// text view's long-press (word selection + edit menu with 复制全文), so the two never fight over one touch.
struct CopyMenu: ViewModifier {
    let text: String
    func body(content: Content) -> some View {
        content.contextMenu {
            Button { UIPasteboard.general.string = text } label: { Label("复制", systemImage: "doc.on.doc") }
        }
    }
}
extension View {
    func copyMenu(_ text: String) -> some View { modifier(CopyMenu(text: text)) }
}
