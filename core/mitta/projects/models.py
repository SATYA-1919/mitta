"""Project and path models.

A project is a **scope**. It answers "which of these memories, conversations and
filesystem locations belong together", and every one of those three already
carries a `project_id` column waiting for something to set it.

A project's name and colour are organisational, and getting them wrong costs
tidiness. Its **paths** are not: `project_paths` is consulted by the policy
engine before a filesystem action, and getting one wrong costs the user a file.
That is why `ProjectPath` is only ever built through `boundary.canonicalise` and
never from a raw string, and why `writable` defaults to false.

Records are frozen dataclasses; drafts are Pydantic models. The split follows
`mitta.conversations.models`: dataclasses are built from trusted database rows,
Pydantic validates input that came from outside the process.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class PathKind(StrEnum):
    """What a registered path *is*, which determines how policy treats it.

    `EXCLUDED` is not "a path we do not care about" — it is a hole punched
    inside a root that has already been granted. A project rooted at
    `~/work/mitta` with `~/work/mitta/.env` excluded is the case this exists
    for, and it only works if exclusion beats containment. See
    `boundary.classify`.
    """

    ROOT = "root"
    REPO = "repo"
    DOCS = "docs"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class Project:
    seq: int
    id: str
    name: str
    description: str | None
    color: str | None
    status: ProjectStatus
    settings: dict[str, Any]
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class ProjectPath:
    seq: int
    project_id: str
    #: Absolute and canonicalised — symlinks resolved, `..` collapsed. Stored
    #: this way because a prefix check against an uncanonicalised path is not a
    #: check at all: `/tmp/../Users/satya/.ssh` is outside `/tmp` by string
    #: comparison and inside it by nothing that matters.
    path: str
    kind: PathKind
    writable: bool
    created_at: int

    @property
    def is_excluded(self) -> bool:
        return self.kind is PathKind.EXCLUDED


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """One row of `episodes`, scoped to a project.

    Read-only here. Episodes are written by whatever observed the event; this
    module only ever queries them, so there is no `EpisodeDraft`.
    """

    seq: int
    id: str
    occurred_at: int
    event_type: str
    title: str
    detail: str | None
    project_id: str | None
    payload: dict[str, Any]


class ProjectDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    #: Free-form, because the UI decides what a colour means. Length-capped so a
    #: caller cannot use it as a general-purpose blob store.
    color: str | None = Field(default=None, max_length=32)
    settings: dict[str, Any] = Field(default_factory=dict)


class ProjectPathDraft(BaseModel):
    """A path as submitted. Canonicalisation happens in the repository.

    `writable` defaults to false, and that default is the whole point: adding a
    root should widen what MITTA can *see* without widening what it can change.
    Granting write is a second, deliberate act.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    kind: PathKind = PathKind.ROOT
    writable: bool = False
