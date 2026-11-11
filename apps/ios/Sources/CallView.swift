import SwiftUI
import AVFoundation
import UIKit
import CoreImage

// v3.9.31 视频通话 (omni realtime voice + video call, like 豆包) over /ws/omni-call (services/api/aiw_api/omni_call.py).
// AVAudioSession .playAndRecord + .voiceChat and voice processing on the AVAudioEngine input (the system's echo
// cancellation, so the model does not hear itself); the input tap is converted to 16 kHz mono pcm16 and sent in 40 ms
// chunks; the model's 24 kHz pcm16 plays through an AVAudioPlayerNode, cut at once on speech_started (barge-in) or a
// tap on the captions (打断). The camera (front/back, can be turned off) gives a JPEG (≤720 px long edge, quality 0.6)
// at most once a second. A transcript with speak=true (a text-only model and no 朗读模型 on the server) is read out
// with AVSpeechSynthesizer. The server saves the call's transcript to the 小秘 chat at hang-up.
// v3.9.36 (a call where the server got camera frames but no microphone audio): the mic tap no longer goes through an
// AVAudioConverter that silently failed / gave 0 frames - each buffer is taken in whatever format it comes (Float32,
// Int16 or Int32, interleaved or not, 1 or more channels: the first channel that has signal is used, like
// channelMap [0]) and resampled to 16 kHz by hand with a fractional position kept across buffers. If no mic buffer
// (or only digital silence) arrives for 3 s the note says 「麦克风没有声音，请检查权限/静音」 and the engine is
// restarted once without voice processing; the server's no_mic error does the same. AVAudioSession interruptions,
// route changes and AVAudioEngine configuration changes restart the engine. start says client "ios"; mute is told to
// the server so a muted mic is not reported as broken.
// v3.9.37 (every answer cut off mid-sentence on speaker: the model's own playback came back through the mic, Gemini's
// VAD took it for the user and interrupted): (1) the no-voice-processing fallback - which turns the system echo
// cancellation off - now only happens when the mic tap delivers NO callbacks at all for 3 s (or the server says no_mic
// and we really got no callbacks), never because the voice-processed signal is quiet (it is near-silent whenever
// nobody talks); which mode is active (vp on/off, why, any setVoiceProcessingEnabled error, input rate/channels,
// route) goes to the server as {"type":"client_audio"} so it shows in 通话诊断, and the screen says when AEC is off.
// Playback stays on the same engine (player -> mainMixerNode of the voice-processing engine, session .voiceChat), so
// the AEC has its reference. (2) An echo gate (EchoGate, same rule as call.js callEchoGate): while the answer plays
// and 300 ms after it drains, mic chunks go out as zeros unless the mic is clearly louder than the echo (RMS >
// max(0.06, 3 x the echo level during playback) for >= 200 ms = a real barge-in). The server's ready.echo_gate
// (setting 防回声) or UserDefaults aiw.call.echoGate = "off" turns it off. (3) Playback: buffers complete on
// .dataPlayedBack; an engine restart (route / config change) re-schedules the buffers not yet played instead of
// dropping the tail of the answer; a stop by 打断 / interrupted bumps a generation so late completions are ignored.

/// v3.9.37 echo gate on 40 ms chunks of 16 kHz pcm16 (see call.js callEchoGate for the same rule).
struct EchoGate {
    var on = true
    var floor: Float = 0.06, factor: Float = 3, cap: Float = 0.5
    var sustainMs = 200, warmMs = 300, releaseMs = 600, chunkMs = 40
    private(set) var echo: Float = 0
    private(set) var thr: Float = 0.06
    private(set) var gatedMs = 0
    private(set) var barges = 0
    private var open = false, loudMs = 0, quietMs = 0, playMs = 0
    private var held: [[Int16]] = []

    static func rms(_ c: [Int16]) -> Float {
        guard !c.isEmpty else { return 0 }
        var sq: Float = 0
        for v in c { let f = Float(v) / 32768; sq += f * f }
        return (sq / Float(c.count)).squareRoot()
    }

