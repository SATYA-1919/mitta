"""Open a website in the default browser.

Exists because "open YouTube" is not "open an application", and MITTA answering
*"I cannot open YouTube yet"* seconds after opening Apple Music is the kind of
gap that reads to a user as the assistant being broken rather than as a missing
tool. YouTube is a site; `open_app` launches apps; nothing joined the two.

**Scheme validation is the whole security surface.** `open` on macOS dispatches
by scheme, and a registered handler will happily accept `file:`, `ftp:` or a
custom application URL. Only `http` and `https` pass here, and the adapter
re-checks rather than trusting this — a check on one side of a boundary is one
that vanishes when a second caller appears.

`READ` risk, matching `web_search`: it reaches the network and is therefore
reported (R5, DEC-081), but it creates nothing and destroys nothing.
"""

from __future__ import annotations

import re
from typing import Any, Final
from urllib.parse import urlsplit

from mitta.tools.base import Risk, ToolResult, ToolSpec

#: Bare hostnames the user will type as a name. Not a redirect list and not a
#: search — just the observation that a person saying "open youtube" means the
#: site, and that leaving the model to guess a URL invites an invented one.
KNOWN_SITES: Final[dict[str, str]] = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "reddit": "https://www.reddit.com",
    "twitter": "https://twitter.com",
    "x": "https://x.com",
    "instagram": "https://www.instagram.com",
    "whatsapp": "https://web.whatsapp.com",
    "netflix": "https://www.netflix.com",
    "spotify": "https://open.spotify.com",
    "wikipedia": "https://www.wikipedia.org",
    "linkedin": "https://www.linkedin.com",
    "chatgpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "stackoverflow": "https://stackoverflow.com",
}

#: A hostname, so `youtube.com` typed without a scheme still works. Deliberately
#: strict: no userinfo, no spaces, no control characters.
_BARE_HOST: Final = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\.[A-Za-z]{2,24}$")


def normalise(target: str) -> str | None:
    """Turn what the model passed into a URL, or `None` if it cannot be one.

    Three accepted shapes, in order: a known site name, a full http(s) URL, a
    bare hostname. Anything else is refused rather than guessed at — a tool that
    turns an unparseable string into a search query is a tool that opens a page
    the user did not ask for.
    """
    value = target.strip()
    if not value:
        return None

    known = KNOWN_SITES.get(value.lower().removeprefix("www."))
    if known is not None:
        return known

    if value.startswith(("http://", "https://")):
        parts = urlsplit(value)
        # A scheme with no host — `https://` alone, or `http:///path` — is not
        # something to hand to the OS.
        return value if parts.hostname else None

    if _BARE_HOST.match(value):
        return f"https://{value}"

    return None


class OpenUrlTool:
    """Opens a URL through an injected opener.

    Takes a callable rather than importing the OS adapter: `mitta.tools` may not
    import `mitta.os_adapter`, and that contract is what keeps platform access
    behind the policy engine (DEC-079).
    """

    def __init__(self, opener: Any) -> None:
        self._open = opener

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="open_url",
            description=(
                "Open a website in the default browser. Use this for anything "
                "that lives on the web — 'open YouTube', 'open my gmail', "
                "'pull up github'. Pass the site name ('youtube') or a full "
                "URL. This is NOT for launching a desktop application (use "
                "open_app) and NOT for looking something up (use web_search)."
            ),
            risk=Risk.READ,
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "A site name like 'youtube', or a full https:// URL",
                    }
                },
                "required": ["url"],
            },
        )

    async def run(self, params: dict[str, Any]) -> ToolResult:
        target = str(params.get("url", ""))
        url = normalise(target)
        if url is None:
            return ToolResult.failure(
                f"{target!r} is not a website I can open. Give me a site name or an https URL."
            )

        try:
            self._open(url)
        except Exception as exc:
            return ToolResult.failure(f"Could not open {url}: {exc}")

        return ToolResult(ok=True, content=f"Opened {url} in the browser.", detail={"url": url})
