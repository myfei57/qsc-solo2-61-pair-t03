"""Read projections served to the environment, fire, audit and console pages.

All four readers call the same board, so disposition state can never drift
between the environment page, fire page, audit trail and console: they are
projections of one board rather than separate lists.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import CLOSED, PENDING, PROCESSING, SUSPENDED, SOURCES


@dataclass(frozen=True)
class SourceStatus:
    """Open alarm counts of one source, embedded in that source's page."""

    source: str
    open: int
    pending: int
    processing: int
    suspended: int
    closed: int

    def as_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "open": self.open,
            PENDING: self.pending,
            PROCESSING: self.processing,
            SUSPENDED: self.suspended,
            CLOSED: self.closed,
        }


@dataclass(frozen=True)
class BoardReport:
    """Board-wide projection used by the console and audit pages."""

    counts: dict[str, int]
    by_source: dict[str, dict[str, int]]
    open_alarms: list[dict[str, object]] = field(default_factory=list)
    notifications_pending: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "counts": dict(self.counts),
            "by_source": {key: dict(value) for key, value in self.by_source.items()},
            "open_alarms": [dict(row) for row in self.open_alarms],
            "notifications_pending": self.notifications_pending,
        }


def source_status(board, source: str) -> SourceStatus:
    """Project one source's alarms for embedding in its own page."""

    rows = board.list(source=source)
    buckets = {PENDING: 0, PROCESSING: 0, SUSPENDED: 0, CLOSED: 0}
    for alarm in rows:
        buckets[alarm.state] += 1
    open_count = sum(buckets[state] for state in (PENDING, PROCESSING, SUSPENDED))
    return SourceStatus(
        source=source,
        open=open_count,
        pending=buckets[PENDING],
        processing=buckets[PROCESSING],
        suspended=buckets[SUSPENDED],
        closed=buckets[CLOSED],
    )


def board_report(board) -> BoardReport:
    """Full projection: every reader page is built from this same data."""

    by_source: dict[str, dict[str, int]] = {
        source: {"total": 0, "open": 0} for source in SOURCES
    }
    by_source.update(board.by_source())
    return BoardReport(
        counts=board.counts(),
        by_source=by_source,
        open_alarms=[alarm.as_dict() for alarm in board.open_alarms()],
        notifications_pending=len(board.outbox().pending()),
    )
