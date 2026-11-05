import SwiftUI
import UIKit

// v3.9.17 拼音输入被打断（"shengcheng" 变成 "s" + "hengcheng"）:
// SwiftUI's TextField publishes the uncommitted pinyin (marked text) to its binding on every key. The first letter
// made the composer non-empty, which swapped the mic for the send button, wrote the @AppStorage draft and re-rendered
// the whole chat; SwiftUI then wrote the text back into the field and iOS committed the marked "s" on its own.
// IMEField is a UITextView that only publishes COMMITTED text (markedTextRange == nil) and never writes into the
// view while a composition is in progress, so the IME keeps its candidate bar intact.
struct IMEField: UIViewRepresentable {
    let placeholder: String
    @Binding var text: String
    @Binding var focused: Bool
    var maxLines: Int = 6

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIView(context: Context) -> IMETextView {
        let v = IMETextView()
        v.delegate = context.coordinator
        v.font = UIFont.preferredFont(forTextStyle: .body)
        v.adjustsFontForContentSizeCategory = true
        v.backgroundColor = .clear
        v.textContainerInset = UIEdgeInsets(top: 8, left: 0, bottom: 8, right: 0)
        v.textContainer.lineFragmentPadding = 0
        v.isScrollEnabled = false
        v.text = text
        v.placeholder.text = placeholder
        v.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        v.setContentHuggingPriority(.defaultLow, for: .horizontal)
        return v
    }

    func updateUIView(_ v: IMETextView, context: Context) {
        context.coordinator.parent = self
        v.placeholder.text = placeholder
        // never touch the text while pinyin / any IME is composing: that is what broke the composition
        if v.markedTextRange == nil && v.text != text {
            v.text = text
            v.refreshPlaceholder()
        }
        if focused && !v.isFirstResponder && v.window != nil {
            DispatchQueue.main.async { _ = v.becomeFirstResponder() }
        } else if !focused && v.isFirstResponder {
            DispatchQueue.main.async { _ = v.resignFirstResponder() }
        }
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView v: IMETextView, context: Context) -> CGSize? {
        let w = max(40, proposal.width ?? UIScreen.main.bounds.width - 120)
        let fit = v.sizeThatFits(CGSize(width: w, height: .greatestFiniteMagnitude))
        let line = (v.font ?? UIFont.preferredFont(forTextStyle: .body)).lineHeight
        let cap = line * CGFloat(maxLines) + v.textContainerInset.top + v.textContainerInset.bottom
        let scroll = fit.height > cap
        if v.isScrollEnabled != scroll { DispatchQueue.main.async { v.isScrollEnabled = scroll } }
        return CGSize(width: w, height: min(fit.height, cap))
    }

    final class Coordinator: NSObject, UITextViewDelegate {
        var parent: IMEField
        init(_ p: IMEField) { parent = p }

        func textViewDidChange(_ tv: UITextView) {
            (tv as? IMETextView)?.refreshPlaceholder()
            tv.invalidateIntrinsicContentSize()
            guard tv.markedTextRange == nil else { return }   // still composing: publish nothing yet
            if parent.text != tv.text { parent.text = tv.text }
        }
        func textViewDidBeginEditing(_ tv: UITextView) {
            if !parent.focused { DispatchQueue.main.async { self.parent.focused = true } }
        }
        func textViewDidEndEditing(_ tv: UITextView) {
            if parent.text != tv.text { parent.text = tv.text }
            if parent.focused { DispatchQueue.main.async { self.parent.focused = false } }
        }
    }
}

final class IMETextView: UITextView {
    let placeholder = UILabel()

    override init(frame: CGRect, textContainer: NSTextContainer?) {
        super.init(frame: frame, textContainer: textContainer)
        placeholder.textColor = .placeholderText
        placeholder.font = UIFont.preferredFont(forTextStyle: .body)
        placeholder.adjustsFontForContentSizeCategory = true
        placeholder.numberOfLines = 1
        placeholder.translatesAutoresizingMaskIntoConstraints = false
        addSubview(placeholder)
        NSLayoutConstraint.activate([
            placeholder.leadingAnchor.constraint(equalTo: frameLayoutGuide.leadingAnchor),
            placeholder.topAnchor.constraint(equalTo: frameLayoutGuide.topAnchor, constant: 8),
            placeholder.trailingAnchor.constraint(lessThanOrEqualTo: frameLayoutGuide.trailingAnchor),
        ])
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    func refreshPlaceholder() { placeholder.isHidden = !(text ?? "").isEmpty || markedTextRange != nil }
    override var text: String! { didSet { refreshPlaceholder() } }
}

/// v3.9.21: IMEField with its own focus and a rounded border, for the answer / 补充 boxes that sit inside views
/// a running task keeps re-rendering. SwiftUI's TextField drops the pinyin being composed on such a re-render,
/// which is what cut the first letter off (the same bug v3.9.17 fixed in the composer).
struct IMEInput: View {
    let placeholder: String
    @Binding var text: String
    var maxLines: Int = 4
    var autofocus = false
    @State private var focused = false
    var body: some View {
        IMEField(placeholder: placeholder, text: $text, focused: $focused, maxLines: maxLines)
            .padding(.horizontal, 8)
            .background(RoundedRectangle(cornerRadius: 8).stroke(Color.secondary.opacity(0.35)))
            .onAppear { if autofocus { focused = true } }
    }
}
