"""The write boundary: where MITTA may act on the filesystem, and where it must ask.

`DATABASE_DESIGN.md` §6 calls `project_paths` a security table rather than an
organisational one, and this module is the reason. Everything here answers one
question — *given a filesystem path, what has the user actually permitted?* —
and the answer is an input to the policy engine's ALLOW/CONFIRM decision
(`ARCHITECTURE.md` §9).

Two rules, and both are load-bearing.

**Canonicalise before comparing.** A prefix check against a path the caller
supplied is not a check. `/tmp/../Users/satya/.ssh/id_ed25519` is outside `/tmp`
under `str.startswith` and inside it under no definition that matters; a symlink
at `~/project/data` pointing to `~/.aws` is inside the root by name and outside
it in reality. Paths are resolved — symlinks followed, `..` collapsed, `~`
expanded — before they are stored and before they are matched.

**Exclusion beats containment.** A project rooted at `~/work/mitta` with
`~/work/mitta/.env` excluded is the case exclusion exists for, and it only works
if the more specific rule wins regardless of insertion order. `classify` resolves
by longest match, so a nested exclusion inside a granted root refuses, and a
nested root inside an excluded tree grants — the user's most specific statement
about a path is the one that holds.

There is no "deny by default outside every root" rule here, and that omission is
deliberate. Outside all roots is *unknown*, not forbidden: MITTA asks. Treating
unknown as forbidden would make a machine with no projects configured a machine
where nothing works, and the honest answer to "may I touch this file you never
told me about" is a question.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePath
from typing import Protocol, runtime_checkable

from mitta.projects.models import PathKind, ProjectPath


class Containment(StrEnum):
    """Where a path sits relative to everything the user has registered."""

    #: Inside a registered path marked writable, with no closer exclusion.
    WRITABLE = "writable"

    #: Inside a registered path, but that path is read-only.
    READ_ONLY = "read_only"

    #: Inside an `excluded` path — or inside a root whose closest matching rule
    #: is an exclusion. The user has said "not this", so nothing here runs
    #: without them saying otherwise again.
    EXCLUDED = "excluded"

    #: Inside no registered path at all. Not forbidden; unknown.
    OUTSIDE = "outside"


@dataclass(frozen=True, slots=True)
class Resolution:
    """The boundary's answer about one path."""

    path: str
    containment: Containment
    #: The registered path that decided it, and the project that owns it. Both
    #: null when nothing matched. Carried so a confirmation prompt can say
    #: *which* rule applied instead of asserting a verdict with no reason — a
    #: prompt that cannot explain itself is a prompt that gets approved by
    #: reflex.
    matched_path: str | None = None
    project_id: str | None = None

    @property
    def is_writable(self) -> bool:
        return self.containment is Containment.WRITABLE

    @property
    def refused(self) -> bool:
        """A standing refusal. No confirmation can lift it — see DEC-111."""
        return self.containment is Containment.EXCLUDED

    @property
    def needs_confirmation(self) -> bool:
        """Whether a write here would have to be asked about.

        Excluded is **not** included, and that exception is the point. An
        exclusion is a refusal, not a question, so reporting it as "needs
        confirmation" would have the UI offer the user a choice the engine will
        not honour. The two are distinguished here, once, rather than in every
        caller that renders a verdict.
        """
        return self.containment in (Containment.READ_ONLY, Containment.OUTSIDE)

    def describe(self) -> str:
        """A human sentence for the confirmation card."""
        # A path contains itself, so the matched rule is often the target. Saying
        # "X is inside X" is technically true and reads as a bug, which costs the
        # sentence the credibility it needs to be read at all.
        itself = self.matched_path == self.path
        match self.containment:
            case Containment.WRITABLE:
                return f"{self.path} is inside a writable project path."
            case Containment.READ_ONLY:
                if itself:
                    return f"{self.path} is registered, but not writable."
                return f"{self.path} is inside {self.matched_path}, which is not writable."
            case Containment.EXCLUDED:
                if itself:
                    return f"{self.path} is excluded."
                return f"{self.path} is inside {self.matched_path}, which you excluded."
            case Containment.OUTSIDE:
                return f"{self.path} is outside every project path you have configured."


def canonicalise(raw: str | Path) -> Path:
    """Expand, absolutise and resolve a path for storage or comparison.

    `strict=False` because a path may legitimately not exist yet — registering a
    project root before creating it, or checking a file about to be written.
    What is resolved is every component that *does* exist, which is what closes
    the symlink hole: an attacker-controlled symlink has to exist to be
    followed, and if it exists it is followed here.
    """
    return Path(raw).expanduser().resolve(strict=False)


def _is_within(candidate: PurePath, ancestor: PurePath) -> bool:
    """Containment on path components, never on characters.

    `str.startswith` reports `/home/satya-backup` as inside `/home/satya`. This
    does not, because it compares `('home', 'satya-backup')` against
    `('home', 'satya')` element by element. A path is also within itself, which
    is what makes registering a file rather than a directory work.
    """
    return candidate == ancestor or ancestor in candidate.parents


def classify(target: str | Path, registered: Iterable[ProjectPath]) -> Resolution:
    """Resolve one filesystem path against every registered project path.

    Longest match wins: the deepest registered path containing the target is the
    one that decides, so a specific exclusion inside a broad root refuses and a
    specific root inside an excluded tree grants. Insertion order is irrelevant,
    which matters because the user adds paths over months and cannot be expected
    to remember what they added first.
    """
    resolved = canonicalise(target)

    best: ProjectPath | None = None
    best_depth = -1
    for entry in registered:
        ancestor = PurePath(entry.path)
        if not _is_within(resolved, ancestor):
            continue
        depth = len(ancestor.parts)
        # Strictly greater, so the first of two equally specific rules wins
        # rather than the last. Ties are only possible between two rules for the
        # same path, which the UNIQUE (project_id, path) constraint already
        # limits to one per project.
        if depth > best_depth:
            best, best_depth = entry, depth

    if best is None:
        return Resolution(path=str(resolved), containment=Containment.OUTSIDE)

    if best.kind is PathKind.EXCLUDED:
        containment = Containment.EXCLUDED
    elif best.writable:
        containment = Containment.WRITABLE
    else:
        containment = Containment.READ_ONLY

    return Resolution(
        path=str(resolved),
        containment=containment,
        matched_path=best.path,
        project_id=best.project_id,
    )


@runtime_checkable
class PathLookup(Protocol):
    """What the boundary needs from storage. Implemented by `ProjectRepository`.

    One method, and it takes the target rather than returning everything: only
    the ancestors of a path can contain it, so the query is bounded by path
    depth instead of by how many projects exist.
    """

    def paths_containing(self, target: Path) -> Sequence[ProjectPath]: ...


class PathBoundary:
    """The policy engine's view of the boundary.

    A one-method seam, held by `PolicyEngine` so the engine never learns that
    project paths live in SQLite. The engine is constructed with this rather
    than with a repository for the same reason the Tool Manager is constructed
    without an OS Adapter: the narrowest reference that does the job is the one
    that cannot be misused later.
    """

    def __init__(self, lookup: PathLookup) -> None:
        self._lookup = lookup

    def resolve(self, target: str | Path) -> Resolution:
        resolved = canonicalise(target)
        # Only the ancestors of the target can contain it, so the repository is
        # asked for those rather than for every registered path. This is what
        # `idx_project_paths_lookup` exists to serve, and it keeps the cost of a
        # filesystem tool call independent of how many projects exist.
        return classify(resolved, self._lookup.paths_containing(resolved))
