"""Idempotency records for state-changing disposition requests.

Every mutating API call may carry a client ``request_id``.  The first call
with a given id runs and its outcome is remembered; an immediate replay
returns the stored outcome instead of performing the action twice.  This
protects acknowledgements, suspensions and closures from double submission
over a flaky console connection, independently of alarm de-duplication
(which merges repeated upstream reports).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class IdempotencyRecord:
    """Result captured the first time a request id was processed."""

    request_id: str
    alarm_id: str
    action: str
    at: str
    outcome: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "alarm_id": self.alarm_id,
            "action": self.action,
            "at": self.at,
            "outcome": dict(self.outcome),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "IdempotencyRecord":
        return cls(
            request_id=str(payload.get("request_id", "")),
            alarm_id=str(payload.get("alarm_id", "")),
            action=str(payload.get("action", "")),
            at=str(payload.get("at", "")),
            outcome=dict(payload.get("outcome", {})),  # type: ignore[arg-type]
        )


class IdempotencyStore:
    """Remembers one outcome per request id."""

    def __init__(self) -> None:
        self._records: dict[str, IdempotencyRecord] = {}
        self._lock = threading.RLock()

    def seen(self, request_id: str) -> IdempotencyRecord | None:
        if not request_id:
            return None
        with self._lock:
            return self._records.get(request_id)

    def remember(self, record: IdempotencyRecord) -> None:
        with self._lock:
            self._records.setdefault(record.request_id, record)

    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def all(self) -> list[IdempotencyRecord]:
        with self._lock:
            return [self._records[key] for key in sorted(self._records)]
