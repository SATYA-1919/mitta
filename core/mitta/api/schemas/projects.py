"""Project wire schemas.

`PathResource` carries `writable` and `kind` on the wire because the UI has to
render a security boundary, and a boundary the user cannot see the shape of is
one they cannot audit (R5). `ResolutionResource` exists for the same reason: it
lets the UI answer "what would MITTA do with this path" *before* a tool asks,
rather than the user discovering the rule from a confirmation card.
"""

from __future__ import annotations

from pydantic import Field

from mitta.api.schemas.common import Schema
from mitta.projects.boundary import Containment, Resolution
from mitta.projects.models import (
    PathKind,
    Project,
    ProjectPath,
    ProjectStatus,
    TimelineEvent,
)


class PathResource(Schema):
    project_id: str
    path: str
    kind: PathKind
    writable: bool
    created_at: int

    @classmethod
    def of(cls, entry: ProjectPath) -> PathResource:
        return cls(
            project_id=entry.project_id,
            path=entry.path,
            kind=entry.kind,
            writable=entry.writable,
            created_at=entry.created_at,
        )


class ProjectSummary(Schema):
    id: str
    name: str
    description: str | None
    color: str | None
    status: ProjectStatus
    settings: dict[str, object]
    #: Denormalised for the list view, which shows it per row. Counting in the
    #: query beats a request per project from the client.
    path_count: int
    created_at: int
    updated_at: int

    @classmethod
    def of(cls, project: Project, *, path_count: int = 0) -> ProjectSummary:
        return cls(
            id=project.id,
            name=project.name,
            description=project.description,
            color=project.color,
            status=project.status,
            settings=project.settings,
            path_count=path_count,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )


class TimelineEventResource(Schema):
    id: str
    occurred_at: int
    event_type: str
    title: str
    detail: str | None
    payload: dict[str, object]

    @classmethod
    def of(cls, event: TimelineEvent) -> TimelineEventResource:
        return cls(
            id=event.id,
            occurred_at=event.occurred_at,
            event_type=event.event_type,
            title=event.title,
            detail=event.detail,
            payload=event.payload,
        )


class ResolutionResource(Schema):
    """What the policy engine would conclude about one path, exposed for inspection."""

    path: str
    containment: Containment
    matched_path: str | None
    project_id: str | None
    #: True when a filesystem action here would be asked about. Derived rather
    #: than recomputed client-side, so the UI and the engine cannot drift.
    needs_confirmation: bool
    #: True when it would be refused outright. Distinct from `needs_confirmation`
    #: and never both: a UI that showed an exclusion as "MITTA will ask" would be
    #: offering the user a choice the engine does not honour.
    refused: bool
    explanation: str

    @classmethod
    def of(cls, resolution: Resolution) -> ResolutionResource:
        return cls(
            path=resolution.path,
            containment=resolution.containment,
            matched_path=resolution.matched_path,
            project_id=resolution.project_id,
            needs_confirmation=resolution.needs_confirmation,
            refused=resolution.refused,
            explanation=resolution.describe(),
        )


class CreateProjectRequest(Schema):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    color: str | None = Field(default=None, max_length=32)
    settings: dict[str, object] = Field(default_factory=dict)


class UpdateProjectRequest(Schema):
    """Every field optional; `None` means "leave it alone".

    `status` is here rather than on a separate archive endpoint because, unlike a
    conversation, archiving a project changes what MITTA may do — an archived
    project's paths stop granting access (`ProjectRepository.paths_containing`).
    Keeping it in the patch body means one place to look for "what changed about
    this project".
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    color: str | None = Field(default=None, max_length=32)
    settings: dict[str, object] | None = None
    status: ProjectStatus | None = None


class AddPathRequest(Schema):
    path: str = Field(min_length=1, description="Absolute or ~-relative. Canonicalised on save.")
    kind: PathKind = PathKind.ROOT
    writable: bool = Field(
        default=False,
        description="Granting write is deliberate and separate from adding the path.",
    )


class ProjectListResponse(Schema):
    projects: list[ProjectSummary]
    total: int


class PathListResponse(Schema):
    paths: list[PathResource]
    project_id: str


class TimelineResponse(Schema):
    events: list[TimelineEventResource]
    project_id: str
