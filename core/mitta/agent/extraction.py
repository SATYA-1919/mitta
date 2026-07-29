"""Learning from conversation.

After a turn completes, an exchange is examined for durable facts and those
become memories. This is what separates MITTA from a chatbot with a scrollback:
telling it something once should be enough.

`Satya_Personal_Profile.pdf` sets the boundaries, and they are treated as
requirements rather than suggestions:

> Long-term memory should focus on stable preferences, recurring interests,
> ongoing projects, and useful decisions.

> Do not permanently store passwords, API keys, authentication tokens, financial
> credentials, government identification numbers, or other secrets.

> Do not infer sensitive personal facts from unrelated conversations.

> Allow Satya to correct, update, or delete stored information whenever needed.

Three of those are enforced in code below. The fourth already holds: everything
written here appears in the Memory surface with the same delete and correct
affordances as anything else, and `source_kind` records that it was extracted
rather than stated.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Final

from mitta.conversations.models import Message, MessageRole
from mitta.llm.gateway import LLMGateway
from mitta.llm.models import ChatMessage, ChatRequest, Role, TaskClass
from mitta.memory.models import MemoryDraft, MemoryKind, SourceKind
from mitta.memory.service import MemoryService
from mitta.telemetry.logging import get_logger
from mitta.telemetry.redaction import SecretRedactor

log = get_logger(__name__)

#: Below this the model was guessing. A wrong memory is worse than a missing
#: one — it gets recalled confidently and quietly corrupts later answers.
MIN_CONFIDENCE: Final = 0.7

#: Extracted facts start lower than stated ones. They were inferred from
#: conversation rather than asserted, so they should decay faster if never
#: confirmed by use (DEC-053).
EXTRACTED_IMPORTANCE: Final = 0.55

#: Only these kinds are extractable. The excluded three all need structure that
#: free-form extraction cannot supply: `episodic` a timestamp, `relationship` a
#: person id, and `project` a project_id — which is a foreign key, so a
#: `project` candidate could never be stored at all. A half-populated record is
#: worse than none, and one that violates a constraint is worse still.
EXTRACTABLE: Final = frozenset({MemoryKind.LONG_TERM, MemoryKind.PREFERENCE, MemoryKind.PROCEDURAL})

#: Anything matching is dropped without appeal. Deliberately broader than the
#: redactor's list: this decides what is written to a database that persists for
#: years, so a false positive costs one forgotten fact and a false negative
#: costs a stored credential.
_SENSITIVE: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\bgsk_[A-Za-z0-9]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9\-_]{12,}"),
    re.compile(r"\bAIza[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    # Words that signal a credential even when its shape is unrecognised.
    re.compile(
        r"(?i)\b(password|passwd|api[\s_-]?key|secret|token|credential|"
        r"aadhaar|aadhar|passport|ssn|social security|credit card|cvv|"
        r"bank account|ifsc|pin\s*(number|code))\b"
    ),
    # Long digit runs: card numbers, national ids, account numbers.
    re.compile(r"\b\d[\d\s\-]{10,}\d\b"),
)

_PROMPT: Final = """Extract durable facts about the user from this exchange.

Return JSON only: {"memories": [{"content": ..., "kind": ..., "confidence": ...}]}

kind is one of: long_term, preference, project, procedural
confidence is a NUMBER between 0 and 1, not a word.

Extract ONLY:
- stable preferences ("prefers X over Y")
- recurring interests
- ongoing projects and their decisions
- durable facts about the user's situation, tools, or people they work with

Do NOT extract:
- anything about this specific conversation
- questions the user asked
- facts about the world rather than about the user
- anything containing a password, key, token, card number or government id
- guesses. If it was not clearly stated, leave it out.

