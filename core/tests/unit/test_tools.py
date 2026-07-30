"""Built-in tools, and the capability statement that stops MITTA denying them.

The two things tested here shipped together because they are the same bug seen
from two sides: MITTA could not open a website, and it did not know what it
could do — so it answered "I cannot open YouTube yet", then explained that it
was a text-only assistant, having opened Apple Music a minute earlier.
"""

from __future__ import annotations

import pytest

from mitta.agent.context import CAPABILITY_PREAMBLE, capability_lines
from mitta.tools.base import Risk, ToolSpec
from mitta.tools.builtin.close_app import CloseAppTool
from mitta.tools.builtin.open_url import OpenUrlTool, normalise


class TestUrlNormalisation:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("youtube", "https://www.youtube.com"),
            ("YouTube", "https://www.youtube.com"),
            ("  github  ", "https://github.com"),
            ("youtube.com", "https://youtube.com"),
            ("https://example.com/a/b?c=d", "https://example.com/a/b?c=d"),
            ("http://localhost.dev", "http://localhost.dev"),
        ],
    )
    def test_accepted_shapes(self, given: str, expected: str) -> None:
        assert normalise(given) == expected

    @pytest.mark.parametrize(
        "given",
        [
            "",
            "   ",
            # The scheme is the whole security surface: `open` dispatches on it,
            # and a registered handler will take any of these.
            "file:///etc/passwd",
            "javascript:alert(1)",
            "ftp://example.com",
            "x-apple-something://do-a-thing",
            "https://",
            # Not a hostname. Guessing a search query from it would open a page
            # the user never asked for.
            "please open the thing",
        ],
    )
    def test_refused_shapes(self, given: str) -> None:
        assert normalise(given) is None


class TestOpenUrlTool:
    async def test_it_opens_a_known_site_by_name(self) -> None:
        opened: list[str] = []
        tool = OpenUrlTool(opened.append)

        result = await tool.run({"url": "youtube"})

        assert result.ok is True
        assert opened == ["https://www.youtube.com"]
        # The reply says what happened, so the answering model has something
        # concrete to acknowledge rather than inventing a denial.
        assert "https://www.youtube.com" in result.content

    async def test_a_refused_url_never_reaches_the_opener(self) -> None:
        opened: list[str] = []
        tool = OpenUrlTool(opened.append)

        result = await tool.run({"url": "file:///etc/passwd"})

        assert result.ok is False
        assert opened == []

    async def test_an_opener_that_raises_becomes_a_readable_failure(self) -> None:
        def explode(_: str) -> None:
            raise OSError("no browser")

        result = await OpenUrlTool(explode).run({"url": "github"})

        assert result.ok is False
        assert "no browser" in result.content

    def test_it_is_read_risk_and_says_what_it_is_not_for(self) -> None:
        spec = OpenUrlTool(lambda _: None).spec
        assert spec.risk is Risk.READ
        # DEC-098: the boundary is the useful half of a tool description.
        assert "open_app" in spec.description
        assert "web_search" in spec.description


class TestCapabilityStatement:
    def test_each_tool_becomes_one_line(self) -> None:
        specs = [
            ToolSpec(name="b_tool", description="Does B. NOT for A.", risk=Risk.READ),
            ToolSpec(name="a_tool", description="Does A", risk=Risk.WRITE),
        ]

        assert capability_lines(specs) == "- a_tool: Does A\n- b_tool: Does B"

    def test_the_preamble_names_the_denial_it_exists_to_stop(self) -> None:
        rendered = CAPABILITY_PREAMBLE.format(capabilities="- open_url: Open a website")

        assert "open_url" in rendered
        assert "text-only" in rendered

    def test_the_preamble_forbids_leaking_a_tool_name(self) -> None:
        # Observed: "I can open Spotify using the open_app tool." Listing the
        # tools to the model is what taught it to name them to the user.
        rendered = CAPABILITY_PREAMBLE.format(capabilities="- open_app: Launch an application")

        assert "Never say a tool's name to the user" in rendered


class TestCloseApp:
    """`close_app` is DESTRUCTIVE, and these tests are mostly about that."""

    def _tool(self, closer: object = None) -> CloseAppTool:
        return CloseAppTool(closer or (lambda name: None))

    def test_it_is_destructive_not_write(self) -> None:
        # Opening an app undoes itself; closing one can lose an unsaved buffer
        # MITTA cannot see. The tier is what makes it ask first.
        assert self._tool().spec.risk is Risk.DESTRUCTIVE

    def test_the_prompt_names_the_app_and_the_risk(self) -> None:
        prompt = self._tool().spec.describe({"app": "Pages"})
        assert "Pages" in prompt
        assert "unsaved" in prompt

    @pytest.mark.asyncio
    async def test_it_closes_through_the_injected_closer(self) -> None:
        closed: list[str] = []
        result = await self._tool(closed.append).run({"app": "Spotify"})
        assert result.ok
        assert closed == ["Spotify"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("app", ["Finder", "finder", "WindowServer", "Dock", "MITTA"])
    async def test_it_refuses_to_quit_the_desktop(self, app: str) -> None:
        # A model told to "close everything" will try. Approving "close apps" is
        # not approving being logged out.
        called: list[str] = []
        result = await self._tool(called.append).run({"app": app})
        assert not result.ok
        assert called == []

    @pytest.mark.asyncio
    async def test_it_rejects_a_name_that_is_a_path_or_an_option(self) -> None:
        called: list[str] = []
        for app in ["../../bin/sh", "-h", "a;rm -rf /", ""]:
            result = await self._tool(called.append).run({"app": app})
            assert not result.ok
        assert called == []

    @pytest.mark.asyncio
    async def test_a_closer_that_raises_becomes_a_readable_failure(self) -> None:
        # The adapter raises when the app is not running, on purpose: saying
        # "closed it" about something never open is a claim the user would act on.
        def boom(name: str) -> None:
            raise RuntimeError(f"{name} is not running")

        result = await self._tool(boom).run({"app": "Spotify"})
        assert not result.ok
        assert "not running" in result.content
