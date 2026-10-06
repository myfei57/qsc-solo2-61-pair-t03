"""Duty shifts and the people on call.

A shift is a time boxed roster entry. Tickets are assigned to the operator who
is on call for the alarm's shift, and a handover moves every unfinished ticket
of an ending shift onto the starting shift.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class Shift:
    """One scheduled duty window."""

    id: str
    starts_at: str
    ends_at: str
    operator: str
    level: int = 1

    def contains(self, at: str) -> bool:
        return self.starts_at <= at < self.ends_at

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "operator": self.operator,
            "level": self.level,
        }


class Roster:
    """Append-maintained schedule of shifts."""

    def __init__(self) -> None:
        self._shifts: list[Shift] = []
        self._lock = threading.RLock()

    def add(self, shift: Shift) -> Shift:
        with self._lock:
            if any(existing.id == shift.id for existing in self._shifts):
                raise ValueError(f"shift {shift.id} already exists")
            self._shifts.append(shift)
            self._shifts.sort(key=lambda item: item.starts_at)
        return shift

    def shifts(self) -> list[Shift]:
        with self._lock:
            return list(self._shifts)

    def on_call(self, at: str, level: int = 1) -> Shift | None:
        """The shift covering ``at`` at the requested escalation level."""

        with self._lock:
            for shift in reversed(self._shifts):
                if shift.contains(at) and shift.level == level:
                    return shift
        return None

    def owner_for(self, at: str, level: int = 1) -> str:
        shift = self.on_call(at, level)
        return shift.operator if shift else ""

    def handover(self, from_shift: str, to_shift: str) -> tuple[str, str]:
        """Resolve the operators for a manual handover between two shifts."""

        source = next((item for item in self._shifts if item.id == from_shift), None)
        target = next((item for item in self._shifts if item.id == to_shift), None)
        if source is None or target is None:
            raise ValueError("unknown shift in handover")
        return source.operator, target.operator

    def describe(self) -> str:
        return f"shifts={len(self._shifts)}"