Write each memory as a standalone sentence that will still make sense in a year,
with no pronouns referring to this conversation. Return {"memories": []} if
there is nothing durable, which is the common case."""


@dataclass(frozen=True, slots=True)
class Candidate:
    content: str
    kind: MemoryKind
    confidence: float


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    stored: list[str]
    rejected_sensitive: int
    rejected_low_confidence: int
    duplicates: int

    @property
    def learned(self) -> int:
        return len(self.stored)


def looks_sensitive(text: str) -> bool:
    """Whether `text` must never be persisted.

    Checked on the *candidate*, not on the conversation: the conversation may
    legitimately contain a key the user pasted for MITTA to look at, and refusing
    to extract anything from that turn would be an odd response. What must not
    happen is the key ending up in a row that outlives the exchange.
    """
    return any(pattern.search(text) for pattern in _SENSITIVE)


#: Models reply with words as readily as numbers, whatever the prompt says.
#: Mapped rather than rejected: dropping a well-formed memory because its
#: confidence was spelled instead of counted loses real data, and does it
#: silently — the failure looks identical to "nothing worth learning".
_CONFIDENCE_WORDS: Final[dict[str, float]] = {
    "certain": 1.0,
    "very high": 0.95,
    "high": 0.9,
    "likely": 0.8,
    "medium": 0.6,
    "moderate": 0.6,
    "low": 0.3,
    "very low": 0.15,
    "uncertain": 0.2,
}


def _confidence(value: object) -> float | None:
    """Coerce a confidence to a number in [0, 1], or `None` if it is nonsense."""
    if isinstance(value, bool):
        return None  # `True` is an int in Python and would read as 1.0
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        text = value.strip().lower().rstrip("%")
        if text in _CONFIDENCE_WORDS:
            return _CONFIDENCE_WORDS[text]
        try:
            number = float(text)
        except ValueError:
            return None
        # "85" means 85%, not 8500%.
        if number > 1.0:
            number /= 100.0
        return max(0.0, min(1.0, number))
    return None


def parse_candidates(raw: str) -> list[Candidate]:
    """Parse the model's JSON reply, discarding anything malformed.

    Tolerant on purpose. A model that returns prose around its JSON, or invents
    a kind, should cost this turn's learning and nothing else — an exception
    here would fail a turn that has already answered the user correctly.
    """
    text = raw.strip()
    # Models wrap JSON in fences despite being told not to.
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()

    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError:
        log.debug("extraction.unparseable_reply")
        return []

    if not isinstance(payload, dict):
        return []
    entries = payload.get("memories")
    if not isinstance(entries, list):
        return []

    candidates: list[Candidate] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content")
        kind_name = entry.get("kind")
        confidence = entry.get("confidence")
        if not isinstance(content, str) or not content.strip():
            continue
        if not isinstance(kind_name, str):
            continue
        try:
            kind = MemoryKind(kind_name)
        except ValueError:
            continue
        if kind not in EXTRACTABLE:
            continue
        score = _confidence(confidence)
        if score is None:
            continue
        candidates.append(
            Candidate(content=content.strip(), kind=kind, confidence=max(0.0, min(1.0, score)))
        )
    return candidates


class MemoryExtractor:
    def __init__(
        self,
        memory: MemoryService,
        gateway: LLMGateway,
        *,
        redactor: SecretRedactor | None = None,
        min_confidence: float = MIN_CONFIDENCE,
    ) -> None:
        self._memory = memory
        self._gateway = gateway
        self._redactor = redactor
        self._min_confidence = min_confidence

    async def extract(self, exchange: list[Message]) -> ExtractionResult:
        """Learn from one completed exchange.

        Runs after the reply has been delivered, never before. Extraction is a
        second model call, and making the user wait for MITTA to take notes
        would trade the thing they asked for against a thing they did not.
        """
        transcript = _render(exchange)
        if not transcript.strip():
            return ExtractionResult([], 0, 0, 0)

        request = ChatRequest(
            messages=[
                ChatMessage(Role.SYSTEM, _PROMPT),
                ChatMessage(Role.USER, transcript),
            ],
            # Cheapest capable model: this runs after every turn, so its cost is
            # multiplied by every message ever sent.
            task=TaskClass.EXTRACTION,
            temperature=0.0,
            max_tokens=800,
            stream=False,
        )

        try:
            result = await self._gateway.complete(request)
        except Exception:
            # Learning is best-effort. A provider outage must not turn a
            # successful turn into a failed one after the fact.
            log.warning("extraction.failed", exc_info=True)
            return ExtractionResult([], 0, 0, 0)

        return self._store(parse_candidates(result.text))

    def _store(self, candidates: list[Candidate]) -> ExtractionResult:
        stored: list[str] = []
        sensitive = 0
        low_confidence = 0
        duplicates = 0
        malformed = 0

        for candidate in candidates:
            if looks_sensitive(candidate.content):
                # Counted, never logged. A log line quoting the rejected text
                # would write the secret to the exact file this check exists to
                # keep it out of.
                sensitive += 1
                continue
            if candidate.confidence < self._min_confidence:
                low_confidence += 1
                continue
            # A registered literal (the live session token, a provider key)
            # appearing verbatim. `redact_text` altering the string is the
            # test, and reuses exactly the patterns the logger trusts.
            if (
                self._redactor is not None
                and self._redactor.redact_text(candidate.content) != candidate.content
            ):
                sensitive += 1
                continue

            before = self._memory.count(status=None)
            try:
                memory = self._memory.remember(
                    MemoryDraft(
                        kind=candidate.kind,
                        content=candidate.content,
                        importance=EXTRACTED_IMPORTANCE,
                        confidence=candidate.confidence,
                        # `conversation`: this was inferred from what was said,
                        # not asserted by the user. The distinction is what lets
                        # them judge a memory they do not recognise.
                        source_kind=SourceKind.CONVERSATION,
                    )
                )
            except Exception:
                # One malformed candidate costs that candidate. It previously
                # cost the whole turn — a validation error propagated out of
                # extraction and closed the WebSocket, taking down a reply the
                # user had already received.
                log.warning("extraction.candidate_rejected", exc_info=True)
                malformed += 1
                continue

            if self._memory.count(status=None) > before:
                stored.append(memory.id)
            else:
                duplicates += 1

        if stored or sensitive or malformed:
            log.info(
                "extraction.complete",
                extra={
                    "learned": len(stored),
                    "duplicates": duplicates,
                    "rejected_sensitive": sensitive,
                    "rejected_low_confidence": low_confidence,
                    "rejected_malformed": malformed,
                },
            )
        return ExtractionResult(stored, sensitive, low_confidence, duplicates)


def _render(exchange: list[Message]) -> str:
    """Render an exchange for the extractor.

    Only user and assistant turns. Tool output is machine text and is where
    credentials and file contents actually appear.
    """
    lines = [
        f"{message.role.value}: {message.content}"
        for message in exchange
        if message.role in (MessageRole.USER, MessageRole.ASSISTANT) and message.content.strip()
    ]
    return "\n\n".join(lines)
