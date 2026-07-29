"""Projects: the repository, the write boundary, and the boundary's effect on policy.

The boundary tests carry most of the weight here. A project's name being wrong is
a cosmetic bug; `classify` being wrong is MITTA writing to a file the user
excluded, so the cases below are written against the specific ways a path check
gets defeated — string prefixes, `..`, symlinks, and rules that fire in the wrong
order.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mitta.errors import NotFoundError
from mitta.persistence.database import Database
from mitta.policy.approval import ApprovalAuthority
from mitta.policy.audit import AuditLog
from mitta.policy.engine import PolicyEngine
from mitta.policy.executor import ToolExecutor
from mitta.projects.boundary import Containment, PathBoundary, canonicalise, classify
from mitta.projects.models import (
    PathKind,
    Project,
    ProjectDraft,
    ProjectPath,
    ProjectPathDraft,
    ProjectStatus,
)
from mitta.projects.repository import ProjectRepository
from mitta.tools.base import Risk, ToolResult, ToolSpec
from mitta.tools.registry import ToolRegistry


def _path(path: str, *, kind: PathKind = PathKind.ROOT, writable: bool = False) -> ProjectPath:
    """A registered path, without a database. `classify` is a pure function."""
    return ProjectPath(
        seq=1,
        project_id="prj_test",
        path=path,
        kind=kind,
        writable=writable,
        created_at=0,
    )


# ── canonicalisation ───────────────────────────────────────────────────────


def test_canonicalise_expands_home() -> None:
    assert canonicalise("~/somewhere").is_absolute()
    assert "~" not in str(canonicalise("~/somewhere"))


def test_canonicalise_collapses_dot_dot(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert canonicalise(nested / ".." / "..") == canonicalise(tmp_path)


def test_canonicalise_resolves_symlinks(tmp_path: Path) -> None:
    """The hole a name-only check leaves open.

    `project/data` looks like it is inside the project and is not. Without
    resolution, registering `project` as writable would silently grant write
    access to `secrets`.
    """
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / "data").symlink_to(secrets)

    assert canonicalise(project / "data" / "key.pem") == canonicalise(secrets / "key.pem")


def test_canonicalise_tolerates_a_path_that_does_not_exist(tmp_path: Path) -> None:
    """Registering a root before creating it, or checking a file about to be
    written, must not raise."""
    target = tmp_path / "not" / "created" / "yet.txt"
    assert canonicalise(target) == target


# ── classification ─────────────────────────────────────────────────────────


def test_no_registered_paths_means_outside_not_denied() -> None:
    """Unknown is a question, not a refusal. A machine with no projects
    configured must not be a machine where nothing works."""
    resolution = classify("/Users/satya/notes.md", [])
    assert resolution.containment is Containment.OUTSIDE
    assert resolution.needs_confirmation


def test_inside_a_writable_root_is_writable() -> None:
    resolution = classify("/work/mitta/core/main.py", [_path("/work/mitta", writable=True)])
    assert resolution.containment is Containment.WRITABLE
    assert not resolution.needs_confirmation
    assert resolution.matched_path == "/work/mitta"
    assert resolution.project_id == "prj_test"


def test_inside_a_read_only_root_still_confirms() -> None:
    resolution = classify("/work/mitta/core/main.py", [_path("/work/mitta")])
    assert resolution.containment is Containment.READ_ONLY
    assert resolution.needs_confirmation


def test_a_registered_path_contains_itself() -> None:
    """Registering a single file, not a directory, has to work."""
    resolution = classify("/work/notes.md", [_path("/work/notes.md", writable=True)])
    assert resolution.containment is Containment.WRITABLE


def test_sibling_with_a_shared_prefix_is_not_inside() -> None:
    """The `startswith` bug, stated as a test.

    `/work/mitta-backup` begins with `/work/mitta` and is a different directory.
    """
    resolution = classify("/work/mitta-backup/dump.sql", [_path("/work/mitta", writable=True)])
    assert resolution.containment is Containment.OUTSIDE


def test_exclusion_beats_the_root_that_contains_it() -> None:
    """The case exclusion exists for."""
    registered = [
        _path("/work/mitta", writable=True),
        _path("/work/mitta/.env", kind=PathKind.EXCLUDED),
    ]
    assert classify("/work/mitta/.env", registered).containment is Containment.EXCLUDED
    # And the rest of the root is untouched by the hole.
    assert classify("/work/mitta/main.py", registered).containment is Containment.WRITABLE


def test_exclusion_covers_everything_beneath_it() -> None:
    registered = [
        _path("/work/mitta", writable=True),
        _path("/work/mitta/secrets", kind=PathKind.EXCLUDED),
    ]
    resolution = classify("/work/mitta/secrets/deep/key.pem", registered)
    assert resolution.containment is Containment.EXCLUDED
    assert resolution.matched_path == "/work/mitta/secrets"


def test_a_root_nested_inside_an_exclusion_grants_again() -> None:
    """Longest match wins in both directions, not just for exclusions.

    The user's most specific statement about a path is the one that holds.
    """
    registered = [
        _path("/work", kind=PathKind.EXCLUDED),
        _path("/work/mitta", writable=True),
    ]
    assert classify("/work/other/file", registered).containment is Containment.EXCLUDED
    assert classify("/work/mitta/file", registered).containment is Containment.WRITABLE


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_insertion_order_does_not_change_the_verdict(order: tuple[int, int]) -> None:
    """Whichever way round the rows come back, the deeper rule decides.

    The user adds paths over months and cannot be expected to remember which
    they added first.
    """
    rules = [_path("/work", writable=True), _path("/work/mitta", kind=PathKind.EXCLUDED)]
    registered = [rules[order[0]], rules[order[1]]]
    assert classify("/work/mitta/x", registered).containment is Containment.EXCLUDED


def test_dot_dot_escape_is_resolved_before_matching() -> None:
    """`/work/mitta/../../etc/passwd` is not inside `/work/mitta`."""
    resolution = classify("/work/mitta/../../etc/passwd", [_path("/work/mitta", writable=True)])
    assert resolution.containment is Containment.OUTSIDE


def test_describe_names_the_rule_that_applied() -> None:
    """A prompt that cannot explain itself is a prompt approved by reflex."""
    registered = [
        _path("/work/mitta", writable=True),
        _path("/work/mitta/.env", kind=PathKind.EXCLUDED),
    ]
    explanation = classify("/work/mitta/.env/inner", registered).describe()
    assert "/work/mitta/.env" in explanation
    assert "excluded" in explanation


def test_describe_does_not_say_a_path_is_inside_itself() -> None:
    """A path contains itself, so the matched rule is usually the target.

    "X is inside X, which you excluded" is true and reads as a bug, which costs
    the sentence the credibility it needs to be read at all.
    """
    explanation = classify(
        "/work/mitta/.env", [_path("/work/mitta/.env", kind=PathKind.EXCLUDED)]
    ).describe()
    assert explanation == "/work/mitta/.env is excluded."


def test_an_exclusion_is_refused_not_confirmed() -> None:
    """`needs_confirmation` must not cover exclusions.

    Found by driving the real UI: the surface rendered "EXCLUDED · MITTA would
    ask", offering the user a choice the engine does not honour.
    """
    resolution = classify("/work/mitta/.env", [_path("/work/mitta/.env", kind=PathKind.EXCLUDED)])
    assert resolution.refused
    assert not resolution.needs_confirmation


@pytest.mark.parametrize(
    ("kind", "writable"),
    [(PathKind.ROOT, False), (PathKind.ROOT, True), (PathKind.EXCLUDED, False)],
)
def test_refused_and_needs_confirmation_are_never_both_true(
    kind: PathKind, writable: bool
) -> None:
    for target in ("/work/mitta/x", "/elsewhere/x"):
        resolution = classify(target, [_path("/work/mitta", kind=kind, writable=writable)])
        assert not (resolution.refused and resolution.needs_confirmation)


# ── repository ─────────────────────────────────────────────────────────────


@pytest.fixture
def project(projects: ProjectRepository) -> Project:
    return projects.create(ProjectDraft(name="MITTA", description="this thing"))


def test_create_and_get(projects: ProjectRepository, project: Project) -> None:
    assert project.id.startswith("prj_")
    assert projects.get(project.id).name == "MITTA"


def test_get_unknown_raises_not_found(projects: ProjectRepository) -> None:
    with pytest.raises(NotFoundError):
        projects.get("prj_nope")


def test_update_leaves_unmentioned_fields_alone(
    projects: ProjectRepository, project: Project
) -> None:
    updated = projects.update(project.id, name="MITTA v2")
    assert updated.name == "MITTA v2"
    assert updated.description == "this thing"


def test_update_on_a_missing_project_raises(projects: ProjectRepository) -> None:
    """Rather than a silent no-op UPDATE."""
    with pytest.raises(NotFoundError):
        projects.update("prj_nope", name="x")


def test_delete_cascades_to_paths(
    projects: ProjectRepository, project: Project, tmp_path: Path
) -> None:
    projects.add_path(project.id, ProjectPathDraft(path=str(tmp_path)))
    projects.delete(project.id)
    assert projects.paths_containing(tmp_path / "file.txt") == []


def test_add_path_stores_the_canonical_form(
    projects: ProjectRepository, project: Project, tmp_path: Path
) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    entry = projects.add_path(project.id, ProjectPathDraft(path=str(nested / ".." / "..")))
    assert entry.path == str(canonicalise(tmp_path))


def test_add_path_defaults_to_read_only(
    projects: ProjectRepository, project: Project, tmp_path: Path
) -> None:
    """Adding a root widens what MITTA can see, not what it can change."""
    assert projects.add_path(project.id, ProjectPathDraft(path=str(tmp_path))).writable is False


def test_re_adding_a_path_updates_it_in_place(
    projects: ProjectRepository, project: Project, tmp_path: Path
) -> None:
    projects.add_path(project.id, ProjectPathDraft(path=str(tmp_path)))
    entry = projects.add_path(project.id, ProjectPathDraft(path=str(tmp_path), writable=True))
    assert entry.writable is True
    assert len(projects.paths(project.id)) == 1


def test_remove_path_is_idempotent_only_once(
    projects: ProjectRepository, project: Project, tmp_path: Path
) -> None:
    projects.add_path(project.id, ProjectPathDraft(path=str(tmp_path)))
    projects.remove_path(project.id, str(tmp_path))
    with pytest.raises(NotFoundError):
        projects.remove_path(project.id, str(tmp_path))


def test_paths_containing_returns_only_ancestors(
    projects: ProjectRepository, project: Project, tmp_path: Path
) -> None:
    inside = tmp_path / "inside"
    outside = tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    projects.add_path(project.id, ProjectPathDraft(path=str(inside)))
    projects.add_path(project.id, ProjectPathDraft(path=str(outside)))

    found = projects.paths_containing(inside / "file.txt")
    assert [entry.path for entry in found] == [str(canonicalise(inside))]


def test_archiving_a_project_withdraws_its_paths(
    projects: ProjectRepository, project: Project, tmp_path: Path
) -> None:
    """Archiving changes what MITTA may do, not just how a list is sorted."""
    projects.add_path(project.id, ProjectPathDraft(path=str(tmp_path), writable=True))
    assert projects.paths_containing(tmp_path / "f") != []

    projects.set_status(project.id, ProjectStatus.ARCHIVED)
    assert projects.paths_containing(tmp_path / "f") == []

    # And unarchiving restores them — the rows were kept, not deleted.
    projects.set_status(project.id, ProjectStatus.ACTIVE)
    assert projects.paths_containing(tmp_path / "f") != []


def test_boundary_resolves_through_the_repository(
    projects: ProjectRepository, project: Project, tmp_path: Path
) -> None:
    """The seam the policy engine actually holds."""
    projects.add_path(project.id, ProjectPathDraft(path=str(tmp_path), writable=True))
    boundary = PathBoundary(projects)
    assert boundary.resolve(tmp_path / "note.md").containment is Containment.WRITABLE
    assert boundary.resolve("/somewhere/else").containment is Containment.OUTSIDE


def test_timeline_is_empty_until_something_writes_an_episode(
    projects: ProjectRepository, project: Project
) -> None:
    assert projects.timeline(project.id) == []


# ── policy: what the boundary changes ──────────────────────────────────────


class _FileTool:
    """A tool that declares a path parameter. Nothing shipped does yet.

    Written here rather than in `mitta.tools` on purpose: the engine's boundary
    check must be verifiable before the first filesystem tool exists, because
    the alternative is shipping that tool and its permission check together and
    discovering the check was wrong from a missing file.
    """

    def __init__(self, risk: Risk = Risk.READ) -> None:
        self._risk = risk

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="read_file",
            description="Read a file.",
            risk=self._risk,
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            path_params=("path",),
        )

    async def run(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(ok=True, content="read it")


@pytest.fixture
def engine(migrated: Database, path_boundary: PathBoundary) -> PolicyEngine:
    return PolicyEngine(AuditLog(migrated), ApprovalAuthority(migrated), boundary=path_boundary)


def test_read_inside_a_registered_root_still_auto_approves(
    engine: PolicyEngine, projects: ProjectRepository, project: Project, tmp_path: Path
) -> None:
    """Reading inside a root is what registering it was for.

    `READ_ONLY` must not escalate a read, or adding a project would make MITTA
    ask before every file it looks at inside that project.
    """
    projects.add_path(project.id, ProjectPathDraft(path=str(tmp_path)))
    decision = engine.evaluate(_FileTool().spec, {"path": str(tmp_path / "f.txt")})
    assert decision.allowed


def test_read_outside_every_project_asks(engine: PolicyEngine, tmp_path: Path) -> None:
    """The gap the boundary closes. Without it this is an unconditional allow."""
    decision = engine.evaluate(_FileTool().spec, {"path": str(tmp_path / "f.txt")})
    assert decision.needs_confirmation
    assert decision.prompt is not None
    assert "outside every project path" in decision.prompt


def test_read_of_an_excluded_path_is_refused(
    engine: PolicyEngine, projects: ProjectRepository, project: Project, tmp_path: Path
) -> None:
    secret = tmp_path / ".env"
    projects.add_path(project.id, ProjectPathDraft(path=str(tmp_path), writable=True))
    projects.add_path(project.id, ProjectPathDraft(path=str(secret), kind=PathKind.EXCLUDED))
    decision = engine.evaluate(_FileTool().spec, {"path": str(secret)})
    assert decision.refused
    assert not decision.needs_confirmation


def test_writable_does_not_make_a_write_silent(
    engine: PolicyEngine, projects: ProjectRepository, project: Project, tmp_path: Path
) -> None:
    """`writable` widens *where*, never *whether*.

    A WRITE tool inside a writable root still asks. Conflating the two would
    turn "this folder is in scope" into "anything here happens unattended".
    """
    projects.add_path(project.id, ProjectPathDraft(path=str(tmp_path), writable=True))
    decision = engine.evaluate(_FileTool(Risk.WRITE).spec, {"path": str(tmp_path / "f.txt")})
    assert decision.needs_confirmation


def test_a_tool_with_no_path_params_is_unaffected(engine: PolicyEngine) -> None:
    """The boundary is opt-in by declaration, so existing tools keep their
    behaviour exactly."""
    spec = ToolSpec(name="web_search", description="search", risk=Risk.READ)
    assert engine.evaluate(spec, {"query": "/etc/passwd"}).allowed


def test_a_non_string_path_argument_fails_closed(engine: PolicyEngine) -> None:
    """A declared path that is a list is a path the boundary cannot resolve.

    Skipping it would hand the tool an unchecked argument.
    """
    assert engine.evaluate(_FileTool().spec, {"path": ["/etc/passwd"]}).refused


def test_the_strictest_of_several_paths_binds(
    engine: PolicyEngine, projects: ProjectRepository, project: Project, tmp_path: Path
) -> None:
    """A call is only as permitted as its least permitted argument.

    Otherwise a copy from an allowed source to an excluded destination goes
    through on the strength of the source.
    """
    excluded = tmp_path / "vault"
    projects.add_path(project.id, ProjectPathDraft(path=str(tmp_path), writable=True))
    projects.add_path(project.id, ProjectPathDraft(path=str(excluded), kind=PathKind.EXCLUDED))
    spec = ToolSpec(
        name="copy_file",
        description="copy",
        risk=Risk.WRITE,
        path_params=("source", "destination"),
    )
    decision = engine.evaluate(
        spec, {"source": str(tmp_path / "ok.txt"), "destination": str(excluded / "x.txt")}
    )
    assert decision.refused


def test_an_approval_token_cannot_lift_a_refusal(
    migrated: Database, projects: ProjectRepository, project: Project, tmp_path: Path
) -> None:
    """The window an exclusion exists to close.

    A token issued while a path was writable must not survive the user excluding
    it. `authorise` checks the refusal before it checks the token.
    """
    approvals = ApprovalAuthority(migrated)
    engine = PolicyEngine(AuditLog(migrated), approvals, boundary=PathBoundary(projects))
    spec = _FileTool(Risk.WRITE).spec
    params = {"path": str(tmp_path / "f.txt")}

    projects.add_path(project.id, ProjectPathDraft(path=str(tmp_path), writable=True))
    token = approvals.issue(tool_name=spec.name, params=params)

    projects.add_path(project.id, ProjectPathDraft(path=str(tmp_path), kind=PathKind.EXCLUDED))
    decision = engine.authorise(spec, params, approval_id=token.id, signature=token.signature)
    assert decision.refused


@pytest.mark.asyncio
async def test_the_executor_reports_a_refusal_instead_of_asking(
    migrated: Database, projects: ProjectRepository, project: Project, tmp_path: Path
) -> None:
    """A refusal must not become an approval card.

    Routing it into the approval branch would put a question in front of the
    user that no answer can resolve — clicking Approve returns to the same
    refusal.
    """
    secret = tmp_path / ".env"
    projects.add_path(project.id, ProjectPathDraft(path=str(secret), kind=PathKind.EXCLUDED))
    registry = ToolRegistry()
    registry.register(_FileTool())
    engine = PolicyEngine(
        AuditLog(migrated), ApprovalAuthority(migrated), boundary=PathBoundary(projects)
    )

    execution = await ToolExecutor(registry, engine, migrated).execute(
        "read_file", {"path": str(secret)}
    )
    assert execution.awaiting_approval is False
    assert execution.result.ok is False
    assert "excluded" in execution.result.content


# ── HTTP surface ───────────────────────────────────────────────────────────


def test_create_list_and_delete_over_http(client: TestClient, auth_headers: dict[str, str]) -> None:
    created = client.post("/v1/projects", json={"name": "MITTA"}, headers=auth_headers)
    assert created.status_code == 201
    project_id = created.json()["id"]

    listed = client.get("/v1/projects", headers=auth_headers)
    assert listed.status_code == 200
    assert [p["id"] for p in listed.json()["projects"]] == [project_id]

    assert client.delete(f"/v1/projects/{project_id}", headers=auth_headers).status_code == 204
    assert client.get(f"/v1/projects/{project_id}", headers=auth_headers).status_code == 404


def test_registering_a_path_canonicalises_and_audits(
    client: TestClient, auth_headers: dict[str, str], migrated_audit: AuditLog, tmp_path: Path
) -> None:
    """The audit entry records the canonical form, not what was submitted.

    `~/work/../.ssh` and `/Users/satya/.ssh` are the same grant; a log that
    records the first hides what was actually permitted.
    """
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    project_id = client.post("/v1/projects", json={"name": "P"}, headers=auth_headers).json()["id"]

    response = client.post(
        f"/v1/projects/{project_id}/paths",
        json={"path": str(nested / ".." / ".."), "writable": True},
        headers=auth_headers,
    )
    assert response.status_code == 201
    assert response.json()["path"] == str(canonicalise(tmp_path))

    actions = [entry.action for entry in migrated_audit.recent(limit=10)]
    assert "project.path_registered" in actions


def test_resolve_path_explains_the_verdict(
    client: TestClient, auth_headers: dict[str, str], tmp_path: Path
) -> None:
    """R5: a boundary whose only observable behaviour is a confirmation card at
    the moment of action is not inspectable."""
    project_id = client.post("/v1/projects", json={"name": "P"}, headers=auth_headers).json()["id"]
    client.post(
        f"/v1/projects/{project_id}/paths",
        json={"path": str(tmp_path), "writable": True},
        headers=auth_headers,
    )

    inside = client.get(
        "/v1/projects/resolve-path",
        params={"path": str(tmp_path / "note.md")},
        headers=auth_headers,
    )
    assert inside.json()["containment"] == "writable"
    assert inside.json()["needs_confirmation"] is False

    outside = client.get(
        "/v1/projects/resolve-path", params={"path": "/elsewhere"}, headers=auth_headers
    )
    assert outside.json()["containment"] == "outside"
    assert "outside every project path" in outside.json()["explanation"]
    assert outside.json()["refused"] is False


def test_resolve_path_reports_an_exclusion_as_refused_not_confirmable(
    client: TestClient, auth_headers: dict[str, str], tmp_path: Path
) -> None:
    """The wire must distinguish the two, or the UI cannot."""
    secret = tmp_path / ".env"
    project_id = client.post("/v1/projects", json={"name": "P"}, headers=auth_headers).json()["id"]
    client.post(
        f"/v1/projects/{project_id}/paths",
        json={"path": str(tmp_path), "writable": True},
        headers=auth_headers,
    )
    client.post(
        f"/v1/projects/{project_id}/paths",
        json={"path": str(secret), "kind": "excluded"},
        headers=auth_headers,
    )

    body = client.get(
        "/v1/projects/resolve-path", params={"path": str(secret)}, headers=auth_headers
    ).json()
    assert body["containment"] == "excluded"
    assert body["refused"] is True
    assert body["needs_confirmation"] is False


def test_resolve_path_is_not_shadowed_by_the_id_route(
    client: TestClient, auth_headers: dict[str, str], tmp_path: Path
) -> None:
    """Route registration order, asserted.

    Declared after `/{project_id}`, this request would be read as a project id
    and 404. The failure is silent in review and obvious here.
    """
    response = client.get(
        "/v1/projects/resolve-path", params={"path": str(tmp_path)}, headers=auth_headers
    )
    assert response.status_code == 200


def test_removing_a_path_is_audited(
    client: TestClient, auth_headers: dict[str, str], migrated_audit: AuditLog, tmp_path: Path
) -> None:
    project_id = client.post("/v1/projects", json={"name": "P"}, headers=auth_headers).json()["id"]
    client.post(
        f"/v1/projects/{project_id}/paths",
        json={"path": str(tmp_path), "writable": True},
        headers=auth_headers,
    )
    response = client.request(
        "DELETE",
        f"/v1/projects/{project_id}/paths",
        params={"path": str(tmp_path)},
        headers=auth_headers,
    )
    assert response.status_code == 204
    actions = [entry.action for entry in migrated_audit.recent(limit=10)]
    assert "project.path_removed" in actions


def test_archiving_over_http_is_audited(
    client: TestClient, auth_headers: dict[str, str], migrated_audit: AuditLog
) -> None:
    project_id = client.post("/v1/projects", json={"name": "P"}, headers=auth_headers).json()["id"]
    response = client.patch(
        f"/v1/projects/{project_id}", json={"status": "archived"}, headers=auth_headers
    )
    assert response.json()["status"] == "archived"
    actions = [entry.action for entry in migrated_audit.recent(limit=10)]
    assert "project.archived" in actions


def test_project_memory_404s_on_an_unknown_id(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """A typo'd filter that answers "no memories" is indistinguishable from a
    project that genuinely has none."""
    response = client.get("/v1/projects/prj_nope/memory", headers=auth_headers)
    assert response.status_code == 404


def test_paths_require_a_token(client: TestClient, tmp_path: Path) -> None:
    """This endpoint edits a security boundary, so it is not exempt from auth."""
    response = client.post("/v1/projects/prj_x/paths", json={"path": str(tmp_path)})
    assert response.status_code == 401


def test_the_router_is_absent_without_its_collaborators(bare_client: TestClient) -> None:
    """A wiring mistake should be an honest 404, not a 500 at request time."""
    assert bare_client.get("/v1/projects").status_code == 404
