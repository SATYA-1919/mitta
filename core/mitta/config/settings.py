"""Configuration model.

Precedence, highest first:

1. Environment variables (``MITTA_*``) — how the Rust supervisor injects
   per-session values such as the session token and storage root.
2. ``config/config.json`` under the storage root — the user's settings.
3. Defaults declared here.

**Secrets are structurally absent from this model.** There is no `api_key`
field, no `secrets` section, and no way to add one without it being obvious in
review. API keys live in the macOS Keychain and are fetched over Channel C at
use time (DEC-017). A settings object that *could* hold a key is a settings
object that eventually gets serialised into a log line or a crash dump.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from mitta.errors import ConfigError

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class ServerSettings(BaseModel):
    """Loopback binding. Not configurable to a non-loopback host, by design."""

    host: Literal["127.0.0.1", "::1"] = "127.0.0.1"
    port: int = Field(
        default=0,
        ge=0,
        le=65535,
        description="0 means ephemeral — the OS assigns. Fixed ports are squattable (DEC-004).",
    )
    shutdown_grace_seconds: float = Field(default=10.0, gt=0)


class DatabaseSettings(BaseModel):
    busy_timeout_ms: int = Field(default=5_000, ge=0)
    cache_size_kib: int = Field(default=20_000, gt=0)
    mmap_size_bytes: int = Field(default=268_435_456, ge=0)
    read_pool_size: int = Field(default=4, ge=1, le=32)
    backup_before_migration: bool = True


class MemorySettings(BaseModel):
    """Retention and decay. Values are DATABASE_DESIGN.md §4.4 and §10."""

    decay_lambda: float = Field(default=0.015, gt=0, description="Half-life ≈ 46 days")
    forget_threshold: float = Field(default=0.05, ge=0.0, le=1.0)
    dedupe_similarity: float = Field(default=0.92, ge=0.0, le=1.0)
    retention_days_llm_requests: int = Field(default=30, ge=1)
    retention_days_audit: int = Field(default=365, ge=1)
    retention_days_tool_invocations: int = Field(default=90, ge=1)
    retention_days_forgotten: int = Field(default=90, ge=1)


class PersonalitySettings(BaseModel):
    """DEC-008, DEC-033. Register is derived per turn, never configured here."""

    enabled: bool = True
    intensity: float = Field(default=1.0, ge=0.0, le=1.0, description="0.0 is a no-op")
    profile: str = "mitta"


class LoggingSettings(BaseModel):
    level: LogLevel = "INFO"
    json_output: bool = True
    max_bytes: int = Field(default=10_485_760, gt=0)
    backup_count: int = Field(default=5, ge=0)
    console: bool = Field(
        default=True,
        description="stderr as well as file. stdout is reserved for the readiness handshake.",
    )


class Settings(BaseSettings):
    """Root configuration object. Constructed once, in the composition root."""

    model_config = SettingsConfigDict(
        env_prefix="MITTA_",
        env_nested_delimiter="__",
        extra="ignore",
        frozen=True,
    )

    storage_root: Path | None = Field(
        default=None,
        description="User-configurable (R4). None resolves to the OS adapter default.",
    )
    runtime_dir: Path | None = None
    log_dir: Path | None = None

    session_token: str | None = Field(
        default=None,
        description="Injected by the Rust supervisor at spawn. Never persisted.",
        repr=False,
    )
    allowed_origins: tuple[str, ...] = ("tauri://localhost", "http://tauri.localhost")

    dev_mode: bool = Field(
        default=False,
        description="Relaxes the origin check and enables docs. Never true in a release build.",
    )

    server: ServerSettings = Field(default_factory=ServerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    personality: PersonalitySettings = Field(default_factory=PersonalitySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    @field_validator("storage_root", "runtime_dir", "log_dir")
    @classmethod
    def _expand(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None

    def __repr__(self) -> str:
        """Never let a token reach a repr, a log line or a traceback frame."""
        return f"Settings(storage_root={self.storage_root!r}, dev_mode={self.dev_mode})"

    __str__ = __repr__


def _validate_config_file(config_file: Path) -> None:
    """Reject a malformed file, or one containing a secret, before it is loaded."""
    try:
        parsed = json.loads(config_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"Configuration file is not valid JSON: {config_file}",
            details={"path": str(config_file), "error": str(exc)},
            cause=exc,
        ) from exc
    if not isinstance(parsed, dict):
        raise ConfigError(
            f"Configuration file must contain a JSON object: {config_file}",
            details={"path": str(config_file)},
        )
    if "session_token" in parsed:
        # A token in the config file means it was written to disk, which DEC-017
        # forbids. Fail loudly rather than silently honouring it.
        raise ConfigError(
            "session_token must never appear in a configuration file (DEC-017). "
            "It is injected by the supervisor via the environment.",
            details={"path": str(config_file)},
        )


def load_settings(config_file: Path | None = None, /, **overrides: Any) -> Settings:
    """Build `Settings` from explicit overrides, the environment and a JSON file.

    Precedence, highest first: **explicit overrides → environment → file →
    defaults.**

    The file is supplied as a pydantic-settings *source* rather than as
    constructor keyword arguments. That distinction is the whole point: init
    kwargs have the highest precedence in pydantic-settings, so passing file
    contents that way would let a stale `config.json` silently override the
    environment — including the storage root the supervisor injected. As a
    source it also merges per-leaf, so `MITTA_MEMORY__DECAY_LAMBDA` overrides one
    nested value without discarding the rest of the file's `memory` block.
    """
    use_file = config_file is not None and config_file.exists()
    if use_file:
        assert config_file is not None
        _validate_config_file(config_file)

    class _ConfiguredSettings(Settings):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
            if use_file:
                sources.append(
                    JsonConfigSettingsSource(settings_cls, json_file=config_file, deep_merge=True)
                )
            return tuple(sources)

    return _ConfiguredSettings(**overrides)
