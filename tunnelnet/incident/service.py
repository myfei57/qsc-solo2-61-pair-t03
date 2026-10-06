"""Incident center: the single disposition state machine for every alarm.

Environment, fire and cable alarms all enter through :meth:`report` and every
console page reads the same ticket projections back out of this class.

Reliability model
-----------------
* The hash chained event log is the source of truth; ticket objects are only a
  projection of it and can be rebuilt by replaying the log.
* State transitions commit to the log inside one lock. Side effects (owner
  notifications) go to an outbox and are delivered afterwards, so a failed or
  half delivered notification never rolls back or blocks the disposition.
  Pending outbox items are retried with :meth:`flush_outbox`.
* Repeated alarm reports are merged onto the open ticket of the same
  ``dedupe_key``; an exact ``report_id`` replay returns the same ticket without
  writing a second event (idempotent ingestion).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol

from .log import EventLog
from .model import (
    EVENT_ACKED,
    EVENT_CLOSED,
    EVENT_DUPLICATE,
    EVENT_ESCALATED,
    EVENT_OPENED,
    EVENT_REASSIGNED,
    EVENT_RESUMED,
    EVENT_SUSPEND_EXPIRED,
    EVENT_SUSPENDED,
    TRANSITIONS,
    Event,
    Severity,
    State,
    Ticket,
)
from .policy import MAX_LEVEL, EscalationPolicy, add_minutes
from .roster import Roster


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Notifier(Protocol):
    """Delivers one disposition notification. Returning False means retry later."""

    def notify(self, channel: str, payload: dict[str, object]) -> bool:  # pragma: no cover - structural
        ...


class LoggingNotifier:
    """Default notifier: records deliveries in memory and always succeeds."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object]]] = []

    def notify(self, channel: str, payload: dict[str, object]) -> bool:
        self.sent.append((channel, payload))
        return True

    def describe(self) -> str:
        return f"notifications={len(self.sent)}"


@dataclass
class OutboxItem:
    """A side effect that must be delivered at least once."""

    id: int
    ticket_id: str
    event_seq: int
    channel: str
    kind: str
    payload: dict[str, object]
    attempts: int = 0
    status: str = "pending"  # pending | done
    last_error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "ticket_id": self.ticket_id,
            "event_seq": self.event_seq,
            "channel": self.channel,
            "kind": self.kind,
            "attempts": self.attempts,
            "status": self.status,
            "last_error": self.last_error,
        }


class IllegalTransitionError(RuntimeError):
    """An event was applied from a state that does not allow it."""


@dataclass
class _Summary:
    by_state: dict[str, int] = field(default_factory=dict)
    by_source: dict[str, int] = field(default_factory=dict)


