"""Tests for the alarm disposition closed loop."""

from __future__ import annotations

import unittest

from tunnelnet.incident import (
    EscalationPolicy,
    EventLog,
    IllegalTransitionError,
    IncidentCenter,
    LoggingNotifier,
    Roster,
    Severity,
    Shift,
    State,
)


class Clock:
    """Deterministic ISO clock advanced manually in minutes."""

    def __init__(self, start: str = "2026-10-05T08:00:00+00:00") -> None:
        from datetime import datetime, timedelta

        self._dt = datetime.fromisoformat(start)
        self._timedelta = timedelta

    def now(self) -> str:
        return self._dt.isoformat()

    def advance(self, minutes: int) -> str:
        self._dt += self._timedelta(minutes=minutes)
        return self.now()


def build_center(clock: Clock) -> IncidentCenter:
    roster = Roster()
    roster.add(Shift("day", "2026-10-05T06:00:00+00:00", "2026-10-05T18:00:00+00:00", "alice", 1))
    roster.add(Shift("day-l2", "2026-10-05T06:00:00+00:00", "2026-10-05T18:00:00+00:00", "bob", 2))
    roster.add(Shift("day-l3", "2026-10-05T06:00:00+00:00", "2026-10-05T18:00:00+00:00", "carol", 3))
    roster.add(Shift("night", "2026-10-05T18:00:00+00:00", "2026-10-06T06:00:00+00:00", "dave", 1))
    roster.add(Shift("night-l2", "2026-10-05T18:00:00+00:00", "2026-10-06T06:00:00+00:00", "erin", 2))
    roster.add(Shift("night-l3", "2026-10-05T18:00:00+00:00", "2026-10-06T06:00:00+00:00", "frank", 3))
    return IncidentCenter(roster, clock=clock.now)


class DispositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.center = build_center(self.clock)

    def test_new_alarm_is_assigned_to_on_call_owner_open(self) -> None:
        ticket, created = self.center.report("env", "env:sensor-7:hot", Severity.MAJOR)
        self.assertTrue(created)
        self.assertEqual(ticket.state, State.OPEN)
        self.assertEqual(ticket.owner, "alice")
        self.assertEqual(ticket.shift_id, "day")
        self.assertEqual(ticket.level, 1)
        self.assertTrue(ticket.ack_due > ticket.opened_at)

    def test_full_lifecycle_ack_suspend_resume_close(self) -> None:
        ticket, _ = self.center.report("fire", "fire:zone-2:smoke", Severity.MAJOR)
        ticket = self.center.acknowledge(ticket.id, "alice", note="on my way")
        self.assertEqual(ticket.state, State.ACTIVE)

        ticket = self.center.suspend(ticket.id, "alice", minutes=20, reason="waiting on site")
        self.assertEqual(ticket.state, State.SUSPENDED)
        self.assertTrue(ticket.suspend_due)

        # Expiry does NOT auto close; it pops back to OPEN and reassigned to the
        # operator on call at expiry time (here still the day shift).
        self.clock.advance(25)
        result = self.center.run_due()
        self.assertEqual(result["expired"], [ticket.id])
        reopened = self.center.get(ticket.id)
        self.assertEqual(reopened.state, State.OPEN)
        self.assertEqual(reopened.suspend_due, "")

        self.center.acknowledge(ticket.id, "alice")
        closed = self.center.close(ticket.id, "alice", note="cooled and vented")
        self.assertEqual(closed.state, State.CLOSED)
        self.assertEqual(closed.close_note, "cooled and vented")
        self.assertTrue(closed.closed_at)

    def test_suspend_expiry_during_handover_goes_to_new_shift(self) -> None:
        ticket, _ = self.center.report("cable", "cable:cab-3:hot", Severity.MINOR)
        self.center.acknowledge(ticket.id, "alice")
        # Suspend near end of the day shift with a due time inside night shift.
        self.clock._dt = self.clock._dt.replace(hour=17, minute=50)
        self.center.suspend(ticket.id, "alice", minutes=30, reason="maintenance window")
        self.clock._dt = self.clock._dt.replace(hour=18, minute=30)
        self.center.run_due()
        reopened = self.center.get(ticket.id)
        self.assertEqual(reopened.state, State.OPEN)
        self.assertEqual(reopened.owner, "dave")
        self.assertEqual(reopened.shift_id, "night")

    def test_stepwise_escalation(self) -> None:
        ticket, _ = self.center.report("env", "env:s-1:hot", Severity.MAJOR)
        # MAJOR ack SLA is 15 minutes.
        self.clock.advance(16)
        result = self.center.run_due()
        self.assertEqual(result["escalated"], [ticket.id])
        lvl2 = self.center.get(ticket.id)
        self.assertEqual(lvl2.level, 2)
        self.assertEqual(lvl2.owner, "bob")
        self.assertTrue(lvl2.escalated)

        # One more timeout steps to L3, not beyond.
        self.clock.advance(16)
        self.center.run_due()
        lvl3 = self.center.get(ticket.id)
        self.assertEqual(lvl3.level, 3)
        self.assertEqual(lvl3.owner, "carol")
        self.clock.advance(16)
        again = self.center.run_due()
        self.assertEqual(again["escalated"], [])
        self.assertEqual(self.center.get(ticket.id).level, 3)

    def test_critical_jumps_straight_to_top(self) -> None:
        ticket, _ = self.center.report("fire", "fire:z-1:smoke", Severity.CRITICAL)
        # CRITICAL ack SLA is 5 minutes and jump_to_top is on.
        self.clock.advance(6)
        result = self.center.run_due()
        self.assertEqual(result["escalated"], [ticket.id])
        jumped = self.center.get(ticket.id)
        self.assertEqual(jumped.level, 3)
        self.assertTrue(jumped.jump_to_top)

    def test_duplicate_reports_merge_and_report_id_is_idempotent(self) -> None:
        first, created = self.center.report("env", "env:s-9:hot", Severity.MAJOR, report_id="r-1")
        self.assertTrue(created)
        merged, created2 = self.center.report("env", "env:s-9:hot", Severity.MAJOR, report_id="r-2")
        self.assertFalse(created2)
        self.assertEqual(merged.id, first.id)
        self.assertEqual(merged.occurrence, 2)

        # Exact replay of the first report returns the same ticket, no new event.
        replay, created3 = self.center.report("env", "env:s-9:hot", Severity.MAJOR, report_id="r-1")
        self.assertFalse(created3)
        self.assertEqual(replay.id, first.id)
        self.assertEqual(replay.occurrence, 2)
        kinds = [event.kind for event in self.center.history(first.id)]
        self.assertEqual(kinds.count("duplicate"), 1)

    def test_same_alarm_after_close_reopens_new_ticket(self) -> None:
        first, _ = self.center.report("cable", "cable:x:hot", Severity.MINOR)
        self.center.close(first.id, "alice", note="fixed")
        second, created = self.center.report("cable", "cable:x:hot", Severity.MINOR)
        self.assertTrue(created)
        self.assertNotEqual(second.id, first.id)
        opened = [event for event in self.center.history(second.id) if event.kind == "opened"][0]
        self.assertEqual(opened.data["reopened_from"], first.id)

    def test_handover_moves_only_unfinished_tickets(self) -> None:
        keep, _ = self.center.report("env", "env:a:hot", Severity.MINOR)
        done, _ = self.center.report("env", "env:b:hot", Severity.MINOR)
        self.center.close(done.id, "alice", note="done")
        moved = self.center.handover("day", "night")
        self.assertEqual(moved, [keep.id])
        self.assertEqual(self.center.get(keep.id).owner, "dave")
        self.assertEqual(self.center.get(keep.id).shift_id, "night")
        self.assertEqual(self.center.get(done.id).owner, "alice")

    def test_illegal_transition_rejected(self) -> None:
        ticket, _ = self.center.report("env", "env:s:hot", Severity.MAJOR)
        with self.assertRaises(IllegalTransitionError):
            self.center.resume(ticket.id, "alice")  # never suspended
        self.center.acknowledge(ticket.id, "alice")
        self.center.suspend(ticket.id, "alice", minutes=10)
        with self.assertRaises(IllegalTransitionError):
            self.center.acknowledge(ticket.id, "alice")  # suspended

    def test_notification_failure_recovers_without_losing_state(self) -> None:
        class Flaky:
            def __init__(self) -> None:
                self.fail = True
                self.seen: set[str] = set()

            def notify(self, channel: str, payload: dict[str, object]) -> bool:
                if self.fail:
                    return False
                # Idempotent key is the event hash; never deliver twice.
                if payload["event"] in self.seen:
                    raise AssertionError("duplicate delivery")
                self.seen.add(payload["event"])
                return True

        flaky = Flaky()
        center = IncidentCenter(self.center._roster, notifier=flaky, clock=self.clock.now)
        ticket, _ = center.report("env", "env:flaky:hot", Severity.MAJOR)
        # The state committed even though the notification failed.
        pending = center.outbox()
        self.assertEqual(len(pending), 1)
        self.assertEqual(center.get(ticket.id).state, State.OPEN)

        flaky.fail = False
        counters = center.flush_outbox()
        self.assertEqual(counters["delivered"], 1)
        self.assertEqual(counters["pending"], 0)
        self.assertEqual(center.outbox(), [])
        self.assertEqual(len(flaky.seen), 1)

    def test_history_is_append_only_and_verifies(self) -> None:
        ticket, _ = self.center.report("env", "env:hist:hot", Severity.MAJOR)
        self.center.acknowledge(ticket.id, "alice")
        self.center.close(ticket.id, "alice", note="ok")
        events = self.center.history(ticket.id)
        self.assertEqual([event.kind for event in events], ["opened", "acked", "closed"])
        self.assertTrue(self.center.verify_history())

    def test_tampered_history_is_detected(self) -> None:
        ticket, _ = self.center.report("env", "env:tamper:hot", Severity.MAJOR)
        self.center.close(ticket.id, "alice", note="ok")
        from tunnelnet.incident import Event

        events = self.center._log.events(ticket.id)
        original = events[1]
        forged = Event(
            seq=original.seq,
            ticket_id=ticket.id,
            kind="closed",
            at=original.at,
            actor="mallory",
            note="rewritten",
            data={},
            prev_hash=original.prev_hash,
            hash=original.hash,  # kept the stored hash; the rewritten body no longer matches
        )
        self.center._log._by_ticket[ticket.id][1] = forged
        with self.assertRaises(Exception):
            self.center.verify_history()

    def test_replay_rebuilds_identical_projection(self) -> None:
        a, _ = self.center.report("env", "env:x:hot", Severity.MAJOR)
        self.clock.advance(16)
        self.center.run_due()
        self.center.acknowledge(a.id, "bob")
        self.center.suspend(a.id, "bob", minutes=10)
        self.clock.advance(11)
        self.center.run_due()
        b, _ = self.center.report("fire", "fire:z:smoke", Severity.CRITICAL, report_id="r-9")
        self.center.close(b.id, "alice")

        rebuilt = IncidentCenter(self.center._roster, log=self.center._log, clock=self.clock.now)
        for ticket_id, original in self.center._tickets.items():
            self.assertEqual(rebuilt.get(ticket_id).as_dict(), original.as_dict())
        self.assertEqual(rebuilt.counts(), self.center.counts())

    def test_single_state_read_across_sources(self) -> None:
        self.center.report("env", "env:1:hot", Severity.MAJOR)
        self.center.report("fire", "fire:2:smoke", Severity.MAJOR)
        self.center.report("cable", "cable:3:hot", Severity.MAJOR)
        self.assertEqual(self.center.list(source="env")[0].source, "env")
        self.assertEqual(self.center.counts()["open"], 3)
        queue = self.center.queue_for("alice")
        self.assertEqual({item.source for item in queue}, {"env", "fire", "cable"})


if __name__ == "__main__":
    unittest.main()
