"""Tasks, plans and schedules (API_DESIGN.md §3.5).

Three routers, one module, because they are three views of one run: a schedule
produces a plan, a plan is made of tasks, and a task is what a user cancels.

**Creating a `tool` schedule is a permission change, and it is audited like
one.** The same argument as `POST /v1/projects/{id}/paths`: the row decides what
MITTA may later do without being asked, so a change to it that left no trace
would be indistinguishable from one that never happened. Deleting a schedule is
audited for the mirror reason — it is how the grant is withdrawn.

Two routes here are additions to §3.5 rather than substitutions. `PATCH
/v1/schedules/{id}` exists because the surface needs an enable toggle and
delete-then-recreate would lose the run history. `POST /v1/schedules/{id}/run`
exists because an automation nobody can test before 08:00 tomorrow is one people
do not trust — and it runs through exactly the same path as a real fire, so what
it proves is the thing that will happen.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response, status

from mitta.api.auth import RequireToken
from mitta.api.schemas.tasks import (
    CheckpointResource,
    CreateScheduleRequest,
    DependencyEdge,
    PlanDetailResponse,
    PlanResource,
    RunResponse,
    ScheduleListResponse,
    ScheduleResource,
    TaskDetailResponse,
    TaskListResponse,
    TaskResource,
    UpdateScheduleRequest,
)
from mitta.errors import NotFoundError, ValidationError
from mitta.tasks.models import ScheduleDraft, ToolAction
from mitta.tasks.repository import TaskRepository
from mitta.tasks.runner import TaskRunner, UnattendedRefusalError, authorised_unattended
from mitta.tasks.scheduler import Scheduler
from mitta.tools.registry import ToolRegistry

tasks_router = APIRouter(prefix="/v1/tasks", tags=["tasks"])
plans_router = APIRouter(prefix="/v1/plans", tags=["tasks"])
schedules_router = APIRouter(prefix="/v1/schedules", tags=["schedules"])


def _repo(request: Request) -> TaskRepository:
    repository: TaskRepository = request.app.state.tasks
    return repository


def _runner(request: Request) -> TaskRunner:
    runner: TaskRunner = request.app.state.task_runner
    return runner


def _scheduler(request: Request) -> Scheduler | None:
    scheduler: Scheduler | None = getattr(request.app.state, "scheduler", None)
    return scheduler


def _run_response(request: Request, plan_id: str) -> RunResponse:
    repository = _repo(request)
    return RunResponse(
        plan=PlanResource.of(repository.get_plan(plan_id)),
        tasks=[TaskResource.of(task) for task in repository.tasks_for(plan_id)],
    )


# -- tasks --------------------------------------------------------------------- #


@tasks_router.get("", response_model=TaskListResponse, summary="Active and recent tasks")
async def list_tasks(
    request: Request,
    _: RequireToken,
    active: bool = Query(default=False, description="Only tasks that have not finished."),
    limit: int = Query(default=50, ge=1, le=200),
) -> TaskListResponse:
    repository = _repo(request)
    tasks = repository.recent_tasks(limit=limit, active_only=active)

    # One lookup per distinct plan rather than per task. A run with six steps
    # would otherwise fetch the same plan six times to render one heading.
    plans = {task.plan_id: repository.get_plan(task.plan_id) for task in tasks}
    return TaskListResponse(
        tasks=[TaskResource.of(task) for task in tasks],
        plans=[PlanResource.of(plan) for plan in plans.values()],
        total=len(tasks),
    )


@tasks_router.get("/{task_id}", response_model=TaskDetailResponse, summary="Detail and checkpoints")
async def get_task(request: Request, task_id: str, _: RequireToken) -> TaskDetailResponse:
    repository = _repo(request)
    task = repository.get_task(task_id)
    return TaskDetailResponse(
        task=TaskResource.of(task),
        plan=PlanResource.of(repository.get_plan(task.plan_id)),
        checkpoints=[
            CheckpointResource.of(checkpoint) for checkpoint in repository.checkpoints(task_id)
        ],
    )


@tasks_router.post("/{task_id}/cancel", response_model=RunResponse, summary="Stop the run")
async def cancel_task(request: Request, task_id: str, _: RequireToken) -> RunResponse:
    """Cancels the whole plan, not just this task.

    A plan is a sequence whose later steps were chosen for the earlier ones, so
    stopping one step and continuing to the next would carry out a request the
    user has just interrupted.
    """
    plan = _runner(request).cancel(task_id)
    return _run_response(request, plan.id)


@tasks_router.post("/{task_id}/resume", response_model=RunResponse, summary="Retry a failed task")
async def resume_task(request: Request, task_id: str, _: RequireToken) -> RunResponse:
    """Re-runs this task and leaves everything already completed alone.

    For a prompt run this is a re-ask rather than a resume — the steps were
    chosen by a model and there is no recorded sequence to continue from. The
    endpoint says so rather than pretending otherwise; see `TaskRunner.resume`.

    A task that is not failed, or that has spent its attempts, is a 409 rather
    than a 400: the request is well-formed and the state is what refuses it.
    """
    outcome = await _runner(request).resume(task_id)
    return _run_response(request, outcome.plan.id)


# -- plans --------------------------------------------------------------------- #


@plans_router.get("/{plan_id}", response_model=PlanDetailResponse, summary="Full plan with status")
async def get_plan(request: Request, plan_id: str, _: RequireToken) -> PlanDetailResponse:
    repository = _repo(request)
    plan = repository.get_plan(plan_id)
    return PlanDetailResponse(
        plan=PlanResource.of(plan),
        tasks=[TaskResource.of(task) for task in repository.tasks_for(plan_id)],
        edges=[
            DependencyEdge(task_id=task_id, depends_on=depends_on)
            for task_id, depends_on in repository.dependencies(plan_id)
        ],
    )


# -- schedules ------------------------------------------------------------------ #


@schedules_router.get("", response_model=ScheduleListResponse, summary="Recurring automations")
async def list_schedules(request: Request, _: RequireToken) -> ScheduleListResponse:
    schedules = _repo(request).list_schedules()
    scheduler = _scheduler(request)
    return ScheduleListResponse(
        schedules=[ScheduleResource.of(schedule) for schedule in schedules],
        total=len(schedules),
        scheduler_running=scheduler is not None and scheduler.running,
    )


@schedules_router.post(
    "",
    response_model=ScheduleResource,
    status_code=status.HTTP_201_CREATED,
    summary="Create an automation",
)
async def create_schedule(
    request: Request, body: CreateScheduleRequest, _: RequireToken
) -> ScheduleResource:
    """Validates the action against the live tool registry before storing it.

    A schedule naming a tool that does not exist, or one that deletes things, is
    rejected here — while the user is looking at the form. The runner checks
    again at every fire, because a tool's risk tier can change between versions
    and a grant written against the old one must not survive it.
    """
    draft = ScheduleDraft(
        name=body.name,
        cron=body.cron,
        timezone=body.timezone,
        action=dict(body.action),
        enabled=body.enabled,
    )
    action = draft.typed_action()

    if isinstance(action, ToolAction):
        registry: ToolRegistry | None = request.app.state.tool_registry
        if registry is None:  # pragma: no cover - the router is not mounted without one
            raise ValidationError("Tool schedules need a tool registry.")
        try:
            spec = registry.get(action.tool).spec
        except NotFoundError as exc:
            raise ValidationError(f"There is no tool called {action.tool!r}.") from exc
        try:
            authorised_unattended(spec)
        except UnattendedRefusalError as exc:
            # 422 rather than the refusal's own 403. Nothing has been asked to
            # run yet — this is a form being rejected, and the field it is about
            # is `action`.
            raise ValidationError(exc.message) from exc

    schedule = _repo(request).create_schedule(draft)

    if isinstance(action, ToolAction):
        # The authorisation half of DEC-122, written down at the moment it is
        # granted. `params` is recorded in full: a log that said only "a
        # write_note schedule was created" would not let the user check *what*
        # they authorised, which is the entire binding.
        request.app.state.audit.record(
            actor="user",
            action="schedule.authorised",
            subject=action.tool,
            verdict="allow",
            detail={
                "schedule_id": schedule.id,
                "name": schedule.name,
                "cron": schedule.cron,
                "timezone": schedule.timezone,
                "params": action.params,
            },
        )
    return ScheduleResource.of(schedule)


@schedules_router.patch(
    "/{schedule_id}", response_model=ScheduleResource, summary="Rename, retime, enable or disable"
)
async def update_schedule(
    request: Request, schedule_id: str, body: UpdateScheduleRequest, _: RequireToken
) -> ScheduleResource:
    schedule = _repo(request).update_schedule(
        schedule_id,
        name=body.name,
        cron=body.cron,
        timezone=body.timezone,
        enabled=body.enabled,
    )
    if body.enabled is not None:
        # Disabling is how a standing authorisation is suspended without losing
        # the run history, so it belongs in the log beside the grant itself.
        request.app.state.audit.record(
            actor="user",
            action=f"schedule.{'enabled' if body.enabled else 'disabled'}",
            subject=schedule_id,
            detail={"name": schedule.name},
        )
    return ScheduleResource.of(schedule)


@schedules_router.delete(
    "/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete an automation"
)
async def delete_schedule(request: Request, schedule_id: str, _: RequireToken) -> Response:
    """Withdraws the grant. Plans it already produced survive it.

    They are the record of what ran, and deleting the automation must not delete
    the evidence of what it did.
    """
    repository = _repo(request)
    schedule = repository.get_schedule(schedule_id)
    repository.delete_schedule(schedule_id)
    request.app.state.audit.record(
        actor="user",
        action="schedule.revoked",
        subject=schedule_id,
        verdict="allow",
        detail={"name": schedule.name, "kind": schedule.action.get("kind")},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@schedules_router.post("/{schedule_id}/run", response_model=RunResponse, summary="Run it now")
async def run_schedule(request: Request, schedule_id: str, _: RequireToken) -> RunResponse:
    """Fire a schedule immediately, through the path a real fire takes.

    Does not disturb the timetable: `next_run_at` is untouched, so a manual run
    at 14:00 does not cancel the 08:00 one tomorrow.
    """
    schedule = _repo(request).get_schedule(schedule_id)
    outcome = await _runner(request).run_now(schedule)
    return _run_response(request, outcome.plan.id)
