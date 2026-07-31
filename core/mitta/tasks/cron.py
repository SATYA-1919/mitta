"""Cron expressions, and the next time one comes due.

Written here rather than pulled from PyPI, for the same reasons `mitta.ids`
implements ULIDs by hand: it is a bounded amount of code, it removes a
dependency from a security-sensitive local application, and the part that is
actually hard is not the parsing — it is what happens twice a year, which most
of the small libraries get wrong quietly.

**Everything is computed in the schedule's own timezone.** A user who asks for
08:00 means 08:00 on their clock, in March and in November alike. Storing the
zone and walking wall-clock minutes is what makes that true; computing an
interval in UTC would silently move the job an hour every spring.

Two consequences follow, and both are choices rather than accidents:

* **The hour that does not exist.** When clocks jump forward, 02:30 never
  happens on that date. A job for 02:30 is *skipped* that day rather than run at
  01:30 or 03:30. Neither substitute is what was asked for, and a job that
  silently runs at a different time is worse than one that visibly does not run.
* **The hour that happens twice.** When clocks go back, 01:30 comes round twice.
  The job runs on the first, and `next_after` never returns the second — a
  backup that runs twice is a bug, and one that runs once is the instruction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: How far ahead `next_after` is willing to look. An expression with no
#: occurrence inside four years — `0 0 30 2 *`, February 30th — is a typo, and
#: returning `None` for it would put the mistake in a nullable column where
#: nobody sees it until the schedule does not fire.
HORIZON_YEARS: Final = 4

#: Belt and braces against a bug in the advance logic. The horizon above is the
#: real bound; this one exists so a loop that fails to move forward raises
#: instead of hanging the scheduler thread.
_MAX_STEPS: Final = 1_000_000

_MONTH_NAMES: Final = {
    name: index
    for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}

#: Cron numbers Sunday as 0. `7` is also Sunday, which is not in the original
#: specification but is in every implementation people have used since.
_DAY_NAMES: Final = {
    name: index
    for index, name in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"), start=0)
}

_ALIASES: Final = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_RANGES: Final = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_FIELD_NAMES: Final = ("minute", "hour", "day-of-month", "month", "day-of-week")


@dataclass(frozen=True, slots=True)
class CronExpression:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    #: Normalised to 0-6 with Sunday at 0.
    weekdays: frozenset[int]
    #: Whether each of the two day fields was narrowed from `*`. Both are needed
    #: because the two fields are combined with OR when both are restricted —
    #: see `matches`.
    dom_restricted: bool
    dow_restricted: bool
    source: str

    def matches(self, moment: datetime) -> bool:
        """Whether this local wall-clock minute is one the expression names.

        The day rule is the one people get wrong. When *both* day-of-month and
        day-of-week are restricted, cron matches if **either** does — so
        `0 0 1 * mon` is the 1st of the month *and* every Monday, not the
        Mondays that fall on the 1st. When only one is restricted, only that one
        decides. This is inherited behaviour rather than a good idea, but a cron
        parser that quietly disagrees with cron is a trap.
        """
        if moment.minute not in self.minutes or moment.hour not in self.hours:
            return False
        if moment.month not in self.months:
            return False

        # Python's Monday-is-0 to cron's Sunday-is-0.
        weekday = (moment.weekday() + 1) % 7
        day_ok = moment.day in self.days
        weekday_ok = weekday in self.weekdays

        if self.dom_restricted and self.dow_restricted:
            return day_ok or weekday_ok
        if self.dom_restricted:
            return day_ok
        if self.dow_restricted:
            return weekday_ok
        return True


def resolve_timezone(name: str) -> ZoneInfo:
    """Look up an IANA zone, turning an unknown one into a `ValueError`.

    Validated when a schedule is created rather than when it fires. A zone the
    machine has never heard of is a typo, and the moment to say so is while the
    user is looking at the form — not at 3am, in a log nobody reads, as a
    schedule that never ran.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ValueError(f"unknown timezone: {name!r}") from exc


def parse(expression: str) -> CronExpression:
    """Parse a five-field cron expression, or one of the `@` aliases."""
    source = expression.strip()
    if not source:
        raise ValueError("empty cron expression")

    normalised = _ALIASES.get(source.lower(), source)
    fields = normalised.split()
    if len(fields) != 5:
        raise ValueError(
            f"expected 5 cron fields (minute hour day-of-month month day-of-week), "
            f"got {len(fields)} in {source!r}"
        )

    values = [_parse_field(field, index) for index, field in enumerate(fields)]

    weekdays = frozenset(0 if day == 7 else day for day in values[4])
    return CronExpression(
        minutes=values[0],
        hours=values[1],
        days=values[2],
        months=values[3],
        weekdays=weekdays,
        dom_restricted=fields[2] != "*",
        dow_restricted=fields[4] != "*",
        source=source,
    )


