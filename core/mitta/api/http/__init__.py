"""HTTP routers, one module per resource."""

from mitta.api.http.audit import router as audit_router
from mitta.api.http.conversations import router as conversations_router
from mitta.api.http.llm import router as providers_router
from mitta.api.http.memory import router as memory_router
from mitta.api.http.system import router as system_router

__all__ = [
    "audit_router",
    "conversations_router",
    "memory_router",
    "providers_router",
    "system_router",
]
