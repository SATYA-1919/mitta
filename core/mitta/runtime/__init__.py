"""Process runtime — readiness handshake and descriptor lifecycle."""

from mitta.runtime.descriptor import (
    READY_PREFIX,
    RuntimeDescriptor,
    announce_ready,
    read_descriptor,
    remove_descriptor,
    write_descriptor,
)

__all__ = [
    "READY_PREFIX",
    "RuntimeDescriptor",
    "announce_ready",
    "read_descriptor",
    "remove_descriptor",
    "write_descriptor",
]
