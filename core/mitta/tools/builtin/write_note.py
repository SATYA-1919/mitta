"""Write a note to a file.

The first `WRITE`-tier tool, and its job is as much to exercise the approval
path as to be useful. It is deliberately narrow: notes go in one directory,
under a validated filename, and nothing else on disk is reachable.

Scope is enforced by resolving the final path and checking it is still inside
the notes directory. Validating the *name* is not enough — `../../.ssh/config`
contains no suspicious characters and passes any reasonable name check, but
resolves outside. The check has to be on the resolved path, after the join.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

from mitta.tools.base import Risk, ToolResult, ToolSpec

_FILENAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
MAX_BYTES: Final = 256 * 1024


class WriteNoteTool:
    def __init__(self, notes_dir: Path) -> None:
        self._dir = notes_dir

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="write_note",
            description=(
                "Save a note to a file in the user's MITTA notes folder. Use when "
                "asked to write something down or save something for later."
            ),
            risk=Risk.WRITE,
            parameters={
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "e.g. 'ideas.md'"},
                    "content": {"type": "string"},
                },
                "required": ["filename", "content"],
            },
            describer=self.describe,
        )

    def describe(self, params: dict[str, Any]) -> str:
        content = str(params.get("content", ""))
        lines = content.count("\n") + 1
        return (
            f"Write {len(content)} characters ({lines} lines) "
            f"to {params.get('filename')!r} in your notes folder?"
        )

    async def run(self, params: dict[str, Any]) -> ToolResult:
        filename = str(params.get("filename", "")).strip()
        content = str(params.get("content", ""))

        if not _FILENAME.match(filename):
            return ToolResult.failure(f"{filename!r} is not a valid filename.")
        if len(content.encode("utf-8")) > MAX_BYTES:
            return ToolResult.failure("That note is too large.")

        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = (self._dir / filename).resolve()

        # The check that matters. A name-only check passes `../../.ssh/config`,
        # which contains nothing suspicious and resolves outside the sandbox.
        if not target.is_relative_to(self._dir.resolve()):
            return ToolResult.failure("That path is outside the notes folder.")

        try:
            existed = target.exists()
            target.write_text(content, encoding="utf-8")
            target.chmod(0o600)
        except OSError as exc:
            return ToolResult.failure(f"Could not write the note: {exc}")

        return ToolResult(
            ok=True,
            content=f"{'Updated' if existed else 'Saved'} {filename}.",
            detail={"path": str(target), "bytes": len(content), "overwrote": existed},
        )
