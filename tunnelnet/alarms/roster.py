"""Duty roster: shifts and the person reached at each escalation level."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .model import LEVEL_NAMES, LEVELS


@dataclass(frozen=True)
class Shift:
    """One scheduled watch and the staff covering every escalation level."""

    id: str
    starts_at: str
    ends_at: str
    members: dict[int, str] = field(default_factory=dict)
    domains: tuple[str, ...] = ()

    def member_at(self, level: int) -> str:
        return self.members.get(level, "")

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "members": {LEVEL_NAMES.get(level, str(level)): who for level, who in self.members.items()},
            "domains": list(self.domains),
        }


class Roster:
    """Thread safe schedule of shifts keyed by shift id.

    Shifts are stored explicitly and the active shift for a moment is the
    last-started shift whose half-open window ``[starts_at, ends_at)``
    contains it.  A roster without a matching shift falls back to an explicit
    default watch, so an alarm can never be raised into an empty assignment.
    """

    def __init__(self, default: Shift | None = None) -> None:
        self._shifts: dict[str, Shift] = {}
        self._default = default
        self._lock = threading.RLock()

    def put(self, shift: Shift) -> Shift:
        with self._lock:
            self._shifts[shift.id] = shift
            return shift

    def remove(self, shift_id: str) -> bool:
        with self._lock:
            return self._shifts.pop(shift_id, None) is not None

    def get(self, shift_id: str) -> Shift | None:
        with self._lock:
            return self._shifts.get(shift_id)

    def resolve(self, shift_id: str) -> Shift | None:
        """Find a scheduled shift, falling back to the default watch by id."""

        with self._lock:
            shift = self._shifts.get(shift_id)
            if shift is not None:
                return shift
            if self._default is not None and self._default.id == shift_id:
                return self._default
            return None

    def list(self) -> list[Shift]:
        with self._lock:
            return [self._shifts[key] for key in sorted(self._shifts)]

    def set_default(self, shift: Shift | None) -> None:
        with self._lock:
            self._default = shift

    def active(self, at: str) -> Shift | None:
        """Return the shift on duty at ``at``.

        When windows overlap during a handover the shift that started latest
        wins, matching the freshly handed-over watch owning the floor.
        """

        with self._lock:
            covering = [
                shift
                for shift in self._shifts.values()
                if shift.starts_at <= at < shift.ends_at
            ]
            if covering:
                return max(covering, key=lambda shift: shift.starts_at)
            return self._default

    def assignee(self, at: str, level: int) -> str:
        """Resolve who must handle an alarm of ``level`` at moment ``at``."""

        shift = self.active(at)
        if shift is None:
            return ""
        return shift.member_at(level)

    def chain_for(self, at: str, level: int) -> list[str]:
        """Escalation path from ``level`` up to the director, inclusive."""

        shift = self.active(at)
        if shift is None:
            return []
        return [shift.member_at(step) for step in LEVELS if step >= level]

    def handover(self, previous_id: str, next_id: str) -> bool:
        """Report whether both watches of a handover are scheduled.

        The roster itself only stores shifts; handover is surfaced explicitly
        because the board reassigns every still-open alarm of the outgoing
        watch when the new watch takes the floor.
        """

        with self._lock:
            return previous_id in self._shifts and next_id in self._shifts

    def describe(self) -> str:
        with self._lock:
            return f"shifts={len(self._shifts)} default={'yes' if self._default else 'no'}"
