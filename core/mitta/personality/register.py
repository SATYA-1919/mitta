"""Register selection (DEC-033).

The product owner's rule, in his own words:

> For simple questions, keep the answer concise. For difficult technical topics,
> break the explanation into clear steps.

> For social conversations, he generally prefers short, confident, playful
> wording rather than long or overly dramatic messages.

So **length follows register, and register follows the topic** — not the other
way round. An earlier design suppressed styling once a reply passed a length
threshold, which had it backwards: it made a long answer plain because it was
long, rather than making it long because the subject deserved it.

Classification is a deterministic heuristic, not a model call. Three reasons:
it runs on every single turn; a probabilistic classifier makes the assistant's
voice vary for the same question asked twice, which reads as instability rather
than personality; and a rule that can be read is a rule the user can predict and
argue with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Register(StrEnum):
    PLAYFUL = "playful"
    SERIOUS = "serious"


#: Vocabulary that marks a request as technical. Matched on the *user's*
#: message: what they asked determines how it should be answered, and judging
#: from the reply would let a verbose model talk itself into a serious register.
_TECHNICAL = re.compile(
    r"(?i)\b(error|exception|traceback|stack ?trace|bug|debug|crash|fail(?:ed|ing|ure)?|"
    r"install|configure|deploy|build|compile|migrat\w+|schema|database|query|index|"
    r"architecture|design|implement|refactor|optimi[sz]e|benchmark|profil\w+|"
    r"api|endpoint|function|class|method|variable|type|async|thread|process|"
    r"docker|kubernetes|sqlite|faiss|python|rust|typescript|javascript|react|"
    r"why does|how do i|how does|what causes|explain|walk me through|difference between)\b"
)

#: Conversational markers. Short reactions, greetings, acknowledgement.
_SOCIAL = re.compile(
    r"(?i)\b(hi|hey|hello|yo|sup|thanks|thank you|ok|okay|cool|nice|great|"
    r"lol|haha|lmao|good ?(morning|night|evening)|how are you|whats up|"
    r"barca|barcelona|argentina|neymar|football|match|goal|transfer)\b"
)

#: Anything with code in it is a technical exchange regardless of wording.
_CODE_SHAPE = re.compile(r"```|`[^`]+`|\bdef \w+|\bclass \w+|[{};]\s*$|^\s*[\w.]+\(", re.MULTILINE)

#: Below this a question is a quick one whatever it is about.
_SHORT_QUESTION_CHARS: Final = 60


@dataclass(frozen=True, slots=True)
class RegisterDecision:
    register: Register
    #: Why. Surfaced in the UI so a long reply is explicable rather than
    #: surprising, and so a wrong call can be argued with.
    reason: str


def classify(user_text: str, *, response_text: str = "") -> RegisterDecision:
    """Pick a register for this turn.

    Order matters: the checks that force `serious` run first, because getting
    that wrong is the costlier error. A technical explanation delivered as
    "yeah just do the thing ra" is useless; a casual remark delivered plainly is
    merely dull.
    """
    text = user_text.strip()

    if _CODE_SHAPE.search(text) or _CODE_SHAPE.search(response_text):
        return RegisterDecision(Register.SERIOUS, "the exchange contains code")

    technical = bool(_TECHNICAL.search(text))
    social = bool(_SOCIAL.search(text))

    if technical:
        # A short technical question still gets a short answer — that is the
        # answer's length, which the rewrite does not control. The register
        # governs *how* it is written, and technical content is written plainly.
        return RegisterDecision(Register.SERIOUS, "technical subject")

    if social:
        return RegisterDecision(Register.PLAYFUL, "conversational")

    if len(text) <= _SHORT_QUESTION_CHARS:
        return RegisterDecision(Register.PLAYFUL, "short exchange")

    # Unmatched and substantial. Defaults to serious, because the failure mode
    # of a wrongly-playful answer is worse than a wrongly-plain one.
    return RegisterDecision(Register.SERIOUS, "no conversational signal")
