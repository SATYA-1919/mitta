"""Personality layer (DEC-008, DEC-033).

The last stage before a reply reaches the user. Text in, text out — it cannot
see memory, tools or the plan, and nothing downstream acts on its output. That
isolation is what makes it safe to run on every reply.
"""

from mitta.personality.guards import Violation, must_not_restyle, protected_spans, verify
from mitta.personality.register import Register, RegisterDecision, classify
from mitta.personality.rewriter import PersonalityLayer, StyleResult

__all__ = [
    "PersonalityLayer",
    "Register",
    "RegisterDecision",
    "StyleResult",
    "Violation",
    "classify",
    "must_not_restyle",
    "protected_spans",
    "verify",
]
