//! Shared shell state.
//!
//! Holds the running sidecar and the session token. Guarded by a mutex because
//! the supervisor thread replaces the sidecar on restart while IPC commands
//! read it, and a command that reads a stale port would silently talk to a
//! process that no longer exists.

use std::path::PathBuf;
use std::sync::Mutex;

use crate::error::ShellError;
use crate::sidecar::{RuntimeInfo, Sidecar, SidecarConfig, SidecarState};
use crate::token;

pub struct AppState {
    inner: Mutex<Inner>,
    /// Generated once per application run, not once per sidecar spawn: the
    /// webview receives it at startup and does not re-handshake on restart.
    token: String,
    pub config: SidecarConfig,
}

struct Inner {
    sidecar: Option<Sidecar>,
    state: SidecarState,
    last_error: Option<String>,
}

impl AppState {
    pub fn new(config: SidecarConfig) -> Self {
        Self {
            inner: Mutex::new(Inner {
                sidecar: None,
                state: SidecarState::Starting,
                last_error: None,
            }),
            token: token::generate(),
            config,
        }
    }

    pub fn token(&self) -> &str {
        &self.token
    }

    /// Spawn the sidecar and adopt it.
    pub fn start(&self) -> Result<u16, ShellError> {
        let sidecar = Sidecar::spawn(&self.config, &self.token)?;
        let port = sidecar.port;

        let mut inner = self.lock();
        // Replace rather than assume none is running. A restart path that
        // leaked the old child would leave two processes contending for the
        // SQLite write lock.
        if let Some(previous) = inner.sidecar.take() {
            previous.shutdown(std::time::Duration::from_secs(5));
        }
        inner.sidecar = Some(sidecar);
        inner.state = SidecarState::Ready;
        inner.last_error = None;
        Ok(port)
    }

    pub fn runtime_info(&self) -> Result<RuntimeInfo, ShellError> {
        let inner = self.lock();
        let sidecar = inner
            .sidecar
            .as_ref()
            .ok_or(ShellError::SidecarUnavailable)?;

        Ok(RuntimeInfo {
            base_url: sidecar.base_url(),
            ws_url: sidecar.ws_url(),
            token: self.token.clone(),
            api_version: "1".to_string(),
        })
    }

    pub fn state(&self) -> SidecarState {
        self.lock().state
    }

    pub fn set_state(&self, state: SidecarState, error: Option<String>) {
        let mut inner = self.lock();
        inner.state = state;
        inner.last_error = error;
    }

    pub fn last_error(&self) -> Option<String> {
        self.lock().last_error.clone()
    }

    /// Whether the sidecar is still running, and how long it has been up.
    pub fn health(&self) -> (bool, std::time::Duration) {
        let mut inner = self.lock();
        match inner.sidecar.as_mut() {
            Some(sidecar) => (sidecar.is_alive(), sidecar.uptime()),
            None => (false, std::time::Duration::ZERO),
        }
    }

    pub fn shutdown(&self, grace: std::time::Duration) {
        let mut inner = self.lock();
        if let Some(sidecar) = inner.sidecar.take() {
            sidecar.shutdown(grace);
        }
        inner.state = SidecarState::Failed;
    }

    /// A poisoned mutex means a thread panicked mid-update. Recovering the
    /// guard is right here: the alternative is that one panic makes every
    /// subsequent command fail, turning a recoverable fault into a dead app.
    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }
}

