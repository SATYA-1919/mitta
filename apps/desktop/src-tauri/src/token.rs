//! Session token generation.
//!
//! The token authenticates the webview to the sidecar. It is generated here,
//! in the parent process, and handed to the child through its environment —
//! never written to a file, never passed as a command-line argument (where
//! `ps` would show it to every process on the machine).

use rand::RngCore;

/// 256 bits, base64url, unpadded.
///
/// Sized to be unguessable rather than to be typed. The token never crosses a
/// human boundary — Rust generates it, the sidecar reads it from its
/// environment, and the webview receives it over IPC.
pub fn generate() -> String {
    let mut bytes = [0u8; 32];
    // `rand::rng` is the OS CSPRNG. A seeded or thread-deterministic generator
    // would make tokens predictable across restarts, which is the one property
    // this must not have.
    rand::rng().fill_bytes(&mut bytes);
    base64url(&bytes)
}

/// Base64url without padding, implemented rather than pulled in.
///
/// The alternative is a dependency for eleven lines. Padding is omitted because
/// `=` is awkward in environment variables and HTTP headers and carries no
/// information here — the length is fixed.
fn base64url(input: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

    let mut out = String::with_capacity(input.len().div_ceil(3) * 4);
    for chunk in input.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let triple = (b0 << 16) | (b1 << 8) | b2;

        out.push(ALPHABET[(triple >> 18 & 0x3F) as usize] as char);
        out.push(ALPHABET[(triple >> 12 & 0x3F) as usize] as char);
        if chunk.len() > 1 {
            out.push(ALPHABET[(triple >> 6 & 0x3F) as usize] as char);
        }
        if chunk.len() > 2 {
            out.push(ALPHABET[(triple & 0x3F) as usize] as char);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    #[test]
    fn tokens_are_url_safe() {
        let token = generate();
        assert!(
            token
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_'),
            "token contained a character needing escaping: {token}"
        );
    }

    #[test]
    fn tokens_encode_the_full_256_bits() {
        // 32 bytes unpadded base64 is 43 characters. A shorter token would mean
        // entropy was lost somewhere in the encoding.
        assert_eq!(generate().len(), 43);
    }

    #[test]
    fn tokens_do_not_repeat() {
        let tokens: HashSet<String> = (0..1000).map(|_| generate()).collect();
        assert_eq!(tokens.len(), 1000, "generate() produced a collision");
    }

    #[test]
    fn encoding_matches_known_vectors() {
        assert_eq!(base64url(b""), "");
        assert_eq!(base64url(b"f"), "Zg");
        assert_eq!(base64url(b"fo"), "Zm8");
        assert_eq!(base64url(b"foo"), "Zm9v");
        assert_eq!(base64url(b"foobar"), "Zm9vYmFy");
        // The two characters that differ from standard base64.
        assert_eq!(base64url(&[0xFB, 0xFF]), "-_8");
    }
}
