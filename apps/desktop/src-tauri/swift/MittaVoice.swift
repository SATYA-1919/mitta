// MITTA — the speech layer.
//
// R7 requires Apple-native, on-device speech: `Speech` for recognition,
// `AVSpeechSynthesizer` for output. Both are Swift/ObjC-only, which is why this
// lives native-side rather than in the Python sidecar (DEC-019).
//
// **The C surface is polled, not callback-driven.** Recognition results arrive
// on Apple's queues, speech synthesis on another, and the audio tap on the
// render thread. Handing Rust three sets of function pointers from three
// threads means getting Send/Sync right across an FFI boundary for callbacks
// that can fire during teardown. Instead every callback writes to state behind
// one lock here, and Rust reads it from its own loop — the same shape the
// shell already uses for metrics at 1 Hz (DEC-003).
//
// Nothing in this file reaches the network. `requiresOnDeviceRecognition` is
// set and checked; if the device cannot do it locally, recognition is refused
// rather than quietly sent to Apple's servers, because R5 is the reason the
// wake word is allowed to exist at all (DEC-105).

import AVFoundation
import Foundation
import Speech

// MARK: - Status codes shared with Rust

@objc public enum MittaAuth: Int {
    case notDetermined = 0
    case denied = 1
    case granted = 2
    case restricted = 3
}

@objc public enum MittaState: Int {
    case idle = 0
    case listening = 1
    case failed = 2
}

// MARK: - The engine

/// All mutable state lives behind `lock`. Every entry point below is either
/// called from Rust's poll thread or from an Apple callback thread, and those
/// overlap freely.
private final class VoiceEngine {
    static let shared = VoiceEngine()

    private let lock = NSLock()
    private let synthesizer = AVSpeechSynthesizer()

    private var engine: AVAudioEngine?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var recognizer: SFSpeechRecognizer?

    private var state: MittaState = .idle
    private var transcript: String = ""
    private var transcriptSeq: UInt64 = 0
    private var isFinal: Bool = false
    private var level: Float = 0
    private var lastError: String = ""
    private var selectedVoiceID: String?

    // MARK: reads

    func snapshotState() -> MittaState { lock.withLock { state } }
    func snapshotLevel() -> Float { lock.withLock { level } }
    func snapshotFinal() -> Bool { lock.withLock { isFinal } }
    func snapshotSeq() -> UInt64 { lock.withLock { transcriptSeq } }
    func snapshotTranscript() -> String { lock.withLock { transcript } }
    func snapshotError() -> String { lock.withLock { lastError } }
    func speaking() -> Bool { synthesizer.isSpeaking }

    // MARK: authorisation

