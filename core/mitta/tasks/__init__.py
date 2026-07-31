"""Tasks, plans and schedules — what MITTA does when nobody is talking to it.

Three tables and one loop. A `schedule` is a cron expression and an action; when
it comes due the scheduler runs the action as a `plan`, and every tool call the
run makes is recorded as a `task`. That record is the whole point: an action
taken while the user was asleep has to be inspectable afterwards, or it is
indistinguishable from something MITTA did not do.
"""
