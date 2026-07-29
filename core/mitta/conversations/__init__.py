"""Conversation, turn and message persistence.

The transcript half of memory: extracted facts live in `mitta.memory`, and this
is the record they came from.

No service layer. The repository is the whole of the current behaviour, and a
pass-through facade would be indirection with no payoff. The logic that will
justify one — auto-titling, turn orchestration, context assembly — belongs to
the agent and arrives with it.
"""

from mitta.conversations.models import (
    Conversation,
    ConversationDraft,
    ConversationStatus,
    InputKind,
    Message,
    MessageDraft,
    MessageRole,
    Register,
    Turn,
    TurnStatus,
)
from mitta.conversations.repository import ConversationRepository

__all__ = [
    "Conversation",
    "ConversationDraft",
    "ConversationRepository",
    "ConversationStatus",
    "InputKind",
    "Message",
    "MessageDraft",
    "MessageRole",
    "Register",
    "Turn",
    "TurnStatus",
]
