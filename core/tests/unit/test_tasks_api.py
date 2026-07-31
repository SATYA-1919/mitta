"""The HTTP surface for tasks, plans and schedules.

The endpoints that matter here are the two that edit a standing authorisation —
creating and deleting a `tool` schedule — and the two that stop or retry a run.
Everything else is CRUD, and is covered once rather than exhaustively.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from mitta.policy.audit import AuditLog
from mitta.tasks.models import PlanStatus, TaskStatus
from mitta.tasks.repository import TaskRepository
from mitta.tools.base import Risk, ToolResult, ToolSpec
from mitta.tools.registry import ToolRegistry

BRIEFING: dict[str, Any] = {
    "name": "Morning briefing",
    "cron": "0 8 * * *",
    "timezone": "Europe/London",
    "action": {"kind": "prompt", "text": "what happened overnight"},
}


class StubTool:
    def __init__(self, name: str, risk: Risk = Risk.READ) -> None:
        self._spec = ToolSpec(name=name, description=name, risk=risk)
        self.runs: list[dict[str, Any]] = []

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    async def run(self, params: dict[str, Any]) -> ToolResult:
        self.runs.append(params)
        return ToolResult(ok=True, content="done")


def create(client: TestClient, headers: dict[str, str], **overrides: Any) -> dict[str, Any]:
    body = {**BRIEFING, **overrides}
    response = client.post("/v1/schedules", json=body, headers=headers)
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


# ── Schedules ──────────────────────────────────────────────────────────────


def test_create_list_and_delete(client: TestClient, auth_headers: dict[str, str]) -> None:
    created = create(client, auth_headers)
    assert created["summary"] == "daily at 08:00"
    # The clock the user set it by, not a UTC epoch they have to convert.
    assert created["next_run_local"] is not None
    assert created["next_run_local"].endswith(("+01:00", "+00:00"))

    listed = client.get("/v1/schedules", headers=auth_headers)
    assert [s["id"] for s in listed.json()["schedules"]] == [created["id"]]
    # No scheduler in the test app, and the response says so rather than
    # showing times nothing is going to keep.
    assert listed.json()["scheduler_running"] is False

    assert client.delete(f"/v1/schedules/{created['id']}", headers=auth_headers).status_code == 204
    assert client.get("/v1/schedules", headers=auth_headers).json()["total"] == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cron", "every morning"),
        ("cron", "0 8 * *"),
        ("timezone", "Mars/Olympus_Mons"),
        ("action", {"kind": "shell", "cmd": "rm -rf /"}),
        ("action", {"kind": "tool"}),
    ],
)
def test_a_malformed_schedule_is_a_422_not_a_500(
    client: TestClient, auth_headers: dict[str, str], field: str, value: Any
) -> None:
    """Validated on the wire schema, so the failure names the field.

    Constructing the domain type inside the handler instead would raise a
    Pydantic error the API has no handler for, and a typo would return a 500.
    """
    response = client.post("/v1/schedules", json={**BRIEFING, field: value}, headers=auth_headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation.failed"


def test_authoring_a_tool_schedule_is_audited_with_its_arguments(
    client: TestClient,
    auth_headers: dict[str, str],
    tool_registry: ToolRegistry,
    migrated_audit: AuditLog,
) -> None:
    """The grant half of DEC-122, written down where the user can read it.

    A log that said only "a write_note schedule was created" would not let them
    check *what* they authorised, which is the entire binding.
    """
    tool_registry.register(StubTool("write_note", Risk.WRITE))
    created = create(
        client,
        auth_headers,
        action={"kind": "tool", "tool": "write_note", "params": {"path": "week.md"}},
    )

    entry = next(e for e in migrated_audit.recent(limit=20) if e.action == "schedule.authorised")
    assert entry.subject == "write_note"
    assert entry.detail["params"] == {"path": "week.md"}
    assert entry.detail["schedule_id"] == created["id"]


def test_revoking_is_audited_too(
    client: TestClient,
    auth_headers: dict[str, str],
    tool_registry: ToolRegistry,
    migrated_audit: AuditLog,
) -> None:
    """Deleting the schedule is how the grant is withdrawn, so it is the mirror
    of the line that recorded it."""
    tool_registry.register(StubTool("write_note", Risk.WRITE))
    created = create(
        client, auth_headers, action={"kind": "tool", "tool": "write_note", "params": {}}
    )
    client.delete(f"/v1/schedules/{created['id']}", headers=auth_headers)

    assert any(e.action == "schedule.revoked" for e in migrated_audit.recent(limit=20))


def test_a_destructive_tool_cannot_be_scheduled(
    client: TestClient, auth_headers: dict[str, str], tool_registry: ToolRegistry
) -> None:
    tool_registry.register(StubTool("wipe_disk", Risk.DESTRUCTIVE))
    response = client.post(
        "/v1/schedules",
        json={**BRIEFING, "action": {"kind": "tool", "tool": "wipe_disk", "params": {}}},
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert "cannot be scheduled" in response.json()["error"]["message"]


def test_a_tool_that_does_not_exist_is_refused_at_the_form(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """While the user is looking at it, rather than at 08:00 in a log."""
    response = client.post(
        "/v1/schedules",
        json={**BRIEFING, "action": {"kind": "tool", "tool": "imaginary", "params": {}}},
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert "imaginary" in response.json()["error"]["message"]


def test_disabling_clears_the_next_fire_and_is_audited(
    client: TestClient, auth_headers: dict[str, str], migrated_audit: AuditLog
) -> None:
    created = create(client, auth_headers)
    response = client.patch(
        f"/v1/schedules/{created['id']}", json={"enabled": False}, headers=auth_headers
    )
    assert response.status_code == 200
    assert response.json()["next_run_at"] is None
    assert response.json()["next_run_local"] is None
    assert any(e.action == "schedule.disabled" for e in migrated_audit.recent(limit=20))


def test_the_action_cannot_be_patched(client: TestClient, auth_headers: dict[str, str]) -> None:
    """Editing the arguments of a `tool` schedule edits a standing
    authorisation, and a patch would let one field widen it without re-stating
    the whole call."""
    created = create(client, auth_headers)
    response = client.patch(
        f"/v1/schedules/{created['id']}",
        json={"action": {"kind": "tool", "tool": "write_note", "params": {"path": "/etc/passwd"}}},
        headers=auth_headers,
    )
    assert response.status_code == 422


def test_running_now_uses_the_real_path_and_leaves_the_timetable_alone(
    client: TestClient, auth_headers: dict[str, str], tool_registry: ToolRegistry
) -> None:
    """A "test this" button that took a different route would prove the button
    works. And a manual run at 14:00 must not cancel the 08:00 one tomorrow."""
    tool = StubTool("web_search")
    tool_registry.register(tool)
    created = create(
        client, auth_headers, action={"kind": "tool", "tool": "web_search", "params": {"q": "x"}}
    )

    response = client.post(f"/v1/schedules/{created['id']}/run", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["plan"]["status"] == PlanStatus.COMPLETED.value
    assert tool.runs == [{"q": "x"}]
    unchanged = client.get("/v1/schedules", headers=auth_headers).json()["schedules"][0]
    assert unchanged["next_run_at"] == created["next_run_at"]


# ── Tasks and plans ────────────────────────────────────────────────────────


def test_a_run_is_visible_as_tasks_and_as_a_plan(
    client: TestClient, auth_headers: dict[str, str], tool_registry: ToolRegistry
) -> None:
    tool_registry.register(StubTool("web_search"))
    created = create(
        client, auth_headers, action={"kind": "tool", "tool": "web_search", "params": {}}
    )
    plan_id = client.post(f"/v1/schedules/{created['id']}/run", headers=auth_headers).json()[
        "plan"
    ]["id"]

    listed = client.get("/v1/tasks", headers=auth_headers).json()
    assert listed["total"] == 1
    assert listed["tasks"][0]["tool_name"] == "web_search"
    # The plan travels with the list so a row can show what the step was for
    # without a request per row.
    assert [p["id"] for p in listed["plans"]] == [plan_id]

    plan = client.get(f"/v1/plans/{plan_id}", headers=auth_headers).json()
    assert plan["plan"]["status"] == PlanStatus.COMPLETED.value
    assert plan["edges"] == []


def test_a_failed_task_reports_why_and_offers_a_retry(
    client: TestClient, auth_headers: dict[str, str], task_repository: TaskRepository
) -> None:
    plan = task_repository.create_plan("goal")
    task = task_repository.add_task(plan.id, _draft("write_note"))
    task_repository.start_task(task.id)
    task_repository.finish_task(
        task.id, TaskStatus.FAILED, error={"code": "tool.failed", "message": "disk full"}
    )

    detail = client.get(f"/v1/tasks/{task.id}", headers=auth_headers).json()

    assert detail["task"]["error"]["message"] == "disk full"
    # Derived server-side so the button and the endpoint cannot disagree.
    assert detail["task"]["resumable"] is True


def test_resuming_something_that_did_not_fail_is_a_409(
    client: TestClient, auth_headers: dict[str, str], task_repository: TaskRepository
) -> None:
    """The request is well-formed; the state is what refuses it."""
    plan = task_repository.create_plan("goal")
    task = task_repository.add_task(plan.id, _draft("web_search"))
    task_repository.finish_task(task.id, TaskStatus.COMPLETED)

    response = client.post(f"/v1/tasks/{task.id}/resume", headers=auth_headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict.state"


def test_cancelling_stops_the_whole_plan(
    client: TestClient, auth_headers: dict[str, str], task_repository: TaskRepository
) -> None:
    plan = task_repository.create_plan("goal", status=PlanStatus.RUNNING)
    first = task_repository.add_task(plan.id, _draft("web_search"))
    second = task_repository.add_task(plan.id, _draft("write_note"))

    response = client.post(f"/v1/tasks/{first.id}/cancel", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["plan"]["status"] == PlanStatus.CANCELLED.value
    assert task_repository.get_task(second.id).status is TaskStatus.SKIPPED


def test_an_unknown_task_is_a_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    assert client.get("/v1/tasks/tsk_nope", headers=auth_headers).status_code == 404


def test_the_routes_need_the_session_token(client: TestClient) -> None:
    assert client.get("/v1/schedules").status_code == 401


def test_the_router_is_absent_without_its_collaborators(bare_client: TestClient) -> None:
    """A router whose handlers would dereference `None` is worse than a 404: it
    turns a wiring mistake into a 500 at request time."""
    assert bare_client.get("/v1/schedules").status_code == 404
    assert bare_client.get("/v1/tasks").status_code == 404


def _draft(tool_name: str) -> Any:
    from mitta.tasks.models import TaskDraft

    return TaskDraft(title=tool_name, tool_name=tool_name)
