"""Rewrite guards — what the personality layer is forbidden to touch.

`ARCHITECTURE.md` §7 states that a style pass which alters meaning is a
correctness bug, and lists the invariants. This module makes that checkable
instead of hopeful: it extracts the spans that must survive verbatim, and
verifies afterwards that they did.

Verification matters more than the prompt. An instruction not to change a file
path is a request to a language model; a check that the path is still present is
a guarantee. When the check fails the rewrite is discarded and the original text
is used, so the worst case is a reply that reads plainly rather than one that
tells the user to `rm -rf ~/Documnets`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

#: Fenced and inline code. Fences first so an inline pattern cannot bite into a
#: block that happens to contain a backtick.
_FENCED = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]+`")

#: Absolute and home-relative paths, plus anything with a directory separator
#: and an extension. Deliberately greedy — a path mangled by a rewrite is a
#: command that destroys the wrong thing.
_PATH = re.compile(r"(?:~|\.{1,2})?/[\w.\-/]+|\b[\w.\-]+/[\w.\-/]+\.\w+\b")

_URL = re.compile(r"\bhttps?://[^\s<>\"')]+")

#: Numbers, including decimals, percentages, sizes and versions. A restyle that
#: turns "47 files" into "a bunch of files" has changed a fact.
_NUMBER = re.compile(r"\b\d+(?:[.,]\d+)*\s*(?:%|[KMGT]i?B|ms|s|x)?\b", re.IGNORECASE)

#: Identifiers the user would search for: ticket ids, model names, ULIDs.
_IDENTIFIER = re.compile(r"\b[A-Z][A-Z0-9]{1,}-\d+\b|\b\w+_[0-9A-HJKMNP-TV-Z]{20,}\b")

_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    _FENCED,
    _INLINE_CODE,
    _PATH,
    _URL,
    _IDENTIFIER,
    _NUMBER,
)

#: A reply containing any of these is never restyled at all. Confirmation
#: prompts and refusals are where ambiguity is dangerous — "are you sure you
#: want to delete 47 files" must not become "shall i nuke these ra".
_NEVER_RESTYLE = re.compile(
    r"(?i)\b(are you sure|confirm|permanently delete|cannot be undone|irreversible|"
    r"i can'?t help|i won'?t|not able to help with|requires approval|approve this)\b"
)


@dataclass(frozen=True, slots=True)
class ProtectedSpans:
    """Substrings that must appear unchanged in any rewrite."""

    spans: tuple[str, ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:
        return bool(self.spans)


def protected_spans(text: str) -> ProtectedSpans:
    """Every substring the rewrite must preserve byte-for-byte."""
    found: list[str] = []
    for pattern in _PATTERNS:
        found.extend(match.group(0) for match in pattern.finditer(text))
    # Deduplicated, longest first: checking the long span first means a nested
    # short one cannot report success on its own.
    unique = sorted(set(found), key=len, reverse=True)
    return ProtectedSpans(tuple(unique))


def must_not_restyle(text: str) -> bool:
    """Whether this reply is off-limits entirely (`ARCHITECTURE.md` §7)."""
    return bool(_NEVER_RESTYLE.search(text))


#: Who a sentence is about. Swapping these is the subtlest way a restyle can
#: change meaning while passing every other check, and it happened twice on the
#: first day tools worked:
#:
#:     "Apple Music is now open on your Mac."
#:  -> "apple music is now open on my mac ra"
#:
#:     "Apple Music is now open. Is there something specific you'd like to do?"
#:  -> "there's something specific i'd like to do in apple music"
#:
#: The second is worse than a pronoun slip: the answer was deleted and a
#: question to the user became MITTA narrating its own intentions. Nothing in
#: the span, number or length checks could see either one.
_SECOND_PERSON = re.compile(r"(?i)\b(you|your|yours|you'?re|you'?d|you'?ll|you'?ve)\b")
_FIRST_PERSON = re.compile(r"(?i)\b(i|me|my|mine|i'?m|i'?d|i'?ll|i'?ve)\b")


def _counts(text: str) -> tuple[int, int]:
    return len(_SECOND_PERSON.findall(text)), len(_FIRST_PERSON.findall(text))


def person_inverted(original: str, rewritten: str) -> bool:
    """Whether the rewrite swapped who the reply is about.

    Narrow on purpose. Dropping a pronoun is a legitimate restyle — "I have
    opened it" to "opened it" loses an "I" and means the same thing. What is
    never legitimate is one party disappearing from the sentence *and* the
    other arriving in their place.

    Symmetric, because both directions were observed within an hour:

        "Apple Music is now open on your Mac."  ->  "... on my mac ra"
        "I'm opening YouTube now."              ->  "you're opening youtube now"

    The second is the worse of the two. It hands the user an instruction they
    did not receive and lets MITTA off the hook for an action it claimed.
    """
    you_before, me_before = _counts(original)
    you_after, me_after = _counts(rewritten)

    # The user vanished and MITTA took their place.
    if you_before > 0 and you_after == 0 and me_after > me_before:
        return True
    # MITTA vanished and the user took its place.
    return me_before > 0 and me_after == 0 and you_after > you_before


@dataclass(frozen=True, slots=True)
class Violation:
    span: str
    reason: str


def verify(original: str, rewritten: str) -> list[Violation]:
    """Check a rewrite. An empty list means it is safe to use.

    Four checks, in increasing subtlety:

    1. Every protected span still present.
    2. No new number introduced — a rewrite that invents "about 50" from
       "47" has fabricated a fact even though the original survived.
    3. Length within bounds. A rewrite that triples the text has not restyled
       it, it has written something else; one that reduces a paragraph to two
       words has dropped content.
    4. The subject did not move from the user to MITTA.
    """
    violations: list[Violation] = []

    for span in protected_spans(original).spans:
        if span not in rewritten:
            violations.append(Violation(span, "protected span was altered or dropped"))

    original_numbers = set(_NUMBER.findall(original))
    for number in set(_NUMBER.findall(rewritten)):
        if number not in original_numbers:
            violations.append(Violation(number, "number not present in the original"))

    if len(rewritten) > max(len(original) * 2, len(original) + 80):
        violations.append(Violation("", "rewrite is far longer than the original"))

    # A short original can legitimately shrink to almost nothing ("Done." →
    # "done ra"), so the floor only applies once there is real content to lose.
    if len(original) > 200 and len(rewritten) < len(original) * 0.25:
        violations.append(Violation("", "rewrite dropped most of the content"))

    if person_inverted(original, rewritten):
        violations.append(Violation("", "rewrite changed who the reply is about"))

    return violations
