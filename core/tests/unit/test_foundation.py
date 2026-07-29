"""IDs, configuration, path resolution and the OS adapter boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mitta import ids
from mitta.config.paths import Paths, resolve_paths
from mitta.config.settings import Settings, load_settings
from mitta.errors import ConfigError
from mitta.os_adapter.factory import create_os_adapter
from mitta.os_adapter.mac import MacAdapter
from mitta.os_adapter.windows import WindowsAdapter
from mitta.runtime.descriptor import (
    RuntimeDescriptor,
    read_descriptor,
    write_descriptor,
)

# -- ids -------------------------------------------------------------------- #


def test_ulid_has_canonical_length() -> None:
    assert len(ids.ulid()) == 26


def test_ulids_sort_chronologically() -> None:
    early = ids.ulid(1_700_000_000_000)
    late = ids.ulid(1_800_000_000_000)
    assert early < late


def test_ulids_are_monotonic_within_a_millisecond() -> None:
    """Without this, "most recent message" is non-deterministic under load."""
    batch = [ids.ulid(1_700_000_000_000) for _ in range(500)]
    assert batch == sorted(batch)
    assert len(set(batch)) == len(batch)


def test_prefixed_ids_are_self_describing() -> None:
    value = ids.prefixed(ids.MEMORY)
    assert value.startswith("mem_")
    assert len(value) == 30


# -- OS adapter ------------------------------------------------------------- #


def test_factory_returns_the_mac_adapter_on_darwin() -> None:
    assert isinstance(create_os_adapter("darwin"), MacAdapter)


def test_factory_returns_the_windows_stub_on_win32() -> None:
    assert isinstance(create_os_adapter("win32"), WindowsAdapter)


def test_unsupported_platform_is_rejected() -> None:
    with pytest.raises(ConfigError):
        create_os_adapter("plan9")


@pytest.mark.parametrize(
    "method", ["default_storage_root", "default_runtime_dir", "default_log_dir"]
)
def test_windows_adapter_raises_not_implemented(method: str) -> None:
    """R1: the stub documents the contract; it must never silently guess."""
    with pytest.raises(NotImplementedError):
        getattr(WindowsAdapter(), method)()


def test_mac_paths_follow_platform_convention() -> None:
    adapter = MacAdapter()
    assert adapter.default_storage_root().parts[-3:] == (
        "Library",
        "Application Support",
        "MITTA",
    )
    assert adapter.platform_name == "macos"


# -- settings --------------------------------------------------------------- #


def test_settings_default_to_ephemeral_port() -> None:
    """A fixed port can be squatted by another local process (DEC-004)."""
    assert Settings().server.port == 0


def test_settings_repr_never_contains_the_token() -> None:
    settings = Settings(session_token="super-secret-token-value")
    assert "super-secret-token-value" not in repr(settings)
    assert "super-secret-token-value" not in str(settings)


def test_environment_overrides_the_config_file(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"dev_mode": False}), encoding="utf-8")
    monkeypatch.setenv("MITTA_DEV_MODE", "true")
    assert load_settings(config).dev_mode is True


def test_nested_settings_load_from_the_file(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"memory": {"decay_lambda": 0.05}}), encoding="utf-8")
    assert load_settings(config).memory.decay_lambda == 0.05


def test_a_token_in_the_config_file_is_rejected(tmp_path: Path) -> None:
    """DEC-017: a token in a file means it was written to disk."""
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"session_token": "leaked"}), encoding="utf-8")
    with pytest.raises(ConfigError, match="never appear in a configuration file"):
        load_settings(config)


def test_malformed_config_fails_loudly(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(config)


def test_missing_config_file_uses_defaults(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "absent.json").dev_mode is False


def test_invalid_values_are_rejected() -> None:
    with pytest.raises(Exception, match="intensity"):
        Settings(personality={"intensity": 5.0})  # type: ignore[arg-type]


# -- paths ------------------------------------------------------------------ #


def test_configured_storage_root_wins_over_the_platform_default(tmp_path: Path) -> None:
    settings = Settings(storage_root=tmp_path / "custom")
    paths = resolve_paths(settings, MacAdapter())
    assert paths.storage_root == (tmp_path / "custom").resolve()


def test_managed_directories_are_created_user_only(tmp_path: Path) -> None:
    """The memory database, LLM payloads and audit log all live here (R5)."""
    paths = Paths(
        storage_root=tmp_path / "s",
        runtime_dir=tmp_path / "r",
        log_dir=tmp_path / "l",
    )
    paths.ensure()
    for directory in paths.managed_directories():
        assert directory.is_dir()
        assert directory.stat().st_mode & 0o077 == 0


def test_ensure_is_idempotent(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "s", tmp_path / "r", tmp_path / "l")
    paths.ensure()
    paths.ensure()


# -- runtime descriptor ------------------------------------------------------ #


def test_descriptor_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    descriptor = RuntimeDescriptor.create(52341, "127.0.0.1", "1")
    write_descriptor(path, descriptor)
    assert read_descriptor(path) == descriptor


def test_descriptor_is_user_only(tmp_path: Path) -> None:
    """It holds the port of an authenticated local socket."""
    path = tmp_path / "runtime.json"
    write_descriptor(path, RuntimeDescriptor.create(1234, "127.0.0.1", "1"))
    assert path.stat().st_mode & 0o077 == 0


def test_descriptor_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    write_descriptor(path, RuntimeDescriptor.create(1234, "127.0.0.1", "1"))
    assert list(tmp_path.glob("*.tmp")) == []


def test_stale_descriptor_reads_as_absent(tmp_path: Path) -> None:
    """A dead PID must not make a new instance think the port is taken."""
    path = tmp_path / "runtime.json"
    write_descriptor(path, RuntimeDescriptor(pid=999_999, port=1, host="127.0.0.1",
                                             api_version="1", started_at=0))
    assert read_descriptor(path) is None


def test_corrupt_descriptor_reads_as_absent(tmp_path: Path) -> None:
    path = tmp_path / "runtime.json"
    path.write_text("{ truncated", encoding="utf-8")
    assert read_descriptor(path) is None


def test_absent_descriptor_reads_as_none(tmp_path: Path) -> None:
    assert read_descriptor(tmp_path / "nothing.json") is None
