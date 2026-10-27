import UIKit
import UniformTypeIdentifiers

/// AiwShare: the "Aiw" entry in the system share sheet. It copies what was shared (files, photos, videos, links, text)
/// into the App Group container as one batch, then opens the app with aiw://import?id=<batch>. The app
/// (Sources/Import.swift) moves the batch into a new chat's composer and uploads the files.
/// Without the App Group (some signings don't grant it) links and text still go through the URL itself;
/// files then need 「用其他应用打开」, which needs no extension.
@objc(ShareViewController)
final class ShareViewController: UIViewController {
    private let group = "group.com.hackjown.aiworkstation"
    private let label = UILabel()
    private let spinner = UIActivityIndicatorView(style: .medium)
    private var started = false

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor.black.withAlphaComponent(0.2)
        let card = UIView()
        card.backgroundColor = .secondarySystemBackground
        card.layer.cornerRadius = 16
        card.translatesAutoresizingMaskIntoConstraints = false
        label.text = "正在交给 Aiw…"
        label.font = .preferredFont(forTextStyle: .subheadline)
        label.numberOfLines = 0
        label.textAlignment = .center
        spinner.startAnimating()
        let stack = UIStackView(arrangedSubviews: [spinner, label])
        stack.axis = .vertical; stack.spacing = 10; stack.alignment = .center
        stack.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(stack)
        view.addSubview(card)
        NSLayoutConstraint.activate([
            card.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            card.centerYAnchor.constraint(equalTo: view.centerYAnchor),
            card.widthAnchor.constraint(equalToConstant: 260),
            stack.topAnchor.constraint(equalTo: card.topAnchor, constant: 20),
            stack.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -20),
            stack.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 16),
            stack.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -16),
        ])
    }

    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        guard !started else { return }
        started = true
        Task { await run() }
    }

    // MARK: - collect

    private func run() async {
        let items = (extensionContext?.inputItems as? [NSExtensionItem]) ?? []
        let fm = FileManager.default
        guard let base = fm.containerURL(forSecurityApplicationGroupIdentifier: group) else {
            // No App Group under this signing: only links/text can travel (inside the URL).
            let texts = await collect(items, into: nil).texts
            if !texts.isEmpty, let u = importURL(text: String(texts.joined(separator: "\n").prefix(6000))) {
                open(u); return finish(after: 0.3)
            }
            show("这个安装方式不能从分享菜单传文件。\n请在微信里点 ··· ›「用其他应用打开」› Aiw。")
            return finish(after: 3.0)
        }
        let root = base.appendingPathComponent("Import", isDirectory: true)
        let id = "\(Int(Date().timeIntervalSince1970 * 1000))-\(UUID().uuidString.prefix(8))"
        let partial = root.appendingPathComponent(".\(id)", isDirectory: true)
        let filesDir = partial.appendingPathComponent("files", isDirectory: true)
        try? fm.createDirectory(at: filesDir, withIntermediateDirectories: true)
        let got = await collect(items, into: filesDir)
        if got.files.isEmpty && got.texts.isEmpty {
            try? fm.removeItem(at: partial)
            show("没有可以交给 Aiw 的内容")
            return finish(after: 1.5)
        }
        let man: [String: Any] = ["texts": got.texts, "files": got.files, "created": Date().timeIntervalSince1970]
        if let d = try? JSONSerialization.data(withJSONObject: man) {
            try? d.write(to: partial.appendingPathComponent("manifest.json"), options: .atomic)
        }
        try? fm.moveItem(at: partial, to: root.appendingPathComponent(id, isDirectory: true))   // now visible to the app
        let n = got.files.count
        show(n > 0 ? "已交给 Aiw：\(n) 个文件" : "已交给 Aiw")
        if let u = URL(string: "aiw://import?id=\(id)") { open(u) }
        finish(after: 0.6)
    }

    private struct Got { var files: [String] = []; var texts: [String] = [] }

    private func collect(_ items: [NSExtensionItem], into dir: URL?) async -> Got {
        var got = Got()
        for item in items {
            let atts = item.attachments ?? []
            for p in atts {
                if let dir = dir, let f = await copyFile(p, into: dir, taken: got.files) { got.files.append(f); continue }
                if let t = await text(p) { got.texts.append(t) }
            }
            if atts.isEmpty, let t = item.attributedContentText?.string.trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty {
                got.texts.append(t)
            }
        }
        return got
    }

    /// A link (not a file) or plain text from a provider.
    private func text(_ p: NSItemProvider) async -> String? {
        if p.hasItemConformingToTypeIdentifier(UTType.url.identifier), !p.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
            if let u = await load(p, UTType.url.identifier) as? URL { return u.absoluteString }
        }
        if p.hasItemConformingToTypeIdentifier(UTType.plainText.identifier) {
            let v = await load(p, UTType.plainText.identifier)
            if let s = v as? String { return s }
            if let d = v as? Data { return String(data: d, encoding: .utf8) }
            if let s = v as? NSAttributedString { return s.string }
        }
        return nil
    }

    /// Copy a file-like attachment into the batch. Returns its file name, or nil for links / plain text.
    private func copyFile(_ p: NSItemProvider, into dir: URL, taken: [String]) async -> String? {
        let types = p.registeredTypeIdentifiers
        let isFileURL = p.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier)
        // Pick the representation to save: a real file URL first, then the richest data type that is not a plain link/text.
        var pick: String?
        if !isFileURL {
            pick = types.first { t in
                guard let u = UTType(t) else { return false }
                if u.conforms(to: .url) { return false }
                if u.conforms(to: .plainText) && types.count == 1 { return false }   // bare text stays text
                return u.conforms(to: .data) || u.conforms(to: .content) || u.conforms(to: .item)
            }
            if pick == nil { return nil }
        }
        let fm = FileManager.default
        func place(_ src: URL, typeID: String?) -> String? {
            var name = p.suggestedName ?? ""
            if name.isEmpty { name = src.lastPathComponent }
            if let dec = name.removingPercentEncoding { name = dec }
            name = name.replacingOccurrences(of: "/", with: "_")
            if name.isEmpty { name = "shared" }
            if (name as NSString).pathExtension.isEmpty {
                let ext = src.pathExtension.isEmpty ? (typeID.flatMap { UTType($0)?.preferredFilenameExtension } ?? "") : src.pathExtension
                if !ext.isEmpty { name += "." + ext }
            }
            var final = name; var i = 2
            while taken.contains(final) || fm.fileExists(atPath: dir.appendingPathComponent(final).path) {
                let stem = (name as NSString).deletingPathExtension, ext = (name as NSString).pathExtension
                final = ext.isEmpty ? "\(stem)-\(i)" : "\(stem)-\(i).\(ext)"; i += 1
            }
            let dst = dir.appendingPathComponent(final)
            let scoped = src.startAccessingSecurityScopedResource()
            defer { if scoped { src.stopAccessingSecurityScopedResource() } }
            return (try? fm.copyItem(at: src, to: dst)) != nil ? final : nil
        }
        if isFileURL, let u = await load(p, UTType.fileURL.identifier) as? URL, u.isFileURL {
            if let n = place(u, typeID: nil) { return n }
        }
        guard let t = pick ?? types.first else { return nil }
        return await withCheckedContinuation { (c: CheckedContinuation<String?, Never>) in
            _ = p.loadFileRepresentation(forTypeIdentifier: t) { url, _ in
                // the system deletes `url` when this handler returns, so copy inside it
                c.resume(returning: url.flatMap { place($0, typeID: t) })
            }
        }
    }

    private func load(_ p: NSItemProvider, _ type: String) async -> NSSecureCoding? {
        await withCheckedContinuation { (c: CheckedContinuation<NSSecureCoding?, Never>) in
            p.loadItem(forTypeIdentifier: type, options: nil) { v, _ in c.resume(returning: v) }
        }
    }

    // MARK: - hand over

    private func importURL(text: String) -> URL? {
        var c = URLComponents(); c.scheme = "aiw"; c.host = "import"
        c.queryItems = [URLQueryItem(name: "text", value: text)]
        return c.url
    }

    /// Extensions may not call UIApplication.shared; walk the responder chain to the host's UIApplication instead
    /// and call -openURL:options:completionHandler: through its IMP (the old -openURL: stopped working on iOS 18).
    @discardableResult
    private func open(_ url: URL) -> Bool {
        let sel = NSSelectorFromString("openURL:options:completionHandler:")
        var r: UIResponder? = self
        while let x = r {
            if let app = x as? UIApplication, app.responds(to: sel), let imp = app.method(for: sel) {
                typealias Fn = @convention(c) (AnyObject, Selector, NSURL, NSDictionary, AnyObject?) -> Void
                unsafeBitCast(imp, to: Fn.self)(app, sel, url as NSURL, NSDictionary(), nil)
                return true
            }
            r = x.next
        }
        return false
    }

    private func show(_ s: String) {
        DispatchQueue.main.async { self.spinner.stopAnimating(); self.spinner.isHidden = true; self.label.text = s }
    }

    private func finish(after s: Double) {
        DispatchQueue.main.asyncAfter(deadline: .now() + s) {
            self.extensionContext?.completeRequest(returningItems: [], completionHandler: nil)
        }
    }
}
