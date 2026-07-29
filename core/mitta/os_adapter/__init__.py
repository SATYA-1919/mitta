"""OS Adapter layer — the only place platform assumptions are permitted."""

from mitta.os_adapter.base import OSAdapter
from mitta.os_adapter.factory import create_os_adapter
from mitta.os_adapter.mac import MacAdapter
from mitta.os_adapter.windows import WindowsAdapter

__all__ = ["MacAdapter", "OSAdapter", "WindowsAdapter", "create_os_adapter"]
