import SwiftUI
import Speech
import AVFoundation

// iOS 3.8 voice input: the round mic at the right of the input bar (like the reference app). Empty text -> the mic;
// text -> the send button. Tap the mic: SFSpeechRecognizer (zh-CN, on-device when the phone supports it) writes what
// you say live into the text field, after what was already typed; tap again (the stop square) to finish, then edit or
// send. Permissions are asked the first time; a refusal says where to turn it back on (设置 › Aiw) and never blocks
// typing. Needs NSMicrophoneUsageDescription + NSSpeechRecognitionUsageDescription (Info.plist).

final class SpeechInput: ObservableObject {
    @Published var recording = false
    @Published var starting = false
    @Published var error = ""
    /// 0…1, the input loudness (the button's ring pulses with it)
    @Published var level: CGFloat = 0
    private let engine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var recognizer: SFSpeechRecognizer?
    private var base = ""
    private var apply: ((String) -> Void)?
    private var finishT: DispatchWorkItem?
    private var gotText = false
    private var onDevice = false

    var active: Bool { recording || starting }

    /// Start, or stop when already listening. `current` is the text already in the field; `apply` gets the whole new text.
    func toggle(current: String, apply: @escaping (String) -> Void) {
        if active { stop() } else { start(current: current, apply: apply) }
    }

    func start(current: String, apply: @escaping (String) -> Void) {
        guard !active else { return }
        starting = true; error = ""
        self.apply = apply
        base = current
        SFSpeechRecognizer.requestAuthorization { st in
            DispatchQueue.main.async {
                guard st == .authorized else {
                    self.fail(st == .restricted ? "这台设备不允许语音识别" : "没有语音识别权限：到 设置 › Aiw 打开「语音识别」")
                    return
                }
                AVAudioSession.sharedInstance().requestRecordPermission { ok in
                    DispatchQueue.main.async {
                        guard ok else { self.fail("没有麦克风权限：到 设置 › Aiw 打开「麦克风」"); return }
                        self.begin(preferOnDevice: true)
                    }
                }
            }
        }
    }

    private func begin(preferOnDevice: Bool) {
        guard starting || recording else { return }   // stopped while the permission sheets were up
        let rec = SFSpeechRecognizer(locale: Locale(identifier: "zh-CN")) ?? SFSpeechRecognizer()
        guard let rec = rec, rec.isAvailable else { fail("语音识别现在用不了（离线识别没装或没网），可以先打字"); return }
        recognizer = rec
        let session = AVAudioSession.sharedInstance()
        do {
            try session.setCategory(.record, mode: .measurement, options: [.duckOthers])
            try session.setActive(true, options: .notifyOthersOnDeactivation)
        } catch { fail("麦克风打不开：\(error.localizedDescription)"); return }
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        req.addsPunctuation = true
        onDevice = preferOnDevice && rec.supportsOnDeviceRecognition
        req.requiresOnDeviceRecognition = onDevice
        request = req
        gotText = false
        let input = engine.inputNode
        let fmt = input.outputFormat(forBus: 0)
        guard fmt.sampleRate > 0, fmt.channelCount > 0 else { fail("没有可用的麦克风"); return }
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: fmt) { [weak self] buf, _ in
            req.append(buf)
            guard let ch = buf.floatChannelData?[0] else { return }
            let n = Int(buf.frameLength)
            guard n > 0 else { return }
            var sum: Float = 0
            for i in stride(from: 0, to: n, by: 4) { sum += ch[i] * ch[i] }
            let rms = sqrt(sum / Float(max(1, n / 4)))
            let lv = CGFloat(min(1, max(0, (20 * log10(max(rms, 1e-6)) + 55) / 45)))
            DispatchQueue.main.async { self?.level = lv }
        }
        engine.prepare()
        do { try engine.start() } catch { input.removeTap(onBus: 0); fail("录音没能开始：\(error.localizedDescription)"); return }
        starting = false; recording = true
        task = rec.recognitionTask(with: req) { [weak self] result, err in
            DispatchQueue.main.async { self?.handle(result, err) }
        }
    }

    private func handle(_ result: SFSpeechRecognitionResult?, _ err: Error?) {
        if let r = result {
            let said = r.bestTranscription.formattedString
            if !said.isEmpty { gotText = true; apply?(SpeechInput.join(base, said)) }
            if r.isFinal { cleanup(); return }
        }
        if let e = err {
            // on-device model missing / failed before any words: try once more with the server recognizer
            if onDevice && !gotText && recording {
                teardownAudio()
                task = nil; request = nil
                recording = false; starting = true
                begin(preferOnDevice: false)
                return
            }
            let ns = e as NSError
            let quiet = ns.domain == "kAFAssistantErrorDomain" && [203, 216, 301, 1110].contains(ns.code)   // no speech / cancelled
            if !gotText && !quiet { error = "没听清（\(e.localizedDescription)），再试一次或直接打字" }
            cleanup()
        }
    }

    /// Stop listening: the last words still come in as the final result (or after 1.5 s at the latest).
    func stop() {
        guard active else { return }
        if starting && !recording { starting = false; return }
        teardownAudio()
        request?.endAudio()
        recording = false
        let w = DispatchWorkItem { [weak self] in self?.task?.cancel(); self?.cleanup() }
        finishT = w
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5, execute: w)
    }

    private func teardownAudio() {
        if engine.isRunning { engine.stop() }
        engine.inputNode.removeTap(onBus: 0)
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        level = 0
    }

    private func cleanup() {
        finishT?.cancel(); finishT = nil
        if engine.isRunning || recording { teardownAudio() }
        task = nil; request = nil
        recording = false; starting = false; level = 0
    }

    private func fail(_ msg: String) {
        error = msg
        cleanup()
    }

    /// What was typed + what was said (a space only between two Latin words).
    static func join(_ base: String, _ said: String) -> String {
        guard !base.isEmpty else { return said }
        if let a = base.last, let b = said.first, a.isASCII && (a.isLetter || a.isNumber) && b.isASCII && (b.isLetter || b.isNumber) {
            return base + " " + said
        }
        return base + said
    }
}

/// The round button at the right of the input bar: mic (empty text), stop (listening), send (text) — the caller picks.
struct MicButton: View {
    @ObservedObject var voice: SpeechInput
    let tap: () -> Void
    var body: some View {
        Button(action: tap) {
            ZStack {
                if voice.recording {
                    Circle().fill(hAmber.opacity(0.18)).scaleEffect(1 + 0.35 * voice.level)
                        .animation(.easeOut(duration: 0.12), value: voice.level)
                    Circle().fill(hAmber)
                    RoundedRectangle(cornerRadius: 3).fill(hInk).frame(width: 12, height: 12)
                } else if voice.starting {
                    Circle().stroke(Color(.separator), lineWidth: 1)
                    ProgressView().controlSize(.small)
                } else {
                    Circle().stroke(Color(.separator), lineWidth: 1)
                    Image(systemName: "mic").font(.system(size: 17, weight: .regular)).foregroundColor(.primary)
                }
            }
            .frame(width: 36, height: 36)
        }
        .accessibilityLabel(voice.recording ? "停止语音输入" : "语音输入")
    }
}
