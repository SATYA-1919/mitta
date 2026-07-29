//! The IPC surface — Channel B (`API_DESIGN.md` §7).
//!
//! Every command here is reachable from the webview, and the webview renders
//! model output. The list is therefore kept deliberately short and each entry
//! is either inert (window control) or narrowly validated (secrets, paths).
//!
//! Notably absent: any command returning an API key, and any command taking a
//! shell string. Both would be convenient. Both are how a rendering surface
//! becomes an execution surface.
//!
//! State is taken as `State<'_, Arc<AppState>>`, matching exactly what
//! `app.manage()` was given. Tauri resolves managed state by type, so
//! `State<'_, AppState>` looks up a key nothing was stored under — and fails at
//! *call* time, not compile time, with "state not managed for field `state`".

use std::collections::HashMap;

use std::sync::Arc;

use tauri::{AppHandle, Manager, State};

use crate::error::ShellError;
use crate::secrets;
use crate::sidecar::{RuntimeInfo, SidecarState};
use crate::state::AppState;
use crate::voice::{self, Authorization, VoiceInfo, VoiceState};
use crate::windows;

#[tauri::command]
pub fn get_runtime_info(state: State<'_, Arc<AppState>>) -> Result<RuntimeInfo, ShellError> {
    state.runtime_info()
}

#[tauri::command]
pub fn get_sidecar_state(state: State<'_, Arc<AppState>>) -> SidecarState {
    state.state()
}

#[tauri::command]
pub fn get_sidecar_error(state: State<'_, Arc<AppState>>) -> Option<String> {
    state.last_error()
}

// -- secrets (DEC-017) ------------------------------------------------------

#[tauri::command]
pub fn store_api_key(provider: String, key: String) -> Result<(), ShellError> {
    secrets::store(&provider, &key)
}

/// Presence only. There is no `get_api_key`, and adding one would defeat the
/// entire design of `secrets.rs`.
#[tauri::command]
pub fn has_api_key(provider: String) -> Result<bool, ShellError> {
    secrets::has(&provider)
}

#[tauri::command]
pub fn delete_api_key(provider: String) -> Result<(), ShellError> {
    secrets::delete(&provider)
}

#[tauri::command]
pub fn list_providers() -> Vec<&'static str> {
    secrets::providers().to_vec()
}

// -- windows ----------------------------------------------------------------

#[tauri::command]
pub fn toggle_palette(app: AppHandle) -> Result<(), ShellError> {
    windows::toggle_palette(&app)
}

#[tauri::command]
pub fn hide_palette(app: AppHandle) -> Result<(), ShellError> {
    windows::hide_palette(&app)
}

#[tauri::command]
pub fn show_main(app: AppHandle) -> Result<(), ShellError> {
    windows::show_main(&app)
}

#[tauri::command]
pub fn window_hide(app: AppHandle, label: String) -> Result<(), ShellError> {
    app.get_webview_window(&label)
        .ok_or(ShellError::WindowNotFound(label))?
        .hide()
        .map_err(|e| ShellError::Internal(e.to_string()))
}

// -- permissions ------------------------------------------------------------

/// Report macOS permission state.
///
/// `microphone` is now a real answer, asked of the Speech framework. The other
/// two stay `unknown` until the automation phase wires their checks.
/// Deliberately not `granted`: a fabricated success would make the onboarding
/// flow untestable and would ship, because nothing would fail until a user hit
/// the feature.
#[tauri::command]
pub fn get_permissions_status() -> HashMap<String, String> {
    let microphone = match voice::authorization() {
        Authorization::Granted => "granted",
        Authorization::Denied => "denied",
        Authorization::Restricted => "denied",
        Authorization::NotDetermined => "notDetermined",
    };

    [
        ("accessibility", "unknown"),
        ("screenRecording", "unknown"),
        ("microphone", microphone),
    ]
    .into_iter()
    .map(|(name, state)| (name.to_string(), state.to_string()))
    .collect()
}

// -- voice (R7, DEC-105) ----------------------------------------------------

/// Ask for microphone and speech-recognition access.
///
/// Fire-and-forget: the prompt is modal and macOS answers it whenever the user
/// does. The webview polls `get_permissions_status` rather than awaiting this,
/// because a command that blocks on a dialog blocks the IPC thread.
#[tauri::command]
pub fn voice_request_permission() {
    voice::request_authorization();
}

/// Open the microphone. Push-to-talk calls this on press (DEC-105).
#[tauri::command]
pub fn voice_start(
    state: State<'_, Arc<VoiceState>>,
    continuous: Option<bool>,
) -> Result<(), ShellError> {
    voice::set_continuous(&state, continuous.unwrap_or(false));
    voice::start_listening().map_err(ShellError::Internal)?;
    // Set only after the recogniser is actually running. Marking it listening
    // first would light the "microphone live" indicator for a session that
    // failed to open, which is the one direction this indicator must never err.
    voice::set_listening(&state, true);
    Ok(())
}

#[tauri::command]
pub fn voice_stop(state: State<'_, Arc<VoiceState>>) {
    voice::set_listening(&state, false);
    voice::set_continuous(&state, false);
    voice::stop_listening();
}

/// Speak a reply. `rate` of zero means the system default.
#[tauri::command]
pub fn voice_speak(text: String, rate: Option<f32>) {
    voice::speak(&text, rate.unwrap_or(0.0));
}

#[tauri::command]
pub fn voice_stop_speaking() {
    voice::stop_speaking();
}

#[tauri::command]
pub fn voice_info() -> VoiceInfo {
    voice::info()
}

#[tauri::command]
pub fn voice_set_voice(identifier: Option<String>) {
    voice::set_voice(identifier.as_deref());
}

/// Open the pane where macOS hides its good voices.
///
/// There is no API to download one, so the most an application can do is take
/// the user to the place it happens instead of describing a five-level Settings
/// path in a tooltip.
#[tauri::command]
pub fn voice_open_settings(app: AppHandle) -> Result<(), ShellError> {
    tauri_plugin_opener::open_url(
        "x-apple.systempreferences:com.apple.preference.universalaccess?SpeakableItems",
        None::<&str>,
    )
    .map_err(|e| ShellError::Internal(e.to_string()))?;
    let _ = app;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn permissions_are_reported_unknown_rather_than_granted() {
        let status = get_permissions_status();
        assert_eq!(status.len(), 3);
        // The microphone is answered for real now, so it is excluded here; the
        // remaining two must still refuse to claim access they never checked.
        let status: HashMap<String, String> = status
            .into_iter()
            .filter(|(name, _)| name != "microphone")
            .collect();
        for (name, state) in status {
            assert_eq!(
                state, "unknown",
                "{name} claimed a state it has not checked"
            );
        }
    }

    #[test]
    fn only_the_confirmed_providers_are_listed() {
        assert_eq!(list_providers(), vec!["groq", "openrouter"]);
    }
}
