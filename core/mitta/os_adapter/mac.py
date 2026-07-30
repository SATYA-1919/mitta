"""macOS implementation of the OS Adapter.

This is the only module in the Python runtime permitted to encode macOS
filesystem conventions.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

APP_NAME = "MITTA"


class MacAdapter:
    """macOS Sequoia (15) and newer, Apple Silicon primary."""

    @property
    def platform_name(self) -> str:
        return "macos"

    def default_storage_root(self) -> Path:
        return Path.home() / "Library" / "Application Support" / APP_NAME

    def default_runtime_dir(self) -> Path:
        """Prefer the per-user, per-boot ``TMPDIR`` that launchd provides.

        macOS gives every user a private, mode-0700 temporary directory that is
        cleared between boots. That is a better home for the runtime descriptor
        than ``/tmp``, which is world-readable and would expose the sidecar port
        to every local process — the descriptor is also mode-0600, but there is
        no reason to rely on only one control.
        """
        tmpdir = os.environ.get("TMPDIR")
        base = Path(tmpdir) if tmpdir else Path("/tmp")  # noqa: S108
        return base / APP_NAME

    def default_log_dir(self) -> Path:
        return Path.home() / "Library" / "Logs" / APP_NAME

    def open_application(self, name: str) -> None:
        """`open -a <name>`.

        Argument list, never a shell string. `subprocess.run` with a list does
        not invoke a shell, so an application name containing `;` or backticks
        is passed through as a literal name — which simply fails to match an app
        — rather than being interpreted.

        `check=True` so a missing application raises here instead of silently
        succeeding and leaving the user waiting for a window.
        """
        subprocess.run(  # noqa: S603 - list form, no shell
            ["/usr/bin/open", "-a", name],
            check=True,
            capture_output=True,
            timeout=15,
        )

    def close_application(self, name: str) -> None:
        """AppleScript `quit`, which is a request the application can answer.

        `osascript -e 'quit app "X"'` sends the Apple Event an app handles by
        running its normal shutdown — an unsaved document still gets its save
        dialog. `pkill` would be simpler, more reliable, and would throw that
        away, which is the one property this must not have.

        The name goes through a separate argument, never interpolated into the
        script text, because a name containing a double quote would otherwise
        end the string literal and the rest would be AppleScript. `argv` inside
        the script is the parameterised-query equivalent for `osascript`.

        `System Events` is asked first whether the app is running, so a request
        to close something already closed fails loudly instead of quietly
        succeeding.
        """
        script = (
            'on run argv\n'
            '  set target to item 1 of argv\n'
            '  tell application "System Events"\n'
            '    if not (exists process target) then error target & " is not running" number 1\n'
            '  end tell\n'
            '  quit application target\n'
            'end run'
        )
        completed = subprocess.run(  # noqa: S603 - list form, no shell; the name is a bound argument
            ["/usr/bin/osascript", "-e", script, name],
            check=False,
            capture_output=True,
            timeout=20,
        )
        if completed.returncode != 0:
            # `check=True` would raise `CalledProcessError`, whose message is the
            # entire argv — including the script source. That string is relayed
            # to the model and then to the user, so the reason has to be the
            # reason: "Spotify is not running", not forty lines of AppleScript.
            detail = completed.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(_osascript_reason(detail))

    def open_url(self, url: str) -> None:
        """`open <url>`, with the scheme re-checked here.

        The caller validates too, but this is the point where a string becomes
        a process and `open` will happily hand a `file:` or a custom app scheme
        to whatever registered for it. A check on only one side of a boundary
        is a check that disappears the first time someone adds a second caller.
        """
        if not url.startswith(("http://", "https://")):
            raise ValueError("only http and https URLs may be opened")

        subprocess.run(  # noqa: S603 - list form, no shell
            ["/usr/bin/open", url],
            check=True,
            capture_output=True,
            timeout=15,
        )


def _osascript_reason(stderr: str) -> str:
    """The human half of an `osascript` failure.

    `osascript` reports errors as `124:150: execution error: Chess is not
    running (1)` — a character range, a category, the sentence, and an error
    number. Only the sentence means anything to the person who asked, and this
    string is relayed to them through the model.
    """
    if not stderr:
        return "the application did not quit"
    line = stderr.splitlines()[-1].strip()
    # Drop the leading `<start>:<end>: ` offsets and the category prefix.
    line = re.sub(r"^\d+:\d+:\s*", "", line)
    line = re.sub(r"^(execution|syntax) error:\s*", "", line)
    # Drop the trailing AppleScript error number.
    line = re.sub(r"\s*\(-?\d+\)$", "", line)
    return line or "the application did not quit"
