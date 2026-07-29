//! The IPC surface — Channel B (`API_DESIGN.md` §7).
//!
//! Every command here is reachable from the webview, and the webview renders
//! model output. The list is therefore kept deliberately short and each entry
//! is either inert (window control) or narrowly validated (secrets, paths).
//!
//! Notably absent: any command returning an API key, and any command taking a
//! shell string. Both would be convenient. Both are how a rendering surface
//! becomes an execution surface.

use std::collections::HashMap;

use tauri::{AppHandle, Manager, State};

use crate::error::ShellError;
use crate::secrets;
use crate::sidecar::{RuntimeInfo, SidecarState};
use crate::state::AppState;
use crate::windows;

#[tauri::command]
pub fn get_runtime_info(state: State<'_, AppState>) -> Result<RuntimeInfo, ShellError> {
    state.runtime_info()
}

#[tauri::command]
pub fn get_sidecar_state(state: State<'_, AppState>) -> SidecarState {
    state.state()
}

#[tauri::command]
pub fn get_sidecar_error(state: State<'_, AppState>) -> Option<String> {
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
/// Returns `unknown` for everything until the voice and automation phases wire
/// the real checks. Deliberately not `granted`: a fabricated success would make
/// the onboarding flow untestable and would ship, because nothing would fail
/// until a user hit the feature.
#[tauri::command]
pub fn get_permissions_status() -> HashMap<String, String> {
    ["accessibility", "screenRecording", "microphone"]
        .into_iter()
        .map(|name| (name.to_string(), "unknown".to_string()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn permissions_are_reported_unknown_rather_than_granted() {
        let status = get_permissions_status();
        assert_eq!(status.len(), 3);
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
