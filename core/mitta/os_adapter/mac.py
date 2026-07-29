"""macOS implementation of the OS Adapter.

This is the only module in the Python runtime permitted to encode macOS
filesystem conventions.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

APP_NAME = "MITTA"


class MacAdapter:
    """macOS Sequoia (15) and newer, Apple Silicon primary."""

    @property
    def platform_name(self) -> str:
        return "macos"

    def default_storage_root(self) -> Path:
        return Path.home() / "Library" / "Application Support" / APP_NAME

    def default_runtime_dir(self) -> Path:
        """Prefer the per-user, per-boot ``TMPDIR`` that launchd provides.

        macOS gives every user a private, mode-0700 temporary directory that is
        cleared between boots. That is a better home for the runtime descriptor
        than ``/tmp``, which is world-readable and would expose the sidecar port
        to every local process — the descriptor is also mode-0600, but there is
        no reason to rely on only one control.
        """
        tmpdir = os.environ.get("TMPDIR")
        base = Path(tmpdir) if tmpdir else Path("/tmp")  # noqa: S108
        return base / APP_NAME

    def default_log_dir(self) -> Path:
        return Path.home() / "Library" / "Logs" / APP_NAME

    def open_application(self, name: str) -> None:
        """`open -a <name>`.

        Argument list, never a shell string. `subprocess.run` with a list does
        not invoke a shell, so an application name containing `;` or backticks
        is passed through as a literal name — which simply fails to match an app
        — rather than being interpreted.

        `check=True` so a missing application raises here instead of silently
        succeeding and leaving the user waiting for a window.
        """
        subprocess.run(  # noqa: S603 - list form, no shell
            ["/usr/bin/open", "-a", name],
            check=True,
            capture_output=True,
            timeout=15,
        )