    func authStatus() -> MittaAuth {
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: return .granted
        case .denied: return .denied
        case .restricted: return .restricted
        default: return .notDetermined
        }
    }

    /// Fire-and-forget. The prompt is modal and the answer arrives later; Rust
    /// polls `authStatus()` rather than waiting on this.
    func requestAuth() {
        SFSpeechRecognizer.requestAuthorization { _ in }
        AVCaptureDevice.requestAccess(for: .audio) { _ in }
    }

    /// On-device recognition for the current locale, or nil with a reason set.
    private func makeRecognizer() -> SFSpeechRecognizer? {
        let candidate = SFSpeechRecognizer(locale: Locale.current) ?? SFSpeechRecognizer()
        guard let candidate else {
            fail("no speech recogniser for this locale")
            return nil
        }
        guard candidate.isAvailable else {
            fail("the speech recogniser is not available right now")
            return nil
        }
        // R5. A recogniser that cannot work locally would send microphone audio
        // to Apple, which is precisely what this application promises not to do.
        guard candidate.supportsOnDeviceRecognition else {
            fail("this Mac cannot recognise speech on-device; refusing to send audio off the machine")
            return nil
        }
        return candidate
    }

    // MARK: listening

    func start() -> Int32 {
        stop()

        guard authStatus() == .granted else {
            fail("microphone or speech recognition permission has not been granted")
            return -1
        }
        guard let recognizer = makeRecognizer() else { return -2 }

        let audio = AVAudioEngine()
        let buffered = SFSpeechAudioBufferRecognitionRequest()
        buffered.shouldReportPartialResults = true
        buffered.requiresOnDeviceRecognition = true

        let input = audio.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0 else {
            fail("no usable audio input device")
            return -3
        }

        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            buffered.append(buffer)
            self?.record(level: rms(of: buffer))
        }

        audio.prepare()
        do {
            try audio.start()
        } catch {
            input.removeTap(onBus: 0)
            fail("could not start the audio engine: \(error.localizedDescription)")
            return -4
        }

        let running = recognizer.recognitionTask(with: buffered) { [weak self] result, error in
            guard let self else { return }
            if let result {
                self.record(
                    transcript: result.bestTranscription.formattedString,
                    final: result.isFinal
                )
            }
            if let error {
                // A cancelled task on `stop()` reports an error too. Recording
                // it as a failure would light the UI red on an ordinary stop.
                if self.snapshotState() == .listening {
                    self.fail(error.localizedDescription)
                }
            }
        }

        lock.withLock {
            self.engine = audio
            self.request = buffered
            self.task = running
            self.recognizer = recognizer
            self.state = .listening
            self.transcript = ""
            self.isFinal = false
            self.lastError = ""
            self.transcriptSeq &+= 1
        }
        return 0
    }

    func stop() {
        let (audio, buffered, running) = lock.withLock {
            let triple = (self.engine, self.request, self.task)
            self.engine = nil
            self.request = nil
            self.task = nil
            if self.state == .listening { self.state = .idle }
            self.level = 0
            return triple
        }

        if let audio {
            audio.inputNode.removeTap(onBus: 0)
            audio.stop()
        }
        buffered?.endAudio()
        running?.cancel()
    }

    // MARK: speaking

    func speak(_ text: String, rate: Float) {
        guard !text.isEmpty else { return }
        // Barge-in: a new reply replaces the one being read rather than queuing
        // behind it. Two utterances overlapping is worse than losing the first.
        synthesizer.stopSpeaking(at: .immediate)

        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = preferredVoice()

        // A measured delivery rather than a neutral one.
        //
        // `AVSpeechSynthesizer` has no emotion control — there is no API for
        // "sound pleased". The three levers that exist are rate, pitch and
        // volume, and the one that actually changes how a line reads is rate:
        // the default clips along at a pace that sounds like a screen reader
        // working through a list. Slightly slower and slightly below neutral
        // pitch is what makes a short confirmation sound deliberate instead of
        // hurried.
        //
        // The real determinant is voice *quality*, not these numbers. A premium
        // voice at default settings beats a compact one tuned perfectly, which
        // is why `voiceQuality()` is exposed for the UI to nag about.
        utterance.rate = rate > 0 ? rate : AVSpeechUtteranceDefaultSpeechRate * 0.94
        utterance.pitchMultiplier = 0.96
        utterance.postUtteranceDelay = 0
        synthesizer.speak(utterance)
    }

    /// The most JARVIS-shaped male voice this Mac actually has.
    ///
    /// Ordered by accent first, then quality: an en-GB male at compact quality
    /// reads closer to what was asked for than a pristine American one. Within
    /// an accent, premium beats enhanced beats compact, and the difference is
    /// large — premium voices are the ones with any warmth to them at all.
    ///
    /// `Fred` is excluded by name. It is the 1980s formant synthesiser, it is
    /// male and English, and it would win a naive "any male voice" search.
    func preferredVoice() -> AVSpeechSynthesisVoice? {
        if let chosen = selectedVoiceID,
           let voice = AVSpeechSynthesisVoice(identifier: chosen) {
            return voice
        }

        let candidates = AVSpeechSynthesisVoice.speechVoices().filter {
            $0.gender == .male
                && $0.language.hasPrefix("en")
                && !$0.identifier.contains("speech.synthesis.voice")
        }

        func score(_ voice: AVSpeechSynthesisVoice) -> Int {
            var value = 0
            switch voice.language {
            case "en-GB": value += 400
            case "en-AU", "en-IE": value += 200
            case "en-US": value += 100
            default: value += 50
            }
            switch voice.quality {
            case .premium: value += 60
            case .enhanced: value += 30
            default: break
            }
            // Named preferences within en-GB, in the order they sound like the
            // thing that was asked for.
            for (index, name) in ["Oliver", "Daniel", "Arthur", "Jamie"].enumerated()
            where voice.name.hasPrefix(name) {
                value += 20 - index
            }
            return value
        }

        return candidates.max { score($0) < score($1) }
            ?? AVSpeechSynthesisVoice(language: "en-GB")
    }

    func setVoice(_ identifier: String?) {
        lock.withLock { self.selectedVoiceID = identifier }
    }

    func stopSpeaking() {
        synthesizer.stopSpeaking(at: .immediate)
    }

    // MARK: writes

    private func record(transcript text: String, final: Bool) {
        lock.withLock {
            guard text != self.transcript || final != self.isFinal else { return }
            self.transcript = text
            self.isFinal = final
            self.transcriptSeq &+= 1
        }
    }

    private func record(level value: Float) {
        lock.withLock { self.level = value }
    }

    private func fail(_ message: String) {
        lock.withLock {
            self.lastError = message
            self.state = .failed
        }
    }
}

/// Root-mean-square of a buffer, mapped to 0...1 for the waveform.
///
/// Scaled logarithmically because a linear RMS of ordinary speech sits near the
/// bottom of the range and produces a waveform that looks broken.
private func rms(of buffer: AVAudioPCMBuffer) -> Float {
    guard let channel = buffer.floatChannelData?[0] else { return 0 }
    let count = Int(buffer.frameLength)
    guard count > 0 else { return 0 }

    var sum: Float = 0
    for index in 0..<count {
        let sample = channel[index]
        sum += sample * sample
    }
    let mean = (sum / Float(count)).squareRoot()
    guard mean > 0 else { return 0 }

    let db = 20 * log10(mean)
    // -50 dB is near silence for a built-in microphone; 0 dB is clipping.
    return max(0, min(1, (db + 50) / 50))
}

