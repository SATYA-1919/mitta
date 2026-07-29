"""Agent — turn orchestration and context assembly.

The layer that turns a user's sentence into an answer: recall, budget, stream,
persist. Sits above memory, conversations and the LLM gateway, and knows nothing
about which vendor answers (`ARCHITECTURE.md` §6).
"""

from mitta.agent.context import AssembledContext, assemble
from mitta.agent.orchestrator import Orchestrator, TurnEvent, TurnOutcome

__all__ = ["AssembledContext", "Orchestrator", "TurnEvent", "TurnOutcome", "assemble"]
