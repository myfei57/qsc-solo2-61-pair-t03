"""Alarm disposition closed loop center.

One :class:`IncidentCenter` instance holds the disposition state of every alarm
raised by the environment, fire and cable subsystems. All pages read it.
"""

from .log import BrokenHistoryError, EventLog
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
    Event,
    Severity,
    State,
    Ticket,
)
from .policy import MAX_LEVEL, EscalationPolicy, SeverityPolicy
from .report import QueueState, queue_state
from .roster import Roster, Shift
from .service import (
    IllegalTransitionError,
    IncidentCenter,
    LoggingNotifier,
    OutboxItem,
    utc_now,
)

__all__ = [
    "BrokenHistoryError",
    "EscalationPolicy",
    "Event",
    "EventLog",
    "IllegalTransitionError",
    "IncidentCenter",
    "LoggingNotifier",
    "MAX_LEVEL",
    "OutboxItem",
    "QueueState",
    "Roster",
    "Severity",
    "SeverityPolicy",
    "Shift",
    "State",
    "Ticket",
    "queue_state",
    "utc_now",
]
