"""API key resolution.

Two supported paths, in order of preference:

1. **The macOS Keychain**, read by the Rust shell and handed to this process in
   its environment at spawn. This is the shipped path (R3, DEC-017) — the key
   is entered in Settings, goes straight to the Keychain, and never touches a
   file or a shell history.

2. **A gitignored `.env` file** at the repository root, for development. Chosen
   deliberately over the Keychain path during development because it survives a
   sidecar restart without a UI round-trip, and because a developer editing
   provider code should not have to click through a settings pane to test it.

Both arrive here as environment variables, so this module has exactly one way
to read a key regardless of which path supplied it.

What is *not* supported: keys in `config.json`, keys in command-line arguments,
or keys in any file the repository tracks. `_validate_config_file` already
rejects the first, `.gitignore` covers the second, and `ps` would expose the
third to every user on the machine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from mitta.telemetry.logging import get_logger

log = get_logger(__name__)

#: Environment variable per provider. The `MITTA_` prefix keeps them from
#: colliding with a globally-exported `GROQ_API_KEY` the user may already have
#: for another tool — MITTA should use the key the user gave *it*.
KEY_VARS: dict[str, str] = {
    "groq": "MITTA_GROQ_API_KEY",
    "openrouter": "MITTA_OPENROUTER_API_KEY",
}


@dataclass(frozen=True, slots=True)
class KeyStatus:
    provider: str
    configured: bool
    source: str | None


def resolve(provider: str, environ: dict[str, str] | None = None) -> str | None:
    """The key for `provider`, or `None`.

    Whitespace-only values read as absent. A key pasted with a trailing newline
    is the single most common way to get a 401 that looks like a revoked
    credential, and stripping it here costs nothing.
    """
    env = environ if environ is not None else dict(os.environ)
    var = KEY_VARS.get(provider)
    if var is None:
        return None

    value = env.get(var, "").strip()
    return value or None


def status(environ: dict[str, str] | None = None) -> list[KeyStatus]:
    """Which providers have a key. Never the values."""
    env = environ if environ is not None else dict(os.environ)
    return [
        KeyStatus(
            provider=provider,
            configured=resolve(provider, env) is not None,
            source="environment" if resolve(provider, env) is not None else None,
        )
        for provider in KEY_VARS
    ]


def load_env_file(path: Path, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Read `KEY=value` lines from a dotenv file into a dict.

    Deliberately minimal — no interpolation, no `export`, no multi-line values.
    A fuller parser would be a dependency, and the surface it adds is a surface
    that mishandles a key containing a `$`.

    **Existing environment wins.** The Rust shell's Keychain-sourced values are
    already in the environment when this runs, and a stale `.env` must not
    silently override the key the user just entered in Settings.
    """
    env = environ if environ is not None else dict(os.environ)
    if not path.is_file():
        return {}

    _warn_if_readable_by_others(path)

    loaded: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or not value:
            continue
        if key in env:
            continue
        loaded[key] = value

    if loaded:
        # Names only. A log line naming the variable is useful; one carrying its
        # value is a key in a file that outlives the process.
        log.info("llm.keys_loaded_from_file", extra={"variables": sorted(loaded)})
    return loaded


def _warn_if_readable_by_others(path: Path) -> None:
    """Say something if the key file is group- or world-readable.

    A `.env` created by a shell redirect gets 0644 by default, which puts every
    API key on the machine within reach of any local account. Warned rather than
    corrected: silently chmod'ing a user's file is its own surprise.
    """
    try:
        mode = path.stat().st_mode & 0o077
    except OSError:  # pragma: no cover - stat failing is not worth handling
        return
    if mode:
        log.warning(
            "llm.key_file_permissive",
            extra={
                "path": str(path),
                "detail": f"mode allows other users to read it; chmod 600 {path}",
            },
        )


def default_env_file() -> Path | None:
    """The development `.env`, if this is running from a source checkout.

    Resolved from `__file__` rather than the working directory: the sidecar is
    spawned by the Rust shell with an unspecified cwd, so a relative lookup
    would find the file only by luck.

    Returns `None` in a bundled application, where the layout does not exist and
    the Keychain is the only path.
    """
    try:
        root = Path(__file__).resolve().parents[3]
    except IndexError:  # pragma: no cover - only in an unexpected layout
        return None
    candidate = root / ".env"
    return candidate if candidate.is_file() else None


def apply_env_file(path: Path) -> None:
    """Load a dotenv file into `os.environ`.

    Called once from the composition root, before providers are constructed.
    Mutating the process environment is not something to do casually, but the
    alternative — threading a key dict through every layer — puts the value in
    more places, and the whole point is for it to exist in as few as possible.
    """
    for key, value in load_env_file(path).items():
        os.environ[key] = value
