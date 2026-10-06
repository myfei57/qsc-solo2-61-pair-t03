"""The alarm disposition board: single source of truth for every alarm.

Environment over-temperature, smoke and cable over-temperature alarms all
flow into one board.  The board owns assignment against the duty roster, the
four-state lifecycle, suspend windows, gradual escalation, duplicate merging,
idempotent disposition commands, recovery of partially failed notifications
and an immutable hash-chained history.

Two policy decisions called out in the requirements are fixed here:

* Suspend expiry **re-pops the alarm as pending** (it never auto-closes).  A
  suspend means "cannot be acted on until X"; closing silently at a handover
  would hide an unresolved hazard, so the deadline bounces it back onto the
  active watch with a fresh acknowledgement SLA.
* Escalation is **one rung at a time**, level by level, until the director
  rung; repeated overdue ticks cannot skip rungs but never stop climbing.
"""

from __future__ import annotations

import itertools
import threading
import uuid
from collections.abc import Callable

from . import clock
from .history import History
from .idempotency import IdempotencyRecord, IdempotencyStore
from .model import (
    CLOSED,
    EVENT_ACKNOWLEDGED,
    EVENT_CLOSED,
    EVENT_DUPLICATE,
    EVENT_ESCALATED,
    EVENT_RAISED,
    EVENT_REASSIGNED,
    EVENT_REOPENED,
    EVENT_RESUMED,
    EVENT_SUSPEND_EXPIRED,
    EVENT_SUSPENDED,
    HIGHEST_LEVEL,
    LEVELS,
    PENDING,
    PROCESSING,
    SOURCES,
    SUSPENDED,
    Alarm,
)
from .outbox import Message, Outbox
from .persistence import AlarmRepository
from .policy import DEFAULT_POLICY, Policy
from .roster import Roster

CLOSE_RESOLVED = "resolved"
CLOSE_FALSE = "false_alarm"
CLOSE_DUPLICATE = "duplicate"
CLOSE_REASONS = (CLOSE_RESOLVED, CLOSE_FALSE, CLOSE_DUPLICATE)


class BoardError(ValueError):
    """A disposition request that is invalid for the current lifecycle."""


