"""Tool registry.

Holds the tools available this run and produces the schema handed to the model.

Registration is explicit — there is no auto-discovery by scanning modules. A
capability that can act on the user's machine should appear in the composition
root where it can be read, not arrive because a file happened to be importable.
"""

from __future__ import annotations

from typing import Any

from mitta.errors import NotFoundError
from mitta.tools.base import Risk, Tool, ToolSpec

_ORDER: dict[Risk, int] = {Risk.READ: 0, Risk.WRITE: 1, Risk.DESTRUCTIVE: 2}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"tool {name!r} is already registered")
        self._tools[name] = tool

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            raise NotFoundError("tool", name)
        return tool

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def schema(self, *, max_risk: Risk = Risk.DESTRUCTIVE) -> list[dict[str, Any]]:
        """Tool definitions for the model.

        `max_risk` exists so a context can offer only safe tools. A model cannot
        request a capability it was never shown, which is a cheaper guarantee
        than refusing the call afterwards.
        """
        return [
            tool.spec.to_wire()
            for tool in self._tools.values()
            if _ORDER[tool.spec.risk] <= _ORDER[max_risk]
        ]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools
