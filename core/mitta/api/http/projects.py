"""Project CRUD, filesystem paths and timeline (API_DESIGN.md §3.4).

Two of these routes are not ordinary CRUD.

`POST` and `DELETE` on `/paths` edit a security boundary — they are what decides
whether a later filesystem action runs, asks or is refused — so both write a line
to the audit log. A permission change that leaves no trace is indistinguishable
from one that never happened, which is the same argument DEC-081 makes about
denials.

`/resolve-path` answers "what would MITTA do with this path" without a tool
asking. R5's enforcement clause is that anything the user cannot inspect they
cannot trust, and a boundary whose only observable behaviour is a confirmation
card at the moment of action is not inspectable. This route is an addition to
§3.4, not a substitution.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Request, Response, status

from mitta.api.auth import RequireToken
from mitta.api.schemas.memory import MemoryListResponse, MemoryResource
from mitta.api.schemas.projects import (
    AddPathRequest,
    CreateProjectRequest,
    PathListResponse,
    PathResource,
    ProjectListResponse,
    ProjectSummary,
    ResolutionResource,
    TimelineEventResource,
    TimelineResponse,
    UpdateProjectRequest,
)
from mitta.memory.models import MemoryKind, MemoryStatus
from mitta.memory.service import MemoryService
from mitta.projects.boundary import PathBoundary
from mitta.projects.models import ProjectDraft, ProjectPathDraft, ProjectStatus
from mitta.projects.repository import ProjectRepository

router = APIRouter(prefix="/v1/projects", tags=["projects"])


def _repo(request: Request) -> ProjectRepository:
    repository: ProjectRepository = request.app.state.projects
    return repository


def _boundary(request: Request) -> PathBoundary:
    boundary: PathBoundary = request.app.state.path_boundary
    return boundary


@router.post(
    "",
    response_model=ProjectSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Create a project",
)
async def create(request: Request, body: CreateProjectRequest, _: RequireToken) -> ProjectSummary:
    draft = ProjectDraft(
        name=body.name,
        description=body.description,
        color=body.color,
        settings=body.settings,
    )
    return ProjectSummary.of(_repo(request).create(draft))


@router.get("", response_model=ProjectListResponse, summary="List projects")
async def list_projects(
    request: Request,
    _: RequireToken,
    # `all` is a literal rather than an omitted parameter. "No filter" cannot be
    # expressed as a query value — an empty `?status=` is a validation error, not
    # a null — so the third state has to be named to be reachable at all.
    project_status: Literal["active", "archived", "all"] = Query(default="active", alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> ProjectListResponse:
    repository = _repo(request)
    wanted = None if project_status == "all" else ProjectStatus(project_status)
    projects = repository.list_projects(status=wanted, limit=limit, offset=offset)
    counts = repository.path_counts()
    return ProjectListResponse(
        projects=[
            ProjectSummary.of(project, path_count=counts.get(project.id, 0)) for project in projects
        ],
        total=repository.count(status=wanted),
    )


# Declared before `/{project_id}`, and the order is load-bearing: FastAPI matches
# routes in registration order, so a literal segment registered after the
# parameterised one is unreachable — every request would be read as a project id.
@router.get("/resolve-path", response_model=ResolutionResource, summary="Inspect the boundary")
async def resolve_path(
    request: Request,
    _: RequireToken,
    path: str = Query(min_length=1, description="Absolute or ~-relative."),
) -> ResolutionResource:
    """What the policy engine would conclude about this path, and why.

    Read-only and side-effect-free. It reports on the boundary; it does not touch
    the filesystem beyond the `stat` calls that resolving symlinks requires, and
    it never reveals whether the path exists.
    """
    return ResolutionResource.of(_boundary(request).resolve(path))


@router.get("/{project_id}", response_model=ProjectSummary, summary="Read one")
async def get(request: Request, project_id: str, _: RequireToken) -> ProjectSummary:
    repository = _repo(request)
    project = repository.get(project_id)
    return ProjectSummary.of(project, path_count=len(repository.paths(project_id)))


@router.patch("/{project_id}", response_model=ProjectSummary, summary="Update or archive")
async def update(
    request: Request, project_id: str, body: UpdateProjectRequest, _: RequireToken
) -> ProjectSummary:
    repository = _repo(request)
    project = repository.update(
        project_id,
        name=body.name,
        description=body.description,
        color=body.color,
        settings=body.settings,
    )
    if body.status is not None and body.status is not project.status:
        project = repository.set_status(project_id, body.status)
        # Archiving withdraws every write grant this project held, which is a
        # change to what MITTA may do rather than a change to how a list is
        # sorted. It belongs in the log the user reads to check that.
        request.app.state.audit.record(
            actor="user",
            action=f"project.{body.status.value}",
            subject=project_id,
            detail={"name": project.name},
        )
    return ProjectSummary.of(project, path_count=len(repository.paths(project_id)))


@router.delete(
    "/{project_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete permanently"
)
async def delete(request: Request, project_id: str, _: RequireToken) -> Response:
    """Irreversible. Cascades to paths and to project-scoped memories.

    Conversations survive and become unscoped — the schema's own
    `ON DELETE SET NULL`. A project memory without its project is meaningless; a
    thread without one is just a thread.
    """
    repository = _repo(request)
    project = repository.get(project_id)
    repository.delete(project_id)
    request.app.state.audit.record(
        actor="user",
        action="project.deleted",
        subject=project_id,
        detail={"name": project.name},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{project_id}/paths", response_model=PathListResponse, summary="Registered paths")
async def paths(request: Request, project_id: str, _: RequireToken) -> PathListResponse:
    repository = _repo(request)
    repository.get(project_id)  # 404 rather than an empty list for a bad id
    return PathListResponse(
        paths=[PathResource.of(entry) for entry in repository.paths(project_id)],
        project_id=project_id,
    )


@router.post(
    "/{project_id}/paths",
    response_model=PathResource,
    status_code=status.HTTP_201_CREATED,
    summary="Register a path",
)
async def add_path(
    request: Request, project_id: str, body: AddPathRequest, _: RequireToken
) -> PathResource:
    """Edits the write boundary, so it is audited.

    The path is canonicalised in the repository, and the audit entry records the
    canonical form rather than what was submitted — `~/work/../.ssh` and
    `/Users/satya/.ssh` are the same grant, and a log that records the first
    hides what was actually permitted.
    """
    repository = _repo(request)
    entry = repository.add_path(
        project_id, ProjectPathDraft(path=body.path, kind=body.kind, writable=body.writable)
    )
    request.app.state.audit.record(
        actor="user",
        action="project.path_registered",
        subject=entry.path,
        verdict="allow",
        detail={
            "project_id": project_id,
            "kind": entry.kind.value,
            "writable": entry.writable,
            "submitted_as": body.path,
        },
    )
    return PathResource.of(entry)


@router.delete(
    "/{project_id}/paths",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deregister a path",
)
async def remove_path(
    request: Request,
    project_id: str,
    _: RequireToken,
    path: str = Query(min_length=1),
) -> Response:
    """The path travels as a query parameter rather than in the URL.

    An absolute filesystem path in a path segment has to be encoded, and a
    double-encoded slash is a class of bug that ends with the wrong permission
    being revoked. `DELETE` with a body is worse — not every client sends one.
    """
    repository = _repo(request)
    entry = repository.get_path(project_id, path)
    repository.remove_path(project_id, path)
    request.app.state.audit.record(
        actor="user",
        action="project.path_removed",
        subject=entry.path,
        verdict="allow",
        detail={"project_id": project_id, "was_writable": entry.writable},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{project_id}/memory", response_model=MemoryListResponse, summary="Scoped memory")
async def project_memory(
    request: Request,
    project_id: str,
    _: RequireToken,
    kind: MemoryKind | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> MemoryListResponse:
    """Project-scoped memory.

    Overlaps `GET /v1/memory?project_id=…` deliberately, and differs in one way
    that matters: an unknown id 404s here instead of returning an empty list. A
    typo'd filter that answers "no memories" is indistinguishable from a project
    that genuinely has none.
    """
    _repo(request).get(project_id)
    service: MemoryService = request.app.state.memory
    memories = service.list_memories(
        kind=kind,
        project_id=project_id,
        status=MemoryStatus.ACTIVE,
        limit=limit,
        offset=offset,
    )
    return MemoryListResponse(
        memories=[MemoryResource.of(memory) for memory in memories],
        total=len(memories),
        limit=limit,
        offset=offset,
    )


@router.get("/{project_id}/timeline", response_model=TimelineResponse, summary="Episodic timeline")
async def timeline(
    request: Request,
    project_id: str,
    _: RequireToken,
    limit: int = Query(default=100, ge=1, le=500),
) -> TimelineResponse:
    """Empty until something writes an episode.

    Nothing in this phase does. The endpoint exists so the shape is fixed and the
    surface is not built against a guess.
    """
    repository = _repo(request)
    repository.get(project_id)
    events = repository.timeline(project_id, limit=limit)
    return TimelineResponse(
        events=[TimelineEventResource.of(event) for event in events],
        project_id=project_id,
    )
