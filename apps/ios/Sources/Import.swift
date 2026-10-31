import Foundation
import UniformTypeIdentifiers

/// Shared with the AiwShare extension (apps/ios/Share). Both targets carry this App Group in their entitlements.
let aiwAppGroup = "group.com.hackjown.aiworkstation"

/// Files and text handed to Aiw from other apps:
///  - 「用其他应用打开」/ Open In (WeChat, Files, Mail…): iOS copies the file to Documents/Inbox and calls onOpenURL
///    with a file URL (Info.plist: CFBundleDocumentTypes, LSSupportsOpeningDocumentsInPlace = NO).
///  - The share sheet (AiwShare extension): it drops a batch in <group>/Import/<id>/ and opens aiw://import?id=…;
///    batches are also picked up whenever the app becomes active, in case that open did not happen.
/// Everything is copied to tmp/AiwImport first; v3.9.2: the app goes back to the chat (RootView.toChatForImport) and
/// its composer (HomeChatView.takeImports) uploads each file through the same /api/uploads/raw path as "+" › 文件 and
/// shows it as an attachment chip. Nothing is sent and no task starts until the user sends a message.
final class ImportCenter: ObservableObject {
    static let shared = ImportCenter()
    @Published private(set) var tick = 0
    private(set) var files: [URL] = []
    private(set) var texts: [String] = []
    var hasPending: Bool { !files.isEmpty || !texts.isEmpty }

    private let fm = FileManager.default
    private var staging: URL { fm.temporaryDirectory.appendingPathComponent("AiwImport", isDirectory: true) }

    func handle(_ url: URL) {
        if url.isFileURL {
            if let c = copyIn(url) { files.append(c); bump() }
            return
        }
        guard url.scheme?.lowercased() == "aiw" else { return }
        // aiw://import?text=… is the extension's fallback when the App Group is not available under this signing
        let q = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let t = q.filter { $0.name == "text" }.compactMap { $0.value }.filter { !$0.isEmpty }
        if !t.isEmpty { texts += t; bump() }
        collectShared()
    }

    /// Batches the share extension left in the App Group container (only finished ones: they carry manifest.json).
    func collectShared() {
        guard let g = fm.containerURL(forSecurityApplicationGroupIdentifier: aiwAppGroup) else { return }
        let root = g.appendingPathComponent("Import", isDirectory: true)
        guard let batches = try? fm.contentsOfDirectory(at: root, includingPropertiesForKeys: nil) else { return }
        var got = false
        for b in batches.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            if b.lastPathComponent.hasPrefix(".") {   // a half-written batch; drop it once it is an hour old
                if let d = (try? fm.attributesOfItem(atPath: b.path))?[.modificationDate] as? Date, d.timeIntervalSinceNow < -3600 {
                    try? fm.removeItem(at: b)
                }
                continue
            }
            let man = b.appendingPathComponent("manifest.json")
            guard let data = try? Data(contentsOf: man),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
            for t in (obj["texts"] as? [String]) ?? [] where !t.isEmpty { texts.append(t); got = true }
            for name in (obj["files"] as? [String]) ?? [] {
                let src = b.appendingPathComponent("files", isDirectory: true).appendingPathComponent(name)
                let dir = staging.appendingPathComponent(UUID().uuidString, isDirectory: true)
                try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
                let dst = dir.appendingPathComponent(name)
                if (try? fm.moveItem(at: src, to: dst)) != nil || (try? fm.copyItem(at: src, to: dst)) != nil {
                    files.append(dst); got = true
                }
            }
            try? fm.removeItem(at: b)
        }
        if got { bump() }
    }

    /// Hand everything pending to the composer and forget it here.
    func take() -> (files: [URL], texts: [String]) {
        let r = (files, texts); files = []; texts = []
        return r
    }

    /// Remove a staged copy (its own UUID folder) once it is uploaded or refused.
    func discard(_ u: URL) {
        let dir = u.deletingLastPathComponent()
        if dir.deletingLastPathComponent().standardizedFileURL == staging.standardizedFileURL { try? fm.removeItem(at: dir) }
        else { try? fm.removeItem(at: u) }
    }

    private func bump() { DispatchQueue.main.async { self.tick += 1 } }

    private func copyIn(_ src: URL) -> URL? {
        let scoped = src.startAccessingSecurityScopedResource()
        defer { if scoped { src.stopAccessingSecurityScopedResource() } }
        var name = src.lastPathComponent
        if let dec = name.removingPercentEncoding, !dec.isEmpty { name = dec }   // keep Chinese names readable
        if name.isEmpty { name = "file-\(Int(Date().timeIntervalSince1970))" }
        let dir = staging.appendingPathComponent(UUID().uuidString, isDirectory: true)
        do {
            try fm.createDirectory(at: dir, withIntermediateDirectories: true)
            let dst = dir.appendingPathComponent(name)
            var err: NSError?
            var ok = false
            NSFileCoordinator().coordinate(readingItemAt: src, options: .withoutChanges, error: &err) { u in
                ok = (try? fm.copyItem(at: u, to: dst)) != nil
            }
            guard ok else { try? fm.removeItem(at: dir); return nil }
            // Open In copies land in Documents/Inbox; they are ours to delete once copied
            if src.path.contains("/Documents/Inbox/") { try? fm.removeItem(at: src) }
            return dst
        } catch { return nil }
    }
}
