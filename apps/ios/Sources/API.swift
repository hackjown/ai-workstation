import Foundation
import SwiftUI

typealias JSON = [String: Any]

extension Dictionary where Key == String, Value == Any {
    func s(_ k: String) -> String { (self[k] as? String) ?? (self[k].map { "\($0)" } ?? "") }
    func d(_ k: String) -> Double { (self[k] as? Double) ?? Double(self[k] as? Int ?? 0) }
    func o(_ k: String) -> JSON { (self[k] as? JSON) ?? [:] }
    func a(_ k: String) -> [JSON] { (self[k] as? [JSON]) ?? [] }
    func b(_ k: String) -> Bool { (self[k] as? Bool) ?? ((self[k] as? Int) ?? 0 != 0) }
}

let amber = Color(red: 0xC8 / 255, green: 0x76 / 255, blue: 0x18 / 255)

struct APIError: LocalizedError { let message: String; var errorDescription: String? { message } }

/// Server address + admin token, kept on the phone. Every call is plain JSON over HTTP(S).
final class API: ObservableObject {
    static let shared = API()
    @Published var server: String = UserDefaults.standard.string(forKey: "server") ?? ""
    @Published var token: String = UserDefaults.standard.string(forKey: "token") ?? ""
    @Published var machine: String = UserDefaults.standard.string(forKey: "machine") ?? "ai-linux-001"
    var configured: Bool { !server.isEmpty && !token.isEmpty }

    func save(server: String, token: String) {
        var s = server.trimmingCharacters(in: .whitespacesAndNewlines)
        if !s.isEmpty && !s.contains("://") { s = "http://" + s }
        while s.hasSuffix("/") { s.removeLast() }
        self.server = s; self.token = token.trimmingCharacters(in: .whitespacesAndNewlines)
        UserDefaults.standard.set(self.server, forKey: "server")
        UserDefaults.standard.set(self.token, forKey: "token")
    }
    func setMachine(_ m: String) { machine = m; UserDefaults.standard.set(m, forKey: "machine") }
    func signOut() { save(server: server, token: "") }

    func url(_ path: String, query: [URLQueryItem] = []) -> URL? {
        guard var c = URLComponents(string: server + path) else { return nil }
        if !query.isEmpty { let old = c.queryItems ?? []; c.queryItems = old + query }
        return c.url
    }
    func artifactURL(_ path: String, download: Bool = true) -> URL? {
        url("/api/artifacts", query: [URLQueryItem(name: "path", value: path), URLQueryItem(name: "download", value: download ? "true" : "false"),
                                       URLQueryItem(name: "token", value: token)])
    }
    func wsURL(_ path: String) -> URL? {
        let base = server.replacingOccurrences(of: "https://", with: "wss://").replacingOccurrences(of: "http://", with: "ws://")
        var c = URLComponents(string: base + path)
        c?.queryItems = [URLQueryItem(name: "token", value: token)]
        return c?.url
    }

