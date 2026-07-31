"""HTTP routers, one module per resource."""

from mitta.api.http.audit import router as audit_router
from mitta.api.http.conversations import router as conversations_router
from mitta.api.http.llm import router as providers_router
from mitta.api.http.memory import router as memory_router
from mitta.api.http.projects import router as projects_router
from mitta.api.http.system import router as system_router
from mitta.api.http.tasks import plans_router, schedules_router, tasks_router

__all__ = [
    "audit_router",
    "conversations_router",
    "memory_router",
    "plans_router",
    "projects_router",
    "providers_router",
    "schedules_router",
    "system_router",
    "tasks_router",
]
