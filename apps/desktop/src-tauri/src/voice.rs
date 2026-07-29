//! The voice layer — Rust's half of R7.
//!
//! The recognition and synthesis themselves live in `swift/MittaVoice.swift`,
//! because `Speech` and `AVSpeechSynthesizer` are Swift/ObjC-only (DEC-019).
//! This module owns the FFI boundary, the poll loop, and the activation policy
//! from DEC-105.
//!
//! **Why polling.** The Swift side keeps its state behind one lock and Rust
//! reads it on a timer, rather than Swift calling back into Rust from three
//! different Apple queues. Transcripts change a few times a second while
//! someone is speaking, so a 20 Hz poll misses nothing a person can perceive,
//! and it keeps every cross-language call on a thread we chose.
//!
//! **Wake word.** Continuous mode matches "mitta" in the transcript, because
//! Apple has no wake-word API and DEC-105 refuses a downloaded model. It is
//! opt-in, and the poll loop emits the microphone state on every change so the
//! indicator cannot silently disagree with the hardware.

use std::ffi::{c_char, c_float, CStr, CString};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter};

/// How often the Swift state is sampled. Fast enough that a partial transcript
/// feels live, slow enough to be free.
const POLL_INTERVAL: Duration = Duration::from_millis(50);

/// Apple's on-device recogniser ends a session after roughly a minute. In
/// continuous mode it is restarted before that, so the wake word does not
/// quietly stop working partway through an afternoon.
const SESSION_RECYCLE: Duration = Duration::from_secs(45);

