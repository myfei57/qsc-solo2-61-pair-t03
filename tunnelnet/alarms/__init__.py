"""Alarm disposition closed loop: assignment, lifecycle, escalation, history."""

from . import feeder
from .board import (
    CLOSE_DUPLICATE,
    CLOSE_FALSE,
    CLOSE_REASONS,
    CLOSE_RESOLVED,
    AlarmBoard,
    BoardError,
)
from .clock import add_seconds, utc_now
from .history import GENESIS, History, digest
from .idempotency import IdempotencyRecord, IdempotencyStore
from .model import (
    CLOSED,
    HIGHEST_LEVEL,
    LEVELS,
    LEVEL_DIRECTOR,
    LEVEL_DUTY_MANAGER,
    LEVEL_NAMES,
    LEVEL_PRIMARY,
    LEVEL_SHIFT_LEAD,
    PENDING,
    PROCESSING,
    SOURCES,
    SOURCE_CABLE,
    SOURCE_ENV,
    SOURCE_FIRE,
    SUSPENDED,
    Alarm,
    Event,
)
from .outbox import Message, Outbox, Sink
from .persistence import AlarmRepository
from .policy import DEFAULT_POLICY, Policy
from .report import BoardReport, SourceStatus, board_report, source_status
from .roster import Roster, Shift
from .view import AlarmView

__all__ = [
    "Alarm",
    "AlarmBoard",
    "AlarmRepository",
    "AlarmView",
    "BoardError",
    "BoardReport",
    "CLOSED",
    "CLOSE_DUPLICATE",
    "CLOSE_FALSE",
    "CLOSE_REASONS",
    "CLOSE_RESOLVED",
    "DEFAULT_POLICY",
    "Event",
    "GENESIS",
    "HIGHEST_LEVEL",
    "History",
    "IdempotencyRecord",
    "IdempotencyStore",
    "LEVELS",
    "LEVEL_DIRECTOR",
    "LEVEL_DUTY_MANAGER",
    "LEVEL_NAMES",
    "LEVEL_PRIMARY",
    "LEVEL_SHIFT_LEAD",
    "Message",
    "Outbox",
    "PENDING",
    "Policy",
    "PROCESSING",
    "Roster",
    "SOURCES",
    "SOURCE_CABLE",
    "SOURCE_ENV",
    "SOURCE_FIRE",
    "Shift",
    "Sink",
    "SourceStatus",
    "SUSPENDED",
    "add_seconds",
    "board_report",
    "digest",
    "feeder",
    "source_status",
    "utc_now",
]
