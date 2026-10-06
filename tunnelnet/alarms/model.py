"""Domain values for the alarm disposition board.

An alarm lives in exactly one of four states:

``pending``   raised and assigned to the on-duty operator, not yet seen
``processing`` the assigned operator has acknowledged and is handling it
``suspended`` parked with an explicit resume deadline
``closed``    terminally resolved with a reason

Severity, and therefore escalation, travels on a separate axis so an alarm
can escalate without losing its disposition state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Disposition lifecycle, in the order a healthy alarm normally travels.
PENDING = "pending"
PROCESSING = "processing"
SUSPENDED = "suspended"
CLOSED = "closed"

STATES = (PENDING, PROCESSING, SUSPENDED, CLOSED)
OPEN_STATES = (PENDING, PROCESSING, SUSPENDED)

#: Escalation levels, low to high.
LEVEL_PRIMARY = 1
LEVEL_SHIFT_LEAD = 2
LEVEL_DUTY_MANAGER = 3
LEVEL_DIRECTOR = 4

LEVELS = (LEVEL_PRIMARY, LEVEL_SHIFT_LEAD, LEVEL_DUTY_MANAGER, LEVEL_DIRECTOR)
LEVEL_NAMES = {
    LEVEL_PRIMARY: "primary",
    LEVEL_SHIFT_LEAD: "shift_lead",
    LEVEL_DUTY_MANAGER: "duty_manager",
    LEVEL_DIRECTOR: "director",
}
HIGHEST_LEVEL = LEVEL_DIRECTOR

#: Alarm sources that feed the shared board.
SOURCE_ENV = "environment"
SOURCE_FIRE = "fire"
SOURCE_CABLE = "cable"
SOURCES = (SOURCE_ENV, SOURCE_FIRE, SOURCE_CABLE)

#: Lifecycle event kinds written into the immutable history.
EVENT_RAISED = "raised"
EVENT_DUPLICATE = "duplicate_merged"
EVENT_ACKNOWLEDGED = "acknowledged"
EVENT_SUSPENDED = "suspended"
EVENT_RESUMED = "resumed"
EVENT_REASSIGNED = "reassigned"
EVENT_ESCALATED = "escalated"
EVENT_SUSPEND_EXPIRED = "suspend_expired"
EVENT_CLOSED = "closed"
EVENT_REOPENED = "reopened"


@dataclass(frozen=True)
class Event:
    """One immutable fact in an alarm's disposition history.

    ``seq`` is the per-alarm, gap-free position of the event, starting at 1.
    ``chain`` links every event to its predecessor so history cannot be
    rewritten or truncated without detection.
    """

    seq: int
    kind: str
    at: str
    actor: str = ""
    detail: dict[str, object] = field(default_factory=dict)
    chain: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "at": self.at,
            "actor": self.actor,
            "detail": dict(self.detail),
            "chain": self.chain,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Event":
        return cls(
            seq=int(payload.get("seq", 0)),
            kind=str(payload.get("kind", "")),
            at=str(payload.get("at", "")),
            actor=str(payload.get("actor", "")),
            detail=dict(payload.get("detail", {})),  # type: ignore[arg-type]
            chain=str(payload.get("chain", "")),
        )


@dataclass
class Alarm:
    """The mutable projection of one alarm and its disposition."""

    id: str
    source: str
    dedupe_key: str
    subject: str
    severity: int = LEVEL_PRIMARY
    state: str = PENDING
    assignee: str = ""
    level: int = LEVEL_PRIMARY
    raised_at: str = ""
    updated_at: str = ""
    acked_at: str = ""
    closed_at: str = ""
    close_reason: str = ""
    suspend_until: str = ""
    suspend_count: int = 0
    occurrence_count: int = 1
    last_occurrence_at: str = ""
    ack_due_at: str = ""
    solve_due_at: str = ""
    escalated_at: str = ""
    reopened_of: str = ""
    history_chain: str = ""

    def is_open(self) -> bool:
        return self.state != CLOSED

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "source": self.source,
            "dedupe_key": self.dedupe_key,
            "subject": self.subject,
            "severity": self.severity,
            "state": self.state,
            "assignee": self.assignee,
            "level": self.level,
            "level_name": LEVEL_NAMES[self.level],
            "raised_at": self.raised_at,
            "updated_at": self.updated_at,
            "acked_at": self.acked_at,
            "closed_at": self.closed_at,
            "close_reason": self.close_reason,
            "suspend_until": self.suspend_until,
            "suspend_count": self.suspend_count,
            "occurrence_count": self.occurrence_count,
            "last_occurrence_at": self.last_occurrence_at,
            "ack_due_at": self.ack_due_at,
            "solve_due_at": self.solve_due_at,
            "escalated_at": self.escalated_at,
            "reopened_of": self.reopened_of,
            "history_chain": self.history_chain,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "Alarm":
        return cls(
            id=str(payload.get("id", "")),
            source=str(payload.get("source", "")),
            dedupe_key=str(payload.get("dedupe_key", "")),
            subject=str(payload.get("subject", "")),
            severity=int(payload.get("severity", LEVEL_PRIMARY)),
            state=str(payload.get("state", PENDING)),
            assignee=str(payload.get("assignee", "")),
            level=int(payload.get("level", LEVEL_PRIMARY)),
            raised_at=str(payload.get("raised_at", "")),
            updated_at=str(payload.get("updated_at", "")),
            acked_at=str(payload.get("acked_at", "")),
            closed_at=str(payload.get("closed_at", "")),
            close_reason=str(payload.get("close_reason", "")),
            suspend_until=str(payload.get("suspend_until", "")),
            suspend_count=int(payload.get("suspend_count", 0)),
            occurrence_count=int(payload.get("occurrence_count", 1)),
            last_occurrence_at=str(payload.get("last_occurrence_at", "")),
            ack_due_at=str(payload.get("ack_due_at", "")),
            solve_due_at=str(payload.get("solve_due_at", "")),
            escalated_at=str(payload.get("escalated_at", "")),
            reopened_of=str(payload.get("reopened_of", "")),
            history_chain=str(payload.get("history_chain", "")),
        )
