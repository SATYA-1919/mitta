//! Sidecar supervisor.
//!
//! Spawns the Python process, reads its readiness handshake, and restarts it if
//! it dies. This is the reason the Rust shell exists at all: the sidecar owns
//! the memory database and the agent loop, and something has to own the
//! sidecar's lifetime.
//!
//! Two properties matter more than the rest.
//!
//! **The port is never guessed.** The sidecar binds an ephemeral port and
//! announces it on stdout as `MITTA_READY <port>`. A fixed port would collide
//! with whatever else the user is running, and a scan would race.
//!
//! **The token never touches disk or a command line.** It goes into the child's
//! environment, which is readable only by the process itself and root — unlike
//! `ps`, which shows arguments to every user on the machine.

use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use crate::backoff::{Backoff, RestartDecision, RestartPolicy};
use crate::error::ShellError;

/// How long to wait for `MITTA_READY` before treating the spawn as failed.
const READY_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, Clone, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RuntimeInfo {
    pub base_url: String,
    pub ws_url: String,
    pub token: String,
    pub api_version: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub enum SidecarState {
    Starting,
    Ready,
    Restarting,
    /// Gave up. The UI says so instead of showing a spinner forever.
    Failed,
}

/// How to launch the sidecar.
#[derive(Debug, Clone)]
pub struct SidecarConfig {
    pub program: PathBuf,
    pub args: Vec<String>,
    pub storage_root: Option<PathBuf>,
    pub dev_mode: bool,
    pub policy: RestartPolicy,
}

pub struct Sidecar {
    child: Child,
    pub port: u16,
    started_at: Instant,
}

impl Sidecar {
    /// Spawn the process and wait for it to announce its port.
    pub fn spawn(config: &SidecarConfig, token: &str) -> Result<Self, ShellError> {
        let started_at = Instant::now();

        let mut command = Command::new(&config.program);
        command
            .args(&config.args)
            // The token rides the environment, not argv. See the module docs.
            .env("MITTA_SESSION_TOKEN", token)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            // Inherited so the sidecar's own structured logs reach the same place
            // as the shell's. It never writes the token there (verified by an
            // integration test on the Python side).
            .stderr(Stdio::inherit());

        if config.dev_mode {
            command.env("MITTA_DEV_MODE", "1");
        }
        if let Some(root) = &config.storage_root {
            command.env("MITTA_STORAGE_ROOT", root);
        }

        let mut child = command
            .spawn()
            .map_err(|e| ShellError::SidecarSpawn(e.to_string()))?;

        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| ShellError::SidecarSpawn("child had no stdout".into()))?;

        match read_ready_port(stdout, READY_TIMEOUT) {
            Ok(port) => Ok(Self {
                child,
                port,
                started_at,
            }),
            Err(e) => {
                // Do not leave a half-started child behind: it holds the SQLite
                // write lock, and the next spawn attempt would fail against it
                // with a confusing "database is locked".
                let _ = child.kill();
                let _ = child.wait();
                Err(e)
            }
        }
    }

    pub fn base_url(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }

    pub fn ws_url(&self) -> String {
        format!("ws://127.0.0.1:{}/v1/ws", self.port)
    }

    pub fn uptime(&self) -> Duration {
        self.started_at.elapsed()
    }

    /// Whether the process is still running, without blocking.
    pub fn is_alive(&mut self) -> bool {
        matches!(self.child.try_wait(), Ok(None))
    }

    /// Ask the sidecar to exit, then make sure it did.
    ///
    /// SIGTERM first so it can close the database cleanly — a killed writer
    /// leaves a WAL to recover and, worse, teaches the user that quitting risks
    /// their data.
    pub fn shutdown(mut self, grace: Duration) {
        #[cfg(unix)]
        unsafe {
            libc_kill(self.child.id() as i32, 15);
        }

        let deadline = Instant::now() + grace;
        while Instant::now() < deadline {
            if let Ok(Some(_)) = self.child.try_wait() {
                return;
            }
            std::thread::sleep(Duration::from_millis(20));
        }

        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[cfg(unix)]
unsafe fn libc_kill(pid: i32, sig: i32) {
    // Declared locally rather than taking a `libc` dependency for one symbol.
    unsafe extern "C" {
        fn kill(pid: i32, sig: i32) -> i32;
    }
    unsafe {
        kill(pid, sig);
    }
}

/// Read stdout until the readiness line appears.
///
/// Runs the blocking read on a worker thread so the timeout is real. Reading
/// inline would mean a sidecar that hangs before printing anything hangs the
/// whole application at launch, with no window and no way to tell why.
fn read_ready_port<R: std::io::Read + Send + 'static>(
    stdout: R,
    timeout: Duration,
) -> Result<u16, ShellError> {
    let (tx, rx) = mpsc::channel();

    std::thread::spawn(move || {
        let reader = BufReader::new(stdout);
        for line in reader.lines() {
            let Ok(line) = line else { break };
            if let Some(port) = parse_ready_line(&line) {
                let _ = tx.send(Ok(port));
                return;
            }
        }
        let _ = tx.send(Err(ShellError::SidecarSpawn(
            "sidecar exited before announcing a port".into(),
        )));
    });

    match rx.recv_timeout(timeout) {
        Ok(result) => result,
        Err(_) => Err(ShellError::SidecarSpawn(format!(
            "sidecar did not become ready within {}s",
            timeout.as_secs()
        ))),
    }
}

/// Parse `MITTA_READY <port>`.
///
/// Strict: anything else on stdout is ignored rather than guessed at. A
/// misparsed port would have the webview talking to some other process on the
/// machine, authenticating to it with our session token.
pub fn parse_ready_line(line: &str) -> Option<u16> {
    let rest = line.trim().strip_prefix("MITTA_READY")?;
    let port: u32 = rest.trim().parse().ok()?;
    // Port 0 means "any port" to bind(2) and is never a real listener.
    if port == 0 || port > u16::MAX as u32 {
        return None;
    }
    Some(port as u16)
}

/// Decide whether to restart, given how long the last run lasted.
pub fn decide_restart(
    backoff: &mut Backoff,
    ran_for: Duration,
    policy: &RestartPolicy,
) -> RestartDecision {
    backoff.record_exit(ran_for, policy)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    #[test]
    fn parses_the_readiness_line() {
        assert_eq!(parse_ready_line("MITTA_READY 55786"), Some(55786));
        assert_eq!(parse_ready_line("  MITTA_READY 1024  "), Some(1024));
        assert_eq!(parse_ready_line("MITTA_READY  65535"), Some(65535));
    }

    #[test]
    fn rejects_anything_that_is_not_a_readiness_line() {
        // A misparse would point the webview at another process and hand it our
        // session token, so this is deliberately unforgiving.
        for line in [
            "",
            "INFO starting up",
            "MITTA_READY",
            "MITTA_READY abc",
            "MITTA_READY 0",
            "MITTA_READY 70000",
            "MITTA_READY -1",
            "NOT_MITTA_READY 8080",
            "MITTA_READY 8080 extra",
        ] {
            assert_eq!(parse_ready_line(line), None, "wrongly accepted: {line:?}");
        }
    }

    #[test]
    fn reads_the_port_past_unrelated_output() {
        let output = "starting\nloading config\nMITTA_READY 41234\nmore logs\n";
        let port = read_ready_port(
            Cursor::new(output.as_bytes().to_vec()),
            Duration::from_secs(1),
        );
        assert_eq!(port.unwrap(), 41234);
    }

    #[test]
    fn reports_a_sidecar_that_exits_without_announcing() {
        let result = read_ready_port(
            Cursor::new(b"failed to start\n".to_vec()),
            Duration::from_secs(1),
        );
        assert!(result.is_err());
    }

    #[test]
    fn times_out_rather_than_hanging_forever() {
        // A sidecar that hangs before printing must not hang the whole app at
        // launch, with no window and nothing to explain why.
        struct Blocking;
        impl std::io::Read for Blocking {
            fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
                std::thread::sleep(Duration::from_secs(60));
                Ok(0)
            }
        }
        let started = Instant::now();
        let result = read_ready_port(Blocking, Duration::from_millis(100));

        assert!(result.is_err());
        assert!(started.elapsed() < Duration::from_secs(5));
    }
}