private extension NSLock {
    func withLock<T>(_ body: () -> T) -> T {
        lock()
        defer { unlock() }
        return body()
    }
}

// MARK: - C ABI

@_cdecl("mitta_voice_auth_status")
public func mitta_voice_auth_status() -> Int32 {
    Int32(VoiceEngine.shared.authStatus().rawValue)
}

@_cdecl("mitta_voice_request_auth")
public func mitta_voice_request_auth() {
    VoiceEngine.shared.requestAuth()
}

@_cdecl("mitta_voice_start")
public func mitta_voice_start() -> Int32 {
    VoiceEngine.shared.start()
}

@_cdecl("mitta_voice_stop")
public func mitta_voice_stop() {
    VoiceEngine.shared.stop()
}

@_cdecl("mitta_voice_state")
public func mitta_voice_state() -> Int32 {
    Int32(VoiceEngine.shared.snapshotState().rawValue)
}

@_cdecl("mitta_voice_level")
public func mitta_voice_level() -> Float {
    VoiceEngine.shared.snapshotLevel()
}

/// Monotonic counter, bumped on every transcript change. Rust compares it
/// against the last value it saw so an unchanged transcript costs no string
/// copy and emits no event.
@_cdecl("mitta_voice_sequence")
public func mitta_voice_sequence() -> UInt64 {
    VoiceEngine.shared.snapshotSeq()
}

@_cdecl("mitta_voice_is_final")
public func mitta_voice_is_final() -> Bool {
    VoiceEngine.shared.snapshotFinal()
}

@_cdecl("mitta_voice_is_speaking")
public func mitta_voice_is_speaking() -> Bool {
    VoiceEngine.shared.speaking()
}

/// Caller owns the returned buffer and must pass it to `mitta_voice_free`.
@_cdecl("mitta_voice_copy_transcript")
public func mitta_voice_copy_transcript() -> UnsafeMutablePointer<CChar>? {
    strdup(VoiceEngine.shared.snapshotTranscript())
}

@_cdecl("mitta_voice_copy_error")
public func mitta_voice_copy_error() -> UnsafeMutablePointer<CChar>? {
    strdup(VoiceEngine.shared.snapshotError())
}

@_cdecl("mitta_voice_speak")
public func mitta_voice_speak(_ text: UnsafePointer<CChar>?, _ rate: Float) {
    guard let text else { return }
    VoiceEngine.shared.speak(String(cString: text), rate: rate)
}

@_cdecl("mitta_voice_stop_speaking")
public func mitta_voice_stop_speaking() {
    VoiceEngine.shared.stopSpeaking()
}

@_cdecl("mitta_voice_free")
public func mitta_voice_free(_ pointer: UnsafeMutablePointer<CChar>?) {
    free(pointer)
}


// MARK: - Voice selection C ABI

@_cdecl("mitta_voice_copy_voice_name")
public func mitta_voice_copy_voice_name() -> UnsafeMutablePointer<CChar>? {
    guard let voice = VoiceEngine.shared.preferredVoice() else { return strdup("") }
    return strdup("\(voice.name) (\(voice.language))")
}

/// 0 compact, 1 enhanced, 2 premium.
///
/// Surfaced so Settings can tell the user their assistant sounds like a 1990s
/// train announcement because the good voice is a free download they have not
/// made — which is not something they can be expected to guess.
@_cdecl("mitta_voice_voice_quality")
public func mitta_voice_voice_quality() -> Int32 {
    guard let voice = VoiceEngine.shared.preferredVoice() else { return 0 }
    switch voice.quality {
    case .premium: return 2
    case .enhanced: return 1
    default: return 0
    }
}

@_cdecl("mitta_voice_set_voice")
public func mitta_voice_set_voice(_ identifier: UnsafePointer<CChar>?) {
    guard let identifier else {
        VoiceEngine.shared.setVoice(nil)
        return
    }
    let value = String(cString: identifier)
    VoiceEngine.shared.setVoice(value.isEmpty ? nil : value)
}

/// Every male English voice installed, as `id\tname\tlanguage\tquality` lines.
@_cdecl("mitta_voice_copy_catalogue")
public func mitta_voice_copy_catalogue() -> UnsafeMutablePointer<CChar>? {
    let rows = AVSpeechSynthesisVoice.speechVoices()
        .filter { $0.language.hasPrefix("en") && $0.gender == .male }
        .map { voice -> String in
            let quality: String
            switch voice.quality {
            case .premium: quality = "premium"
            case .enhanced: quality = "enhanced"
            default: quality = "compact"
            }
            return "\(voice.identifier)\t\(voice.name)\t\(voice.language)\t\(quality)"
        }
    return strdup(rows.joined(separator: "\n"))
}
