"""Retention scoring — the definition of "low-value information".

`DATABASE_DESIGN.md` §4.4:

    retention = importance x exp(-lambda * days_since_last_access)
              + 0.15 * log10(1 + access_count)

Two properties matter more than the exact constants.

**Decay demotes; it never deletes.** A memory falling below the threshold moves
to `forgotten` and leaves the vector index. The row stays. A system that
silently destroys user data because a formula said so is not one anyone should
trust with their life's context, and no confidence in the formula justifies it.

**Pinned bypasses the arithmetic entirely.** Explicit user intent is not an
input to be weighed against decay; it is the answer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

SECONDS_PER_DAY = 86_400

# Half-life of ~46 days: exp(-0.015 * 46) ~= 0.5.
DEFAULT_DECAY_LAMBDA = 0.015
DEFAULT_ACCESS_WEIGHT = 0.15
DEFAULT_FORGET_THRESHOLD = 0.05


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    decay_lambda: float = DEFAULT_DECAY_LAMBDA
    access_weight: float = DEFAULT_ACCESS_WEIGHT
    forget_threshold: float = DEFAULT_FORGET_THRESHOLD

    def __post_init__(self) -> None:
        if self.decay_lambda < 0:
            raise ValueError("decay_lambda must be non-negative")
        if not 0.0 <= self.forget_threshold < 1.0:
            raise ValueError("forget_threshold must be in [0, 1)")

    def half_life_days(self) -> float:
        """Days until an untouched memory retains half its importance."""
        if self.decay_lambda == 0:
            return math.inf
        return math.log(2) / self.decay_lambda


# A single shared instance, so `policy=None` and "the default policy" are the
# same object everywhere. Frozen, so sharing it is safe.
DEFAULT_POLICY = RetentionPolicy()


def retention_score(
    *,
    importance: float,
    last_accessed_at: int | None,
    created_at: int,
    access_count: int,
    now: int,
    policy: RetentionPolicy | None = None,
) -> float:
    """Current retention score for one memory.

    `last_accessed_at` falls back to `created_at`: a memory that has never been
    read still ages from when it was written, otherwise a null would read as
    "accessed at epoch zero" and everything unread would decay instantly.
    """
    policy = policy or DEFAULT_POLICY
    reference = last_accessed_at if last_accessed_at is not None else created_at
    elapsed_days = max(0.0, (now - reference) / SECONDS_PER_DAY)

    decayed = importance * math.exp(-policy.decay_lambda * elapsed_days)
    # Logarithmic on purpose: re-reading a memory should keep it alive, but
    # frequency alone must not let trivia outrank an important fact recalled once.
    frequency = policy.access_weight * math.log10(1 + max(0, access_count))
    return decayed + frequency


def should_forget(
    *,
    score: float,
    pinned: bool,
    policy: RetentionPolicy | None = None,
) -> bool:
    """Whether a memory has fallen below the retention threshold."""
    if pinned:
        return False
    return score < (policy or DEFAULT_POLICY).forget_threshold
