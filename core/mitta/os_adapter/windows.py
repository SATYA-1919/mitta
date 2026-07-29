"""Windows adapter — contract documented, implementation deferred (DEC-013, R1).

This file exists so the abstraction stays honest. Every method states what a
real implementation must return, so the Windows port is a matter of filling in
known blanks rather than rediscovering the contract.

Do not "helpfully" implement these with a `%APPDATA%` guess. An untested
implementation is worse than a loud failure: it would let Windows-specific bugs
accumulate silently behind a boundary nobody is exercising.
"""

from __future__ import annotations

from pathlib import Path

_DEFERRED = "Windows support is deferred (DEC-013). macOS is the only implemented platform."


class WindowsAdapter:
    """Stub. Raises on every call."""

    @property
    def platform_name(self) -> str:
        return "windows"

    def default_storage_root(self) -> Path:
        # Should return %LOCALAPPDATA%\MITTA
        raise NotImplementedError(_DEFERRED)

    def default_runtime_dir(self) -> Path:
        # Should return %LOCALAPPDATA%\Temp\MITTA, ACL-restricted to the user.
        raise NotImplementedError(_DEFERRED)

    def default_log_dir(self) -> Path:
        # Should return %LOCALAPPDATA%\MITTA\Logs
        raise NotImplementedError(_DEFERRED)

    def open_application(self, name: str) -> None:
        raise NotImplementedError(_DEFERRED)
