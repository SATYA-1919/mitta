"""Cron parsing and the next fire.

The parsing tests are table-driven because the interesting part is the field
grammar. The scheduling tests are not: each one is a specific way a
hand-written cron implementation gets the calendar wrong, and the two DST cases
at the bottom are the reason this module exists rather than a `timedelta`.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from mitta.tasks.cron import HORIZON_YEARS, describe, next_after, parse, resolve_timezone

LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")


def at(zone: ZoneInfo, *args: int) -> int:
    """Epoch seconds for a local wall-clock time."""
    return int(datetime(*args, tzinfo=zone).timestamp())  # type: ignore[arg-type]


def local(epoch: int, zone: ZoneInfo) -> str:
    return datetime.fromtimestamp(epoch, zone).strftime("%Y-%m-%d %H:%M %Z")


class TestParsing:
    def test_a_five_field_expression_yields_every_field(self) -> None:
        expression = parse("30 8 * * 1-5")
        assert expression.minutes == frozenset({30})
        assert expression.hours == frozenset({8})
        assert expression.weekdays == frozenset({1, 2, 3, 4, 5})
        assert expression.dow_restricted is True
        assert expression.dom_restricted is False

    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            ("*/15", {0, 15, 30, 45}),
            ("0,30", {0, 30}),
            ("5-8", {5, 6, 7, 8}),
            ("0-30/10", {0, 10, 20, 30}),
            # A bare value with a step runs to the top of the range, which is
            # what `10/15` means everywhere else and would be surprising here if
            # it did not.
            ("50/5", {50, 55}),
        ],
    )
    def test_minute_grammar(self, field: str, expected: set[int]) -> None:
        assert parse(f"{field} * * * *").minutes == frozenset(expected)

    def test_names_are_accepted_for_months_and_days(self) -> None:
        expression = parse("0 0 * jan-mar sun")
        assert expression.months == frozenset({1, 2, 3})
        assert expression.weekdays == frozenset({0})

    def test_seven_is_sunday(self) -> None:
        """Not in the original specification, in every implementation since."""
        assert parse("0 0 * * 7").weekdays == parse("0 0 * * 0").weekdays

    def test_aliases_expand(self) -> None:
        assert parse("@daily").minutes == parse("0 0 * * *").minutes
        assert parse("@weekly").weekdays == frozenset({0})

    @pytest.mark.parametrize(
        "expression",
        [
            "",
            "0 0 * *",  # four fields
            "0 0 * * * *",  # six
            "60 * * * *",  # minute out of range
            "* 24 * * *",  # hour out of range
            "0 0 32 * *",  # day out of range
            "0 0 * 13 *",  # month out of range
            "10-5 * * * *",  # reversed range
            "*/0 * * * *",  # zero step
            "banana * * * *",
        ],
    )
    def test_rejects_nonsense(self, expression: str) -> None:
        with pytest.raises(ValueError):
            parse(expression)

    def test_unknown_timezone_is_a_value_error(self) -> None:
        """Caught when the schedule is created, not at 3am inside the tick."""
        with pytest.raises(ValueError, match="unknown timezone"):
            resolve_timezone("Mars/Olympus_Mons")


class TestNextFire:
    def test_the_next_minute_that_matches(self) -> None:
        after = at(LONDON, 2026, 3, 2, 7, 59)
        assert local(next_after("0 8 * * *", after=after, timezone="Europe/London"), LONDON) == (
            "2026-03-02 08:00 GMT"
        )

    def test_strictly_after_so_a_fire_cannot_repeat_for_a_minute(self) -> None:
        """The scheduler computes the next run from the instant of the last one.

        Returning the matching minute it was given would fire in a loop for
        sixty seconds — which is what `claim_due` would then do.
        """
        exactly_eight = at(LONDON, 2026, 3, 2, 8, 0)
        following = next_after("0 8 * * *", after=exactly_eight, timezone="Europe/London")
        assert local(following, LONDON) == "2026-03-03 08:00 GMT"

    def test_day_of_month_and_day_of_week_are_ored(self) -> None:
        """Inherited cron behaviour, and the one people get wrong.

        `0 0 1 * mon` is the 1st *and* every Monday, not the Mondays that fall
        on a 1st. 2026-06-01 is a Monday, so the following fire is Tuesday the
        2nd only if the fields were ANDed — and Monday the 8th if they were not.
        """
        after = at(LONDON, 2026, 6, 1, 12, 0)
        following = next_after("0 0 1 * mon", after=after, timezone="Europe/London")
        assert local(following, LONDON) == "2026-06-08 00:00 BST"

    def test_a_weekday_schedule_skips_the_weekend(self) -> None:
        friday_evening = at(LONDON, 2026, 6, 5, 18, 0)
        following = next_after("30 9 * * 1-5", after=friday_evening, timezone="Europe/London")
        assert local(following, LONDON) == "2026-06-08 09:30 BST"

    def test_february_29_waits_for_a_leap_year(self) -> None:
        after = at(LONDON, 2026, 3, 1, 0, 0)
        following = next_after("0 0 29 2 *", after=after, timezone="Europe/London")
        assert local(following, LONDON) == "2028-02-29 00:00 GMT"

    def test_an_impossible_date_raises_rather_than_returning_nothing(self) -> None:
        """February 30th is a typo, and a `None` would hide it in a nullable column."""
        with pytest.raises(ValueError, match=f"within {HORIZON_YEARS} years"):
            next_after("0 0 30 2 *", after=at(LONDON, 2026, 1, 1, 0, 0), timezone="Europe/London")


class TestDaylightSaving:
    """The reason this module is 300 lines rather than a `timedelta`."""

    def test_the_hour_stays_put_across_a_spring_forward(self) -> None:
        """08:00 means 08:00 on the user's clock, in March and in November.

        Computing an interval in UTC would move this to 07:00 for half the year,
        and the symptom — "my briefing arrives an hour early in the summer" —
        looks like anything except a timezone bug.
        """
        # UK clocks go forward on 2026-03-29.
        before = next_after(
            "0 8 * * *", after=at(LONDON, 2026, 3, 28, 12, 0), timezone="Europe/London"
        )
        after = next_after(
            "0 8 * * *", after=at(LONDON, 2026, 3, 29, 12, 0), timezone="Europe/London"
        )
        assert local(before, LONDON) == "2026-03-29 08:00 BST"
        assert local(after, LONDON) == "2026-03-30 08:00 BST"

    def test_a_time_inside_the_lost_hour_is_skipped_not_moved(self) -> None:
        """01:30 does not happen on the day the clocks jump 01:00 → 02:00.

        Running it at 00:30 or 02:30 instead would be MITTA doing something at a
        time the user did not ask for, which is worse than visibly not running.
        """
        # New York goes forward at 02:00 on 2026-03-08, so 02:30 does not exist.
        following = next_after(
            "30 2 * * *", after=at(NEW_YORK, 2026, 3, 7, 12, 0), timezone="America/New_York"
        )
        assert local(following, NEW_YORK) == "2026-03-09 02:30 EDT"

    def test_an_ambiguous_time_fires_once_not_twice(self) -> None:
        """When the clocks go back, 01:30 comes round twice.

        A backup that runs twice is a bug. The first occurrence fires, and the
        next fire computed from it is the following day — never the repeat, which
        is an epoch *later* than the first but the same wall clock.
        """
        # New York goes back at 02:00 on 2026-11-01: 01:30 EDT then 01:30 EST.
        first = next_after(
            "30 1 * * *", after=at(NEW_YORK, 2026, 10, 31, 12, 0), timezone="America/New_York"
        )
        assert local(first, NEW_YORK) == "2026-11-01 01:30 EDT"

        following = next_after("30 1 * * *", after=first, timezone="America/New_York")
        assert local(following, NEW_YORK) == "2026-11-02 01:30 EST"


class TestDescribe:
    def test_a_daily_time_reads_as_one(self) -> None:
        assert describe("0 8 * * *") == "daily at 08:00"

    def test_named_days_are_listed(self) -> None:
        assert describe("30 17 * * 5") == "Fri at 17:30"

    def test_anything_complicated_is_returned_verbatim(self) -> None:
        """A wrong translation of a schedule is a false statement about when
        something will happen, so past the simple cases there is no translation."""
        assert describe("*/5 9-17 * * 1-5") == "*/5 9-17 * * 1-5"
