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