def next_after(expression: str | CronExpression, *, after: int, timezone: str = "UTC") -> int:
    """The first epoch second strictly after `after` that the expression names.

    Strictly after, which is what stops a schedule firing twice: the scheduler
    computes the next run from the instant the last one started, and an
    implementation that returned `after` itself for a matching minute would fire
    in a loop for sixty seconds.
    """
    parsed = parse(expression) if isinstance(expression, str) else expression
    zone = resolve_timezone(timezone)

    # Walk wall-clock minutes, converting only when a candidate matches. Walking
    # in UTC instead would drift the job by an hour across a DST boundary, which
    # is the entire reason the zone is stored alongside the expression.
    start = datetime.fromtimestamp(after, zone).replace(second=0, microsecond=0, tzinfo=None)
    moment = start + timedelta(minutes=1)
    horizon = start.replace(year=start.year + HORIZON_YEARS)

    for _ in range(_MAX_STEPS):
        if moment > horizon:
            break

        if moment.month not in parsed.months:
            moment = _next_month(moment)
            continue
        if not _day_matches(parsed, moment):
            moment = _next_day(moment)
            continue
        if moment.hour not in parsed.hours:
            moment = _next_hour(moment)
            continue
        if moment.minute not in parsed.minutes:
            moment = moment + timedelta(minutes=1)
            continue

        epoch = _to_epoch(moment, zone)
        if epoch is None or epoch <= after:
            # `None` is the spring-forward gap: this wall-clock time does not
            # exist on this date, so the job is skipped rather than moved.
            #
            # `epoch <= after` is the autumn repeat. During the ambiguous hour
            # the same wall time maps to two instants and this walk only ever
            # produces the first; without this check the scheduler would be
            # handed a "next run" in the past and fire immediately, forever.
            moment = moment + timedelta(minutes=1)
            continue

        return epoch

    raise ValueError(
        f"{parsed.source!r} has no occurrence within {HORIZON_YEARS} years "
        f"of {datetime.fromtimestamp(after, UTC).isoformat()}"
    )


def describe(expression: str) -> str:
    """A short human rendering, for a log line or a confirmation.

    Deliberately not a full English translation of every expression — those read
    worse than the cron itself past the simple cases, and a wrong translation of
    a schedule is a lie about when something will happen.
    """
    parsed = parse(expression)
    if len(parsed.minutes) == 1 and len(parsed.hours) == 1:
        minute = next(iter(parsed.minutes))
        hour = next(iter(parsed.hours))
        clock = f"{hour:02d}:{minute:02d}"
        if not parsed.dom_restricted and not parsed.dow_restricted:
            return f"daily at {clock}"
        if parsed.dow_restricted and not parsed.dom_restricted:
            days = ",".join(_day_label(day) for day in sorted(parsed.weekdays))
            return f"{days} at {clock}"
        return f"at {clock}"
    return parsed.source


# -- field parsing ---------------------------------------------------------- #


def _parse_field(field: str, index: int) -> frozenset[int]:
    low, high = _RANGES[index]
    names = _MONTH_NAMES if index == 3 else (_DAY_NAMES if index == 4 else {})

    values: set[int] = set()
    for part in field.split(","):
        values |= _parse_part(part.strip(), low, high, names, index)
    if not values:
        raise ValueError(f"{_FIELD_NAMES[index]} field {field!r} matches nothing")
    return frozenset(values)


def _parse_part(part: str, low: int, high: int, names: dict[str, int], index: int) -> set[int]:
    if not part:
        raise ValueError(f"empty value in {_FIELD_NAMES[index]} field")

    step = 1
    if "/" in part:
        part, _, raw_step = part.partition("/")
        if not raw_step.isdigit() or int(raw_step) == 0:
            raise ValueError(f"bad step {raw_step!r} in {_FIELD_NAMES[index]} field")
        step = int(raw_step)

    if part in ("*", ""):
        start, end = low, high
    elif "-" in part[1:]:  # `part[1:]`, so a negative number is not read as a range
        raw_start, _, raw_end = part.partition("-")
        start = _value(raw_start, names, low, high, index)
        end = _value(raw_end, names, low, high, index)
        if end < start:
            raise ValueError(f"reversed range {part!r} in {_FIELD_NAMES[index]} field")
    else:
        start = _value(part, names, low, high, index)
        # A bare value with a step means "from here to the top of the range",
        # which is what `*/15` and `10/15` both rely on.
        end = high if step > 1 else start

    return set(range(start, end + 1, step))


def _value(raw: str, names: dict[str, int], low: int, high: int, index: int) -> int:
    token = raw.strip().lower()
    if token in names:
        return names[token]
    if not token.isdigit():
        raise ValueError(f"{token!r} is not a valid {_FIELD_NAMES[index]}")
    value = int(token)
    if not low <= value <= high:
        raise ValueError(f"{value} is outside {low}-{high} for {_FIELD_NAMES[index]}")
    return value


# -- calendar walking ------------------------------------------------------- #


def _day_matches(parsed: CronExpression, moment: datetime) -> bool:
    weekday = (moment.weekday() + 1) % 7
    day_ok = moment.day in parsed.days
    weekday_ok = weekday in parsed.weekdays
    if parsed.dom_restricted and parsed.dow_restricted:
        return day_ok or weekday_ok
    if parsed.dom_restricted:
        return day_ok
    if parsed.dow_restricted:
        return weekday_ok
    return True


def _next_month(moment: datetime) -> datetime:
    year = moment.year + (1 if moment.month == 12 else 0)
    month = 1 if moment.month == 12 else moment.month + 1
    return datetime(year, month, 1)


def _next_day(moment: datetime) -> datetime:
    return datetime(moment.year, moment.month, moment.day) + timedelta(days=1)


def _next_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0) + timedelta(hours=1)


def _to_epoch(moment: datetime, zone: ZoneInfo) -> int | None:
    """Local wall time to epoch seconds, or `None` if that time does not exist.

    The round trip is the test. Attaching a zone to a naive datetime always
    produces *something*, including for the hour skipped by a spring-forward —
    and what it produces is a different wall time. Converting back and comparing
    is the only way to notice, and noticing is what turns "ran an hour early"
    into "did not run".
    """
    aware = moment.replace(tzinfo=zone)
    epoch = int(aware.timestamp())
    if datetime.fromtimestamp(epoch, zone).replace(tzinfo=None) != moment:
        return None
    return epoch


def _day_label(day: int) -> str:
    return ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")[day]