class AlarmBoard:
    """Thread safe, optionally durable registry of alarm dispositions."""

    def __init__(
        self,
        roster: Roster | None = None,
        policy: Policy = DEFAULT_POLICY,
        repository: AlarmRepository | None = None,
        outbox: Outbox | None = None,
        time: Callable[[], str] = clock.utc_now,
    ) -> None:
        self._roster = roster or Roster()
        self._policy = policy
        self._repo = repository
        self._outbox = outbox or Outbox()
        self._clock = time
        self._idempotency = IdempotencyStore()
        self._alarms: dict[str, Alarm] = {}
        self._histories: dict[str, History] = {}
        self._open_by_key: dict[str, str] = {}
        self._counter = itertools.count(1)
        self._lock = threading.RLock()
        self._recover()

    # ------------------------------------------------------------------ load

    def _recover(self) -> None:
        """Rebuild the projection from durable state when a repository exists."""

        if self._repo is None:
            return
        for alarm_id in self._repo.list_state_ids():
            alarm = self._repo.load_state(alarm_id)
            if alarm is None:
                continue
            self._alarms[alarm_id] = alarm
            self._histories[alarm_id] = self._repo.load_history(alarm_id)
            if alarm.is_open():
                self._open_by_key[alarm.dedupe_key] = alarm_id
        for message in self._repo.load_messages():
            self._outbox.enqueue(message)
        for key, alarm_id in self._repo.load_dedupe_index().items():
            self._open_by_key.setdefault(key, alarm_id)
        self._counter = itertools.count(max(self._numbered_alarms(), default=0) + 1)

    def _numbered_alarms(self) -> list[int]:
        numbers: list[int] = []
        for alarm_id in self._alarms:
            if alarm_id.startswith("alarm-") and alarm_id[6:].isdigit():
                numbers.append(int(alarm_id[6:]))
        return numbers

    # --------------------------------------------------------------- raising

    def raise_alarm(
        self,
        source: str,
        subject: str,
        dedupe_key: str = "",
        severity: int | None = None,
        at: str = "",
        detail: dict | None = None,
    ) -> Alarm:
        """Raise or merge one upstream alarm report.

        Repeats carrying the same ``dedupe_key`` while an alarm is open are
        merged into that alarm: its occurrence counter and latest occurrence
        move, no new row appears, and the response is identical for the
        original and every duplicate report (idempotent ingestion).
        """

        if source not in SOURCES:
            raise BoardError(f"unknown alarm source {source!r}")
        stamp = at or self._clock()
        key = dedupe_key or f"{source}:{subject}"
        with self._lock:
            existing_id = self._open_by_key.get(key)
            if existing_id is not None:
                return self._merge(existing_id, stamp, detail or {})
            alarm_id = f"alarm-{next(self._counter):04d}"
            level = severity or self._policy.entry_level
            if level not in LEVELS:
                raise BoardError(f"severity must be one of {LEVELS}")
            assignee = self._roster.assignee(stamp, level)
            alarm = Alarm(
                id=alarm_id,
                source=source,
                dedupe_key=key,
                subject=subject,
                severity=level,
                level=level,
                assignee=assignee,
                raised_at=stamp,
                updated_at=stamp,
                last_occurrence_at=stamp,
                ack_due_at=clock.add_seconds(stamp, self._policy.ack_seconds),
                solve_due_at=clock.add_seconds(stamp, self._policy.solve_seconds),
            )
            history = self._history(alarm_id)
            history.append(
                EVENT_RAISED,
                stamp,
                actor="system",
                detail={"subject": subject, "severity": level, "assignee": assignee, **(detail or {})},
            )
            self._store_alarm(alarm, history)
            self._open_by_key[key] = alarm_id
            self._notify(alarm, stamp, "new alarm pending acknowledgement")
            return alarm

    def _merge(self, alarm_id: str, stamp: str, detail: dict) -> Alarm:
        alarm = self._alarms[alarm_id]
        history = self._history(alarm_id)
        alarm.occurrence_count += 1
        alarm.last_occurrence_at = stamp
        alarm.updated_at = stamp
        history.append(
            EVENT_DUPLICATE,
            stamp,
            actor="system",
            detail={"occurrence": alarm.occurrence_count, **detail},
        )
        self._store_alarm(alarm, history)
        return alarm

    # ------------------------------------------------------------ disposition

    def acknowledge(self, alarm_id: str, actor: str, at: str = "", request_id: str = "") -> Alarm:
        """Pending -> processing; the assigned operator takes ownership."""

        return self._command(
            alarm_id,
            EVENT_ACKNOWLEDGED,
            "acknowledge",
            actor,
            request_id,
            at,
            self._do_acknowledge,
        )

    def _do_acknowledge(self, alarm: Alarm, stamp: str, actor: str) -> None:
        if alarm.state != PENDING:
            raise BoardError(f"cannot acknowledge a {alarm.state} alarm")
        alarm.state = PROCESSING
        alarm.acked_at = stamp
        alarm.updated_at = stamp

    def suspend(
        self,
        alarm_id: str,
        actor: str,
        reason: str,
        until: str = "",
        seconds: int | None = None,
        at: str = "",
        request_id: str = "",
    ) -> Alarm:
        """Processing -> suspended with an explicit, bounded resume deadline."""

        stamp = at or self._clock()
        span = seconds if seconds is not None else self._policy.suspend_seconds
        deadline = until or clock.add_seconds(stamp, span)

        def apply(alarm: Alarm, moment: str, who: str) -> None:
            if alarm.state != PROCESSING:
                raise BoardError(f"cannot suspend a {alarm.state} alarm")
            if alarm.suspend_count >= self._policy.max_suspends:
                raise BoardError(f"suspend limit {self._policy.max_suspends} reached")
            if seconds is not None and seconds > self._policy.max_suspend_seconds:
                raise BoardError(
                    f"suspend window exceeds {self._policy.max_suspend_seconds}s"
                )
            if deadline <= moment:
                raise BoardError("suspend deadline must be in the future")
            alarm.state = SUSPENDED
            alarm.suspend_until = deadline
            alarm.suspend_count += 1
            alarm.updated_at = moment

        return self._command(
            alarm_id,
            EVENT_SUSPENDED,
            "suspend",
            actor,
            request_id,
            stamp,
            apply,
            detail={"reason": reason, "until": deadline},
        )

    def resume(self, alarm_id: str, actor: str, at: str = "", request_id: str = "") -> Alarm:
        """Suspended -> processing; the operator resumes work early."""

        return self._command(
            alarm_id,
            EVENT_RESUMED,
            "resume",
            actor,
            request_id,
            at,
            self._do_resume,
        )

    def _do_resume(self, alarm: Alarm, stamp: str, actor: str) -> None:
        if alarm.state != SUSPENDED:
            raise BoardError(f"cannot resume a {alarm.state} alarm")
        alarm.state = PROCESSING
        alarm.suspend_until = ""
        alarm.updated_at = stamp

    def close(
        self,
        alarm_id: str,
        actor: str,
        reason: str = CLOSE_RESOLVED,
        note: str = "",
        at: str = "",
        request_id: str = "",
    ) -> Alarm:
        """Any open state -> closed, terminally and with a recorded reason."""

        if reason not in CLOSE_REASONS:
            raise BoardError(f"close reason must be one of {CLOSE_REASONS}")

        def apply(alarm: Alarm, stamp: str, who: str) -> None:
            if not alarm.is_open():
                raise BoardError("alarm is already closed")
            alarm.state = CLOSED
            alarm.closed_at = stamp
            alarm.close_reason = reason
            alarm.suspend_until = ""
            alarm.updated_at = stamp

        return self._command(
            alarm_id,
            EVENT_CLOSED,
            "close",
            actor,
            request_id,
            at,
            apply,
            detail={"reason": reason, "note": note},
        )

    def reopen(self, alarm_id: str, actor: str, at: str = "", request_id: str = "") -> Alarm:
        """Closed -> pending; used when the condition returns after closure."""

        stamp = at or self._clock()
        with self._lock:
            original = self._require(alarm_id)
            if original.is_open():
                raise BoardError("only a closed alarm can be reopened")
            # Reopening starts a fresh alarm linked to the closed one so the
            # closed history stays immutable while the new episode is tracked.
            reopened = self.raise_alarm(
                original.source,
                original.subject,
                dedupe_key=original.dedupe_key,
                severity=original.severity,
                at=stamp,
            )
            reopened.reopened_of = original.id
            history = self._history(reopened.id)
            history.append(EVENT_REOPENED, stamp, actor=actor, detail={"reopened_of": original.id})
            self._store_alarm(reopened, history)
            return self._idempotent_outcome(
                reopened, "reopen", actor, request_id, stamp
            )

    def reassign(self, alarm_id: str, actor: str, assignee: str, at: str = "",
                 request_id: str = "") -> Alarm:
        """Hand the alarm to a specific person (manual override or handover)."""

        def apply(alarm: Alarm, stamp: str, who: str) -> None:
            if not alarm.is_open():
                raise BoardError("cannot reassign a closed alarm")
            alarm.assignee = assignee
            alarm.updated_at = stamp

        return self._command(
            alarm_id,
            EVENT_REASSIGNED,
            "reassign",
            actor,
            request_id,
            at,
            apply,
            detail={"assignee": assignee},
        )

    # ------------------------------------------------------------- escalation

    def escalate(self, alarm_id: str, actor: str = "system", at: str = "",
                 reason: str = "manual", request_id: str = "") -> Alarm:
        """Climb exactly one escalation rung, reassigning to that on-duty rung.

        Gradual escalation: each call moves ``level`` by one until the
        director rung, where further overdue ticks are recorded but cannot
        raise the level past the hard cap.
        """

        def apply(alarm: Alarm, stamp: str, who: str) -> None:
            if not alarm.is_open():
                raise BoardError("cannot escalate a closed alarm")
            if alarm.level >= HIGHEST_LEVEL:
                alarm.updated_at = stamp
                return
            alarm.level += 1
            alarm.assignee = self._roster.assignee(stamp, alarm.level)
            alarm.escalated_at = stamp
            alarm.updated_at = stamp

        return self._command(
            alarm_id,
            EVENT_ESCALATED,
            "escalate",
            actor,
            request_id,
            at,
            apply,
            detail={"reason": reason},
            notify=True,
        )

    def run_due(self, now: str = "") -> dict[str, object]:
        """Apply every time-driven transition due at ``now``.

        Suspended alarms whose deadline passed pop back to pending (never
        auto-close) with a fresh acknowledgement SLA; pending alarms past
        their ack deadline and processing alarms past their solve deadline
        climb one escalation rung.  One call performs all due transitions so
        the console tick, a cron job or an explicit operator flush all use
        the same code path.
        """

        stamp = now or self._clock()
        resumed: list[str] = []
        escalated: list[str] = []
        with self._lock:
            for alarm in list(self._alarms.values()):
                if not alarm.is_open():
                    continue
                if alarm.state == SUSPENDED and alarm.suspend_until and stamp >= alarm.suspend_until:
                    self._expire_suspension(alarm, stamp)
                    resumed.append(alarm.id)
                if alarm.state == PENDING and stamp >= alarm.ack_due_at and self._due_again(alarm, stamp):
                    self.escalate(alarm.id, at=stamp, reason="ack_timeout")
                    alarm = self._alarms[alarm.id]
                    alarm.ack_due_at = clock.add_seconds(stamp, self._policy.escalate_seconds)
                    self._store_alarm(alarm, self._history(alarm.id))
                    escalated.append(alarm.id)
                elif alarm.state == PROCESSING and stamp >= alarm.solve_due_at and self._due_again(
                    alarm, stamp
                ):
                    self.escalate(alarm.id, at=stamp, reason="solve_timeout")
                    alarm = self._alarms[alarm.id]
                    alarm.solve_due_at = clock.add_seconds(stamp, self._policy.escalate_seconds)
                    self._store_alarm(alarm, self._history(alarm.id))
                    escalated.append(alarm.id)
        return {"at": stamp, "resumed": resumed, "escalated": sorted(set(escalated))}

    def _due_again(self, alarm: Alarm, stamp: str) -> bool:
        """Escalate on a quiet period so one overdue alarm bumps once per window."""

        if not alarm.escalated_at:
            return True
        return stamp >= clock.add_seconds(alarm.escalated_at, self._policy.escalate_seconds)

    def _expire_suspension(self, alarm: Alarm, stamp: str) -> None:
        """Suspend deadline reached: bounce back onto the active watch as pending."""

        history = self._history(alarm.id)
        alarm.state = PENDING
        alarm.suspend_until = ""
        alarm.assignee = self._roster.assignee(stamp, alarm.level)
        alarm.ack_due_at = clock.add_seconds(stamp, self._policy.ack_seconds)
        alarm.updated_at = stamp
        history.append(
            EVENT_SUSPEND_EXPIRED,
            stamp,
            actor="system",
            detail={"assignee": alarm.assignee},
        )
        self._store_alarm(alarm, history)
        self._notify(alarm, stamp, "suspension expired, alarm returned to pending")

    def handover(self, outgoing_shift: str, incoming_shift: str, actor: str = "system",
                 at: str = "") -> dict[str, object]:
        """Shift handover: reassign every open alarm of the outgoing watch.

        Expired suspends pop first (a deadline reached during the previous
        watch must not silently carry over), then all open alarms move to the
        incoming watch at their current level.  This is why suspension expiry
        re-pops rather than closes: at a handover the new watch visibly owns
        the unresolved alarm instead of inheriting an auto-closed ghost.
        """

        stamp = at or self._clock()
        outgoing = self._roster.resolve(outgoing_shift)
        incoming = self._roster.resolve(incoming_shift)
        if outgoing is None or incoming is None:
            raise BoardError("both shifts of a handover must be scheduled")
        due = self.run_due(stamp)
        moved: list[str] = []
        with self._lock:
            for alarm in self._alarms.values():
                if not alarm.is_open():
                    continue
                target = incoming.member_at(alarm.level)
                if alarm.assignee == target:
                    continue
                history = self._history(alarm.id)
                alarm.assignee = target
                alarm.updated_at = stamp
                history.append(
                    EVENT_REASSIGNED,
                    stamp,
                    actor=actor,
                    detail={"from_shift": outgoing_shift, "to_shift": incoming_shift, "assignee": target},
                )
                self._store_alarm(alarm, history)
                moved.append(alarm.id)
        return {"at": stamp, "reassigned": sorted(moved), **due}

    # -------------------------------------------------------------- internals

    def _command(
        self,
        alarm_id: str,
        event_kind: str,
        action: str,
        actor: str,
        request_id: str,
        at: str,
        apply: Callable[[Alarm, str, str], None],
        detail: dict | None = None,
        notify: bool = False,
    ) -> Alarm:
        stamp = at or self._clock()
        with self._lock:
            seen = self._idempotency.seen(request_id)
            if seen is not None:
                if seen.action != action or seen.alarm_id != alarm_id:
                    raise BoardError("request id reused for a different command")
                return self._require(seen.alarm_id)
            alarm = self._require(alarm_id)
            apply(alarm, stamp, actor)
            history = self._history(alarm_id)
            history.append(event_kind, stamp, actor=actor, detail=detail or {})
            self._store_alarm(alarm, history)
            if notify:
                self._notify(alarm, stamp, f"escalated to level {alarm.level}")
            if request_id:
                self._idempotency.remember(
                    IdempotencyRecord(
                        request_id=request_id,
                        alarm_id=alarm_id,
                        action=action,
                        at=stamp,
                        outcome={"state": alarm.state, "level": alarm.level},
                    )
                )
            return alarm

    def _idempotent_outcome(self, alarm: Alarm, action: str, actor: str,
                            request_id: str, stamp: str) -> Alarm:
        if request_id:
            self._idempotency.remember(
                IdempotencyRecord(
                    request_id=request_id,
                    alarm_id=alarm.id,
                    action=action,
                    at=stamp,
                    outcome={"state": alarm.state, "level": alarm.level},
                )
            )
        return alarm

    def _notify(self, alarm: Alarm, stamp: str, subject: str) -> None:
        message = Message(
            id=uuid.uuid4().hex,
            alarm_id=alarm.id,
            channel="duty",
            recipient=alarm.assignee,
            subject=subject,
            at=stamp,
            payload={"alarm": alarm.as_dict()},
        )
        self._outbox.enqueue(message)
        if self._repo is not None:
            self._repo.save_message(message)

    def _history(self, alarm_id: str) -> History:
        history = self._histories.get(alarm_id)
        if history is None:
            history = self._repo.load_history(alarm_id) if self._repo is not None else History(alarm_id)
            self._histories[alarm_id] = history
        return history

    def _store_alarm(self, alarm: Alarm, history: History) -> None:
        alarm.history_chain = history.tail_chain()
        self._alarms[alarm.id] = alarm
        if not alarm.is_open():
            self._open_by_key.pop(alarm.dedupe_key, None)
        if self._repo is None:
            return
        self._repo.append_event(alarm.id, history, history.events()[-1])
        self._repo.save_state(alarm)
        self._repo.save_dedupe_index(dict(self._open_by_key))

    def _require(self, alarm_id: str) -> Alarm:
        alarm = self._alarms.get(alarm_id)
        if alarm is None:
            raise BoardError(f"alarm {alarm_id!r} not found")
        return alarm

    # ---------------------------------------------------------------- queries

    def get(self, alarm_id: str) -> Alarm | None:
        with self._lock:
            return self._alarms.get(alarm_id)

    def list(
        self,
        state: str = "",
        source: str = "",
        assignee: str = "",
        include_closed: bool = True,
    ) -> list[Alarm]:
        with self._lock:
            alarms = list(self._alarms.values())
        if state:
            alarms = [alarm for alarm in alarms if alarm.state == state]
        if source:
            alarms = [alarm for alarm in alarms if alarm.source == source]
        if assignee:
            alarms = [alarm for alarm in alarms if alarm.assignee == assignee]
        if not include_closed:
            alarms = [alarm for alarm in alarms if alarm.is_open()]
        return sorted(alarms, key=lambda alarm: (alarm.raised_at, alarm.id))

    def open_alarms(self) -> list[Alarm]:
        return self.list(include_closed=False)

    def history(self, alarm_id: str) -> list[dict[str, object]]:
        with self._lock:
            self._require(alarm_id)
            return self._history(alarm_id).as_dicts()

    def verify_history(self, alarm_id: str) -> bool:
        with self._lock:
            alarm = self._require(alarm_id)
            history = self._history(alarm_id)
            if not history.verify():
                return False
            # The projection remembers the last journal link; a journal that
            # was truncated or replaced with a shorter valid prefix fails.
            if history.events() and history.tail_chain() != alarm.history_chain:
                return False
            return True

    def verify_all(self) -> dict[str, bool]:
        with self._lock:
            return {alarm_id: history.verify() for alarm_id, history in self._histories.items()}

    def counts(self) -> dict[str, int]:
        with self._lock:
            result = {state: 0 for state in (PENDING, PROCESSING, SUSPENDED, CLOSED)}
            for alarm in self._alarms.values():
                result[alarm.state] += 1
            result["total"] = len(self._alarms)
            result["open"] = sum(1 for alarm in self._alarms.values() if alarm.is_open())
            return result

    def by_source(self) -> dict[str, dict[str, int]]:
        with self._lock:
            report: dict[str, dict[str, int]] = {}
            for alarm in self._alarms.values():
                row = report.setdefault(alarm.source, {"total": 0, "open": 0})
                row["total"] += 1
                if alarm.is_open():
                    row["open"] += 1
            return report

    def repository(self) -> AlarmRepository | None:
        return self._repo

    def repository_shifts(self) -> list[dict[str, object]]:
        return [] if self._repo is None else self._repo.load_shifts()

    def outbox(self) -> Outbox:
        return self._outbox

    def flush_notifications(self) -> dict[str, int]:
        """Deliver every pending notification and persist delivery marks."""

        tally = self._outbox.deliver_pending()
        if self._repo is not None:
            for message in self._outbox.all():
                self._repo.save_message(message)
        return tally

    def idempotency(self) -> IdempotencyStore:
        return self._idempotency

    def describe(self) -> str:
        with self._lock:
            return f"alarmboard alarms={len(self._alarms)} open={len(self._open_by_key)}"
