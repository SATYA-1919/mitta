//! MITTA shell — process supervision, secrets, metrics, windows.
//!
//! The Rust side owns everything the sidecar cannot: the sidecar's own
//! lifetime, the Keychain, the global hotkey, the tray, and the two windows.
//! It deliberately owns no product logic — reasoning, memory and tools all live
//! in Python, so the shell can be reasoned about as a supervisor rather than as
//! half the application.

pub mod backoff;
pub mod commands;
pub mod error;
pub mod metrics;
pub mod secrets;
pub mod sidecar;
pub mod state;
pub mod token;
pub mod voice;
pub mod windows;

use std::sync::Arc;
use std::time::Duration;

use tauri::{Emitter, Manager, WindowEvent};

use crate::backoff::{Backoff, RestartDecision};
use crate::metrics::MetricsSampler;
use crate::sidecar::SidecarState;
use crate::state::AppState;

/// How often metrics are sampled and pushed to the webview (DEC-003).
const METRICS_INTERVAL: Duration = Duration::from_secs(1);

/// How often the supervisor checks whether the sidecar is still alive.
const HEALTH_INTERVAL: Duration = Duration::from_millis(500);

const SHUTDOWN_GRACE: Duration = Duration::from_secs(5);

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let dev_mode = cfg!(debug_assertions);

    // Without this, every `log::info!` and `log::warn!` in the supervisor goes
    // nowhere. That is not a cosmetic gap: it meant the shell could fail to
    // spawn or restart the sidecar and say nothing at all, and three separate
    // connection bugs were diagnosed from the Python side's output alone
    // because the Rust side was silent.
    //
    // stderr rather than a file: the shell is launched from a terminal in
    // development and Console.app captures it in a bundle, and a log the
    // supervisor writes while the supervisor is the thing failing should not
    // depend on the filesystem.
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or(if dev_mode {
        "info"
    } else {
        "warn"
    }))
    .format_timestamp_secs()
    .init();

    log::info!("MITTA shell starting (dev_mode={dev_mode})");

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .setup(move |app| {
            let resource_dir = app.path().resource_dir().ok();
            let config = state::resolve_sidecar(dev_mode, resource_dir);
            let app_state = Arc::new(AppState::new(config));
            app.manage(Arc::clone(&app_state));

            // The microphone stays closed until asked for. This only starts
            // the loop that reports its state; DEC-105 makes opening it an
            // explicit act by the user.
            let voice_state = Arc::new(voice::VoiceState::new());
            app.manage(Arc::clone(&voice_state));
            voice::spawn_poll_loop(app.handle().clone(), voice_state);

            spawn_supervisor(app.handle().clone(), Arc::clone(&app_state));
            spawn_metrics(app.handle().clone());
            register_hotkey(app.handle());

            // Shown only after the supervisor has been started, so the window
            // never appears before there is anything for it to connect to.
            windows::show_main(app.handle()).ok();
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::Focused(false) = event {
                if window.label() == windows::PALETTE {
                    windows::on_palette_focus_lost(window.app_handle());
                }
            }
            // The main window closing means "put it away", not "quit". MITTA is
            // resident — it has a tray item and a global hotkey, and killing the
            // sidecar because a window was closed would drop background work.
            if let WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == windows::MAIN {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            commands::get_runtime_info,
            commands::get_sidecar_state,
            commands::get_sidecar_error,
            commands::store_api_key,
            commands::has_api_key,
            commands::delete_api_key,
            commands::list_providers,
            commands::toggle_palette,
            commands::hide_palette,
            commands::show_main,
            commands::window_hide,
            commands::get_permissions_status,
            commands::voice_request_permission,
            commands::voice_start,
            commands::voice_stop,
            commands::voice_speak,
            commands::voice_stop_speaking,
            commands::voice_info,
            commands::voice_set_voice,
            commands::voice_open_settings,
        ])
        .build(tauri::generate_context!())
        .expect("failed to build MITTA")
        .run(|app, event| {
            if let tauri::RunEvent::ExitRequested { .. } = event {
                // The sidecar holds the SQLite write lock. Leaving it running
                // after the shell exits orphans a process the user cannot see
                // and cannot stop from the dock.
                if let Some(state) = app.try_state::<Arc<AppState>>() {
                    state.shutdown(SHUTDOWN_GRACE);
                }
            }
        });
}

/// Start the sidecar and keep it running.
fn spawn_supervisor(app: tauri::AppHandle, state: Arc<AppState>) {
    std::thread::spawn(move || {
        let mut backoff = Backoff::new();

        loop {
            state.set_state(SidecarState::Starting, None);
            let _ = app.emit("sidecar:state", SidecarState::Starting);

            match state.start() {
                Ok(port) => {
                    log::info!("sidecar ready on port {port}");
                    state.set_state(SidecarState::Ready, None);
                    let _ = app.emit("sidecar:state", SidecarState::Ready);
                    backoff.reset();
                }
                Err(e) => {
                    let message = e.to_string();
                    log::error!("sidecar failed to start: {message}");
                    state.set_state(SidecarState::Failed, Some(message.clone()));
                    let _ = app.emit("sidecar:state", SidecarState::Failed);

                    match backoff.record_exit(Duration::ZERO, &state.config.policy) {
                        RestartDecision::Retry(delay) => {
                            std::thread::sleep(delay);
                            continue;
                        }
                        RestartDecision::GiveUp => {
                            log::error!("giving up on the sidecar after repeated failures");
                            let _ = app.emit("sidecar:state", SidecarState::Failed);
                            return;
                        }
                    }
                }
            }

            // Watch until it dies.
            let ran_for = loop {
                std::thread::sleep(HEALTH_INTERVAL);
                let (alive, uptime) = state.health();
                if !alive {
                    break uptime;
                }
            };

            log::warn!("sidecar exited after {:?}", ran_for);
            state.set_state(SidecarState::Restarting, None);
            let _ = app.emit("sidecar:state", SidecarState::Restarting);

            match backoff.record_exit(ran_for, &state.config.policy) {
                RestartDecision::Retry(delay) => std::thread::sleep(delay),
                RestartDecision::GiveUp => {
                    state.set_state(
                        SidecarState::Failed,
                        Some("The MITTA backend keeps stopping. Check the logs.".into()),
                    );
                    let _ = app.emit("sidecar:state", SidecarState::Failed);
                    return;
                }
            }
        }
    });
}

fn spawn_metrics(app: tauri::AppHandle) {
    std::thread::spawn(move || {
        let mut sampler = MetricsSampler::new();
        loop {
            std::thread::sleep(METRICS_INTERVAL);
            // Emitted regardless of whether a window is listening: `emit` to
            // nobody is cheap, and gating on visibility would mean the first
            // reading after showing a window is always a stale one.
            let _ = app.emit("metrics:update", sampler.sample());
        }
    });
}

fn register_hotkey(app: &tauri::AppHandle) {
    use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut};

    // Cmd+Shift+Space. Cmd+Space is Spotlight and Cmd+Alt+Space is its file
    // search — taking either would break something the user already relies on.
    let shortcut = Shortcut::new(Some(Modifiers::SUPER | Modifiers::SHIFT), Code::Space);
    let handle = app.clone();

    if let Err(e) = app.global_shortcut().on_shortcut(shortcut, move |_, _, _| {
        let _ = windows::toggle_palette(&handle);
    }) {
        // Not fatal. Another application may already hold the combination, and
        // an assistant that refuses to launch over a hotkey conflict is worse
        // than one whose hotkey needs reassigning in settings.
        log::warn!("could not register the global hotkey: {e}");
    }
}
