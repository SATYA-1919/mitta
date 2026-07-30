"""Close an application.

`DESTRUCTIVE`, not `WRITE`, and the tier is the whole design of this tool.

Opening an app costs nothing and undoes itself (`open_app` is `READ`). Closing
one can destroy work that was never saved, and MITTA cannot see whether it will:
an unsaved buffer in an editor is invisible from out here. The adapter asks the
app to quit rather than killing it, so the app's own save dialog still appears —
but "the app might catch it" is a mitigation, not a permission model. Anything
that can lose the user's afternoon asks first, with the name in the prompt.

The name is validated here for the same reason `open_app` validates it: this
module knows what an application name looks like, and the adapter turns the
string into a process.
"""

from __future__ import annotations

import re
from typing import Any, Final

from mitta.tools.base import Risk, ToolResult, ToolSpec

#: Same shape `open_app` accepts. Letters, digits, spaces and the punctuation
#: real app names use — no slashes, no shell metacharacters, no leading dash.
_APP_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}$")

#: Quitting these does not close a window, it ends the session or the desktop.
#: A model that has been asked to "close everything" will otherwise try, and the
#: user's approval for "close apps" is not approval to be logged out.
_PROTECTED: Final = frozenset(
    {
        "finder",
        "dock",
        "systemuiserver",
        "loginwindow",
        "windowserver",
        "mitta",
    }
)


class CloseAppTool:
    """Closes an application through an injected closer.

    Takes a callable rather than importing the OS adapter, because
    `mitta.tools` may not import `mitta.os_adapter` — that contract is what
    keeps platform access behind the policy engine (DEC-079).
    """

    def __init__(self, closer: Any) -> None:
        self._close = closer

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="close_app",
            description=(
                "Close or quit a running application by name, e.g. 'Spotify', "
                "'Safari'. Use whenever asked to close, quit, exit or shut "
                "something. Asks the app to quit, so unsaved work still "
                "prompts. Does NOT close a single tab or window — it quits the "
                "whole application."
            ),
            risk=Risk.DESTRUCTIVE,
            parameters={
                "type": "object",
                "properties": {"app": {"type": "string", "description": "Application name"}},
                "required": ["app"],
            },
            describer=self.describe,
        )

    def describe(self, params: dict[str, Any]) -> str:
        app = str(params.get("app", "")).strip()
        return f"Quit {app}? Anything unsaved in it will prompt you to save."

    async def run(self, params: dict[str, Any]) -> ToolResult:
        app = str(params.get("app", "")).strip()
        if not _APP_NAME.match(app):
            return ToolResult.failure(f"{app!r} is not a valid application name.")
        if app.casefold() in _PROTECTED:
            # Refused rather than confirmed. There is no phrasing of a prompt
            # that makes quitting the window server a reasonable thing to
            # approve mid-conversation.
            return ToolResult.failure(f"{app} runs the desktop — MITTA will not quit it.")

        try:
            self._close(app)
        except Exception as exc:
            # Covers the not-running case, which the adapter raises for
            # deliberately: reporting "closed it" about something that was never
            # open is a claim the user would act on.
            return ToolResult.failure(f"Could not close {app}: {exc}")

        return ToolResult(ok=True, content=f"Closed {app}.", detail={"app": app})
