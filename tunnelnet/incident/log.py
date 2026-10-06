"""Append only, hash chained disposition event log.

There is deliberately no update or delete method. Every event commits the hash
of the previous event for the same ticket, so rewriting, reordering or removing
a historical event breaks the chain and is caught by :meth:`EventLog.verify`.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Iterable

from .model import EVENT_OPENED, GENESIS, Event


class BrokenHistoryError(RuntimeError):
    """Raised when a stored event fails hash chain verification."""


class EventLog:
    def __init__(self) -> None:
        self._events: list[Event] = []
        self._by_ticket: dict[str, list[Event]] = defaultdict(list)
        self._lock = threading.RLock()

    def append(self, event: Event) -> Event:
        """Seal and commit an event; reject any gap or fork in the chain."""

        sealed = event.seal() if not event.hash else event
        with self._lock:
            history = self._by_ticket[sealed.ticket_id]
            expected_seq = 0 if sealed.kind == EVENT_OPENED and not history else len(history)
            if sealed.seq != expected_seq:
                raise ValueError(
                    f"event seq {sealed.seq} out of order for {sealed.ticket_id} (expected {expected_seq})"
                )
            expected_prev = history[-1].hash if history else GENESIS
            if sealed.prev_hash != expected_prev:
                raise ValueError(f"event {sealed.seq} forks history of {sealed.ticket_id}")
            if sealed.hash != sealed.compute_hash():
                raise ValueError(f"event {sealed.seq} of {sealed.ticket_id} has a bad hash")
            self._events.append(sealed)
            history.append(sealed)
        return sealed

    def events(self, ticket_id: str) -> list[Event]:
        with self._lock:
            return list(self._by_ticket.get(ticket_id, ()))

    def all(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def count(self) -> int:
        with self._lock:
            return len(self._events)

    def verify(self, ticket_id: str | None = None) -> bool:
        """Recompute every hash and chain link. Returns True or raises."""

        with self._lock:
            groups: Iterable
            if ticket_id is None:
                groups = list(self._by_ticket.values())
            else:
                groups = [list(self._by_ticket.get(ticket_id, ()))]
            for history in groups:
                prev = GENESIS
                for seq, event in enumerate(history):
                    if event.seq != seq:
                        raise BrokenHistoryError(f"{event.ticket_id} seq gap at {event.seq}")
                    if event.prev_hash != prev:
                        raise BrokenHistoryError(f"{event.ticket_id} broken chain at {event.seq}")
                    if event.hash != event.compute_hash():
                        raise BrokenHistoryError(f"{event.ticket_id} tampered event at {event.seq}")
                    prev = event.hash
        return True

    def describe(self) -> str:
        return f"events={self.count()}"
