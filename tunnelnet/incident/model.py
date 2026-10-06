"""Alarm disposition domain model.

A ticket is the single disposition unit shared by environment, fire and cable
alarms. Tickets are event sourced: the mutable current state is a projection of
an append-only, hash-chained event log, so the history of every disposition
action is immutable and independently verifiable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum

GENESIS = "0" * 64


class State(str, Enum):
    """Lifecycle states of an alarm ticket."""

    OPEN = "open"          # 待确认, waiting for the duty owner to acknowledge
    ACTIVE = "active"      # 处理中, owner is working it
    SUSPENDED = "suspended"  # 挂起, waiting on something, with a due time
    CLOSED = "closed"      # 已关闭, disposition finished


class Severity(str, Enum):
    """How serious an alarm is; decides entry level, SLA and escalation mode."""

    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


# Every event the state machine accepts.
EVENT_OPENED = "opened"
EVENT_ACKED = "acked"
EVENT_SUSPENDED = "suspended"
EVENT_RESUMED = "resumed"
EVENT_CLOSED = "closed"
EVENT_DUPLICATE = "duplicate"
EVENT_ESCALATED = "escalated"
EVENT_SUSPEND_EXPIRED = "suspend_expired"
EVENT_REASSIGNED = "reassigned"


# Which states each event is legal from. Anything outside this table is rejected.
TRANSITIONS: dict[str, frozenset[State]] = {
    EVENT_OPENED: frozenset(),
    EVENT_ACKED: frozenset({State.OPEN}),
    EVENT_SUSPENDED: frozenset({State.OPEN, State.ACTIVE}),
    EVENT_RESUMED: frozenset({State.SUSPENDED}),
    EVENT_CLOSED: frozenset({State.OPEN, State.ACTIVE, State.SUSPENDED}),
    EVENT_DUPLICATE: frozenset({State.OPEN, State.ACTIVE, State.SUSPENDED}),
    EVENT_ESCALATED: frozenset({State.OPEN, State.ACTIVE, State.SUSPENDED}),
    EVENT_SUSPEND_EXPIRED: frozenset({State.SUSPENDED}),
    EVENT_REASSIGNED: frozenset({State.OPEN, State.ACTIVE, State.SUSPENDED}),
}


def canonical(payload: dict[str, object]) -> bytes:
    """Deterministic byte encoding used both for hashing and replay."""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class Event:
    """One immutable disposition action.

    ``prev_hash`` chains the event to the previous one for the same ticket and
    ``hash`` covers the full event body, so any rewrite of history breaks the
    chain or the final hash.
    """

    seq: int
    ticket_id: str
    kind: str
    at: str
    actor: str = ""
    note: str = ""
    data: dict[str, object] = field(default_factory=dict)
    prev_hash: str = GENESIS
    hash: str = ""

    def body(self) -> dict[str, object]:
        """Everything the hash covers (the hash itself excluded)."""

        return {
            "seq": self.seq,
            "ticket_id": self.ticket_id,
            "kind": self.kind,
            "at": self.at,
            "actor": self.actor,
            "note": self.note,
            "data": self.data,
            "prev_hash": self.prev_hash,
        }

    def compute_hash(self) -> str:
        return hashlib.sha256(canonical(self.body())).hexdigest()

    def seal(self) -> "Event":
        """Return a copy with the hash filled in."""

        return Event(
            seq=self.seq,
            ticket_id=self.ticket_id,
            kind=self.kind,
            at=self.at,
            actor=self.actor,
            note=self.note,
            data=dict(self.data),
            prev_hash=self.prev_hash,
            hash=self.compute_hash(),
        )

    def as_dict(self) -> dict[str, object]:
        body = self.body()
        body["hash"] = self.hash
        return body


@dataclass(frozen=True)
class Ticket:
    """Current projection of one alarm's disposition state."""

    id: str
    source: str               # env | fire | cable
    dedupe_key: str           # stable identity of the underlying alarm
    severity: Severity
    state: State
    level: int                # current escalation level, 1 based
    owner: str
    shift_id: str
    occurrence: int           # how many raw reports were merged into this ticket
    opened_at: str
    updated_at: str
    suspend_due: str = ""
    ack_due: str = ""         # SLA deadline for the first acknowledgement
    resolve_due: str = ""     # SLA deadline for overall disposition
    level_due: str = ""       # deadline that triggers the next level escalation
    escalated: bool = False
    jump_to_top: bool = False
    last_event_hash: str = GENESIS
    closed_at: str = ""
    close_note: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "source": self.source,
            "dedupe_key": self.dedupe_key,
            "severity": self.severity.value,
            "state": self.state.value,
            "level": self.level,
            "owner": self.owner,
            "shift_id": self.shift_id,
            "occurrence": self.occurrence,
            "opened_at": self.opened_at,
            "updated_at": self.updated_at,
            "suspend_due": self.suspend_due,
            "ack_due": self.ack_due,
            "resolve_due": self.resolve_due,
            "level_due": self.level_due,
            "escalated": self.escalated,
            "jump_to_top": self.jump_to_top,
            "closed_at": self.closed_at,
            "close_note": self.close_note,
        }
