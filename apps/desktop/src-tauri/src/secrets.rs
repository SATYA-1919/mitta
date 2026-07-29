//! API keys, stored in the macOS Keychain (R3, DEC-017).
//!
//! The rule the whole module exists to enforce: **a key value never travels
//! back to the webview.** It goes settings input → IPC → Keychain, and from
//! then on only the sidecar's outbound HTTP request ever sees it. There is no
//! `get_api_key` command, only `has_api_key`.
//!
//! This is not paranoia about the webview being malicious. It is that the
//! webview renders model output, and a value that can be read into JavaScript
//! is a value that can end up in a DOM node, a log line, a crash report or a
//! screenshot. Never reading it back removes the entire class.

use keyring::Entry;

use crate::error::ShellError;

const SERVICE: &str = "com.mitta.desktop";

/// Providers whose keys may be stored.
///
/// An allowlist rather than a free-form string: the account name becomes a
/// Keychain item, and an unvalidated one lets a caller write arbitrary items
/// into the user's Keychain under MITTA's service name.
const ALLOWED_PROVIDERS: &[&str] = &["groq", "openrouter"];

fn validate(provider: &str) -> Result<(), ShellError> {
    if ALLOWED_PROVIDERS.contains(&provider) {
        Ok(())
    } else {
        Err(ShellError::Keychain(format!(
            "unknown provider: {provider}"
        )))
    }
}

fn entry(provider: &str) -> Result<Entry, ShellError> {
    validate(provider)?;
    Entry::new(SERVICE, provider).map_err(ShellError::from)
}

pub fn store(provider: &str, key: &str) -> Result<(), ShellError> {
    if key.trim().is_empty() {
        return Err(ShellError::Keychain("key is empty".into()));
    }
    entry(provider)?.set_password(key).map_err(ShellError::from)
}

/// Presence only. There is deliberately no function returning the value.
pub fn has(provider: &str) -> Result<bool, ShellError> {
    match entry(provider)?.get_password() {
        Ok(_) => Ok(true),
        Err(keyring::Error::NoEntry) => Ok(false),
        Err(e) => Err(ShellError::from(e)),
    }
}

pub fn delete(provider: &str) -> Result<(), ShellError> {
    match entry(provider)?.delete_credential() {
        Ok(()) => Ok(()),
        // Deleting something already absent is the caller's desired end state.
        Err(keyring::Error::NoEntry) => Ok(()),
        Err(e) => Err(ShellError::from(e)),
    }
}

/// Read a key for the sidecar's own use.
///
/// `pub(crate)` and never exposed as an IPC command. The only caller is the
/// code that hands keys to the sidecar at spawn; if this ever appears in
/// `generate_handler!`, the guarantee at the top of this file is gone.
#[allow(dead_code, reason = "caller lands with the LLM gateway in Phase 7")]
pub(crate) fn read_for_sidecar(provider: &str) -> Result<Option<String>, ShellError> {
    match entry(provider)?.get_password() {
        Ok(value) => Ok(Some(value)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(e) => Err(ShellError::from(e)),
    }
}

pub fn providers() -> &'static [&'static str] {
    ALLOWED_PROVIDERS
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unknown_providers_are_rejected() {
        // The provider name becomes a Keychain account, so an unvalidated one
        // lets a caller write arbitrary items under MITTA's service name.
        for provider in ["", "../../etc", "anthropic", "groq\0", "GROQ"] {
            assert!(validate(provider).is_err(), "accepted: {provider:?}");
        }
    }

    #[test]
    fn the_confirmed_providers_are_allowed() {
        assert!(validate("groq").is_ok());
        assert!(validate("openrouter").is_ok());
    }

    #[test]
    fn an_empty_key_is_rejected() {
        // Storing "" would make `has` report true for a key that cannot work,
        // and the failure would surface much later as a provider auth error.
        assert!(store("groq", "   ").is_err());
    }

    #[test]
    fn no_public_function_returns_a_key() {
        // Enforced by review, asserted here so the intent is recorded next to
        // the code: `has` returns bool, `read_for_sidecar` is pub(crate).
        fn assert_returns_bool(_: fn(&str) -> Result<bool, ShellError>) {}
        assert_returns_bool(has);
    }
}
