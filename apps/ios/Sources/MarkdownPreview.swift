import SwiftUI
import WebKit
import UIKit

// In-app preview of text output files: Markdown rendered (headings, GFM tables that scroll sideways, fenced code with
// a copy button, lists), JSON pretty-printed monospace, other text as is. A WKWebView loads the bundled
// mdview/mdview.html (copied from apps/web by build-ipa.sh: the same markdown-it + DOMPurify + mdview.js/css the web
// app uses). Light/dark follow the app's appearance (prefers-color-scheme), the body size follows Dynamic Type.
// Everything else (PDF, images, Office) still opens in QuickLook.
// v3.9.2: Office files (Word / Excel / PowerPoint / Pages / Numbers / Keynote) are opened by QuickLook from the ORIGINAL
// downloaded file, which renders them natively with their real formatting; no PDF conversion on iOS.

/// "md", "json", "text" or "" (not ours, use QuickLook).
func textPreviewKind(_ url: URL) -> String {
    switch url.pathExtension.lowercased() {
    case "md", "markdown", "mdown": return "md"
    case "json", "jsonl": return "json"
    case "txt", "log", "csv", "tsv", "yaml", "yml", "ini", "toml", "py", "js", "sh", "xml": return "text"
    default: return ""
    }
}

/// The name the downloaded copy is saved under: QuickLook picks its renderer from the extension, so a card title
/// without one ("周报") borrows the extension of the file on the server ("…/周报.docx").
func previewFileName(_ name: String, path: String) -> String {
    let base = (name as NSString).lastPathComponent.isEmpty ? (path as NSString).lastPathComponent : (name as NSString).lastPathComponent
    let ext = (path as NSString).pathExtension
    if (base as NSString).pathExtension.isEmpty && !ext.isEmpty { return base + "." + ext }
    return base.isEmpty ? "file" : base
}

/// The bundled page, or nil when this build has no mdview folder (then QuickLook is used).
func textPreviewPage() -> URL? {
    guard let u = Bundle.main.url(forResource: "mdview", withExtension: "html", subdirectory: "mdview") else { return nil }
    return u
}

/// Sheet content for a downloaded file: rendered page + 关闭 / 复制全文 / share.
struct FilePreviewView: View {
    let url: URL
    let kind: String
    @Environment(\.dismiss) private var dismiss
    @State private var copied = false

    var text: String {
        guard let d = try? Data(contentsOf: url) else { return "" }
        return String(decoding: d, as: UTF8.self)
    }

    var body: some View {
        NavigationView {
            MarkdownWebView(fileURL: url, kind: kind)
                .ignoresSafeArea(edges: .bottom)
                .navigationTitle(url.lastPathComponent)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .navigationBarLeading) { Button("关闭") { dismiss() } }
                    ToolbarItemGroup(placement: .navigationBarTrailing) {
                        Button {
                            UIPasteboard.general.string = text
                            UINotificationFeedbackGenerator().notificationOccurred(.success)
                            copied = true
                            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { copied = false }
                        } label: { Image(systemName: copied ? "checkmark" : "doc.on.doc") }
                        .accessibilityLabel("复制全文")
                        ShareLink(item: url) { Image(systemName: "square.and.arrow.up") }
                            .accessibilityLabel("分享或导出")
                    }
                }
                .overlay(alignment: .bottom) {
                    if copied {
                        Text("已复制").font(.footnote.weight(.semibold)).padding(.horizontal, 14).padding(.vertical, 8)
                            .background(.ultraThinMaterial, in: Capsule()).padding(.bottom, 24).transition(.opacity)
                    }
                }
                .animation(.easeInOut(duration: 0.2), value: copied)
        }
        .navigationViewStyle(.stack)
    }
}

/// WKWebView showing one file through mdview.html.
struct MarkdownWebView: UIViewRepresentable {
    let fileURL: URL
    let kind: String

    func makeCoordinator() -> Coord { Coord(fileURL: fileURL, kind: kind) }

    func makeUIView(context: Context) -> WKWebView {
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(WeakHandler(context.coordinator), name: "aiwCopy")
        cfg.dataDetectorTypes = []
        let w = WKWebView(frame: .zero, configuration: cfg)
        w.isOpaque = false
        w.backgroundColor = .systemBackground
        w.scrollView.backgroundColor = .systemBackground
        w.navigationDelegate = context.coordinator
        w.uiDelegate = context.coordinator
        w.allowsLinkPreview = false
        context.coordinator.web = w
        if let page = textPreviewPage() {
            w.loadFileURL(page, allowingReadAccessTo: page.deletingLastPathComponent())
        }
        NotificationCenter.default.addObserver(context.coordinator, selector: #selector(Coord.fontChanged),
                                               name: UIContentSizeCategory.didChangeNotification, object: nil)
        return w
    }

    func updateUIView(_ w: WKWebView, context: Context) {}

    static func dismantleUIView(_ w: WKWebView, coordinator: Coord) {
        w.configuration.userContentController.removeScriptMessageHandler(forName: "aiwCopy")
        NotificationCenter.default.removeObserver(coordinator)
    }

    /// Dynamic Type body size, scaled so the default (17 pt) gives a 16 px page.
    static func bodyPx() -> Int { Int((UIFont.preferredFont(forTextStyle: .body).pointSize * 16 / 17).rounded()) }

    final class Coord: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
        let fileURL: URL
        let kind: String
        weak var web: WKWebView?
        init(fileURL: URL, kind: String) { self.fileURL = fileURL; self.kind = kind }

        func webView(_ w: WKWebView, didFinish navigation: WKNavigation!) {
            let data = (try? Data(contentsOf: fileURL)) ?? Data()
            // decode lossy, re-encode: the page always gets valid UTF-8
            let b64 = Data(String(decoding: data, as: UTF8.self).utf8).base64EncodedString()
            w.evaluateJavaScript("aiwShow('\(b64)','\(kind)',\(MarkdownWebView.bodyPx()))", completionHandler: nil)
        }
        @objc func fontChanged() {
            web?.evaluateJavaScript("aiwFont(\(MarkdownWebView.bodyPx()))", completionHandler: nil)
        }
        // links open in Safari, never inside the preview
        func webView(_ w: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if action.navigationType == .linkActivated, let u = action.request.url, u.scheme != "file" {
                UIApplication.shared.open(u); decisionHandler(.cancel); return
            }
            decisionHandler(.allow)
        }
        func webView(_ w: WKWebView, createWebViewWith c: WKWebViewConfiguration, for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let u = action.request.url, u.scheme == "http" || u.scheme == "https" { UIApplication.shared.open(u) }
            return nil
        }
        // copy buttons on code blocks
        func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
            guard m.name == "aiwCopy", let s = m.body as? String else { return }
            UIPasteboard.general.string = s
            UIImpactFeedbackGenerator(style: .light).impactOccurred()
        }
    }

    /// WKUserContentController keeps its handlers strongly; this breaks the cycle.
    final class WeakHandler: NSObject, WKScriptMessageHandler {
        weak var target: WKScriptMessageHandler?
        init(_ t: WKScriptMessageHandler) { target = t }
        func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) { target?.userContentController(c, didReceive: m) }
    }
}
