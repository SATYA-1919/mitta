#!/usr/bin/env python3
"""Fail the build if a credential is about to be committed.

Exists because of a real near-miss: `.env.example` is committed and `.env` is
not, the names differ by eight characters, and pasting into the wrong one
publishes the key. Documentation does not prevent that. This does.

Run by `make check` and safe to wire into a pre-commit hook.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Files that are committed and must therefore never hold a value.
TEMPLATE_FILES = (".env.example",)

# Any of these appearing with a non-empty value in a tracked file is a finding.
KEY_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?(\w*(?:API_KEY|SECRET|TOKEN|PASSWORD)\w*)\s*=\s*(.+)$",
    re.IGNORECASE,
)

# Provider key prefixes, which are recognisable regardless of variable name.
KEY_SHAPES = (
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),          # Groq
    re.compile(r"sk-or-v1-[A-Za-z0-9]{20,}"),     # OpenRouter
    re.compile(r"sk-[A-Za-z0-9]{32,}"),           # OpenAI-style
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"),    # Anthropic
)

# Values that are obviously not real credentials.
PLACEHOLDER = re.compile(
    r"^(|\"\"|''|<[^>]*>|your[-_ ].*|xxx+|fake.*|dummy.*|example.*|changeme|\.\.\.)$",
    re.IGNORECASE,
)

# A file may opt out by declaring this marker, with a reason. Explicit and
# greppable, so adding one is a decision a reviewer can see — as opposed to a
# blanket exemption for `tests/`, which would hide a real key pasted into a
# fixture while debugging.
ALLOW_MARKER = re.compile(r"check-no-secrets:\s*allow\b")

findings: list[str] = []


def report(path: str, line_no: int, message: str) -> None:
    findings.append(f"{path}:{line_no}: {message}")


def check_template(path: Path) -> None:
    """A committed template must have every key slot empty."""
    if not path.is_file():
        return
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if raw.lstrip().startswith("#"):
            continue
        match = KEY_ASSIGNMENT.match(raw)
        if match is None:
            continue
        name, value = match.group(1), match.group(2).strip().strip("\"'")
        if value and not PLACEHOLDER.match(value):
            report(
                str(path.relative_to(ROOT)),
                line_no,
                f"{name} has a value. This file is COMMITTED — put real keys in .env instead.",
            )


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return []
    return [ROOT / name for name in result.stdout.splitlines()]


def check_tracked_for_key_shapes(path: Path) -> None:
    """Scan committed files for anything shaped like a provider key."""
    if path.name == Path(__file__).name:
        return  # this file necessarily contains the patterns
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return  # binary or unreadable

    if ALLOW_MARKER.search(text):
        return

    for line_no, line in enumerate(text.splitlines(), 1):
        for shape in KEY_SHAPES:
            if shape.search(line):
                report(
                    str(path.relative_to(ROOT)),
                    line_no,
                    "looks like a provider API key in a tracked file",
                )
                return


def main() -> int:
    for name in TEMPLATE_FILES:
        check_template(ROOT / name)

    for path in tracked_files():
        if path.is_file():
            check_tracked_for_key_shapes(path)

    if findings:
        print("✗ Possible credential in a committed file:\n", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        print(
            "\n  Real keys belong in .env (gitignored) or the macOS Keychain.\n"
            "  If a key was ever committed, rotate it — removing the commit is not enough.",
            file=sys.stderr,
        )
        return 1

    print("✓ no credentials in committed files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
