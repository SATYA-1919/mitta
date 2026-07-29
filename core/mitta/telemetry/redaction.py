"""Secret redaction for log output (DEC-017).

Keys live in the Keychain and are held in memory only at use time, so in
principle they should never reach a log line. This module exists because "in
principle" is not a security control: an exception carrying a request object, a
third-party library logging its own headers at DEBUG, or a `repr()` of a
provider client all route a key into the log without anyone writing
``log.info(api_key)``.

Two mechanisms, because they fail differently:

* **Pattern redaction** catches key-shaped strings by structure. It works on
  values nobody registered, which is the common case, but only for formats it
  knows.
* **Literal redaction** catches exact values registered at runtime — the session
  token, and any provider key currently held in memory. It works regardless of
  format, but only for values the process knows it holds.

Neither is sufficient alone.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Final

REDACTED: Final = "«redacted»"

_MIN_LITERAL_LENGTH: Final = 8
"""Below this, a "secret" is too short to redact without mangling ordinary text."""

_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # Provider-specific key formats, most specific first.
    re.compile(r"\bgsk_[A-Za-z0-9]{16,}\b"),                      # Groq
    re.compile(r"\bsk-or-v1-[A-Za-z0-9]{16,}\b"),                 # OpenRouter
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}\b"),                # Anthropic
    re.compile(r"\bsk-proj-[A-Za-z0-9_\-]{16,}\b"),               # OpenAI project
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),                       # OpenAI classic
    re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}\b"),                   # Google
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),                # GitHub
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),             # Slack
    # Authorization headers in any casing.
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{12,}"),
    # key-ish assignment in any common syntax:
    #   api_key=…   |   "token": "…"   |   secret = '…'   |   password: …
    # The optional quote after the name is what makes the JSON form work; without
    # it the closing quote of the key breaks the match.
    re.compile(
        r"""(?ix)
        \b(?P<name>api[_-]?key|access[_-]?token|refresh[_-]?token|session[_-]?token
           |secret|password|passwd|authorization|auth[_-]?token|client[_-]?secret)
        \b["']?\s*[:=]\s*
        (?P<quote>["']?)
        (?P<value>[^\s"',;}\)]{8,})
        (?P=quote)
        """
    ),
    # JWTs.
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
)

_SENSITIVE_KEY_NAMES: Final[frozenset[str]] = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "auth_token",
        "client_secret",
        "key",
        "password",
        "passwd",
        "refresh_token",
        "secret",
        "session_token",
        "token",
    }
)


class SecretRedactor:
    """Redacts secrets from arbitrary log payloads.

    Registered literals are stored, not compared to a hash, because the process
    already holds the plaintext — hashing them here would protect nothing while
    making the comparison slower.
    """

    __slots__ = ("_literals",)

    def __init__(self, literals: Iterable[str] = ()) -> None:
        self._literals: set[str] = set()
        for literal in literals:
            self.register(literal)

    def register(self, secret: str | None) -> None:
        """Register an exact value to redact. Short or empty values are ignored."""
        if secret and len(secret) >= _MIN_LITERAL_LENGTH:
            self._literals.add(secret)

    def forget(self, secret: str) -> None:
        self._literals.discard(secret)

    def redact_text(self, text: str) -> str:
        for literal in self._literals:
            if literal in text:
                text = text.replace(literal, REDACTED)
        for pattern in _PATTERNS:
            if "name" in pattern.groupindex:
                text = pattern.sub(lambda m: f"{m.group('name')}={REDACTED}", text)
            else:
                text = pattern.sub(REDACTED, text)
        return text

    def redact(self, value: Any, *, _depth: int = 0) -> Any:
        """Recursively redact strings inside common containers.

        Depth is capped because log payloads occasionally contain cyclic or
        pathologically nested objects, and a logging call must never be the
        thing that raises.
        """
        if _depth > 6:
            return value
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            out: dict[Any, Any] = {}
            for key, item in value.items():
                if isinstance(key, str) and key.lower() in _SENSITIVE_KEY_NAMES:
                    out[key] = REDACTED
                else:
                    out[key] = self.redact(item, _depth=_depth + 1)
            return out
        if isinstance(value, (list, tuple, set)):
            redacted = [self.redact(item, _depth=_depth + 1) for item in value]
            return type(value)(redacted) if not isinstance(value, set) else set(redacted)
        return value
