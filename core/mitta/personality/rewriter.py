"""The personality layer — the last stage before a reply reaches the user.

Takes text in and returns text out. It cannot see the memory, the tools or the
plan, and nothing downstream of it acts on anything. That isolation is the whole
point (`ARCHITECTURE.md` §7): a style pass that could influence reasoning would
be a reasoning component with a misleading name.

The order of operations is deliberate:

    reason → answer → check guards → rewrite → verify → use or discard

**Verify, then use.** The rewrite is asked not to change meaning, and then
checked. If a protected span is gone, a number was invented, or the length
moved implausibly, the rewrite is thrown away and the original is returned. The
worst case is a plain reply — never a wrong one.
"""

from __future__ import annotations

from dataclasses import dataclass

from mitta.llm.gateway import LLMGateway
from mitta.llm.models import ChatMessage, ChatRequest, Role, TaskClass
from mitta.personality.guards import Violation, must_not_restyle, verify
from mitta.personality.register import Register, RegisterDecision, classify
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)

#: Below this an answer is already short enough that a restyle can only damage
#: it, and the model round-trip costs more latency than the change is worth.
MIN_LENGTH_TO_RESTYLE = 12

PLAYFUL_STYLE = """Rewrite the reply the way the user talks, out loud, to a friend.

This is spoken register. Say it, do not write it.

Length: 1 to 8 words. Aim for about three. If the reply says one thing, that is
one short line, not a sentence with a clause hanging off it.

Voice: lowercase. Terminal full stops are usually dropped. Commas are rare.
Apostrophes are often dropped and that is correct here — "thats", "whats".
ALL CAPS is how emphasis is done. Never an exclamation mark.

Patterns this voice actually uses:

- Doubled affirmation, not elaboration:  "yeah yeah thats good"
- One-word verdicts:  yeah · nah · done · wait · coming · okay re
- Sentence-final "ra", "re", "vro" or "man" — natural, never forced onto
  every line
- "it's" and "its" are spoken and written "itsh". This is the spelling, not a
  mistake. It applies to that word only — never invent phonetic spellings for
  anything else.

When a tool has already done something, lead with the doubled affirmation and
then say what happened. This is the default shape, not one option among several:

    "yeah yeah thats good, opening now"
    "yeah yeah thats good, itsh open man"

Vary the tail, never drop the affirmation. Do not narrate what happened, do not
offer next steps, do not ask a follow-up question.

Never: "Certainly", "I'd be happy to", "As an AI", "Let me know if", "I
understand", a greeting, a sign-off, or an unprompted compliment.

Keep every fact, number, name, path, command, URL and code block exactly as
written. Do not add information. Do not invent a fact to fill the line.

Never change who the reply is about: "your Mac" stays "your Mac", and a
question you are asking the user stays a question to the user.

Return only the rewritten reply."""

SERIOUS_STYLE = """Rewrite the reply so it sounds like a person, not a manual.

Keep the full explanation, every step and all detail — length is correct here.
Remove corporate padding: "I'd be happy to", "Certainly", "Great question",
"It's important to note that", "In conclusion". Do not add enthusiasm.

Keep every fact, number, name, path, command, URL and code block exactly as
written. Do not add information. Do not remove information.

Rewrite every sentence. Do not drop one. Never change who the reply is about:
"your Mac" stays "your Mac", and a question you are asking the user stays a
question to the user.

Return only the rewritten reply."""


@dataclass(frozen=True, slots=True)
class StyleResult:
    text: str
    register: Register
    reason: str
    #: True only when the text actually changed. `styled` on the message row
    #: records that the pass *ran*; this records that it *did* something, and
    #: the UI must not swap displayed text for an identical string (DEC-046).
    changed: bool
    violations: tuple[Violation, ...] = ()

    @property
    def rejected(self) -> bool:
        return bool(self.violations)


class PersonalityLayer:
    def __init__(
        self,
        gateway: LLMGateway,
        *,
        enabled: bool = True,
        intensity: float = 1.0,
    ) -> None:
        self._gateway = gateway
        self._enabled = enabled
        self._intensity = intensity

    async def apply(self, text: str, *, user_text: str) -> StyleResult:
        """Style one reply. Never raises."""
        decision = classify(user_text, response_text=text)

        skip = self._skip_reason(text)
        if skip is not None:
            return StyleResult(text, decision.register, skip, changed=False)

        try:
            rewritten = await self._rewrite(text, decision)
        except Exception:
            # A provider failure here must not lose an answer the user has
            # already effectively received. Plain text is a fine outcome.
            log.warning("personality.rewrite_failed", exc_info=True)
            return StyleResult(text, decision.register, "rewrite unavailable", changed=False)

        violations = verify(text, rewritten)
        if violations:
            log.warning(
                "personality.rewrite_rejected",
                extra={
                    "register": decision.register.value,
                    # Reasons only. The spans themselves can contain paths and
                    # identifiers from the user's own machine.
                    "reasons": sorted({v.reason for v in violations}),
                },
            )
            return StyleResult(
                text,
                decision.register,
                "rewrite changed protected content",
                changed=False,
                violations=tuple(violations),
            )

        changed = rewritten.strip() != text.strip()
        return StyleResult(rewritten, decision.register, decision.reason, changed=changed)

    def _skip_reason(self, text: str) -> str | None:
        if not self._enabled or self._intensity <= 0.0:
            # `intensity = 0` is a documented no-op (ARCHITECTURE.md §7), not a
            # weak rewrite. Half-styling is worse than either extreme.
            return "personality disabled"
        if len(text.strip()) < MIN_LENGTH_TO_RESTYLE:
            return "already short"
        if must_not_restyle(text):
            # Confirmation prompts and refusals. Ambiguity here is dangerous.
            return "safety-relevant text is never restyled"
        return None

    async def _rewrite(self, text: str, decision: RegisterDecision) -> str:
        style = PLAYFUL_STYLE if decision.register is Register.PLAYFUL else SERIOUS_STYLE
        result = await self._gateway.complete(
            ChatRequest(
                messages=[ChatMessage(Role.SYSTEM, style), ChatMessage(Role.USER, text)],
                # Cheapest and fastest available: this runs on every reply, so
                # its latency is felt directly and its cost is multiplied by
                # every message ever sent (DEC-066).
                task=TaskClass.PERSONALITY,
                temperature=0.4,
                # Headroom over the original, and a hard ceiling on a model that
                # decides to explain itself instead of rewriting.
                max_tokens=min(2000, len(text) // 2 + 200),
                stream=False,
            )
        )
        return result.text.strip()