class IncidentCenter:
    def __init__(
        self,
        roster: Roster,
        policy: EscalationPolicy | None = None,
        log: EventLog | None = None,
        notifier: Notifier | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self._roster = roster
        self._policy = policy or EscalationPolicy()
        self._log = log or EventLog()
        self._notifier = notifier or LoggingNotifier()
        self._clock = clock
        self._tickets: dict[str, Ticket] = {}
        self._open_by_key: dict[str, str] = {}
        self._report_ids: set[str] = set()
        self._outbox: list[OutboxItem] = []
        self._lock = threading.RLock()
        self._rebuild()

    # ---------------------------------------------------------------- ingest

    def report(
        self,
        source: str,
        dedupe_key: str,
        severity: Severity,
        at: str | None = None,
        report_id: str = "",
        detail: dict[str, object] | None = None,
    ) -> tuple[Ticket, bool]:
        """Raise or merge one alarm. Returns (ticket, created_this_call)."""

        at = at or self._clock()
        with self._lock:
            if report_id and report_id in self._report_ids:
                # Exact replay of a delivered report: same answer, no new event.
                existing = self._open_by_key.get(dedupe_key)
                if existing is not None:
                    return self._tickets[existing], False
            if report_id:
                self._report_ids.add(report_id)

            ticket_id = self._open_by_key.get(dedupe_key)
            if ticket_id is not None:
                ticket = self._tickets[ticket_id]
                event = self._commit(
                    ticket,
                    EVENT_DUPLICATE,
                    at,
                    actor="system",
                    data={"report_id": report_id, "detail": detail or {}},
                )
                return self._tickets[ticket.id], False

            reopened_from = self._closed_id_for(dedupe_key)
            ticket = self._open(source, dedupe_key, severity, at, report_id, detail, reopened_from)
            return ticket, True

    def _open(
        self,
        source: str,
        dedupe_key: str,
        severity: Severity,
        at: str,
        report_id: str,
        detail: dict[str, object] | None,
        reopened_from: str,
    ) -> Ticket:
        rules = self._policy.for_severity(severity)
        level = rules.entry_level
        shift = self._roster.on_call(at, level)
        shift_id = shift.id if shift else ""
        owner = shift.operator if shift else ""
        ticket_id = self._next_ticket_id()
        ticket = Ticket(
            id=ticket_id,
            source=source,
            dedupe_key=dedupe_key,
            severity=severity,
            state=State.OPEN,
            level=level,
            owner=owner,
            shift_id=shift_id,
            occurrence=1,
            opened_at=at,
            updated_at=at,
            ack_due=rules.ack_deadline(at),
            resolve_due=rules.resolve_deadline(at),
            level_due=rules.ack_deadline(at),
            jump_to_top=rules.jump_to_top,
        )
        data: dict[str, object] = {
            "source": source,
            "dedupe_key": dedupe_key,
            "severity": severity.value,
            "level": level,
            "ack_due": ticket.ack_due,
            "resolve_due": ticket.resolve_due,
            "level_due": ticket.level_due,
            "jump_to_top": ticket.jump_to_top,
            "report_id": report_id,
            "detail": detail or {},
            "shift_id": shift_id,
            "owner": owner,
        }
        if reopened_from:
            data["reopened_from"] = reopened_from
        event = self._commit(ticket, EVENT_OPENED, at, actor="system", data=data)
        self._enqueue(ticket, event, "new")
        self._flush()
        return self._tickets[ticket_id]

    # ------------------------------------------------------------ disposition

    def acknowledge(self, ticket_id: str, actor: str, at: str | None = None, note: str = "") -> Ticket:
        at = at or self._clock()
        with self._lock:
            ticket = self._require(ticket_id)
            self._commit(ticket, EVENT_ACKED, at, actor=actor, note=note, data={})
            self._flush()
            return self._tickets[ticket_id]

    def suspend(
        self, ticket_id: str, actor: str, minutes: int, reason: str = "", at: str | None = None
    ) -> Ticket:
        if minutes <= 0:
            raise ValueError("suspend minutes must be positive")
        at = at or self._clock()
        with self._lock:
            ticket = self._require(ticket_id)
            due = add_minutes(at, minutes)
            self._commit(
                ticket,
                EVENT_SUSPENDED,
                at,
                actor=actor,
                note=reason,
                data={"suspend_due": due, "minutes": minutes},
            )
            self._flush()
            return self._tickets[ticket_id]

    def resume(self, ticket_id: str, actor: str, at: str | None = None, note: str = "") -> Ticket:
        at = at or self._clock()
        with self._lock:
            ticket = self._require(ticket_id)
            self._commit(ticket, EVENT_RESUMED, at, actor=actor, note=note, data={})
            self._flush()
            return self._tickets[ticket_id]

    def close(self, ticket_id: str, actor: str, note: str = "", at: str | None = None) -> Ticket:
        at = at or self._clock()
        with self._lock:
            ticket = self._require(ticket_id)
            event = self._commit(ticket, EVENT_CLOSED, at, actor=actor, note=note, data={})
            self._enqueue(self._tickets[ticket_id], event, "closed")
            self._flush()
            return self._tickets[ticket_id]

    def reassign(self, ticket_id: str, operator: str, actor: str, shift_id: str = "",
                 at: str | None = None, note: str = "") -> Ticket:
        at = at or self._clock()
        with self._lock:
            ticket = self._require(ticket_id)
            self._commit(
                ticket,
                EVENT_REASSIGNED,
                at,
                actor=actor,
                note=note,
                data={"owner": operator, "shift_id": shift_id},
            )
            self._flush()
            return self._tickets[ticket_id]

    def handover(self, from_shift: str, to_shift: str, actor: str = "system",
                 at: str | None = None) -> list[str]:
        """Move every unfinished ticket of an ending shift to the next shift."""

        at = at or self._clock()
        moved: list[str] = []
        with self._lock:
            _, operator = self._roster.handover(from_shift, to_shift)
            for ticket in list(self._tickets.values()):
                if ticket.shift_id != from_shift or ticket.state == State.CLOSED:
                    continue
                self._commit(
                    ticket,
                    EVENT_REASSIGNED,
                    at,
                    actor=actor,
                    note="shift handover",
                    data={"owner": operator, "shift_id": to_shift},
                )
                moved.append(ticket.id)
            self._flush()
        return moved

    # ----------------------------------------------------------------- timers

    def run_due(self, now: str | None = None) -> dict[str, list[str]]:
        """Process suspend expiries and SLA escalations. Safe to call repeatedly."""

        now = now or self._clock()
        expired: list[str] = []
        escalated: list[str] = []
        with self._lock:
            for ticket in list(self._tickets.values()):
                if ticket.state == State.SUSPENDED and ticket.suspend_due and now >= ticket.suspend_due:
                    self._expire_suspension(ticket, now)
                    expired.append(ticket.id)

            for ticket in list(self._tickets.values()):
                if ticket.state in (State.OPEN, State.ACTIVE) and ticket.level_due and now > ticket.level_due:
                    if self._escalate(ticket, now):
                        escalated.append(ticket.id)
            self._flush()
        return {"expired": expired, "escalated": escalated}

    def _expire_suspension(self, ticket: Ticket, now: str) -> None:
        """Policy: suspension pops the alarm back to OPEN for the current shift."""

        shift = self._roster.on_call(now, ticket.level)
        shift_id = shift.id if shift else ticket.shift_id
        owner = shift.operator if shift else ticket.owner
        rules = self._policy.for_severity(ticket.severity)
        event = self._commit(
            ticket,
            EVENT_SUSPEND_EXPIRED,
            now,
            actor="system",
            data={"shift_id": shift_id, "owner": owner, "level_due": rules.ack_deadline(now)},
        )
        self._enqueue(self._tickets[ticket.id], event, "reopened")

    def _escalate(self, ticket: Ticket, now: str) -> bool:
        if self._policy.at_top(ticket.level):
            return False
        next_level = self._policy.next_level(ticket.level, ticket.severity)
        if next_level == ticket.level:
            return False
        shift = self._roster.on_call(now, next_level)
        owner = shift.operator if shift else ticket.owner
        rules = self._policy.for_severity(ticket.severity)
        event = self._commit(
            ticket,
            EVENT_ESCALATED,
            now,
            actor="system",
            data={
                "from_level": ticket.level,
                "to_level": next_level,
                "owner": owner,
                "level_due": rules.ack_deadline(now),
                "jumped": next_level > ticket.level + 1,
            },
        )
        self._enqueue(self._tickets[ticket.id], event, "escalation")
        return True

    # ---------------------------------------------------------------- queries

    def get(self, ticket_id: str) -> Ticket:
        with self._lock:
            return self._require(ticket_id)

    def list(
        self,
        state: State | None = None,
        source: str = "",
        owner: str = "",
        shift_id: str = "",
    ) -> list[Ticket]:
        with self._lock:
            tickets = list(self._tickets.values())
        if state is not None:
            tickets = [item for item in tickets if item.state == state]
        if source:
            tickets = [item for item in tickets if item.source == source]
        if owner:
            tickets = [item for item in tickets if item.owner == owner]
        if shift_id:
            tickets = [item for item in tickets if item.shift_id == shift_id]
        return sorted(tickets, key=lambda item: item.opened_at + item.id)

    def queue_for(self, owner: str) -> list[Ticket]:
        return [item for item in self.list(owner=owner) if item.state != State.CLOSED]

    def history(self, ticket_id: str) -> list[Event]:
        self._require(ticket_id)
        return self._log.events(ticket_id)

    def overdue(self, now: str | None = None) -> dict[str, list[Ticket]]:
        now = now or self._clock()
        ack_overdue = [
            item
            for item in self.list()
            if item.state == State.OPEN and item.ack_due and now > item.ack_due
        ]
        resolve_overdue = [
            item
            for item in self.list()
            if item.state != State.CLOSED and item.resolve_due and now > item.resolve_due
        ]
        return {"ack": ack_overdue, "resolve": resolve_overdue}

    def counts(self) -> dict[str, int]:
        result = {state.value: 0 for state in State}
        result["total"] = 0
        for ticket in self._tickets.values():
            result[ticket.state.value] += 1
            result["total"] += 1
        return result

    # ------------------------------------------------------------ reliability

    def flush_outbox(self) -> dict[str, int]:
        """Retry every undelivered side effect. Returns delivery counters."""

        with self._lock:
            return self._flush()

    def outbox(self, include_done: bool = False) -> list[OutboxItem]:
        with self._lock:
            return [item for item in self._outbox if include_done or item.status == "pending"]

    def verify_history(self) -> bool:
        return self._log.verify()

    # --------------------------------------------------------------- internals

    def _commit(self, ticket: Ticket, kind: str, at: str, actor: str = "", note: str = "",
                data: dict[str, object] | None = None) -> Event:
        allowed = TRANSITIONS[kind]
        if kind != EVENT_OPENED and ticket.state not in allowed:
            raise IllegalTransitionError(
                f"ticket {ticket.id} cannot {kind} from {ticket.state.value}"
            )
        history = self._log.events(ticket.id)
        event = Event(
            seq=len(history),
            ticket_id=ticket.id,
            kind=kind,
            at=at,
            actor=actor,
            note=note,
            data=dict(data or {}),
            prev_hash=ticket.last_event_hash,
        ).seal()
        self._log.append(event)
        self._apply(event)
        return event

    def _apply(self, event: Event) -> None:
        current = self._tickets.get(event.ticket_id)
        if event.kind == EVENT_OPENED:
            data = event.data
            ticket = Ticket(
                id=event.ticket_id,
                source=str(data.get("source", "")),
                dedupe_key=str(data.get("dedupe_key", "")),
                severity=Severity(data["severity"]),
                state=State.OPEN,
                level=int(data.get("level", 1)),
                owner=str(data.get("owner", "")),
                shift_id=str(data.get("shift_id", "")),
                occurrence=1,
                opened_at=event.at,
                updated_at=event.at,
                ack_due=str(data.get("ack_due", "")),
                resolve_due=str(data.get("resolve_due", "")),
                level_due=str(data.get("level_due", "")),
                jump_to_top=bool(data.get("jump_to_top", False)),
                last_event_hash=event.hash,
            )
            self._tickets[ticket.id] = ticket
            self._open_by_key[ticket.dedupe_key] = ticket.id
            return

        assert current is not None
        data = event.data
        if event.kind == EVENT_DUPLICATE:
            self._patch(current.id, occurrence=current.occurrence + 1, updated_at=event.at)
        elif event.kind == EVENT_ACKED:
            # Once acknowledged the owner is working it; the level timer now
            # runs against the overall resolution SLA.
            self._patch(
                current.id,
                state=State.ACTIVE,
                level_due=current.resolve_due,
                updated_at=event.at,
            )
        elif event.kind == EVENT_SUSPENDED:
            self._patch(
                current.id,
                state=State.SUSPENDED,
                suspend_due=str(data.get("suspend_due", "")),
                level_due="",
                updated_at=event.at,
            )
        elif event.kind == EVENT_RESUMED:
            self._patch(
                current.id,
                state=State.ACTIVE,
                suspend_due="",
                level_due=current.resolve_due,
                updated_at=event.at,
            )
        elif event.kind == EVENT_CLOSED:
            self._patch(
                current.id,
                state=State.CLOSED,
                suspend_due="",
                level_due="",
                closed_at=event.at,
                close_note=event.note,
                updated_at=event.at,
            )
            self._open_by_key.pop(current.dedupe_key, None)
        elif event.kind == EVENT_SUSPEND_EXPIRED:
            self._patch(
                current.id,
                state=State.OPEN,
                suspend_due="",
                owner=str(data.get("owner", current.owner)),
                shift_id=str(data.get("shift_id", current.shift_id)),
                level_due=str(data.get("level_due", "")),
                updated_at=event.at,
            )
        elif event.kind == EVENT_ESCALATED:
            self._patch(
                current.id,
                level=int(data.get("to_level", current.level)),
                owner=str(data.get("owner", current.owner)),
                level_due=str(data.get("level_due", "")),
                escalated=True,
                updated_at=event.at,
            )
        elif event.kind == EVENT_REASSIGNED:
            self._patch(
                current.id,
                owner=str(data.get("owner", current.owner)),
                shift_id=str(data.get("shift_id", current.shift_id)),
                updated_at=event.at,
            )
        self._tickets[event.ticket_id] = Ticket(
            **{**self._tickets[event.ticket_id].__dict__, "last_event_hash": event.hash}
        )

    def _patch(self, ticket_id: str, **changes: object) -> None:
        current = self._tickets[ticket_id]
        self._tickets[ticket_id] = Ticket(**{**current.__dict__, **changes})

    def _enqueue(self, ticket: Ticket, event: Event, kind: str) -> None:
        item = OutboxItem(
            id=len(self._outbox) + 1,
            ticket_id=ticket.id,
            event_seq=event.seq,
            channel=f"owner:{ticket.owner or 'unassigned'}",
            kind=kind,
            payload={
                "ticket_id": ticket.id,
                "event": event.hash,
                "state": ticket.state.value,
                "level": ticket.level,
                "owner": ticket.owner,
                "source": ticket.source,
                "severity": ticket.severity.value,
                "kind": kind,
            },
        )
        self._outbox.append(item)

    def _flush(self) -> dict[str, int]:
        delivered = 0
        failed = 0
        for item in self._outbox:
            if item.status == "done":
                continue
            item.attempts += 1
            try:
                ok = bool(self._notifier.notify(item.channel, item.payload))
            except Exception as exc:  # delivery failure must not touch state
                ok = False
                item.last_error = str(exc)
            if ok:
                item.status = "done"
                item.last_error = ""
                delivered += 1
            else:
                item.last_error = item.last_error or "notifier returned False"
                failed += 1
        return {"delivered": delivered, "pending": failed}

    def _require(self, ticket_id: str) -> Ticket:
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            raise KeyError(f"unknown ticket {ticket_id}")
        return ticket

    def _next_ticket_id(self) -> str:
        return f"T-{len(self._tickets) + 1:04d}"

    def _closed_id_for(self, dedupe_key: str) -> str:
        """If a closed ticket used this key, remember it on the reopened one."""

        for ticket in self._tickets.values():
            if ticket.dedupe_key == dedupe_key and ticket.state == State.CLOSED:
                return ticket.id
        return ""

    def _rebuild(self) -> None:
        """Replay every committed event into fresh projections."""

        self._tickets = {}
        self._open_by_key = {}
        for event in self._log.all():
            self._apply(event)
        # Recover the set of already ingested report ids from duplicate/open data.
        for event in self._log.all():
            report_id = str(event.data.get("report_id", ""))
            if report_id:
                self._report_ids.add(report_id)

    def describe(self) -> str:
        return (
            f"incidents tickets={len(self._tickets)} open={self.counts()[State.OPEN.value]} "
            f"active={self.counts()[State.ACTIVE.value]} suspended={self.counts()[State.SUSPENDED.value]} "
            f"closed={self.counts()[State.CLOSED.value]}"
        )