extern "C" {
    fn mitta_voice_auth_status() -> i32;
    fn mitta_voice_request_auth();
    fn mitta_voice_start() -> i32;
    fn mitta_voice_stop();
    fn mitta_voice_state() -> i32;
    fn mitta_voice_level() -> c_float;
    fn mitta_voice_sequence() -> u64;
    fn mitta_voice_is_final() -> bool;
    fn mitta_voice_is_speaking() -> bool;
    fn mitta_voice_copy_transcript() -> *mut c_char;
    fn mitta_voice_copy_error() -> *mut c_char;
    fn mitta_voice_speak(text: *const c_char, rate: c_float);
    fn mitta_voice_stop_speaking();
    fn mitta_voice_free(pointer: *mut c_char);
    fn mitta_voice_copy_voice_name() -> *mut c_char;
    fn mitta_voice_voice_quality() -> i32;
    fn mitta_voice_copy_catalogue() -> *mut c_char;
    fn mitta_voice_set_voice(identifier: *const c_char);
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum Authorization {
    NotDetermined,
    Denied,
    Granted,
    Restricted,
}

impl From<i32> for Authorization {
    fn from(raw: i32) -> Self {
        match raw {
            1 => Self::Denied,
            2 => Self::Granted,
            3 => Self::Restricted,
            // An unrecognised value is treated as "not yet asked" rather than
            // as granted. Guessing upward on a permission is how an app ends up
            // reporting access it does not have.
            _ => Self::NotDetermined,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub enum ListenState {
    Idle,
    Listening,
    Failed,
}

impl From<i32> for ListenState {
    fn from(raw: i32) -> Self {
        match raw {
            1 => Self::Listening,
            2 => Self::Failed,
            _ => Self::Idle,
        }
    }
}

/// What the poll loop publishes to the webview on `voice:update`.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct VoiceUpdate {
    pub state: ListenState,
    pub transcript: String,
    pub is_final: bool,
    /// 0..1, for the waveform. Sampled from the audio tap, never synthesised —
    /// a waveform that moves when the microphone is closed is a lie about
    /// whether someone is being recorded.
    pub level: f32,
    pub speaking: bool,
    pub continuous: bool,
    pub error: Option<String>,
    /// True when this transcript arrived after the wake word in continuous
    /// mode, so the webview knows to act on it rather than only display it.
    pub triggered: bool,
}

#[derive(Default)]
pub struct VoiceState {
    /// Set while the user holds push-to-talk or continuous mode is on.
    listening: AtomicBool,
    continuous: AtomicBool,
    last_sequence: AtomicU64,
}

impl VoiceState {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn is_listening(&self) -> bool {
        self.listening.load(Ordering::SeqCst)
    }

    pub fn is_continuous(&self) -> bool {
        self.continuous.load(Ordering::SeqCst)
    }
}

/// The wake word (R7), matched against a live transcript because Apple offers
/// nothing better (DEC-105).
///
/// Deliberately forgiving about what the recogniser produces for a proper noun
/// it does not know: "mitta", "meta", "mita" and "mitt a" are all it hearing
/// the same word. A false positive costs one ignored turn; a false negative
/// means the user says the name and nothing happens, which reads as broken.
pub fn wake_word_at(transcript: &str) -> Option<usize> {
    const SPELLINGS: [&str; 5] = ["mitta", "mitt a", "mita", "meta", "mittah"];
    let lowered = transcript.to_lowercase();

    SPELLINGS
        .iter()
        .filter_map(|spelling| lowered.find(spelling).map(|at| at + spelling.len()))
        .min()
}

/// The request that follows the wake word, or `None` if nothing has been said
/// after it yet.
pub fn after_wake_word(transcript: &str) -> Option<String> {
    let at = wake_word_at(transcript)?;
    let rest = transcript[at..]
        .trim_start_matches([' ', ',', '.', '?', '!', ':', '-'])
        .trim();
    (!rest.is_empty()).then(|| rest.to_string())
}

// -- FFI wrappers ----------------------------------------------------------- //

fn take_string(pointer: *mut c_char) -> String {
    if pointer.is_null() {
        return String::new();
    }
    // SAFETY: the Swift side returns `strdup`ed UTF-8 and documents that the
    // caller owns it. Copied out before it is handed back to be freed.
    let owned = unsafe { CStr::from_ptr(pointer) }
        .to_string_lossy()
        .into_owned();
    unsafe { mitta_voice_free(pointer) };
    owned
}

pub fn authorization() -> Authorization {
    Authorization::from(unsafe { mitta_voice_auth_status() })
}

pub fn request_authorization() {
    unsafe { mitta_voice_request_auth() }
}

pub fn start_listening() -> Result<(), String> {
    let code = unsafe { mitta_voice_start() };
    if code == 0 {
        return Ok(());
    }
    let detail = take_string(unsafe { mitta_voice_copy_error() });
    Err(if detail.is_empty() {
        format!("the speech recogniser refused to start (code {code})")
    } else {
        detail
    })
}

pub fn stop_listening() {
    unsafe { mitta_voice_stop() }
}

pub fn speak(text: &str, rate: f32) {
    let Ok(encoded) = CString::new(text) else {
        // An interior NUL cannot be spoken and is not worth failing a turn for.
        log::warn!("voice: refusing to speak text containing a NUL byte");
        return;
    };
    unsafe { mitta_voice_speak(encoded.as_ptr(), rate) }
}

pub fn stop_speaking() {
    unsafe { mitta_voice_stop_speaking() }
}

/// Which voice is in use, and whether the user could do better.
#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct VoiceInfo {
    pub name: String,
    /// `compact` | `enhanced` | `premium`.
    pub quality: String,
    /// True when only compact voices are installed. macOS ships the good ones
    /// as an opt-in download, and nothing in the system tells you that — so an
    /// assistant that sounds like a 1990s train announcement is a settings
    /// problem the user has no way to know about.
    pub can_improve: bool,
    pub available: Vec<VoiceChoice>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct VoiceChoice {
    pub id: String,
    pub name: String,
    pub language: String,
    pub quality: String,
}

pub fn info() -> VoiceInfo {
    let quality = match unsafe { mitta_voice_voice_quality() } {
        2 => "premium",
        1 => "enhanced",
        _ => "compact",
    };

    let available: Vec<VoiceChoice> = take_string(unsafe { mitta_voice_copy_catalogue() })
        .lines()
        .filter_map(|line| {
            let mut parts = line.split('\t');
            Some(VoiceChoice {
                id: parts.next()?.to_string(),
                name: parts.next()?.to_string(),
                language: parts.next()?.to_string(),
                quality: parts.next()?.to_string(),
            })
        })
        .collect();

    VoiceInfo {
        name: take_string(unsafe { mitta_voice_copy_voice_name() }),
        quality: quality.to_string(),
        can_improve: quality == "compact",
        available,
    }
}

pub fn set_voice(identifier: Option<&str>) {
    match identifier.and_then(|value| CString::new(value).ok()) {
        Some(encoded) => unsafe { mitta_voice_set_voice(encoded.as_ptr()) },
        None => unsafe { mitta_voice_set_voice(std::ptr::null()) },
    }
}

fn snapshot(state: &VoiceState, triggered: bool) -> VoiceUpdate {
    let listen_state = ListenState::from(unsafe { mitta_voice_state() });
    let error = if listen_state == ListenState::Failed {
        let detail = take_string(unsafe { mitta_voice_copy_error() });
        (!detail.is_empty()).then_some(detail)
    } else {
        None
    };

    VoiceUpdate {
        state: listen_state,
        transcript: take_string(unsafe { mitta_voice_copy_transcript() }),
        is_final: unsafe { mitta_voice_is_final() },
        level: unsafe { mitta_voice_level() },
        speaking: unsafe { mitta_voice_is_speaking() },
        continuous: state.is_continuous(),
        error,
        triggered,
    }
}

/// Sample the Swift side and publish changes to the webview.
///
/// One thread for the process, started at setup. It emits only when something
/// changed, so an idle MITTA with the microphone closed costs a comparison
/// every 50 ms and no IPC at all.
pub fn spawn_poll_loop(app: AppHandle, state: Arc<VoiceState>) {
    std::thread::spawn(move || {
        let mut session_started = std::time::Instant::now();
        let mut last_emitted: Option<(ListenState, u64, bool)> = None;
        let mut last_level_emit = std::time::Instant::now();

        loop {
            std::thread::sleep(POLL_INTERVAL);

            if !state.is_listening() {
                // Nothing to report while the microphone is closed, but the
                // first idle tick after stopping must still be published or the
                // waveform freezes at its last value and keeps implying sound.
                if last_emitted.is_some() {
                    let _ = app.emit("voice:update", snapshot(&state, false));
                    last_emitted = None;
                }
                continue;
            }

            let sequence = unsafe { mitta_voice_sequence() };
            let listen_state = ListenState::from(unsafe { mitta_voice_state() });

            // Continuous mode outlives Apple's session limit by restarting
            // before it expires (DEC-105). Push-to-talk never runs long enough
            // to need this.
            if state.is_continuous() && session_started.elapsed() > SESSION_RECYCLE {
                stop_listening();
                if let Err(error) = start_listening() {
                    log::warn!("voice: could not recycle the recognition session: {error}");
                }
                session_started = std::time::Instant::now();
            }

            let transcript = take_string(unsafe { mitta_voice_copy_transcript() });
            let triggered = state.is_continuous() && wake_word_at(&transcript).is_some();

            let key = (listen_state, sequence, triggered);
            let changed = last_emitted != Some(key);
            // The level moves continuously and would emit on every tick. It is
            // published at a slower cadence than the transcript so the waveform
            // animates without flooding the IPC channel.
            let level_due = last_level_emit.elapsed() >= Duration::from_millis(100);

            if changed || level_due {
                let mut update = snapshot(&state, triggered);
                update.transcript = transcript;
                let _ = app.emit("voice:update", update);
                last_emitted = Some(key);
                last_level_emit = std::time::Instant::now();
            }
        }
    });
}

pub fn set_listening(state: &VoiceState, listening: bool) {
    state.listening.store(listening, Ordering::SeqCst);
}

pub fn set_continuous(state: &VoiceState, continuous: bool) {
    state.continuous.store(continuous, Ordering::SeqCst);
}

pub fn note_sequence(state: &VoiceState, sequence: u64) {
    state.last_sequence.store(sequence, Ordering::SeqCst);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_wake_word_is_found_however_the_recogniser_spells_it() {
        // All four have been produced by the on-device recogniser for the same
        // spoken word. Matching only the correct spelling would mean the name
        // works some mornings and not others.
        for heard in [
            "mitta open youtube",
            "Meta open youtube",
            "mita, open youtube",
        ] {
            assert!(
                wake_word_at(heard).is_some(),
                "missed the wake word in {heard:?}"
            );
        }
    }

    #[test]
    fn a_transcript_without_the_name_does_not_trigger() {
        assert!(wake_word_at("open youtube please").is_none());
        assert!(wake_word_at("").is_none());
    }

    #[test]
    fn the_request_is_what_follows_the_name() {
        assert_eq!(
            after_wake_word("hey mitta, open youtube").as_deref(),
            Some("open youtube")
        );
        assert_eq!(
            after_wake_word("mitta what's the weather").as_deref(),
            Some("what's the weather")
        );
    }

    #[test]
    fn the_name_on_its_own_is_not_yet_a_request() {
        // Someone has said the wake word and is still drawing breath. Acting on
        // an empty request would send a blank turn.
        assert_eq!(after_wake_word("mitta"), None);
        assert_eq!(after_wake_word("hey mitta..."), None);
    }

    #[test]
    fn the_earliest_spelling_wins() {
        // "meta" appears before "mitta" here; the request is everything after
        // the first thing that sounded like the name.
        assert_eq!(
            after_wake_word("meta open mitta docs").as_deref(),
            Some("open mitta docs")
        );
    }

    #[test]
    fn an_unknown_authorisation_code_is_never_granted() {
        assert_eq!(Authorization::from(99), Authorization::NotDetermined);
        assert_eq!(Authorization::from(-1), Authorization::NotDetermined);
        assert_eq!(Authorization::from(2), Authorization::Granted);
    }
}