    /// The chunks to send now ([] = hold this one for a moment).
    mutating func feed(_ chunk: [Int16], playing: Bool) -> [[Int16]] {
        let r = EchoGate.rms(chunk)
        if !on || !playing {   // the answer is over: everything goes through, a held start of speech included
            let out = held + [chunk]
            held = []; open = false; loudMs = 0; quietMs = 0; playMs = 0
            return out
        }
        playMs += chunkMs
        thr = min(cap, max(floor, factor * echo))
        if open {   // a barge-in in progress
            if r > thr * 0.5 { quietMs = 0 } else { quietMs += chunkMs }
            if quietMs < releaseMs { return [chunk] }
            open = false; loudMs = 0; quietMs = 0
        }
        let warm = playMs <= warmMs
        if !warm && r > thr {
            held.append(chunk); loudMs += chunkMs
            if loudMs >= sustainMs {
                open = true; barges += 1; quietMs = 0
                let out = held
                held = []; loudMs = 0
                return out
            }
            return []
        }
        echo += (r - echo) * (warm ? 0.3 : 0.1)
        let out = (held + [chunk]).map { [Int16](repeating: 0, count: $0.count) }
        gatedMs += chunkMs * out.count
        held = []; loudMs = 0
        return out
    }
}

final class CallEngine: NSObject, ObservableObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    @Published var mode = ""          // "realtime" | "turn" | "gemini" (v3.9.32) once the server is ready
    @Published var model = ""
    @Published var note = ""
    @Published var userText = ""
    @Published var botText = "正在接通…"
    @Published var muted = false
    @Published var cameraOn = true
    @Published var front = true
    @Published var talking = false
    @Published var ended = false
    @Published var error = ""
    @Published var seconds = 0

    let capture = AVCaptureSession()
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let outFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 24000, channels: 1, interleaved: false)!
    private var rsRate: Double = 0              // v3.9.36 resampler state (audio thread only)
    private var rsPos: Double = 0
    private var rsTail: [Float] = []
    private let micLock = NSLock()              // guards the three stamps below (audio thread writes, main reads)
    private var lastBufAt: TimeInterval = 0     // last mic buffer of any kind
    private var lastVoiceAt: TimeInterval = 0   // last mic buffer that was not digital silence
    private var audioStartAt: TimeInterval = 0  // when the engine (re)started
    private var micChecker: Timer?
    private var micWarned = false
    private var vpOn = true
    private var vpFallbackTried = false
    private var vpErr = ""                       // v3.9.37: why setVoiceProcessingEnabled failed, if it did
    private var audioWhy = "start"               // why the engine last (re)started (main thread)
    private var inRate: Double = 0
    private var inChans = 0
    private var gate = EchoGate()                // v3.9.37 (audio thread, under micLock)
    private var playEndAt: TimeInterval = 0      // when the queued answer should have finished playing (micLock)
    private let echoHold: TimeInterval = 0.3     // the gate stays shut this long after the answer drains
    private let playLock = NSLock()              // guards queued / playGen (socket thread, main, player callbacks)
    private var queued: [(id: Int, buf: AVAudioPCMBuffer)] = []
    private var playGen = 0
    private var bufSeq = 0
    private var observers: [NSObjectProtocol] = []
    private var restartWork: DispatchWorkItem?
    private var task: URLSessionWebSocketTask?
    private let videoQueue = DispatchQueue(label: "aiw.call.video")
    private let ci = CIContext()
    private var lastFrame = Date.distantPast
    private var clock: Timer?
    private var pinger: Timer?
    private var pending = 0
    private var acc = Data()
    private let voice = AVSpeechSynthesizer()
    private var cameraReady = false
    private var isMuted = false       // read on the audio thread
    private var lastErr = ""          // v3.9.35: the server's last error, kept on screen when it ends the call

    // MARK: start / stop
    func start(video: Bool) {
        cameraOn = video
        AVAudioSession.sharedInstance().requestRecordPermission { ok in
            DispatchQueue.main.async {
                guard ok else { self.fail("没有麦克风权限：到 设置 › Aiw 打开「麦克风」"); return }
                if video {
                    AVCaptureDevice.requestAccess(for: .video) { granted in
                        DispatchQueue.main.async {
                            if !granted { self.cameraOn = false; self.note = "没有摄像头权限，先用语音（设置 › Aiw › 相机）" }
                            self.begin()
                        }
                    }
                } else {
                    self.begin()
                }
            }
        }
    }

    private func begin() {
        do {
            let s = AVAudioSession.sharedInstance()
            try s.setCategory(.playAndRecord, mode: .voiceChat, options: [.defaultToSpeaker, .allowBluetooth])
            try s.setActive(true)
            try startAudio(voiceProcessing: true)
        } catch {
            fail("麦克风打不开：\(error.localizedDescription)")
            return
        }
        observeAudio()
        if cameraOn { startCamera() }
        connect()
        clock = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.seconds += 1 }
        pinger = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in self?.send(["type": "ping"]) }
        micChecker = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.checkMic() }
        UIApplication.shared.isIdleTimerDisabled = true
    }

    /// v3.9.36: (re)builds the input tap and starts the engine. The input format is read after voice processing is
    /// switched (it can change the channel count and rate); the tap takes that format and onMic copes with any.
    private func startAudio(voiceProcessing: Bool, why: String = "start") throws {
        let input = engine.inputNode
        let carry = takeQueue()   // v3.9.37: the answer's buffers not yet played survive the restart
        if engine.isRunning { engine.stop() }
        input.removeTap(onBus: 0)
        vpErr = ""
        if input.isVoiceProcessingEnabled != voiceProcessing {
            do {   // system AEC / noise suppression on the engine (input AND output: the player is the reference)
                try input.setVoiceProcessingEnabled(voiceProcessing)
            } catch {
                vpErr = error.localizedDescription
            }
        }
        vpOn = input.isVoiceProcessingEnabled
        if vpOn { input.isVoiceProcessingBypassed = false }
        let inFmt = input.outputFormat(forBus: 0)
        guard inFmt.sampleRate > 0, inFmt.channelCount > 0 else {
            throw NSError(domain: "aiw.call", code: 1, userInfo: [NSLocalizedDescriptionKey: "没有可用的麦克风"])
        }
        input.installTap(onBus: 0, bufferSize: 1024, format: inFmt) { [weak self] buf, _ in self?.onMic(buf) }
        if player.engine == nil { engine.attach(player) }
        // the answer plays through the SAME engine as the voice-processed mic (no separate output path), so the
        // system echo canceller hears what it has to remove
        engine.connect(player, to: engine.mainMixerNode, format: outFormat)
        engine.prepare()
        try engine.start()
        player.play()
        micLock.lock()
        audioStartAt = ProcessInfo.processInfo.systemUptime
        playEndAt = 0
        micLock.unlock()
        audioWhy = why; inRate = inFmt.sampleRate; inChans = Int(inFmt.channelCount)
        DispatchQueue.main.async { [weak self] in self?.pending = 0 }   // before the carried buffers count again
        for b in carry { schedule(b) }
        DispatchQueue.main.async { [weak self] in
            guard let self = self else { return }
            if !self.vpOn {
                self.note = "回声消除没开（麦克风兼容模式），免提可能会自己打断自己，建议戴耳机"
            } else if self.note.hasPrefix("回声消除没开") {
                self.note = ""
            }
            if !self.mode.isEmpty { self.reportAudio("restart") }   // the first start is reported at ready
        }
    }

    /// v3.9.37: what the audio path is (voice processing on/off and why, the echo gate's numbers) -> the server's
    /// 通话诊断 timeline.
    private func reportAudio(_ event: String, why: String = "") {
        micLock.lock()
        let g = gate
        micLock.unlock()
        var o: JSON = ["type": "client_audio", "event": event, "vp": vpOn, "gate": g.on,
                       "echo": Double(g.echo), "thr": Double(g.thr), "gated_s": Double(g.gatedMs) / 1000, "barges": g.barges]
        o["why"] = why.isEmpty ? audioWhy : why
        if !vpErr.isEmpty { o["err"] = vpErr }
        if inRate > 0 { o["rate"] = inRate; o["chans"] = inChans }
        let outs = AVAudioSession.sharedInstance().currentRoute.outputs.map { $0.portType.rawValue }
        if !outs.isEmpty { o["route"] = outs.joined(separator: ",") }
        send(o)
    }

    /// Restart the engine a moment after an interruption / route change / config change (they come in bursts).
    private func scheduleRestart(_ why: String, voiceProcessing: Bool? = nil) {
        if voiceProcessing == nil {   // our own engine start can post a config / route change: no restart loop
            micLock.lock()
            let started = audioStartAt
            micLock.unlock()
            if ProcessInfo.processInfo.systemUptime - started < 1.0 { return }
        }
        restartWork?.cancel()
        let w = DispatchWorkItem { [weak self] in
            guard let self = self, !self.ended else { return }
            do {
                try? AVAudioSession.sharedInstance().setActive(true)
                try self.startAudio(voiceProcessing: voiceProcessing ?? self.vpOn, why: why)
            } catch {
                self.note = "麦克风重启失败（\(why)）：\(error.localizedDescription)"
            }
        }
        restartWork = w
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3, execute: w)
    }

    private func observeAudio() {
        let nc = NotificationCenter.default
        let session = AVAudioSession.sharedInstance()
        observers.append(nc.addObserver(forName: AVAudioSession.interruptionNotification, object: session, queue: .main) { [weak self] n in
            guard let self = self, !self.ended,
                  let raw = n.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
            if type == .began {
                self.note = "通话声音被系统打断了…"
            } else {
                self.note = ""
                self.scheduleRestart("interruption")
            }
        })
        observers.append(nc.addObserver(forName: AVAudioSession.routeChangeNotification, object: session, queue: .main) { [weak self] n in
            guard let self = self, !self.ended,
                  let raw = n.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt,
                  let reason = AVAudioSession.RouteChangeReason(rawValue: raw) else { return }
            switch reason {
            case .newDeviceAvailable, .oldDeviceUnavailable, .routeConfigurationChange:
                self.scheduleRestart("route")
            default:
                break   // .categoryChange / .override come from our own setup
            }
        })
        observers.append(nc.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main) { [weak self] _ in
            guard let self = self, !self.ended else { return }
            self.scheduleRestart("config")
        })
    }

    /// Every second: no mic buffer (or only digital silence) for 3 s since the engine started -> say so. v3.9.37: the
    /// restart without voice processing (= without echo cancellation) happens ONLY when the tap gave no callbacks at
    /// all for 3 s - a quiet or even all-zero voice-processed signal is normal when nobody talks, and turning AEC off
    /// for it is what made the phone interrupt itself.
    private func checkMic() {
        guard !ended, !isMuted else { return }
        let now = ProcessInfo.processInfo.systemUptime
        micLock.lock()
        let started = audioStartAt, buf = lastBufAt, voiced = lastVoiceAt
        micLock.unlock()
        guard started > 0, now - started >= 3 else { return }
        let noCallbacks = now - max(buf, started) >= 3
        let silent = noCallbacks || now - max(voiced, started) >= 3
        if silent {
            if !micWarned && (noCallbacks || now - max(voiced, started) >= 8) {   // all-zero: only note it after 8 s
                micWarned = true
                note = noCallbacks ? "麦克风没有声音，请检查权限/静音" : "麦克风只有静音，请检查权限/静音"
            }
            if noCallbacks && vpOn && !vpFallbackTried {
                vpFallbackTried = true
                scheduleRestart("no mic callbacks", voiceProcessing: false)
            }
        } else if micWarned {
            micWarned = false
            if note.hasPrefix("麦克风") { note = "" }
        }
    }

    func hangup() {
        guard !ended else { return }
        send(["type": "hangup"])
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { [weak self] in
            if let s = self, !s.ended { s.finish() }   // the server answers "ended" after saving the transcript
        }
    }

    func finish() {
        guard !ended else { return }
        ended = true
        stopPlayback()
        clock?.invalidate(); pinger?.invalidate(); micChecker?.invalidate()
        restartWork?.cancel()
        observers.forEach { NotificationCenter.default.removeObserver($0) }
        observers.removeAll()
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        videoQueue.async { if self.capture.isRunning { self.capture.stopRunning() } }
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        UIApplication.shared.isIdleTimerDisabled = false
    }

    private func fail(_ msg: String) {
        error = msg
        finish()
    }

    // MARK: socket
    private func connect() {
        guard let u = API.shared.wsURL("/ws/omni-call") else { fail("服务器地址不对"); return }
        let t = URLSession.shared.webSocketTask(with: u)
        t.maximumMessageSize = 4 * 1024 * 1024
        task = t
        t.resume()
        let ver = (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "?"
        let ua = "aiw-ios/\(ver) iOS \(UIDevice.current.systemVersion) \(UIDevice.current.model)"
        send(["type": "start", "video": cameraOn, "client": "ios", "ua": ua])
        if isMuted { send(["type": "mute", "on": true]) }
        receive(t)
    }

    private func send(_ obj: JSON) {
        guard let t = task, let d = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: d, encoding: .utf8) else { return }
        t.send(.string(s)) { _ in }
    }

    private func receive(_ t: URLSessionWebSocketTask) {
        t.receive { [weak self] r in
            guard let self = self else { return }
            switch r {
            case .success(let m):
                var data: Data?
                switch m {
                case .string(let s): data = s.data(using: .utf8)
                case .data(let d): data = d
                @unknown default: data = nil
                }
                if let d = data, let o = try? JSONSerialization.jsonObject(with: d) as? JSON {
                    if o.s("type") == "audio" { self.play(o.s("data")) }   // straight to the player, no hop to main
                    else { DispatchQueue.main.async { self.on(o) } }
                }
                self.receive(t)
            case .failure:
                DispatchQueue.main.async {
                    if !self.ended {
                        if self.mode.isEmpty && self.error.isEmpty { self.error = "通话没接通" }
                        self.finish()
                    }
                }
            }
        }
    }

    private func on(_ o: JSON) {
        switch o.s("type") {
        case "ready":
            mode = o.s("mode"); model = o.s("model"); note = o.s("note")
            // v3.9.37 防回声: the server's echo_gate (absent = on), and a local override
            let off = (o["echo_gate"] as? Bool) == false || UserDefaults.standard.string(forKey: "aiw.call.echoGate") == "off"
            micLock.lock()
            gate.on = !off
            micLock.unlock()
            reportAudio("start")   // vp on/off (and why), gate on/off, input format, route
            botText = note.isEmpty ? "已接通，说话吧" : note
            if !vpOn && note.isEmpty { note = "回声消除没开（麦克风兼容模式），免提可能会自己打断自己，建议戴耳机" }
        case "speech_started":
            stopPlayback()
        case "transcript":
            if o.s("role") == "user" {
                userText = o.s("text")
                if o.b("final") { botText = "" }
            } else {
                botText = o.s("text")
                if o.b("final") && o.b("speak") { say(o.s("text")) }
            }
        case "tool":
            let name = o.s("name") == "create_aiw_task" ? "交给 aiw" : "查任务进度"
            note = o.s("status") == "running" ? "正在\(name)…" : (o.s("text").isEmpty ? "\(name)完成" : o.s("text"))
        case "error":
            lastErr = o.s("message")
            if o.s("code") == "disabled" || mode.isEmpty { error = o.s("message") } else { note = o.s("message") }
            if o.s("code") == "no_mic" && !vpFallbackTried {   // v3.9.36: the server heard nothing from us
                // v3.9.37: only drop voice processing when our tap really gives nothing; otherwise it is the network
                micLock.lock()
                let noCallbacks = lastBufAt < audioStartAt || ProcessInfo.processInfo.systemUptime - lastBufAt >= 3
                micLock.unlock()
                if noCallbacks && vpOn {
                    vpFallbackTried = true
                    scheduleRestart("server no_mic", voiceProcessing: false)
                }
            }
        case "ended":
            // v3.9.35: a call the server ended (model gone, quota, no response…) stays on screen with the reason
            // instead of closing at once, so the user sees why; a normal hang-up still closes the view
            let r = o.s("reason")
            if error.isEmpty && !lastErr.isEmpty && r != "hangup" && r != "disconnect" { error = lastErr }
            finish()
        default:
            break
        }
    }

    // MARK: audio
    private func onMic(_ buf: AVAudioPCMBuffer) {
        let frames = Int(buf.frameLength)
        let chans = Int(buf.format.channelCount)
        let rate = buf.format.sampleRate
        guard frames > 0, chans > 0, rate > 0 else { return }
        let mono = monoSamples(buf, frames: frames, chans: chans)
        var peak: Float = 0
        for v in mono { let a = abs(v); if a > peak { peak = a } }
        let now = ProcessInfo.processInfo.systemUptime
        micLock.lock()
        lastBufAt = now
        if peak > 0.0001 { lastVoiceAt = now }
        micLock.unlock()
        if rate != rsRate { rsRate = rate; rsPos = 0; rsTail = [] }   // the route changed the rate: start over
        let ratio = rate / 16000
        let raw = rsTail + mono
        var pos = rsPos
        var out = [Int16]()
        out.reserveCapacity(Int(Double(raw.count) / ratio) + 1)
        while pos + ratio <= Double(raw.count) {   // average each 1/16000 s window (a box filter) to 16 kHz
            let a = Int(pos)
            let b = max(a + 1, Int(pos + ratio))
            var sum: Float = 0
            for j in a..<b { sum += raw[j] }
            var v = sum / Float(b - a)
            if v.isNaN { v = 0 }
            v = max(-1, min(1, v))
            out.append(Int16(v < 0 ? v * 32768 : v * 32767))
            pos += ratio
        }
        let used = min(Int(pos), raw.count)
        rsTail = Array(raw[used...])
        rsPos = pos - Double(used)
        out.withUnsafeBufferPointer { acc.append(Data(buffer: $0)) }
        while acc.count >= 1280 {   // 40 ms of 16 kHz pcm16
            let chunk = Data(acc.prefix(1280))
            acc.removeFirst(1280)
            if isMuted { continue }
            // v3.9.37 echo gate: while the answer plays (and 300 ms after), zeros unless clearly louder than the echo
            var samples = [Int16](repeating: 0, count: 640)
            chunk.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
                for i in 0..<640 { samples[i] = Int16(bitPattern: UInt16(raw[2 * i]) | (UInt16(raw[2 * i + 1]) << 8)) }
            }
            micLock.lock()
            let playing = ProcessInfo.processInfo.systemUptime < playEndAt + echoHold
            let outs = gate.feed(samples, playing: playing)
            micLock.unlock()
            for c in outs {
                let d = c.withUnsafeBufferPointer { Data(buffer: $0) }
                send(["type": "audio", "data": d.base64EncodedString()])
            }
        }
    }

    /// One channel of the tap buffer as Float in -1...1, whatever its sample format and layout: the first channel
    /// with signal (voice processing can hand over several channels with the voice in the first).
    private func monoSamples(_ buf: AVAudioPCMBuffer, frames: Int, chans: Int) -> [Float] {
        let inter = buf.format.isInterleaved
        let stride = inter ? chans : 1
        var best = [Float](repeating: 0, count: frames)
        for c in 0..<chans {
            var ch = [Float](repeating: 0, count: frames)
            if let f = buf.floatChannelData {
                let p = inter ? f[0] + c : f[c]
                for i in 0..<frames { ch[i] = p[i * stride] }
            } else if let q = buf.int16ChannelData {
                let p = inter ? q[0] + c : q[c]
                for i in 0..<frames { ch[i] = Float(p[i * stride]) / 32768 }
            } else if let q = buf.int32ChannelData {
                let p = inter ? q[0] + c : q[c]
                for i in 0..<frames { ch[i] = Float(p[i * stride]) / 2147483648 }
            } else {
                return best
            }
            if ch.contains(where: { $0 != 0 }) { return ch }
            if c == 0 { best = ch }
        }
        return best
    }

    private func play(_ b64: String) {
        guard let d = Data(base64Encoded: b64), d.count >= 2 else { return }
        let n = d.count / 2
        guard let buf = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: AVAudioFrameCount(n)),
              let dst = buf.floatChannelData?[0] else { return }
        buf.frameLength = AVAudioFrameCount(n)
        d.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            for i in 0..<n {
                let v = Int16(bitPattern: UInt16(raw[2 * i]) | (UInt16(raw[2 * i + 1]) << 8))
                dst[i] = Float(v) / 32768
            }
        }
        schedule(buf)
    }

    /// v3.9.37: queue one answer buffer on the player. Each buffer is remembered until it has really been played
    /// (.dataPlayedBack), so an engine restart can re-schedule the rest; playEndAt (for the echo gate) moves on by
    /// its length. A completion from before a stop (generation changed) is ignored.
    private func schedule(_ buf: AVAudioPCMBuffer) {
        let dur = Double(buf.frameLength) / outFormat.sampleRate
        playLock.lock()
        bufSeq += 1
        let id = bufSeq, gen = playGen
        queued.append((id: id, buf: buf))
        playLock.unlock()
        micLock.lock()
        playEndAt = max(ProcessInfo.processInfo.systemUptime, playEndAt) + dur
        micLock.unlock()
        DispatchQueue.main.async { self.pending += 1; self.talking = true }
        player.scheduleBuffer(buf, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            guard let s = self else { return }
            s.playLock.lock()
            let current = gen == s.playGen
            if current { s.queued.removeAll { $0.id == id } }
            let left = s.queued.count
            s.playLock.unlock()
            guard current else { return }
            DispatchQueue.main.async {
                s.pending = max(0, s.pending - 1)
                if left == 0 {
                    s.pending = 0
                    s.talking = false
                    s.micLock.lock()
                    s.playEndAt = min(s.playEndAt, ProcessInfo.processInfo.systemUptime)
                    s.micLock.unlock()
                    s.reportAudio("turn")
                }
            }
        }
        if engine.isRunning && !player.isPlaying { player.play() }
    }

    /// The buffers not yet played (and a new generation, so the stop that follows does not count them as played).
    private func takeQueue() -> [AVAudioPCMBuffer] {
        playLock.lock()
        let rest = queued.map { $0.buf }
        queued.removeAll()
        playGen += 1
        playLock.unlock()
        return rest
    }

    func stopPlayback() {
        _ = takeQueue()   // 打断 / interrupted: drop the rest of the answer
        player.stop()
        if engine.isRunning { player.play() }
        pending = 0
        talking = false
        micLock.lock()   // the room still rings for a moment: the echo gate holds echoHold more
        playEndAt = min(playEndAt, ProcessInfo.processInfo.systemUptime)
        micLock.unlock()
        if voice.isSpeaking { voice.stopSpeaking(at: .immediate) }
    }

    private func say(_ text: String) {
        let u = AVSpeechUtterance(string: text)
        u.voice = AVSpeechSynthesisVoice(language: "zh-CN")
        voice.speak(u)
    }

    func interrupt() {
        stopPlayback()
        send(["type": "interrupt"])
    }

    func toggleMute() {
        muted.toggle()
        isMuted = muted
        send(["type": "mute", "on": muted])
        if !muted {   // a fresh 3 s before the no-mic note
            micLock.lock()
            audioStartAt = ProcessInfo.processInfo.systemUptime
            micLock.unlock()
        }
    }

    // MARK: camera
    private func configureCamera() {
        capture.beginConfiguration()
        if capture.canSetSessionPreset(.hd1280x720) { capture.sessionPreset = .hd1280x720 }
        for i in capture.inputs { capture.removeInput(i) }
        let pos: AVCaptureDevice.Position = front ? .front : .back
        if let dev = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: pos),
           let inp = try? AVCaptureDeviceInput(device: dev), capture.canAddInput(inp) {
            capture.addInput(inp)
        }
        if capture.outputs.isEmpty {
            let out = AVCaptureVideoDataOutput()
            out.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
            out.alwaysDiscardsLateVideoFrames = true
            out.setSampleBufferDelegate(self, queue: videoQueue)
            if capture.canAddOutput(out) { capture.addOutput(out) }
        }
        if let c = capture.outputs.first?.connection(with: .video) {
            if c.isVideoOrientationSupported { c.videoOrientation = .portrait }
            if c.isVideoMirroringSupported { c.automaticallyAdjustsVideoMirroring = false; c.isVideoMirrored = front }
        }
        capture.commitConfiguration()
        cameraReady = true
    }

    private func startCamera() {
        videoQueue.async {
            self.configureCamera()
            if !self.capture.isRunning { self.capture.startRunning() }
        }
    }

    func toggleCamera() {
        cameraOn.toggle()
        if cameraOn { startCamera() } else { videoQueue.async { if self.capture.isRunning { self.capture.stopRunning() } } }
    }

    func flipCamera() {
        front.toggle()
        if cameraOn { videoQueue.async { self.configureCamera() } }
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        let now = Date()
        guard now.timeIntervalSince(lastFrame) >= 1.0, let px = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        lastFrame = now
        var img = CIImage(cvPixelBuffer: px)
        let e = img.extent
        let k = min(1, 720 / max(e.width, e.height))
        if k < 1 { img = img.transformed(by: CGAffineTransform(scaleX: k, y: k)) }
        guard let cg = ci.createCGImage(img, from: img.extent),
              let jpg = UIImage(cgImage: cg).jpegData(compressionQuality: 0.6), jpg.count < 190_000 else { return }
        send(["type": "frame", "data": jpg.base64EncodedString()])
    }
}

