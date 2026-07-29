"""Persistence layer — SQLite is the system of record (DEC-005)."""

from mitta.persistence.database import Database
from mitta.persistence.migrations import current_version, migrate

__all__ = ["Database", "current_version", "migrate"]
