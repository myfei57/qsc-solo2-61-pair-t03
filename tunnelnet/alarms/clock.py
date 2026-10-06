"""Wall clock and ISO-8601 deadline arithmetic for the alarm board.

Timestamps inside the board are always UTC strings ending in ``Z`` so they
sort lexicographically in chronological order.  Keeping the clock injectable
makes every SLA and escalation test deterministic without sleeping.
"""

from __future__ import annotations

import time
from calendar import timegm
from datetime import datetime, timedelta, timezone

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now() -> str:
    """Current UTC time in the compact form used across the service."""

    return time.strftime(STAMP_FORMAT, time.gmtime())


def parse(stamp: str) -> datetime:
    """Parse an output of :func:`utc_now` into an aware UTC datetime."""

    return datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=timezone.utc)


def format(moment: datetime) -> str:
    """Render an aware datetime in the canonical stamp form."""

    return moment.astimezone(timezone.utc).strftime(STAMP_FORMAT)


def add_seconds(stamp: str, seconds: int) -> str:
    """Return the stamp ``seconds`` after ``stamp``."""

    return format(parse(stamp) + timedelta(seconds=seconds))


def is_after(stamp: str, reference: str) -> bool:
    """Whether ``stamp`` is strictly later than ``reference``."""

    return parse(stamp) > parse(reference)


def epoch(stamp: str) -> int:
    """Seconds since the Unix epoch, used for chaining hashes."""

    return timegm(parse(stamp).timetuple())
