"""End-to-end test of the sidecar as a real process.

Everything else in the suite constructs the app in-process. This spawns the
actual binary the Rust supervisor will spawn and exercises the contract between
them: the readiness handshake, the runtime descriptor, loopback binding,
authentication over the wire, and graceful shutdown on SIGTERM.

It exists because the whole class of bugs it catches is invisible in-process.
The `extra={"name": ...}` logging collision that broke startup passed 96 unit
tests and failed on the first real boot.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import pytest

TOKEN = "integration-session-token-0123456789"
BOOT_TIMEOUT_S = 30.0
CORE_DIR = Path(__file__).resolve().parents[2]


class Sidecar(NamedTuple):
    process: subprocess.Popen[str]
    port: int
    root: Path

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _get(url: str, token: str | None) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(url)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.fixture(scope="module")
def sidecar(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Sidecar]:
    root = tmp_path_factory.mktemp("sidecar")
    env = {
        **os.environ,
        "MITTA_SESSION_TOKEN": TOKEN,
        "MITTA_STORAGE_ROOT": str(root / "storage"),
        "MITTA_RUNTIME_DIR": str(root / "runtime"),
        "MITTA_LOG_DIR": str(root / "logs"),
        "MITTA_LOGGING__CONSOLE": "false",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "mitta"],
        cwd=CORE_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert process.stdout is not None
    deadline = time.monotonic() + BOOT_TIMEOUT_S
    line = ""
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if line or process.poll() is not None:
            break
    if not line.startswith("MITTA_READY "):
        process.kill()
        stderr = process.stderr.read() if process.stderr else ""
        pytest.fail(f"Sidecar did not announce readiness.\nstdout: {line!r}\nstderr:\n{stderr}")

    try:
        yield Sidecar(process=process, port=int(line.split()[1]), root=root)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        # Popen does not close its pipes on exit unless used as a context
        # manager. Leaving them open leaks file descriptors across the session.
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


# -- handshake --------------------------------------------------------------- #


def test_bound_port_is_ephemeral(sidecar: Sidecar) -> None:
    """A fixed port could be squatted by another local process (DEC-004)."""
    assert sidecar.port > 1024


def test_descriptor_matches_the_announced_port(sidecar: Sidecar) -> None:
    descriptor = json.loads((sidecar.root / "runtime" / "runtime.json").read_text())
    assert descriptor["port"] == sidecar.port
    assert descriptor["pid"] == sidecar.process.pid
    assert descriptor["api_version"] == "1"


def test_descriptor_is_not_world_readable(sidecar: Sidecar) -> None:
    """It holds the port of an authenticated local socket."""
    path = sidecar.root / "runtime" / "runtime.json"
    assert path.stat().st_mode & 0o077 == 0


# -- authentication over the wire -------------------------------------------- #


def test_health_is_reachable_without_a_token(sidecar: Sidecar) -> None:
    status, body = _get(f"{sidecar.base_url}/health", None)
    assert status == 200
    assert body["status"] == "ok"


def test_status_rejects_an_unauthenticated_request(sidecar: Sidecar) -> None:
    status, body = _get(f"{sidecar.base_url}/v1/status", None)
    assert status == 401
    assert body["error"]["code"] == "auth.missing_token"


def test_status_rejects_a_wrong_token(sidecar: Sidecar) -> None:
    status, body = _get(f"{sidecar.base_url}/v1/status", "not-the-token")
    assert status == 401
    assert body["error"]["code"] == "auth.invalid_token"


def test_status_reports_ready_with_the_real_token(sidecar: Sidecar) -> None:
    status, body = _get(f"{sidecar.base_url}/v1/status", TOKEN)
    assert status == 200
    assert body["ready"] is True
    assert body["schema_version"] >= 1
    assert body["platform"] == "macos"


# -- on-disk state ------------------------------------------------------------ #


def test_database_is_created_user_only(sidecar: Sidecar) -> None:
    database = sidecar.root / "storage" / "mitta.db"
    assert database.exists()
    assert database.stat().st_mode & 0o077 == 0


def test_logs_are_json_lines(sidecar: Sidecar) -> None:
    lines = (sidecar.root / "logs" / "mitta.log").read_text().splitlines()
    assert lines
    for line in lines:
        parsed = json.loads(line)
        assert {"ts", "level", "logger", "message"} <= set(parsed)


def test_the_session_token_never_reaches_the_log(sidecar: Sidecar) -> None:
    """DEC-017, verified against a real boot rather than a synthetic record."""
    assert TOKEN not in (sidecar.root / "logs" / "mitta.log").read_text()


# -- shutdown ----------------------------------------------------------------- #


def test_sigterm_shuts_down_cleanly_and_removes_the_descriptor(sidecar: Sidecar) -> None:
    """Runs last in the module: an orphaned sidecar holding an open agent loop
    is a real failure mode, so exit must be clean and observable."""
    descriptor = sidecar.root / "runtime" / "runtime.json"
    assert descriptor.exists()

    sidecar.process.send_signal(signal.SIGTERM)
    assert sidecar.process.wait(timeout=15) == 0
    assert not descriptor.exists()
