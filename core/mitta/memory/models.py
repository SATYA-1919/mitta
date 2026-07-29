"""Memory domain models.

Six conceptual stores, one table, one `kind` discriminator (DEC-023). What
differs between kinds lives in `attributes`, validated here against a per-kind
model rather than trusted as free-form JSON.

That validation is the point of this module. `attributes` is a JSON column, so
SQLite will accept `{"catgory": "work"}` without complaint and the memory
becomes permanently unqueryable by category — a silent data-loss bug that
surfaces months later as "why doesn't search find this". The schema cannot catch
it. This can.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mitta.errors import ValidationError

# ── Enumerations ───────────────────────────────────────────────────────────
#
# Values are the exact strings in the `CHECK` constraints of `0001_initial.sql`.
# Drift here is a runtime IntegrityError, so these are load-bearing.


class MemoryKind(StrEnum):
    LONG_TERM = "long_term"
    PROJECT = "project"
    EPISODIC = "episodic"
    RELATIONSHIP = "relationship"
    PREFERENCE = "preference"
    PROCEDURAL = "procedural"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    FORGOTTEN = "forgotten"


class SourceKind(StrEnum):
    CONVERSATION = "conversation"
    TOOL = "tool"
    USER = "user"
    IMPORT = "import"
    CONSOLIDATION = "consolidation"


# ── Per-kind attributes ────────────────────────────────────────────────────


class _Attributes(BaseModel):
    """Base for every attribute model.

    `extra="forbid"` is deliberate. An unrecognised key is far more likely to be
    a typo than a feature, and accepting it writes a value nothing will ever
    read back.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class LongTermAttributes(_Attributes):
    category: str | None = None
    entities: list[str] = Field(default_factory=list)


class ProjectAttributes(_Attributes):
    artifact_type: str | None = None
    path: str | None = None
    commit_sha: str | None = None


class EpisodicAttributes(_Attributes):
    # Unix seconds. When the event happened, which is not when it was recorded —
    # "last Tuesday's outage" is written today and must not sort as today.
    occurred_at: int | None = None
    event_type: str | None = None
    participants: list[str] = Field(default_factory=list)


class RelationshipAttributes(_Attributes):
    person_id: str | None = None
    relation: str | None = None
    sentiment: float | None = Field(default=None, ge=-1.0, le=1.0)


class PreferenceAttributes(_Attributes):
    domain: str | None = None
    polarity: Literal["likes", "dislikes", "neutral"] | None = None
    derived_from: str | None = None


class ProceduralAttributes(_Attributes):
    trigger: str | None = None
    steps: list[str] = Field(default_factory=list)
    success_count: int = Field(default=0, ge=0)


type MemoryAttributes = (
    LongTermAttributes
    | ProjectAttributes
    | EpisodicAttributes
    | RelationshipAttributes
    | PreferenceAttributes
    | ProceduralAttributes
)

ATTRIBUTES_BY_KIND: dict[MemoryKind, type[_Attributes]] = {
    MemoryKind.LONG_TERM: LongTermAttributes,
    MemoryKind.PROJECT: ProjectAttributes,
    MemoryKind.EPISODIC: EpisodicAttributes,
    MemoryKind.RELATIONSHIP: RelationshipAttributes,
    MemoryKind.PREFERENCE: PreferenceAttributes,
    MemoryKind.PROCEDURAL: ProceduralAttributes,
}

# Every kind must have a model. Checked at import, because a missing entry would
# otherwise be a KeyError on the first write of a rarely used kind.
assert set(ATTRIBUTES_BY_KIND) == set(MemoryKind), "attribute model missing for a memory kind"


def parse_attributes(kind: MemoryKind, raw: object) -> _Attributes:
    """Validate `raw` against the attribute model for `kind`."""
    model = ATTRIBUTES_BY_KIND[kind]
    if not isinstance(raw, dict):
        raise ValidationError(f"attributes for {kind} must be an object, got {type(raw).__name__}")
    try:
        return model.model_validate(raw)
    except Exception as exc:
        raise ValidationError(f"invalid attributes for kind {kind}: {exc}") from exc


# ── Records ────────────────────────────────────────────────────────────────

Importance = Annotated[float, Field(ge=0.0, le=1.0)]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class MemoryDraft(BaseModel):
    """A memory as submitted for writing. No identity, no timestamps.

    Separate from `Memory` so the write path cannot accept a caller-supplied
    `seq`, `created_at` or `access_count` — those are the store's to assign, and
    a model that permits them invites a caller to set them.
    """

    model_config = ConfigDict(extra="forbid")

    kind: MemoryKind
    content: str = Field(min_length=1)
    summary: str | None = None
    attributes: dict[str, object] = Field(default_factory=dict)
    project_id: str | None = None

    importance: Importance = 0.5
    confidence: Confidence = 1.0

    source_kind: SourceKind = SourceKind.USER
    source_message_id: str | None = None

    pinned: bool = False
    expires_at: int | None = None

    @model_validator(mode="after")
    def _validate_attributes(self) -> Self:
        parse_attributes(self.kind, self.attributes)
        return self

    @model_validator(mode="after")
    def _project_memories_need_a_project(self) -> Self:
        # A project memory with no project is unscoped and will surface in every
        # unrelated context — the precise failure a project store exists to avoid.
        if self.kind is MemoryKind.PROJECT and self.project_id is None:
            raise ValueError("project memories require a project_id")
        return self


class Memory(BaseModel):
    """A stored memory, as read back."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    seq: int
    id: str
    kind: MemoryKind
    project_id: str | None

    content: str
    summary: str | None
    attributes: dict[str, object]

    importance: Importance
    confidence: Confidence

    status: MemoryStatus
    superseded_by: str | None

    source_kind: SourceKind
    source_message_id: str | None

    content_hash: str
    pinned: bool
    access_count: int
    last_accessed_at: int | None
    expires_at: int | None

    created_at: int
    updated_at: int

    @property
    def context_text(self) -> str:
        """What goes into a context window.

        Summary when present: context budget is the scarcest resource in the
        system (R5's chokepoint), and a 40-token summary that carries the fact
        beats a 400-token verbatim record that carries it more slowly.
        """
        return self.summary if self.summary else self.content


class MemoryPatch(BaseModel):
    """A partial update. Absent fields are left alone.

    `None` is a legal value for the nullable fields, so "not supplied" cannot be
    encoded as `None` — hence the sentinel-free approach of a separate
    `model_fields_set` check in the repository.
    """

    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(default=None, min_length=1)
    summary: str | None = None
    attributes: dict[str, object] | None = None
    importance: Importance | None = None
    confidence: Confidence | None = None
    pinned: bool | None = None
    expires_at: int | None = None
