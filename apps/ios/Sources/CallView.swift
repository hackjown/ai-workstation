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
    private let micFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
    private var converter: AVAudioConverter?
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
            let input = engine.inputNode
            try? input.setVoiceProcessingEnabled(true)   // system AEC / noise suppression on the engine
            let inFmt = input.outputFormat(forBus: 0)
            guard inFmt.sampleRate > 0, inFmt.channelCount > 0 else { fail("没有可用的麦克风"); return }
            converter = AVAudioConverter(from: inFmt, to: micFormat)
            input.removeTap(onBus: 0)
            input.installTap(onBus: 0, bufferSize: 1024, format: inFmt) { [weak self] buf, _ in self?.onMic(buf) }
            engine.attach(player)
            engine.connect(player, to: engine.mainMixerNode, format: outFormat)
            engine.prepare()
            try engine.start()
            player.play()
        } catch {
            fail("麦克风打不开：\(error.localizedDescription)")
            return
        }
        if cameraOn { startCamera() }
        connect()
        clock = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.seconds += 1 }
        pinger = Timer.scheduledTimer(withTimeInterval: 10, repeats: true) { [weak self] _ in self?.send(["type": "ping"]) }
        UIApplication.shared.isIdleTimerDisabled = true
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
        clock?.invalidate(); pinger?.invalidate()
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
        send(["type": "start", "video": cameraOn])
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
            botText = note.isEmpty ? "已接通，说话吧" : note
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
        guard let conv = converter else { return }
        let cap = AVAudioFrameCount(Double(buf.frameLength) * micFormat.sampleRate / buf.format.sampleRate + 64)
        guard let out = AVAudioPCMBuffer(pcmFormat: micFormat, frameCapacity: cap) else { return }
        var fed = false
        var err: NSError?
        conv.convert(to: out, error: &err) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true
            status.pointee = .haveData
            return buf
        }
        guard err == nil, out.frameLength > 0, let ch = out.int16ChannelData else { return }
        acc.append(Data(bytes: ch[0], count: Int(out.frameLength) * 2))
        while acc.count >= 1280 {   // 40 ms of 16 kHz pcm16
            let chunk = Data(acc.prefix(1280))
            acc.removeFirst(1280)
            if !isMuted { send(["type": "audio", "data": chunk.base64EncodedString()]) }
        }
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
        DispatchQueue.main.async { self.pending += 1; self.talking = true }
        player.scheduleBuffer(buf) { [weak self] in
            DispatchQueue.main.async {
                guard let s = self else { return }
                s.pending = max(0, s.pending - 1)
                if s.pending == 0 { s.talking = false }
            }
        }
        if !player.isPlaying { player.play() }
    }

    func stopPlayback() {
        player.stop()
        if engine.isRunning { player.play() }
        pending = 0
        talking = false
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
