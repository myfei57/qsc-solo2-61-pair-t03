"""SLA and escalation policy.

Two policy decisions are fixed here, on purpose:

* Suspension never closes a ticket on its own. When the suspend deadline passes
  the ticket pops back to OPEN for the current shift. The suspended interval
  counts toward the overall resolution SLA, so a handover cannot quietly bury an
  alarm by suspending it across the boundary.
* Escalation walks up one level at a time (L1 -> L2 -> L3). A severity may opt
  in to ``jump_to_top`` which skips straight to the highest level on the first
  timeout; CRITICAL alarms do this.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .model import Severity

MAX_LEVEL = 3


def _parse(at: str) -> datetime:
    return datetime.fromisoformat(at)


def _format(value: datetime) -> str:
    return value.isoformat()


def add_minutes(at: str, minutes: int) -> str:
    return _format(_parse(at) + timedelta(minutes=minutes))


def is_overdue(deadline: str, now: str) -> bool:
    return bool(deadline) and now > deadline


@dataclass(frozen=True)
class SeverityPolicy:
    """Deadlines and routing for one severity."""

    severity: Severity
    entry_level: int
    ack_minutes: int
    resolve_minutes: int
    jump_to_top: bool

    def ack_deadline(self, opened_at: str) -> str:
        return add_minutes(opened_at, self.ack_minutes)

    def resolve_deadline(self, opened_at: str) -> str:
        return add_minutes(opened_at, self.resolve_minutes)


DEFAULT_POLICIES: dict[Severity, SeverityPolicy] = {
    Severity.MINOR: SeverityPolicy(Severity.MINOR, 1, 30, 240, False),
    Severity.MAJOR: SeverityPolicy(Severity.MAJOR, 1, 15, 120, False),
    Severity.CRITICAL: SeverityPolicy(Severity.CRITICAL, 1, 5, 30, True),
}


class EscalationPolicy:
    """Resolves the next escalation level for a ticket."""

    def __init__(self, policies: dict[Severity, SeverityPolicy] | None = None) -> None:
        self._policies = dict(policies or DEFAULT_POLICIES)

    def for_severity(self, severity: Severity) -> SeverityPolicy:
        return self._policies[severity]

    def entry_level(self, severity: Severity) -> int:
        return self._policies[severity].entry_level

    def jumps_to_top(self, severity: Severity) -> bool:
        return self._policies[severity].jump_to_top

    def next_level(self, current: int, severity: Severity) -> int:
        """Level after one timeout: step up, or jump to the top on first breach."""

        policy = self._policies[severity]
        if policy.jump_to_top and current == policy.entry_level:
            return MAX_LEVEL
        return min(MAX_LEVEL, current + 1)

    def at_top(self, level: int) -> bool:
        return level >= MAX_LEVEL

    def set_minutes(self, severity: Severity, ack: int, resolve: int) -> SeverityPolicy:
        policy = self._policies[severity]
        replaced = SeverityPolicy(severity, policy.entry_level, ack, resolve, policy.jump_to_top)
        self._policies[severity] = replaced
        return replaced

    def as_dicts(self) -> list[dict[str, Any]]:
        return [
            {
                "severity": severity.value,
                "entry_level": policy.entry_level,
                "ack_minutes": policy.ack_minutes,
                "resolve_minutes": policy.resolve_minutes,
                "jump_to_top": policy.jump_to_top,
            }
            for severity, policy in self._policies.items()
        ]
