"""Composition root.

The **only** place collaborators are wired together. Every other module receives
what it needs through its constructor and never reaches out for a global.

This is not ceremony. Two guarantees in the architecture are enforced by
construction rather than by convention, and both live here:

* The Tool Manager is built without a reference to the OS Adapter — the policy
  engine holds it (ARCHITECTURE.md §3). Bypassing policy therefore requires
  editing this file, which is reviewed, and the import contract, which is
  checked (DEC-029).
* Nothing above the OS Adapter ever learns a platform-specific path, because
  paths are resolved once, here, from the adapter.

Landing in Phase 3: config, telemetry, OS adapter, persistence, API.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from mitta.api.app import create_app
from mitta.config.paths import Paths, resolve_paths
from mitta.config.settings import Settings, load_settings
from mitta.os_adapter.base import OSAdapter
from mitta.os_adapter.factory import create_os_adapter
from mitta.persistence.database import Database
from mitta.persistence.migrations import migrate
from mitta.telemetry.logging import get_logger, setup_logging
from mitta.telemetry.redaction import SecretRedactor

log = get_logger(__name__)


@dataclass(slots=True)
class Runtime:
    """Everything the process owns. Constructed once, disposed once."""

    settings: Settings
    paths: Paths
    os_adapter: OSAdapter
    database: Database
    redactor: SecretRedactor
    app: FastAPI

    def shutdown(self) -> None:
        self.database.close()


def build_runtime(
    *,
    config_file: Path | None = None,
    platform: str | None = None,
    **overrides: Any,
) -> Runtime:
    """Construct the full runtime.

    Order matters and is not arbitrary:

    1. OS adapter — nothing can resolve a path before it exists.
    2. Settings, then paths, then directories.
    3. Redactor, seeded with the session token, then logging. Logging is
       configured **before** anything else can log, so no line is ever emitted
       through an unredacted handler.
    4. Database, then migrations.
    5. The FastAPI app last, over collaborators that are already live.
    """
    os_adapter = create_os_adapter(platform)

    bootstrap_settings = load_settings(**overrides)
    bootstrap_paths = resolve_paths(bootstrap_settings, os_adapter)

    # The config file lives under the storage root, which the settings choose —
    # so settings are loaded twice: once to find the file, once including it.
    settings = load_settings(config_file or bootstrap_paths.config_file, **overrides)
    paths = resolve_paths(settings, os_adapter)
    paths.ensure()

    redactor = SecretRedactor()
    redactor.register(settings.session_token)
    setup_logging(settings.logging, paths.log_dir, redactor)

    log.info(
        "runtime.starting",
        extra={
            "platform": os_adapter.platform_name,
            "storage_root": str(paths.storage_root),
            "dev_mode": settings.dev_mode,
        },
    )

    database = Database(paths.database, settings.database)
    database.connect()
    migrate(
        database,
        backup_dir=paths.backups if settings.database.backup_before_migration else None,
    )

    app = create_app(
        settings=settings,
        paths=paths,
        database=database,
        os_adapter=os_adapter,
    )

    return Runtime(
        settings=settings,
        paths=paths,
        os_adapter=os_adapter,
        database=database,
        redactor=redactor,
        app=app,
    )
