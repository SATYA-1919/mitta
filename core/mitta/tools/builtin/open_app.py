"""Open an application.

`READ` risk, which deserves justifying: launching an app changes nothing the
user owns, cannot destroy data, and is trivially undone by closing the window.
It is still logged and surfaced like every other action.

The app name is validated against a strict pattern rather than passed through.
`macos.open_application` shells out, and an unvalidated name is an argument
injection waiting to happen — the check is here because this module knows what a
legitimate application name looks like.
"""

from __future__ import annotations

import re
from typing import Any, Final

from mitta.tools.base import Risk, ToolResult, ToolSpec

#: Letters, digits, spaces, and the punctuation real app names use. No slashes
#: (a path, not a name), no shell metacharacters, no leading dash (an option).
_APP_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}$")


class OpenAppTool:
    """Launches an application through an injected opener.

    Takes a callable rather than importing the OS adapter, because
    `mitta.tools` may not import `mitta.os_adapter` — that contract is what
    keeps platform access behind the policy engine (DEC-079).
    """

    def __init__(self, opener: Any) -> None:
        self._open = opener

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="open_app",
            description=(
                "Open an application on the user's Mac by name, e.g. 'Safari', "
                "'Spotify', 'Visual Studio Code'."
            ),
            risk=Risk.READ,
            parameters={
                "type": "object",
                "properties": {"app": {"type": "string", "description": "Application name"}},
                "required": ["app"],
            },
        )

    async def run(self, params: dict[str, Any]) -> ToolResult:
        app = str(params.get("app", "")).strip()
        if not _APP_NAME.match(app):
            return ToolResult.failure(f"{app!r} is not a valid application name.")

        try:
            self._open(app)
        except Exception as exc:
            return ToolResult.failure(f"Could not open {app}: {exc}")

        return ToolResult(ok=True, content=f"Opened {app}.", detail={"app": app})
