"""Append-only, hash-chained disposition history.

History is write-once by construction: there is no update or delete API.
Every event carries the SHA-256 chain of the previous event, so deleting or
editing an old record breaks every later link and is reported by
:meth:`History.verify`.  A history kept purely in memory still gets the
chaining guarantees; the durable journal adds persistence on top.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import replace

from .clock import epoch
from .model import Event

GENESIS = "0" * 64


def digest(event: Event, previous_chain: str) -> str:
    """Compute the chain hash for an event following ``previous_chain``."""

    payload = json.dumps(
        {
            "seq": event.seq,
            "kind": event.kind,
            "at": event.at,
            "actor": event.actor,
            "detail": event.detail,
            "previous": previous_chain,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class History:
    """The ordered, immutable events of one alarm."""

    def __init__(self, alarm_id: str) -> None:
        self.alarm_id = alarm_id
        self._events: list[Event] = []
        self._lock = threading.RLock()

    def append(self, kind: str, at: str, actor: str = "", detail: dict | None = None) -> Event:
        with self._lock:
            previous = self._events[-1].chain if self._events else GENESIS
            event = Event(
                seq=len(self._events) + 1,
                kind=kind,
                at=at,
                actor=actor,
                detail=dict(detail or {}),
            )
            chained = replace(event, chain=digest(event, previous))
            self._events.append(chained)
            return chained

    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def count(self) -> int:
        with self._lock:
            return len(self._events)

    def kinds(self) -> list[str]:
        return [event.kind for event in self.events()]

    def tail_chain(self) -> str:
        with self._lock:
            return self._events[-1].chain if self._events else GENESIS

    def verify(self) -> bool:
        """Recompute every link and report whether the chain is intact."""

        with self._lock:
            previous = GENESIS
            expected_seq = 1
            for event in self._events:
                if event.seq != expected_seq:
                    return False
                if event.chain != digest(event, previous):
                    return False
                previous = event.chain
                expected_seq += 1
            return True

    def as_dicts(self) -> list[dict[str, object]]:
        return [event.as_dict() for event in self.events()]

    @classmethod
    def from_dicts(cls, alarm_id: str, rows: list[dict[str, object]]) -> "History":
        history = cls(alarm_id)
        for row in rows:
            history._events.append(Event.from_dict(row))
        return history


def stamp_ordering_key(at: str) -> int:
    """Tie breaker used when several alarms share a raised timestamp."""

    return epoch(at)
