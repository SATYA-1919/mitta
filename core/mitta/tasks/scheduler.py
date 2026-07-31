"""The tick — the only thing in MITTA that starts work on its own.

One loop, one query, no threads. It wakes twice a minute, asks the repository
for anything due, and hands each claim to the runner without waiting for it.

**Lateness is preferred to silence.** A laptop that was closed at 08:00 and
opened at 11:00 runs the 08:00 briefing at 11:00, once, and says how late it
was — rather than skipping it, which leaves the user to work out from an absence
whether the feature is broken. Missed occurrences are collapsed rather than
replayed: `claim_due` computes the next fire from *now*, so a machine that was
off for a week comes back to one run per schedule, not to seven.

**A schedule never overlaps itself.** A run still in flight when its next
occurrence comes round means the interval is shorter than the work, and starting
a second copy is how one slow briefing becomes six of them competing for the
same rate limit.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Final

from mitta.tasks.repository import TaskRepository
from mitta.tasks.runner import RunOutcome, TaskRunner
from mitta.telemetry.logging import get_logger

log = get_logger(__name__)

#: How often to look. Cron resolves to a minute, so half a minute bounds how
#: late a fire can be without making the query rate meaningful — it is one
#: indexed lookup against `idx_schedules_due` on an empty table most of the time.
TICK_SECONDS: Final = 30

#: How often to run the maintenance that has no better home: expired approval
#: tokens that were never answered. Hourly, and only from the loop that is
#: already awake.
MAINTENANCE_SECONDS: Final = 3_600

#: Returns how many rows it cleared. Supplied by the composition root, so this
#: module never learns what a policy engine is — it only knows that something
#: periodic needs doing and that it is the only thing awake to do it.
MaintenanceFn = Callable[[], int]


class Scheduler:
    def __init__(
        self,
        repository: TaskRepository,
        runner: TaskRunner,
        *,
        tick_seconds: float = TICK_SECONDS,
        on_maintenance: MaintenanceFn | None = None,
    ) -> None:
        self._repository = repository
        self._runner = runner
        self._tick_seconds = tick_seconds
        self._on_maintenance = on_maintenance
        self._loop_task: asyncio.Task[None] | None = None
        #: In-flight runs, keyed by schedule. The value is the runner's task, so
        #: a shutdown can wait for them rather than tearing them off mid-write.
        self._active: dict[str, asyncio.Task[RunOutcome]] = {}
        self._last_maintenance = 0.0

    # -- lifecycle -------------------------------------------------------------- #

    def start(self) -> None:
        """Begin ticking. Idempotent."""
        if self._loop_task is not None:
            return
        self._loop_task = asyncio.create_task(self._loop())
        log.info("scheduler.started", extra={"tick_seconds": self._tick_seconds})

    async def stop(self) -> None:
        """Stop ticking and let in-flight runs finish.

        Runs are given the rest of the shutdown to complete rather than being
        cancelled outright: a tool call interrupted between acting and recording
        is the one state the tasks table cannot describe honestly.
        """
        if self._loop_task is not None:
            self._loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None

        if self._active:
            log.info("scheduler.draining", extra={"runs": len(self._active)})
            await asyncio.gather(*self._active.values(), return_exceptions=True)
        log.info("scheduler.stopped")

    @property
    def running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    @property
    def active_runs(self) -> int:
        return len(self._active)

    # -- the loop --------------------------------------------------------------- #

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._tick_seconds)
                self.tick()
                self._maintain()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A tick that raises must not end the loop. Everything after it
                # would silently never run again, and the symptom — schedules
                # stopping at an arbitrary point — looks nothing like its cause.
                log.exception("scheduler.tick_failed")

    def tick(self, *, now: int | None = None) -> int:
        """Claim and launch everything due. Returns how many runs started.

        Synchronous and non-blocking: claiming is one transaction and launching
        is `create_task`. The work itself happens in those tasks, so a tick
        costs the same whether it starts nothing or five.
        """
        ts = now if now is not None else int(time.time())
        started = 0

        for schedule in self._repository.claim_due(now=ts):
            in_flight = self._active.get(schedule.id)
            if in_flight is not None and not in_flight.done():
                log.warning(
                    "schedule.overlapped",
                    extra={"schedule_id": schedule.id, "name": schedule.name},
                )
                continue

            lateness = ts - (schedule.last_run_at or ts)
            runner = self._runner.launch(schedule)
            self._active[schedule.id] = runner
            runner.add_done_callback(lambda _, sid=schedule.id: self._active.pop(sid, None))  # type: ignore[misc]
            started += 1

            log.info(
                "schedule.launched",
                extra={
                    "schedule_id": schedule.id,
                    "name": schedule.name,
                    # Zero on a normal fire. Non-zero means the machine was
                    # asleep or busy, which is the first thing to check when a
                    # user says something ran at the wrong time.
                    "late_by_s": max(0, lateness),
                    "next_run_at": schedule.next_run_at,
                },
            )

        return started

    def _maintain(self) -> None:
        if self._on_maintenance is None:
            return
        now = time.monotonic()
        if now - self._last_maintenance < MAINTENANCE_SECONDS:
            return
        self._last_maintenance = now
        purged = self._on_maintenance()
        if purged:
            log.info("scheduler.maintenance", extra={"purged_approvals": purged})
