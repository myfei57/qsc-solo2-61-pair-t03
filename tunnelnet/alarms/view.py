"""One alarm row in the shared disposition projection."""

from __future__ import annotations

from dataclasses import dataclass

from .model import LEVEL_NAMES


@dataclass(frozen=True)
class AlarmView:
    """Flat row shared by every page that displays alarm disposition."""

    id: str
    source: str
    subject: str
    state: str
    level: str
    assignee: str
    occurrences: int
    suspend_until: str

    @classmethod
    def from_alarm(cls, alarm) -> "AlarmView":
        return cls(
            id=alarm.id,
            source=alarm.source,
            subject=alarm.subject,
            state=alarm.state,
            level=LEVEL_NAMES[alarm.level],
            assignee=alarm.assignee,
            occurrences=alarm.occurrence_count,
            suspend_until=alarm.suspend_until,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "source": self.source,
            "subject": self.subject,
            "state": self.state,
            "level": self.level,
            "assignee": self.assignee,
            "occurrences": self.occurrences,
            "suspend_until": self.suspend_until,
        }
