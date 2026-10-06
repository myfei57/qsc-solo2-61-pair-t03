"""Durable journal and projection storage on top of the file ``Store``.

The board is event sourced.  Three record kinds are persisted:

``alarm-journal/<alarm_id>``  append-only list of hash-chained events
``alarm-state/<alarm_id>``    current projection, rewritten atomically
``alarm-outbox/<message_id>`` owed notification, rewritten until delivered

A single append writes the event first and only then replaces the
projection, so a crash between the two is repaired on load by replaying the
journal tail.  Journal documents are never overwritten with fewer events and
there is no delete API, which keeps the disposition history tamper-evident
and non-rewritable.
"""

from __future__ import annotations

from typing import Protocol

from tunnelnet.store import Store

from .history import History
from .model import Event
from .outbox import Message

JOURNAL_KIND = "alarm-journal"
STATE_KIND = "alarm-state"
OUTBOX_KIND = "alarm-outbox"
INDEX_KIND = "alarm-index"
ROSTER_KIND = "alarm-roster"
INDEX_ID = "dedupe"


class JournalStore(Protocol):
    """Storage surface the repository needs, satisfied by the file Store."""

    def save(self, kind: str, record_id: str, value: object) -> None:  # pragma: no cover
        ...

    def load(self, kind: str, record_id: str) -> dict[str, object] | None:  # pragma: no cover
        ...

    def list(self, kind: str) -> list[str]:  # pragma: no cover
        ...


class AlarmRepository:
    """Loads and persists alarms, histories and outbox messages."""

    def __init__(self, store: JournalStore) -> None:
        self._store = store

    def load_events(self, alarm_id: str) -> list[Event]:
        payload = self._store.load(JOURNAL_KIND, alarm_id)
        if payload is None:
            return []
        rows = payload.get("events", [])
        return [Event.from_dict(row) for row in rows]  # type: ignore[arg-type]

    def load_history(self, alarm_id: str) -> History:
        return History.from_dicts(alarm_id, [event.as_dict() for event in self.load_events(alarm_id)])

    def append_event(self, alarm_id: str, history: History, event: Event) -> None:
        """Append one event, never shrinking or rewriting older events."""

        events = self.load_events(alarm_id)
        events.append(event)
        self._store.save(JOURNAL_KIND, alarm_id, {"id": alarm_id, "events": [row.as_dict() for row in events]})

    def save_state(self, alarm) -> None:
        self._store.save(STATE_KIND, alarm.id, alarm.as_dict())

    def load_state(self, alarm_id: str):
        from .model import Alarm

        payload = self._store.load(STATE_KIND, alarm_id)
        return None if payload is None else Alarm.from_dict(payload)

    def list_state_ids(self) -> list[str]:
        return self._store.list(STATE_KIND)

    def save_message(self, message: Message) -> None:
        self._store.save(OUTBOX_KIND, message.id, message.as_dict())

    def load_messages(self) -> list[Message]:
        return [
            message
            for message_id in self._store.list(OUTBOX_KIND)
            if (payload := self._store.load(OUTBOX_KIND, message_id)) is not None
            for message in (Message.from_dict(payload),)
        ]

    def save_dedupe_index(self, mapping: dict[str, str]) -> None:
        self._store.save(INDEX_KIND, INDEX_ID, {"open_by_key": mapping})

    def load_dedupe_index(self) -> dict[str, str]:
        payload = self._store.load(INDEX_KIND, INDEX_ID)
        if payload is None:
            return {}
        return dict(payload.get("open_by_key", {}))  # type: ignore[arg-type]

    def save_shift(self, shift) -> None:
        self._store.save(ROSTER_KIND, shift.id, shift.as_dict())

    def load_shifts(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for shift_id in self._store.list(ROSTER_KIND):
            payload = self._store.load(ROSTER_KIND, shift_id)
            if payload is not None:
                rows.append(payload)
        return rows
