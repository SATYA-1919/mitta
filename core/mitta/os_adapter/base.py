"""OS Adapter contract.

Everything platform-specific lives behind this protocol. Per R1 no `osascript`
call, no `~/Library` path and no AppleScript string may appear above this
boundary — `importlinter.ini` enforces the mechanical half of that.

The adapter is deliberately narrow in Phase 3: only what the backend foundation
needs, which is path resolution. It grows in Phase 10. A protocol that declares
methods nobody implements is worse than one that grows, because the stub methods
become a to-do list nobody reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class OSAdapter(Protocol):
    """Platform capabilities the rest of the runtime may rely on."""

    @property
    def platform_name(self) -> str:
        """Stable identifier, e.g. ``"macos"``. Used in logs and diagnostics."""
        ...

    def default_storage_root(self) -> Path:
        """Per-user application data directory, following platform convention.

        The caller may override this with a configured path; this is only the
        default. Never called for a path the user has already chosen.
        """
        ...

    def default_runtime_dir(self) -> Path:
        """Directory for ephemeral per-session files (the runtime descriptor).

        Distinct from the storage root because its contents do not survive a
        reboot and must never be backed up — it holds the port and, indirectly,
        the liveness of an authenticated local socket.
        """
        ...

    def default_log_dir(self) -> Path:
        """Directory for rotated structured logs."""
        ...

    def open_application(self, name: str) -> None:
        """Launch an application by name.

        On the protocol rather than in a tool because R1 forbids any platform
        assumption above this boundary, and `mitta.tools` is barred from
        importing this module at all (DEC-079). A tool receives this as an
        injected callable.

        The name is validated by the caller. Implementations must still treat it
        as untrusted — this is the point where a string becomes a process.
        """
        ...
