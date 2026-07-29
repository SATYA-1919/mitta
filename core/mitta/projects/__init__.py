"""Projects — the scope that memory, conversations and file access hang off.

`projects` is organisational; `project_paths` is a security boundary, resolved
by the policy engine before any filesystem action (`DATABASE_DESIGN.md` §6).
`project_resources` has a table and no code — nothing can reach it yet, so
nothing pretends to.

No service layer, for the same reason `mitta.conversations` has none: the
repository plus `boundary` is the whole of the current behaviour, and a
pass-through facade would be indirection with no payoff.
"""

from mitta.projects.boundary import (
    Containment,
    PathBoundary,
    PathLookup,
    Resolution,
    canonicalise,
    classify,
)
from mitta.projects.models import (
    PathKind,
    Project,
    ProjectDraft,
    ProjectPath,
    ProjectPathDraft,
    ProjectStatus,
    TimelineEvent,
)
from mitta.projects.repository import ProjectRepository

__all__ = [
    "Containment",
    "PathBoundary",
    "PathKind",
    "PathLookup",
    "Project",
    "ProjectDraft",
    "ProjectPath",
    "ProjectPathDraft",
    "ProjectRepository",
    "ProjectStatus",
    "Resolution",
    "TimelineEvent",
    "canonicalise",
    "classify",
]
