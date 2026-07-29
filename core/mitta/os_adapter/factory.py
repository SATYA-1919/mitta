"""Platform detection and adapter construction."""

from __future__ import annotations

import sys

from mitta.errors import ConfigError
from mitta.os_adapter.base import OSAdapter
from mitta.os_adapter.mac import MacAdapter
from mitta.os_adapter.windows import WindowsAdapter


def create_os_adapter(platform: str | None = None) -> OSAdapter:
    """Return the adapter for `platform`, defaulting to the running platform.

    The explicit `platform` argument exists for tests, which must be able to
    assert that the Windows stub raises without running on Windows.
    """
    name = platform or sys.platform
    if name == "darwin":
        return MacAdapter()
    if name in ("win32", "cygwin"):
        return WindowsAdapter()
    raise ConfigError(
        f"Unsupported platform: {name!r}. MITTA targets macOS; Windows is deferred.",
        details={"platform": name},
    )
