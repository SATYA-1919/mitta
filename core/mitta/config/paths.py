"""Resolved filesystem layout.

Every path the runtime uses is resolved here, once, from the OS adapter and the
user's configuration. Nothing else in the codebase joins application paths — a
second place that builds ``storage_root / "vectors"`` is a second place that has
to be found when the layout changes.

Layout is ARCHITECTURE.md §10.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mitta.config.settings import Settings
from mitta.os_adapter.base import OSAdapter


@dataclass(frozen=True, slots=True)
class Paths:
    """Resolved, absolute, existing directories."""

    storage_root: Path
    runtime_dir: Path
    log_dir: Path

    @property
    def database(self) -> Path:
        return self.storage_root / "mitta.db"

    @property
    def vectors(self) -> Path:
        return self.storage_root / "vectors"

    @property
    def models(self) -> Path:
        return self.storage_root / "models"

    @property
    def config_dir(self) -> Path:
        return self.storage_root / "config"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def projects(self) -> Path:
        return self.storage_root / "projects"

    @property
    def screenshots(self) -> Path:
        return self.storage_root / "screenshots"

    @property
    def voice_cache(self) -> Path:
        return self.storage_root / "voice_cache"

    @property
    def plugins(self) -> Path:
        return self.storage_root / "plugins"

    @property
    def backups(self) -> Path:
        return self.storage_root / "backups"

    @property
    def runtime_descriptor(self) -> Path:
        return self.runtime_dir / "runtime.json"

    def managed_directories(self) -> tuple[Path, ...]:
        return (
            self.storage_root,
            self.vectors,
            self.models,
            self.config_dir,
            self.projects,
            self.screenshots,
            self.voice_cache,
            self.plugins,
            self.backups,
            self.log_dir,
            self.runtime_dir,
        )

    def ensure(self) -> None:
        """Create every managed directory, user-only.

        Mode 0700 throughout. The storage root holds the memory database, the
        LLM payload record and the audit log; on a shared machine, a
        world-readable default would undo R5 without a single line of code
        being wrong.
        """
        for directory in self.managed_directories():
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)


def resolve_paths(settings: Settings, adapter: OSAdapter) -> Paths:
    """Resolve the layout from configuration, falling back to platform defaults."""
    storage_root = settings.storage_root or adapter.default_storage_root()
    runtime_dir = settings.runtime_dir or adapter.default_runtime_dir()
    log_dir = settings.log_dir or adapter.default_log_dir()
    return Paths(
        storage_root=storage_root.expanduser().resolve(),
        runtime_dir=runtime_dir.expanduser().resolve(),
        log_dir=log_dir.expanduser().resolve(),
    )
