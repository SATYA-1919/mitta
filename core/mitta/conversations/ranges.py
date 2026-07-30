"""Named time ranges for clearing history.

Calendar boundaries in the machine's **local** time, not rolling durations from
now. "Clear today" must mean since midnight — a 24-hour window would delete last
night's conversations at 9am, which is not what the word says.

Weeks start on Monday, following ISO 8601 and the rest of the world outside the
United States. Worth stating rather than leaving to `weekday()` to imply.

The cutoff is computed here rather than sent by the client. A browser that
disagrees with the sidecar about the timezone — or a client that simply passes
the wrong number — would delete a different set of conversations than the button
promised, and this is a destructive operation whose blast radius must be
derivable from the word the user pressed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum


class HistoryRange(StrEnum):
    TODAY = "today"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"
    ALL = "all"


def cutoff_for(period: HistoryRange, *, now: datetime | None = None) -> int | None:
    """Epoch seconds at the start of `period`, or `None` for everything.

    `None` rather than `0` for `ALL`: the caller has to branch on it anyway to
    reach `delete_all`, and a sentinel that also happens to be a valid timestamp
    is the kind of thing that silently keeps working while meaning something else.
    """
    moment = (now or datetime.now()).astimezone()

    match period:
        case HistoryRange.ALL:
            return None
        case HistoryRange.TODAY:
            start = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        case HistoryRange.WEEK:
            midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
            start = midnight - timedelta(days=midnight.weekday())
        case HistoryRange.MONTH:
            start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        case HistoryRange.YEAR:
            start = moment.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    return int(start.timestamp())
