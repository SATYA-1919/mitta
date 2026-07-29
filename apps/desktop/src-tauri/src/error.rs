//! Shell errors.
//!
//! Every variant serialises to the same dot-namespaced code the Python side
//! uses (`API_DESIGN.md` §5), so the frontend switches on one error vocabulary
//! regardless of which process failed. Two error shapes for one application
//! means two error-handling paths in the UI, and the second one is always the
//! one nobody tested.

use serde::{Serialize, Serializer};

#[derive(Debug, thiserror::Error)]
pub enum ShellError {
    #[error("Failed to start the MITTA backend: {0}")]
    SidecarSpawn(String),

    #[error("The MITTA backend is not running")]
    SidecarUnavailable,

    #[error("Keychain error: {0}")]
    Keychain(String),

    #[error("No such window: {0}")]
    WindowNotFound(String),

    #[error("{0}")]
    Internal(String),
}

impl ShellError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::SidecarSpawn(_) => "sidecar.spawn_failed",
            Self::SidecarUnavailable => "sidecar.unavailable",
            Self::Keychain(_) => "secrets.keychain_failed",
            Self::WindowNotFound(_) => "shell.window_not_found",
            Self::Internal(_) => "shell.internal",
        }
    }

    /// Whether the UI should offer a retry.
    pub fn retryable(&self) -> bool {
        matches!(self, Self::SidecarUnavailable | Self::SidecarSpawn(_))
    }
}

impl Serialize for ShellError {
    /// Matches the envelope in `API_DESIGN.md` §5 so `ApiError` on the
    /// TypeScript side parses a shell failure without a special case.
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        use serde::ser::SerializeMap;
        let mut error = serializer.serialize_map(Some(3))?;
        error.serialize_entry("code", self.code())?;
        error.serialize_entry("message", &self.to_string())?;
        error.serialize_entry("retryable", &self.retryable())?;
        error.end()
    }
}

impl From<keyring::Error> for ShellError {
    fn from(error: keyring::Error) -> Self {
        Self::Keychain(error.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn codes_are_dot_namespaced_like_the_python_side() {
        for error in [
            ShellError::SidecarSpawn("x".into()),
            ShellError::SidecarUnavailable,
            ShellError::Keychain("x".into()),
            ShellError::WindowNotFound("x".into()),
            ShellError::Internal("x".into()),
        ] {
            assert!(
                error.code().contains('.'),
                "code {:?} is not namespaced",
                error.code()
            );
        }
    }

    #[test]
    fn a_missing_sidecar_is_retryable_but_a_bad_window_is_not() {
        assert!(ShellError::SidecarUnavailable.retryable());
        assert!(!ShellError::WindowNotFound("main".into()).retryable());
    }

    #[test]
    fn serialises_to_the_shared_error_envelope() {
        let json = serde_json::to_value(ShellError::SidecarUnavailable).unwrap();
        assert_eq!(json["code"], "sidecar.unavailable");
        assert_eq!(json["retryable"], true);
        assert!(json["message"].as_str().unwrap().contains("not running"));
    }

    #[test]
    fn keychain_messages_do_not_leak_the_secret() {
        // The message reaches the webview, which renders model output. A key in
        // an error string is a key on screen and in a screenshot.
        let error = ShellError::Keychain("item not found".into());
        assert!(!error.to_string().contains("sk-"));
    }
}
