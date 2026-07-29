"""Prefixed ULID generation (DATABASE_DESIGN.md §1).

ULIDs rather than UUID4 because they sort lexicographically by creation time,
which means `ORDER BY id` is chronological and a B-tree index on them stays
dense instead of scattering inserts across the whole keyspace.

Implemented here rather than pulled from PyPI: it is forty lines, it removes a
supply-chain dependency from a security-sensitive local application, and the
monotonic-within-millisecond guarantee below is one that several published
implementations do not provide.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Final

_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32, no I L O U
_ENCODED_LENGTH: Final = 26
_RANDOM_BITS: Final = 80
_TIME_BITS: Final = 48

_lock = threading.Lock()
_last_ms: int = -1
_last_random: int = 0


def _encode(value: int, length: int) -> str:
    chars = [""] * length
    for i in range(length - 1, -1, -1):
        chars[i] = _ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(chars)


def ulid(timestamp_ms: int | None = None) -> str:
    """Generate a 26-character ULID.

    Monotonic within a millisecond: two ULIDs created in the same millisecond
    still sort in creation order, because the random component is incremented
    rather than redrawn. Without this, ids created in the same tick sort
    arbitrarily, and "most recent message" becomes non-deterministic under load.
    """
    global _last_ms, _last_random

    now = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    with _lock:
        if now == _last_ms:
            _last_random += 1
            if _last_random >= (1 << _RANDOM_BITS):  # pragma: no cover - needs 2^80 ids/ms
                now += 1
                _last_ms = now
                _last_random = secrets.randbits(_RANDOM_BITS)
        else:
            _last_ms = now
            _last_random = secrets.randbits(_RANDOM_BITS)
        randomness = _last_random
        timestamp = _last_ms

    combined = (timestamp << _RANDOM_BITS) | randomness
    return _encode(combined, _ENCODED_LENGTH)


def prefixed(prefix: str, timestamp_ms: int | None = None) -> str:
    """Return ``<prefix>_<ULID>`` — e.g. ``mem_01HQ8…``.

    The prefix makes a stray identifier in a log line or a bug report
    self-describing, which is worth three bytes.
    """
    return f"{prefix}_{ulid(timestamp_ms)}"


# Canonical prefixes. Defined here so they cannot drift between call sites.
MEMORY: Final = "mem"
CONVERSATION: Final = "cnv"
MESSAGE: Final = "msg"
TURN: Final = "trn"
PROJECT: Final = "prj"
PLAN: Final = "pln"
TASK: Final = "tsk"
PERSON: Final = "per"
EPISODE: Final = "epi"
INVOCATION: Final = "inv"
APPROVAL: Final = "apv"
PLUGIN: Final = "plg"
REQUEST: Final = "req"
NOTIFICATION: Final = "ntf"
AUDIT: Final = "aud"
LLM_REQUEST: Final = "llm"
SCHEDULE: Final = "sch"


def now_ms() -> int:
    """Current UTC time in epoch milliseconds — the project's timestamp unit."""
    return int(time.time() * 1000)
