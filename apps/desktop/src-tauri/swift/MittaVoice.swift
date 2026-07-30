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

    /// RMS above which a buffer is treated as speech rather than room noise.
    ///
    /// Calibrated, not guessed: `mitta_voice_calibrate` measures the actual
    /// quiet level of this room and this microphone and moves the threshold to
    /// sit above it. The default is a conservative starting point — low enough
    /// that a soft voice opens the gate on an uncalibrated machine, which errs
    /// toward spending battery rather than toward missing the wake word.
    static var speechThreshold: Float = 0.012

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

    /// Both grants, reduced to one answer.
    ///
    /// Microphone and Speech Recognition are two separate TCC permissions, in
    /// two separate panes of System Settings, and speech is the one nobody
    /// thinks to look for. Reporting only the speech status — as this did —
    /// produces "permission has not been granted" for a user who has just
    /// granted microphone access and can see it ticked, which sends them back to
    /// the pane that was already correct.
    ///
    /// Denied beats not-determined: a denial is a decision and re-prompting will
    /// not clear it, so the caller must be told to open Settings rather than ask
    /// again.
    func authStatus() -> MittaAuth {
        let speech: MittaAuth
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: speech = .granted
        case .denied: speech = .denied
        case .restricted: speech = .restricted
        default: speech = .notDetermined
        }

        let mic: MittaAuth
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: mic = .granted
        case .denied: mic = .denied
        case .restricted: mic = .restricted
        default: mic = .notDetermined
        }

        if speech == .denied || mic == .denied { return .denied }
        if speech == .restricted || mic == .restricted { return .restricted }
        if speech == .granted && mic == .granted { return .granted }
        return .notDetermined
    }

    /// Which permission is actually missing, for the message the user reads.
    func missingPermission() -> String {
        let speechOK = SFSpeechRecognizer.authorizationStatus() == .authorized
        let micOK = AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
        switch (micOK, speechOK) {
        case (true, false):
            return "Speech Recognition access has not been granted — it is a separate "
                + "permission from the microphone, under Privacy & Security › Speech Recognition"
        case (false, true):
            return "Microphone access has not been granted — Privacy & Security › Microphone"
        default:
            return "microphone and Speech Recognition access have not been granted — they are "
                + "two separate permissions under Privacy & Security"
        }
    }

    /// Ask for both permissions, in sequence, then run `then`.
    ///
    /// Sequential rather than concurrent: these are two modal dialogs, and
    /// firing them together stacks one on top of the other so the first is
    /// answered blind.
    func requestAuth(then: (() -> Void)? = nil) {
        SFSpeechRecognizer.requestAuthorization { _ in
            AVCaptureDevice.requestAccess(for: .audio) { _ in
                then?()
            }
        }
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

    /// - Parameter gated: feed the recogniser only while there is speech-like
    ///   energy. For wake mode, which stays on for hours; never for
    ///   push-to-talk, where the user is holding a button and expects every
    ///   syllable captured.
    func start(gated: Bool) -> Int32 {
        stop()

        // Ask, rather than complain about not having been given.
        //
        // This refused with "access has not been granted" and never raised a
        // prompt, so the only way to grant it was to find two separate panes in
        // System Settings unaided — and a permission granted that way attaches
        // to whichever build was running at the time, which is not necessarily
        // this one. The dialog is the supported path and it was never shown.
        let status = authStatus()
        if status == .notDetermined {
            fail("asking for microphone and speech access — answer the prompt, then try again")
            requestAuth { [weak self] in
                guard let self, self.authStatus() == .granted else { return }
                // Start straight away once allowed, so saying yes is enough and
                // the user does not have to press the button a second time.
                _ = self.start(gated: gated)
            }
            return -5
        }

        guard status == .granted else {
            fail(missingPermission())
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

        // Energy gate.
        //
        // Wake mode holds the microphone open indefinitely, and appending every
        // buffer keeps the on-device recogniser working continuously — which is
        // the whole battery cost of leaving wake mode on, and therefore the
        // reason a user turns it off. Computing RMS is arithmetic on a buffer we
        // already have; recognition is not. So the tap always measures and only
        // feeds the recogniser once something is being said.
        //
        // The pre-roll is what makes this safe. By the time energy crosses the
        // threshold the first consonant is already in a buffer that has gone
        // past, so a naive gate eats the start of every phrase — and the word
        // being eaten is "mitta", which is the one word that has to be heard.
        // The last few buffers are therefore always retained and flushed ahead
        // of the first gated one.
        //
        // Hangover holds the gate open through the pauses inside a sentence, so
        // "mitta ... open spotify" is one utterance rather than two fragments.
        let preRoll = max(1, Int(0.30 * format.sampleRate / 1024))
        let hangover = max(1, Int(0.80 * format.sampleRate / 1024))
        var recent: [AVAudioPCMBuffer] = []
        var quietFrames = 0
        var open = false

        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            let energy = rms(of: buffer)
            self?.record(level: energy)

            guard gated else {
                buffered.append(buffer)
                return
            }

            if energy >= Self.speechThreshold {
                if !open {
                    // Flush the pre-roll, oldest first, then the buffer that
                    // tripped the gate.
                    for held in recent { buffered.append(held) }
                    recent.removeAll(keepingCapacity: true)
                    open = true
                }
                quietFrames = 0
                buffered.append(buffer)
                return
            }

            if open {
                // Still inside the hangover: silence between words is part of
                // the utterance, not the end of it.
                quietFrames += 1
                buffered.append(buffer)
                if quietFrames >= hangover {
                    open = false
                    quietFrames = 0
                }
                return
            }

            // Closed. Keep a rolling pre-roll and nothing else.
            recent.append(buffer)
            if recent.count > preRoll { recent.removeFirst() }
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
    /// Rishi (Indian English) is the default, by product-owner decision.
    ///
    /// Accent outranks quality here, and deliberately. Rishi ships only as a
    /// compact voice unless the user downloads the enhanced one, so a
    /// quality-first ordering would pick a premium American voice over the
    /// accent that was actually asked for. `canImprove` on `VoiceInfo` is how
    /// the upgrade gets offered instead — macOS mentions it nowhere the user
    /// would look.
    ///
    /// `Fred` is excluded by name. It is the 1980s formant synthesiser, it is
    /// male and English, and it would win a naive "any male voice" search.
    func preferredVoice() -> AVSpeechSynthesisVoice? {
        if let chosen = selectedVoiceID,
           let voice = AVSpeechSynthesisVoice(identifier: chosen) {
            return voice
        }

        let candidates = AVSpeechSynthesisVoice.speechVoices().filter {
            $0.language.hasPrefix("en") && !$0.name.hasPrefix("Fred")
        }

        func score(_ voice: AVSpeechSynthesisVoice) -> Int {
            var value = 0
            // Rishi by name, above every accent and quality consideration. It is
            // the requested voice; anything else is a fallback for a machine
            // that does not have it.
            if voice.name.hasPrefix("Rishi") { value += 10_000 }

            switch voice.language {
            case "en-IN": value += 800
            case "en-GB": value += 400
            case "en-AU", "en-IE": value += 200
            case "en-US": value += 100
            default: value += 50
            }
            if voice.gender == .male { value += 40 }
            switch voice.quality {
            case .premium: value += 60
            case .enhanced: value += 30
            default: break
            }
            return value
        }

        return candidates.max { score($0) < score($1) }
            // Ask for the accent even when no voice enumerated cleanly, before
            // falling back to whatever English exists.
            ?? AVSpeechSynthesisVoice(language: "en-IN")
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
public func mitta_voice_start(_ gated: Bool) -> Int32 {
    VoiceEngine.shared.start(gated: gated)
}

/// Measure the room and set the speech threshold above it.
///
/// Two seconds of ambient audio, thresholded a margin above the loudest quiet
/// frame observed. This is the part of "train MITTA to my voice" that Apple's
/// APIs actually permit: not a voiceprint, but a gate tuned to how loud this
/// person is in this room on this microphone, which is what decides whether the
/// wake word is heard at all.
@_cdecl("mitta_voice_calibrate")
public func mitta_voice_calibrate(_ observed: Float) -> Float {
    // A floor under the result: a perfectly silent measurement would otherwise
    // set a threshold that room noise later trips constantly.
    let calibrated = max(observed * 2.5, 0.006)
    VoiceEngine.speechThreshold = calibrated
    return calibrated
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


/// The two permissions separately, as `speech,microphone` status integers.
///
/// Lumping them into one answer is what made this hard to diagnose: the app
/// said "not granted" while System Settings showed both switches on, and there
/// was no way to tell which side disagreed or whether the grant belonged to a
/// different build of the binary.
@_cdecl("mitta_voice_copy_auth_detail")
public func mitta_voice_copy_auth_detail() -> UnsafeMutablePointer<CChar>? {
    func code(_ raw: Int) -> String { String(raw) }
    let speech: Int
    switch SFSpeechRecognizer.authorizationStatus() {
    case .authorized: speech = 2
    case .denied: speech = 1
    case .restricted: speech = 3
    default: speech = 0
    }
    let mic: Int
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: mic = 2
    case .denied: mic = 1
    case .restricted: mic = 3
    default: mic = 0
    }
    return strdup("\(code(speech)),\(code(mic))")
}
