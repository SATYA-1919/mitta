"""LLM gateway — the only layer that knows a vendor's name.

Everything above asks for a task class and a set of capabilities; which company
answers is decided here (`ARCHITECTURE.md` §6, R3).
"""

from mitta.llm.gateway import LLMGateway, ProviderStatus
from mitta.llm.health import HealthPolicy, HealthTracker
from mitta.llm.models import (
    Capabilities,
    ChatChunk,
    ChatMessage,
    ChatRequest,
    ChatResult,
    ModelDescriptor,
    Role,
    TaskClass,
    Usage,
)
from mitta.llm.provider import Provider

__all__ = [
    "Capabilities",
    "ChatChunk",
    "ChatMessage",
    "ChatRequest",
    "ChatResult",
    "HealthPolicy",
    "HealthTracker",
    "LLMGateway",
    "ModelDescriptor",
    "Provider",
    "ProviderStatus",
    "Role",
    "TaskClass",
    "Usage",
]