    @discardableResult
    func call(_ method: String, _ path: String, _ body: Any? = nil, timeout: TimeInterval = 30) async throws -> Any {
        guard let u = url(path) else { throw APIError(message: "服务器地址不对") }
        var r = URLRequest(url: u, timeoutInterval: timeout)
        r.httpMethod = method
        r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body = body {
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
            r.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, resp) = try await URLSession.shared.data(for: r)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        let obj = (try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])) ?? [:]
        if code == 401 { throw APIError(message: "令牌不对或已失效") }
        if code >= 400 {
            let detail = (obj as? JSON)?.s("detail") ?? ""
            throw APIError(message: detail.isEmpty ? "请求失败（\(code)）" : detail)
        }
        return obj
    }
    /// POST a JSON body with upload progress (0…1) reported on the main thread; used for /api/uploads.
    func postWithProgress(_ path: String, _ body: JSON, timeout: TimeInterval = 900, progress: @escaping (Double) -> Void) async throws -> JSON {
        guard let u = url(path) else { throw APIError(message: "服务器地址不对") }
        var r = URLRequest(url: u, timeoutInterval: timeout)
        r.httpMethod = "POST"
        r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let payload = try JSONSerialization.data(withJSONObject: body)
        let (data, resp) = try await URLSession.shared.upload(for: r, from: payload, delegate: UploadProgress(progress))
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        let obj = (try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])) ?? [:]
        if code == 401 { throw APIError(message: "令牌不对或已失效") }
        if code == 413 { throw APIError(message: "文件太大（服务器上限 100MB）") }
        if code >= 400 {
            let detail = (obj as? JSON)?.s("detail") ?? ""
            throw APIError(message: detail.isEmpty ? "请求失败（\(code)）" : detail)
        }
        return (obj as? JSON) ?? [:]
    }
    /// v2 (3.5): a photo / video / file as the request body (POST /api/uploads/raw), streamed from disk with progress.
    func postFile(_ path: String, file: URL, contentType: String = "application/octet-stream", timeout: TimeInterval = 1800,
                  progress: @escaping (Double) -> Void) async throws -> JSON {
        guard let u = url(path) else { throw APIError(message: "服务器地址不对") }
        var r = URLRequest(url: u, timeoutInterval: timeout)
        r.httpMethod = "POST"
        r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        r.setValue(contentType, forHTTPHeaderField: "Content-Type")
        let (data, resp) = try await URLSession.shared.upload(for: r, fromFile: file, delegate: UploadProgress(progress))
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        let obj = (try? JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed])) ?? [:]
        if code == 401 { throw APIError(message: "令牌不对或已失效") }
        if code == 404 { throw APIError(message: "服务器版本太旧，不支持大文件上传：请先更新服务器") }
        if code >= 400 {
            let detail = (obj as? JSON)?.s("detail") ?? ""
            throw APIError(message: code == 413 ? "文件太大（\(detail)）" : detail.isEmpty ? "上传失败（\(code)）" : detail)
        }
        return (obj as? JSON) ?? [:]
    }
    func list(_ path: String) async throws -> [JSON] { (try await call("GET", path) as? [JSON]) ?? [] }
    func obj(_ method: String, _ path: String, _ body: Any? = nil) async throws -> JSON { (try await call(method, path, body) as? JSON) ?? [:] }
}

final class UploadProgress: NSObject, URLSessionTaskDelegate {
    let report: (Double) -> Void
    init(_ report: @escaping (Double) -> Void) { self.report = report }
    func urlSession(_ session: URLSession, task: URLSessionTask, didSendBodyData bytesSent: Int64,
                    totalBytesSent: Int64, totalBytesExpectedToSend: Int64) {
        guard totalBytesExpectedToSend > 0 else { return }
        let f = min(1, Double(totalBytesSent) / Double(totalBytesExpectedToSend))
        DispatchQueue.main.async { self.report(f) }
    }
}

/// A websocket that reconnects and hands each JSON message to `onMessage` on the main thread.
final class Socket {
    private var task: URLSessionWebSocketTask?
    private var alive = false
    private let url: URL
    var onMessage: (JSON) -> Void = { _ in }
    init(_ url: URL) { self.url = url }
    func start() { alive = true; connect() }
    func stop() { alive = false; task?.cancel(with: .goingAway, reason: nil); task = nil }
    func send(_ obj: JSON) {
        guard let d = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: d, encoding: .utf8) else { return }
        task?.send(.string(s)) { _ in }
    }
    private func connect() {
        guard alive else { return }
        task = URLSession.shared.webSocketTask(with: url)
        task?.resume()
        receive()
    }
    private func receive() {
        task?.receive { [weak self] res in
            guard let self = self else { return }
            switch res {
            case .success(let m):
                var data: Data?
                if case .string(let s) = m { data = s.data(using: .utf8) }
                if case .data(let d) = m { data = d }
                if let d = data, let o = try? JSONSerialization.jsonObject(with: d) as? JSON {
                    DispatchQueue.main.async { self.onMessage(o) }
                }
                self.receive()
            case .failure:
                DispatchQueue.main.asyncAfter(deadline: .now() + 3) { [weak self] in self?.connect() }
            }
        }
    }
}

func when(_ ts: Double) -> String {
    guard ts > 0 else { return "" }
    let d = Date(timeIntervalSince1970: ts)
    let f = DateFormatter(); f.locale = Locale(identifier: "zh_CN")
    f.dateFormat = Calendar.current.isDateInToday(d) ? "HH:mm" : "M/d HH:mm"
    return f.string(from: d)
}

let statusText: [String: String] = [
    "pending": "准备中", "running": "进行中", "paused": "已暂停", "takeover": "你在接管", "waiting_confirm": "等你确认",
    "waiting_input": "等你回答", "queued": "排队中", "interrupted": "已中断", "succeeded": "完成", "failed": "失败", "stopped": "已停止"]
func isLive(_ s: String) -> Bool { ["pending", "running", "paused", "takeover", "waiting_confirm", "waiting_input", "queued"].contains(s) }
