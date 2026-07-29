"""Runtime descriptor — how the Rust supervisor discovers the sidecar port.

The sidecar binds an ephemeral port (DEC-004), so the port is not knowable in
advance. Two discovery paths, because each fails differently:

* ``runtime.json`` in the per-user runtime directory, mode 0600.
* A single ``MITTA_READY <port>`` line on stdout.

Rust uses whichever arrives first. The file survives a missed stdout read; the
stdout line survives a stale or unreadable file.

**stdout carries only this line.** Log output goes to stderr and the log file
(`telemetry/logging.py`), because a log line on stdout would corrupt the
handshake the supervisor is parsing.
"""

from __future__ import annotations

import errno
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from mitta.ids import now_ms
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)

READY_PREFIX = "MITTA_READY"


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    pid: int
    port: int
    host: str
    api_version: str
    started_at: int

    @classmethod
    def create(cls, port: int, host: str, api_version: str) -> RuntimeDescriptor:
        return cls(
            pid=os.getpid(),
            port=port,
            host=host,
            api_version=api_version,
            started_at=now_ms(),
        )


def write_descriptor(path: Path, descriptor: RuntimeDescriptor) -> None:
    """Write the descriptor atomically, user-readable only.

    Written to a temporary file and renamed, so the supervisor never observes a
    half-written descriptor — a truncated read here would have it connecting to
    a nonsense port. The file is created 0600 *before* content is written, not
    chmod'd after, which would leave a window where it is world-readable.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(descriptor), handle)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    log.debug("runtime.descriptor_written", extra={"path": str(path), "port": descriptor.port})


def announce_ready(port: int) -> None:
    """Emit the readiness line the supervisor parses. stdout, flushed, once."""
    sys.stdout.write(f"{READY_PREFIX} {port}\n")
    sys.stdout.flush()


def remove_descriptor(path: Path) -> None:
    path.unlink(missing_ok=True)


def read_descriptor(path: Path) -> RuntimeDescriptor | None:
    """Read a descriptor, returning None if it is absent, corrupt or stale.

    A descriptor whose PID is no longer alive is stale — a previous sidecar was
    killed without cleanup. Returning None rather than the stale record stops a
    second instance from assuming the port is taken and refusing to start.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        descriptor = RuntimeDescriptor(**raw)
    except TypeError:
        return None
    return descriptor if _process_alive(descriptor.pid) else None


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        # EPERM means it exists but belongs to another user — alive either way.
        return exc.errno == errno.EPERM
    return True
