"""Tool contract.

A tool is a named, typed capability the agent may request. Two rules shape
everything here.

**Tools declare their risk; they do not decide it.** `Risk` is metadata the
policy engine reads. A tool that could mark itself `READ` to skip confirmation
would make the whole permission model advisory.

**Tools never touch the OS Adapter.** `mitta.tools` importing `mitta.os_adapter`
is forbidden by an import contract (DEC-029). Platform access is reached through
the policy engine, so bypassing permission means editing the composition root
*and* breaking a CI-checked contract — not just calling a different function.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class Risk(StrEnum):
    """What a tool can do, and therefore how it is gated."""

    #: Observes without changing anything. Runs without asking — but is still
    #: logged and reported, because a web search sends the query off the machine
    #: (R5). "Did not ask" is not "did not tell".
    READ = "read"

    #: Creates or modifies something recoverable.
    WRITE = "write"

    #: Deletes, overwrites, or acts outward where undo does not exist.
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    risk: Risk
    #: JSON Schema for the arguments, handed to the model for tool calling.
    parameters: dict[str, Any] = field(default_factory=dict)

    def describe(self, params: dict[str, Any]) -> str:
        """The confirmation prompt.

        Overridden by tools that can say something concrete. The default names
        the tool and its arguments, which is worse than a purpose-written
        sentence and far better than "perform an action" — a prompt nobody reads
        is a prompt that approves everything.
        """
        rendered = ", ".join(f"{k}={v!r}" for k, v in sorted(params.items()))
        return f"Run {self.name}({rendered})?"

    def subject(self, params: dict[str, Any]) -> str | None:
        """What the action is *about*, for the audit log — a path, a URL, a query."""
        for key in ("path", "url", "query", "app", "target"):
            value = params.get(key)
            if isinstance(value, str):
                return value
        return None

    def to_wire(self) -> dict[str, Any]:
        """OpenAI tool-calling shape, which both providers accept."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters
                or {"type": "object", "properties": {}, "required": []},
            },
        }


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    #: Rendered back to the model. Kept small — tool output lands in the next
    #: request's context, and a tool returning a megabyte would blow the budget
    #: that R5's chokepoint exists to enforce.
    content: str
    detail: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def failure(cls, message: str) -> ToolResult:
        return cls(ok=False, content=message)


@runtime_checkable
class Tool(Protocol):
    @property
    def spec(self) -> ToolSpec: ...

    async def run(self, params: dict[str, Any]) -> ToolResult: ...