/// Where the sidecar lives.
///
/// In development it is the project virtualenv running `python -m mitta`. In a
/// bundled app it is the PyInstaller binary next to the executable. Resolved at
/// startup rather than guessed per call.
pub fn resolve_sidecar(dev_mode: bool, resource_dir: Option<PathBuf>) -> SidecarConfig {
    use crate::backoff::RestartPolicy;

    if dev_mode {
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../..")
            .canonicalize()
            .unwrap_or_else(|_| PathBuf::from("."));

        return SidecarConfig {
            program: repo_root.join(".venv/bin/python"),
            args: vec!["-m".into(), "mitta".into()],
            storage_root: Some(repo_root.join(".dev/storage")),
            dev_mode: true,
            policy: RestartPolicy::default(),
        };
    }

    let program = resource_dir
        .map(|dir| dir.join("mitta-core"))
        .unwrap_or_else(|| PathBuf::from("mitta-core"));

    SidecarConfig {
        program,
        args: Vec::new(),
        storage_root: None,
        dev_mode: false,
        policy: RestartPolicy::default(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> SidecarConfig {
        resolve_sidecar(true, None)
    }

    #[test]
    fn a_token_is_generated_before_any_sidecar_starts() {
        // The webview may ask for runtime info during startup; a token created
        // lazily on first spawn would be absent exactly then.
        let state = AppState::new(config());
        assert_eq!(state.token().len(), 43);
    }

    #[test]
    fn the_token_is_stable_across_restarts() {
        // The webview receives it once at startup and does not re-handshake.
        let state = AppState::new(config());
        let first = state.token().to_string();
        state.set_state(SidecarState::Restarting, None);
        assert_eq!(state.token(), first);
    }

    #[test]
    fn each_run_gets_a_different_token() {
        let a = AppState::new(config());
        let b = AppState::new(config());
        assert_ne!(a.token(), b.token());
    }

    #[test]
    fn runtime_info_is_refused_before_the_sidecar_is_up() {
        // Never a fabricated port. A plausible-looking default would have the
        // webview authenticating to whatever else is listening.
        let state = AppState::new(config());
        let error = state.runtime_info().unwrap_err();
        assert_eq!(error.code(), "sidecar.unavailable");
    }

    #[test]
    fn health_reports_absence_rather_than_pretending() {
        let state = AppState::new(config());
        let (alive, uptime) = state.health();
        assert!(!alive);
        assert_eq!(uptime, std::time::Duration::ZERO);
    }

    #[test]
    fn dev_mode_points_at_the_project_virtualenv() {
        let config = resolve_sidecar(true, None);
        assert!(config.program.to_string_lossy().contains(".venv"));
        assert_eq!(config.args, vec!["-m", "mitta"]);
        assert!(config.dev_mode);
    }

    #[test]
    fn release_mode_points_at_the_bundled_binary() {
        let config = resolve_sidecar(false, Some(PathBuf::from("/Apps/MITTA.app/Resources")));
        assert!(config.program.ends_with("mitta-core"));
        assert!(config.args.is_empty());
        assert!(!config.dev_mode);
        // No storage override: the bundled app uses the real storage root the
        // OS adapter resolves.
        assert!(config.storage_root.is_none());
    }
}

#[cfg(test)]
mod managed_state_tests {

    /// Tauri resolves managed state by type. `app.manage(Arc::clone(&state))`
    /// stores it under `Arc<AppState>`, so every command must ask for
    /// `State<'_, Arc<AppState>>`.
    ///
    /// Getting this wrong compiles cleanly and fails at *call* time with
    /// "state not managed for field `state`" — which is exactly how it
    /// presented: the window opened, the sidecar ran, and every IPC call
    /// returned an error the UI could only report as disconnected.
    #[test]
    fn commands_ask_for_the_type_that_is_actually_managed() {
        // Comments stripped first: the explanation in `commands.rs` names the
        // wrong type on purpose, and a grep over prose would match it.
        let commands: String = include_str!("commands.rs")
            .lines()
            .filter(|line| !line.trim_start().starts_with("//"))
            .collect::<Vec<_>>()
            .join("\n");
        let wiring = include_str!("lib.rs");

        assert!(
            wiring.contains("app.manage(Arc::clone(&app_state))"),
            "the composition root no longer manages an Arc; update commands.rs to match"
        );
        assert!(
            !commands.contains("State<'_, AppState>"),
            "a command asks for State<AppState> while Arc<AppState> is what is managed"
        );
        assert!(
            commands.contains("State<'_, Arc<AppState>>"),
            "commands should take the managed Arc"
        );
    }
}
