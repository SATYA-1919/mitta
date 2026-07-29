//! Window management for the two surfaces (R2).
//!
//! The palette is shown and hidden rather than created and destroyed. Creating
//! a window means loading a document, parsing a bundle and running React —
//! nowhere near the sub-100 ms budget. Keeping it alive and hidden makes
//! opening it a compositor operation.

use tauri::{AppHandle, Manager, WebviewWindow};

use crate::error::ShellError;

pub const MAIN: &str = "main";
pub const PALETTE: &str = "palette";

fn window(app: &AppHandle, label: &str) -> Result<WebviewWindow, ShellError> {
    app.get_webview_window(label)
        .ok_or_else(|| ShellError::WindowNotFound(label.to_string()))
}

fn internal(e: impl std::fmt::Display) -> ShellError {
    ShellError::Internal(e.to_string())
}

pub fn show_main(app: &AppHandle) -> Result<(), ShellError> {
    let window = window(app, MAIN)?;
    window.show().map_err(internal)?;
    window.unminimize().ok();
    window.set_focus().map_err(internal)?;
    Ok(())
}

pub fn show_palette(app: &AppHandle) -> Result<(), ShellError> {
    let window = window(app, PALETTE)?;
    // Re-centre on every open. The palette should appear where the user is
    // looking, not where it happened to be left on another display.
    window.center().ok();
    window.show().map_err(internal)?;
    window.set_focus().map_err(internal)?;
    Ok(())
}

pub fn hide_palette(app: &AppHandle) -> Result<(), ShellError> {
    window(app, PALETTE)?.hide().map_err(internal)
}

/// Toggle, based on what is actually on screen.
///
/// Reads the real visibility rather than tracking it in a boolean. A cached
/// flag drifts the moment the window is hidden by anything else — losing focus,
/// the tray, a display change — and then the hotkey does the opposite of what
/// the user expects, which is worse than not having a hotkey.
pub fn toggle_palette(app: &AppHandle) -> Result<(), ShellError> {
    let window = window(app, PALETTE)?;
    if window.is_visible().unwrap_or(false) {
        window.hide().map_err(internal)
    } else {
        show_palette(app)
    }
}

/// Hide the palette when it loses focus.
///
/// An overlay that stays after you click elsewhere is a modal you did not ask
/// for. Hidden rather than closed, for the reason in the module docs.
pub fn on_palette_focus_lost(app: &AppHandle) {
    let _ = hide_palette(app);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn labels_match_tauri_conf() {
        // These strings are duplicated in tauri.conf.json, which the compiler
        // cannot check. A mismatch surfaces as a runtime "no such window".
        let config = include_str!("../tauri.conf.json");
        assert!(config.contains(&format!("\"label\": \"{MAIN}\"")));
        assert!(config.contains(&format!("\"label\": \"{PALETTE}\"")));
    }

    #[test]
    fn the_palette_starts_hidden_and_undecorated() {
        // R2: an overlay, not a second application window.
        let config = include_str!("../tauri.conf.json");
        let palette = config
            .split("\"label\": \"palette\"")
            .nth(1)
            .expect("palette window missing from tauri.conf.json");
        assert!(palette.contains("\"visible\": false"));
        assert!(palette.contains("\"decorations\": false"));
        assert!(palette.contains("\"alwaysOnTop\": true"));
    }
}
