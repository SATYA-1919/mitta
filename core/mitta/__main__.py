"""Sidecar entry point.

Binds an ephemeral loopback port, announces readiness, serves, and cleans up.

The socket is created here rather than handed to uvicorn as a port number,
because the port must be *known* before the server starts in order to announce
it. Binding to port 0 and asking uvicorn afterwards introduces a race the
supervisor would lose on a fast machine.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import socket
import sys
from pathlib import Path
from typing import Any

import uvicorn

from mitta.api.app import API_VERSION
from mitta.bootstrap import Runtime, build_runtime
from mitta.errors import MittaError
from mitta.runtime.descriptor import (
    RuntimeDescriptor,
    announce_ready,
    remove_descriptor,
    write_descriptor,
)
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)


def _bind(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    sock.set_inheritable(True)
    return sock


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="mitta-core", description="MITTA agent runtime")
    parser.add_argument("--config", type=Path, default=None, help="Path to config.json")
    parser.add_argument("--storage-root", type=Path, default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None, help="0 for ephemeral (default)")
    parser.add_argument("--dev", action="store_true", help="Enable dev mode")
    # Deliberately no --session-token: a token on the command line is visible in
    # the process list to every local user (DEC-017). It arrives via the
    # environment, which is per-process and not world-readable on macOS.
    return parser.parse_args(argv)


async def _serve(runtime: Runtime, sock: socket.socket, descriptor_path: Path) -> None:
    config = uvicorn.Config(
        runtime.app,
        log_config=None,  # logging is already configured, and ours redacts
        access_log=False,  # RequestContextMiddleware does this, with redaction
        lifespan="on",
        timeout_graceful_shutdown=int(runtime.settings.server.shutdown_grace_seconds),
    )
    server = uvicorn.Server(config)

    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        log.info("runtime.signal_received")
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_stop)

    try:
        await server.serve(sockets=[sock])
    finally:
        remove_descriptor(descriptor_path)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    overrides: dict[str, Any] = {}
    if args.storage_root is not None:
        overrides["storage_root"] = args.storage_root
    if args.dev:
        overrides["dev_mode"] = True

    try:
        runtime = build_runtime(config_file=args.config, **overrides)
    except MittaError as exc:
        # Startup failures land before logging may be configured, so they go to
        # stderr in a form the supervisor can surface to the user.
        print(f"mitta-core: {exc.code}: {exc.message}", file=sys.stderr)
        return 2

    host = args.host or runtime.settings.server.host
    port = args.port if args.port is not None else runtime.settings.server.port

    try:
        sock = _bind(host, port)
    except OSError as exc:
        log.error("runtime.bind_failed", extra={"host": host, "port": port, "error": str(exc)})
        runtime.shutdown()
        return 3

    bound_port = sock.getsockname()[1]
    descriptor_path = runtime.paths.runtime_descriptor
    write_descriptor(
        descriptor_path,
        RuntimeDescriptor.create(bound_port, host, API_VERSION),
    )
    announce_ready(bound_port)
    log.info("runtime.listening", extra={"host": host, "port": bound_port})

    try:
        asyncio.run(_serve(runtime, sock, descriptor_path))
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        sock.close()
        runtime.shutdown()
        log.info("runtime.stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
