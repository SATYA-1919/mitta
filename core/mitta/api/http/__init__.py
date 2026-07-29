"""HTTP routers, one module per resource."""

from mitta.api.http.memory import router as memory_router
from mitta.api.http.system import router as system_router

__all__ = ["memory_router", "system_router"]