/// The camera picture behind the call (AVCaptureVideoPreviewLayer, aspect fill).
struct CallCameraPreview: UIViewRepresentable {
    let session: AVCaptureSession
    final class PreviewView: UIView {
        override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        var preview: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
    }
    func makeUIView(context: Context) -> PreviewView {
        let v = PreviewView()
        v.preview.session = session
        v.preview.videoGravity = .resizeAspectFill
        v.backgroundColor = .black
        return v
    }
    func updateUIView(_ uiView: PreviewView, context: Context) {}
}

struct CallView: View {
    var video: Bool = true
    @StateObject private var c = CallEngine()
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ZStack {
            Color(red: 0.06, green: 0.07, blue: 0.08).ignoresSafeArea()
            if c.cameraOn { CallCameraPreview(session: c.capture).ignoresSafeArea() }
            VStack(spacing: 14) {
                HStack(spacing: 10) {
                    Text(c.mode.isEmpty ? "连接中…" : "\(c.mode == "realtime" ? "实时" : c.mode == "gemini" ? "Gemini" : "轮流") · \(c.model)")
                        .font(.footnote).padding(.horizontal, 12).padding(.vertical, 4)
                        .background(Color.black.opacity(0.42)).clipShape(Capsule())
                    Text(String(format: "%02d:%02d", c.seconds / 60, c.seconds % 60))
                        .font(.footnote.monospacedDigit()).padding(.horizontal, 12).padding(.vertical, 4)
                        .background(Color.black.opacity(0.42)).clipShape(Capsule())
                }
                .padding(.top, 8)
                Spacer()
                if !c.cameraOn {
                    Image(systemName: "waveform.circle.fill")
                        .font(.system(size: 120))
                        .foregroundStyle(Color(red: 0.78, green: 0.46, blue: 0.09))
                        .scaleEffect(c.talking ? 1.08 : 1.0)
                        .animation(.easeInOut(duration: 0.6).repeatForever(autoreverses: true), value: c.talking)
                        .accessibilityLabel("球球")
                }
                Spacer()
                VStack(spacing: 6) {
                    if !c.error.isEmpty {
                        Text(c.error).font(.body).multilineTextAlignment(.center).foregroundColor(.orange)
                    }
                    if !c.note.isEmpty && c.error.isEmpty {
                        Text(c.note).font(.footnote).opacity(0.8).multilineTextAlignment(.center)
                    }
                    Text(c.userText).font(.subheadline).opacity(0.72).lineLimit(2)
                    Text(c.botText).font(.title3).lineLimit(4).multilineTextAlignment(.center)
                }
                .shadow(color: .black.opacity(0.7), radius: 3)
                .padding(.horizontal, 18)
                .contentShape(Rectangle())
                .onTapGesture { c.interrupt() }
                .accessibilityHint("点一下打断")
                HStack(spacing: 22) {
                    roundButton(c.muted ? "mic.slash.fill" : "mic.fill", on: c.muted, label: c.muted ? "取消静音" : "静音") { c.toggleMute() }
                    roundButton(c.cameraOn ? "video.fill" : "video.slash.fill", on: !c.cameraOn, label: c.cameraOn ? "关摄像头" : "开摄像头") { c.toggleCamera() }
                    if c.cameraOn {
                        roundButton("arrow.triangle.2.circlepath.camera", on: false, label: "切换前后摄像头") { c.flipCamera() }
                    }
                    Button {
                        if c.ended { dismiss() } else { c.hangup() }
                    } label: {
                        Image(systemName: c.ended ? "xmark" : "phone.down.fill").font(.system(size: 24))
                            .frame(width: 62, height: 62).background(Color.red).foregroundColor(.white).clipShape(Circle())
                    }
                    .accessibilityLabel(c.ended ? "关闭" : "挂断")
                }
                .padding(.bottom, 24)
            }
            .foregroundColor(.white)
        }
        .onAppear { c.start(video: video) }
        .onDisappear { c.hangup(); c.finish() }
        .onChange(of: c.ended) { e in if e && c.error.isEmpty { dismiss() } }
    }

    private func roundButton(_ sym: String, on: Bool, label: String, _ act: @escaping () -> Void) -> some View {
        Button(action: act) {
            Image(systemName: sym).font(.system(size: 22))
                .frame(width: 62, height: 62)
                .background(on ? Color.white : Color.white.opacity(0.16))
                .foregroundColor(on ? .black : .white)
                .clipShape(Circle())
        }
        .accessibilityLabel(label)
    }
}
