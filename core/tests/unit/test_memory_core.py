"""Normalisation, retention and domain-model validation."""

from __future__ import annotations

import math

import pytest

from mitta.errors import ValidationError
from mitta.memory.models import (
    MemoryDraft,
    MemoryKind,
    ProceduralAttributes,
    parse_attributes,
)
from mitta.memory.normalise import content_hash, normalise
from mitta.memory.retention import (
    RetentionPolicy,
    retention_score,
    should_forget,
)


class TestNormalise:
    def test_collapses_only_differences_a_model_cannot_see(self) -> None:
        assert normalise("hello  world") == "hello  world"  # internal spacing kept
        assert normalise("hello\r\nworld") == "hello\nworld"
        assert normalise("trailing   \nspace") == "trailing\nspace"
        assert normalise("\n\npadded\n\n") == "padded"

    def test_unicode_forms_that_render_identically_hash_identically(self) -> None:
        composed = "café"  # é as one codepoint
        decomposed = "café"  # e + combining acute
        assert composed != decomposed
        assert content_hash(composed) == content_hash(decomposed)

    def test_case_and_punctuation_are_preserved(self) -> None:
        # Both change meaning the embedding model encodes. Folding them would
        # make a real edit invisible to the staleness check.
        assert content_hash("the deploy failed") != content_hash("The deploy failed")
        assert content_hash("it works") != content_hash("it works?")

    def test_indentation_is_preserved(self) -> None:
        assert normalise("def f():\n    return 1") == "def f():\n    return 1"

    def test_hash_is_stable_and_short(self) -> None:
        digest = content_hash("anything")
        assert digest == content_hash("anything")
        assert len(digest) == 32


class TestAttributes:
    def test_typo_in_an_attribute_key_is_rejected(self) -> None:
        # The whole reason attributes are validated: SQLite would store
        # {"catgory": "work"} happily and nothing would ever read it back.
        with pytest.raises(ValidationError):
            parse_attributes(MemoryKind.LONG_TERM, {"catgory": "work"})

    def test_valid_attributes_parse_per_kind(self) -> None:
        parsed = parse_attributes(
            MemoryKind.PROCEDURAL,
            {"trigger": "every friday", "steps": ["open report", "send"], "success_count": 3},
        )
        assert isinstance(parsed, ProceduralAttributes)
        assert parsed.success_count == 3

    def test_attributes_from_the_wrong_kind_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            parse_attributes(MemoryKind.LONG_TERM, {"trigger": "friday"})

    def test_non_object_attributes_are_rejected(self) -> None:
        with pytest.raises(ValidationError):
            parse_attributes(MemoryKind.LONG_TERM, ["not", "an", "object"])

    def test_sentiment_is_bounded(self) -> None:
        with pytest.raises(ValidationError):
            parse_attributes(MemoryKind.RELATIONSHIP, {"sentiment": 5.0})

    def test_project_memory_requires_a_project(self) -> None:
        # An unscoped project memory surfaces in every unrelated context, which
        # is the exact failure a project store exists to prevent.
        with pytest.raises(ValueError, match="project_id"):
            MemoryDraft(kind=MemoryKind.PROJECT, content="uses pnpm")

        MemoryDraft(kind=MemoryKind.PROJECT, content="uses pnpm", project_id="prj_1")

    def test_draft_rejects_store_owned_fields(self) -> None:
        with pytest.raises(ValueError, match="seq"):
            MemoryDraft.model_validate(
                {"kind": "long_term", "content": "x", "seq": 5},
            )


class TestRetention:
    def test_fresh_important_memory_scores_near_its_importance(self) -> None:
        score = retention_score(
            importance=0.9, last_accessed_at=1000, created_at=1000, access_count=0, now=1000
        )
        assert score == pytest.approx(0.9)

    def test_decays_by_half_over_the_half_life(self) -> None:
        policy = RetentionPolicy()
        half_life = policy.half_life_days()
        assert half_life == pytest.approx(46.2, abs=0.5)

        later = int(half_life * 86_400)
        score = retention_score(
            importance=0.8, last_accessed_at=0, created_at=0, access_count=0, now=later
        )
        assert score == pytest.approx(0.4, abs=0.01)

    def test_access_term_is_logarithmic(self) -> None:
        # Frequency must not let trivia outrank an important fact recalled once:
        # ten times the reads is nowhere near ten times the boost.
        def boost(count: int) -> float:
            return retention_score(
                importance=0.0, last_accessed_at=0, created_at=0, access_count=count, now=0
            )

        # Ten times the reads buys well under ten times the boost, and the
        # return keeps shrinking — which is what stops a frequently-glanced-at
        # triviality from outranking a genuinely important fact.
        assert boost(10) < 0.5 * (10 * boost(1))
        assert boost(100) < 0.25 * (10 * boost(10))

    def test_never_accessed_memory_ages_from_creation(self) -> None:
        # A null last_accessed_at must not read as "accessed at the epoch",
        # which would decay everything unread to nothing immediately.
        now = 10_000_000
        score = retention_score(
            importance=1.0, last_accessed_at=None, created_at=now, access_count=0, now=now
        )
        assert score == pytest.approx(1.0)

    def test_clock_skew_does_not_produce_growth(self) -> None:
        # A reference timestamp in the future would otherwise make exp(+x)
        # amplify importance beyond 1.
        score = retention_score(
            importance=0.5, last_accessed_at=2000, created_at=2000, access_count=0, now=1000
        )
        assert score == pytest.approx(0.5)

    def test_pinned_is_never_forgotten(self) -> None:
        assert should_forget(score=0.0, pinned=True) is False
        assert should_forget(score=0.0, pinned=False) is True

    def test_zero_decay_means_infinite_half_life(self) -> None:
        assert math.isinf(RetentionPolicy(decay_lambda=0.0).half_life_days())

    def test_invalid_policies_are_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="decay_lambda"):
            RetentionPolicy(decay_lambda=-1.0)
        with pytest.raises(ValueError, match="forget_threshold"):
            RetentionPolicy(forget_threshold=1.0)
