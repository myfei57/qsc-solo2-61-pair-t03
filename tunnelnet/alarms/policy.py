"""SLA policy: acknowledgement deadline, solve deadline and suspend limits."""

from __future__ import annotations

from dataclasses import dataclass

from .model import LEVEL_DIRECTOR, LEVEL_PRIMARY


@dataclass(frozen=True)
class Policy:
    """Timing rules applied to every open alarm.

    ``ack_seconds``       max time an alarm may sit pending before escalation
    ``solve_seconds``     max time an alarm may stay processing before escalation
    ``escalate_seconds``  quiet period between automatic level bumps
    ``max_suspends``      how many times one alarm may be suspended
    ``suspend_seconds``   default suspend window when the caller omits one
    ``max_suspend_seconds`` ceiling on any single suspend window
    ``hard_cap_level``    alarms overdue past every rung land on this level
    """

    ack_seconds: int = 300
    solve_seconds: int = 1800
    escalate_seconds: int = 900
    max_suspends: int = 2
    suspend_seconds: int = 1800
    max_suspend_seconds: int = 7200
    hard_cap_level: int = LEVEL_DIRECTOR
    entry_level: int = LEVEL_PRIMARY

    def as_dict(self) -> dict[str, int]:
        return {
            "ack_seconds": self.ack_seconds,
            "solve_seconds": self.solve_seconds,
            "escalate_seconds": self.escalate_seconds,
            "max_suspends": self.max_suspends,
            "suspend_seconds": self.suspend_seconds,
            "max_suspend_seconds": self.max_suspend_seconds,
            "hard_cap_level": self.hard_cap_level,
            "entry_level": self.entry_level,
        }


DEFAULT_POLICY = Policy()
