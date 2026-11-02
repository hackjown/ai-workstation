import SwiftUI
import WebKit
import UIKit
import CoreImage.CIFilterBuiltins

// 3.9.13: .html deliverables (装修设计器, 全景漫游) open inside the app in a WKWebView (WebGL, touch, gyro all work),
// with 分享链接: POST /api/shares makes a read-only /s/<code> link (designer in 只看模式) that anyone can open without
// signing in, shown with a QR code, 复制链接 and the system share sheet (微信 etc).

func isHtmlFile(_ url: URL) -> Bool { ["html", "htm"].contains(url.pathExtension.lowercased()) }

struct HtmlPreviewView: View {
    let url: URL
    let serverPath: String
    @Environment(\.dismiss) private var dismiss
    @State private var sharing = false

    var body: some View {
        NavigationView {
            HtmlWebView(fileURL: url)
                .ignoresSafeArea(edges: .bottom)
                .navigationTitle(url.deletingPathExtension().lastPathComponent)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .navigationBarLeading) { Button("关闭") { dismiss() } }
                    ToolbarItemGroup(placement: .navigationBarTrailing) {
                        if !serverPath.isEmpty {
                            Button { sharing = true } label: { Image(systemName: "qrcode") }
                                .accessibilityLabel("分享链接")
                        }
                        ShareLink(item: url) { Image(systemName: "square.and.arrow.up") }
                            .accessibilityLabel("发送文件")
                    }
                }
                .sheet(isPresented: $sharing) {
                    ShareLinkSheet(path: serverPath, title: url.deletingPathExtension().lastPathComponent)
                }
        }
        .navigationViewStyle(.stack)
    }
}

struct HtmlWebView: UIViewRepresentable {
    let fileURL: URL
    func makeUIView(context: Context) -> WKWebView {
        let cfg = WKWebViewConfiguration()
        cfg.allowsInlineMediaPlayback = true
        let w = WKWebView(frame: .zero, configuration: cfg)
        w.scrollView.bounces = false
        w.scrollView.contentInsetAdjustmentBehavior = .never
        w.isOpaque = false
        w.loadFileURL(fileURL, allowingReadAccessTo: fileURL.deletingLastPathComponent())
        return w
    }
    func updateUIView(_ w: WKWebView, context: Context) {}
    static func dismantleUIView(_ w: WKWebView, coordinator: ()) { w.stopLoading() }
}

struct ShareLinkSheet: View {
    let path: String
    let title: String
    @Environment(\.dismiss) private var dismiss
    @State private var viewOnly = true
    @State private var days = 30
    @State private var link = ""
    @State private var code = ""
    @State private var busy = false
    @State private var err = ""
    @State private var copied = false

    var body: some View {
        NavigationView {
            Form {
                Section {
                    Toggle("只看模式（不能改方案）", isOn: $viewOnly)
                    Picker("有效期", selection: $days) {
                        Text("7 天").tag(7); Text("30 天").tag(30); Text("1 年").tag(365); Text("永久").tag(0)
                    }
                    Button(link.isEmpty ? "生成链接" : "重新生成") { Task { await make() } }.disabled(busy)
                } footer: { Text("对方不用登录，微信里点链接或扫码就能看。链接经过你的 AIW 服务器，服务器要能从外网访问。") }
                if !err.isEmpty { Section { Text(err).foregroundColor(.red).font(.footnote) } }
                if !link.isEmpty, let u = URL(string: link) {
                    Section {
                        HStack { Spacer(); if let img = qr(link) { Image(uiImage: img).interpolation(.none).resizable().frame(width: 200, height: 200) }; Spacer() }
                        Text(link).font(.footnote).textSelection(.enabled)
                        Button(copied ? "已复制" : "复制链接") {
                            UIPasteboard.general.string = link; copied = true
                            UINotificationFeedbackGenerator().notificationOccurred(.success)
                        }
                        ShareLink(item: u, subject: Text(title), message: Text(title)) { Label("发给别人", systemImage: "square.and.arrow.up") }
                        Button("作废这个链接", role: .destructive) { Task { await revoke() } }
                    }
                }
            }
            .navigationTitle("分享").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .navigationBarTrailing) { Button("完成") { dismiss() } } }
        }
    }

    func make() async {
        busy = true; err = ""; copied = false
        defer { busy = false }
        do {
            let r = try await API.shared.call("POST", "/api/shares", ["path": path, "days": days, "view_only": viewOnly, "title": title]) as? JSON ?? [:]
            link = r.s("url"); code = r.s("code")
        } catch { err = "生成失败：\(error.localizedDescription)" }
    }
    func revoke() async {
        guard !code.isEmpty else { return }
        do { _ = try await API.shared.call("DELETE", "/api/shares/\(code)"); link = ""; code = "" }
        catch { err = "作废失败：\(error.localizedDescription)" }
    }
    func qr(_ s: String) -> UIImage? {
        let f = CIFilter.qrCodeGenerator()
        f.message = Data(s.utf8); f.correctionLevel = "M"
        guard let out = f.outputImage?.transformed(by: CGAffineTransform(scaleX: 8, y: 8)),
              let cg = CIContext().createCGImage(out, from: out.extent) else { return nil }
        return UIImage(cgImage: cg)
    }
}
