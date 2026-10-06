"""Behavioural tests for the alarm disposition closed loop."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tunnelnet.alarms import (
    AlarmBoard,
    AlarmRepository,
    BoardError,
    CLOSE_FALSE,
    History,
    IdempotencyStore,
    LEVEL_DIRECTOR,
    LEVEL_DUTY_MANAGER,
    LEVEL_PRIMARY,
    LEVEL_SHIFT_LEAD,
    Message,
    Outbox,
    PENDING,
    PROCESSING,
    SUSPENDED,
    CLOSED,
    Policy,
    Roster,
    Shift,
    add_seconds,
    board_report,
    digest,
    feeder,
    source_status,
)
from tunnelnet.alarms.history import GENESIS
from tunnelnet.alarms.model import EVENT_CLOSED
from tunnelnet.store import Store

T0 = "2026-10-05T10:00:00Z"


def watch(
    shift_id: str = "day",
    start: str = "2026-10-05T08:00:00Z",
    end: str = "2026-10-05T20:00:00Z",
    members: dict[int, str] | None = None,
) -> Shift:
    return Shift(
        id=shift_id,
        starts_at=start,
        ends_at=end,
        members=members or {1: "alice", 2: "bob", 3: "carol", 4: "dave"},
    )


class Clock:
    def __init__(self, stamp: str = T0) -> None:
        self.stamp = stamp

    def __call__(self) -> str:
        return self.stamp


def make_board(policy: Policy | None = None, **roster_kwargs) -> tuple[AlarmBoard, Clock]:
    clock = Clock()
    roster = Roster(default=watch(**roster_kwargs))
    return AlarmBoard(roster, policy=policy or Policy(), time=clock), clock


class RaisingCase(unittest.TestCase):
    def test_new_alarm_is_pending_and_assigned_to_primary(self) -> None:
        board, _ = make_board()
        alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
        self.assertEqual(alarm.state, PENDING)
        self.assertEqual(alarm.level, LEVEL_PRIMARY)
        self.assertEqual(alarm.assignee, "alice")
        self.assertEqual(alarm.ack_due_at, add_seconds(T0, 300))
        self.assertEqual(alarm.occurrence_count, 1)

    def test_duplicate_reports_merge_and_are_idempotent(self) -> None:
        board, clock = make_board()
        first = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
        second = feeder.raise_environment(board, "sensor-1", 91.0, at=add_seconds(T0, 30))
        third = feeder.raise_environment(board, "sensor-1", 92.0, at=add_seconds(T0, 60))
        self.assertIs(first, second)
        self.assertIs(second, third)
        self.assertEqual(first.occurrence_count, 3)
        self.assertEqual(first.last_occurrence_at, add_seconds(T0, 60))
        self.assertEqual(len(board.list()), 1)
        kinds = [event["kind"] for event in board.history(first.id)]
        self.assertEqual(kinds, ["raised", "duplicate_merged", "duplicate_merged"])

    def test_duplicate_fire_and_cable_sources_share_one_board(self) -> None:
        board, _ = make_board()
        smoke_1 = feeder.raise_fire(board, "zone-1", at=T0)
        smoke_2 = feeder.raise_fire(board, "zone-1", at=add_seconds(T0, 10))
        cable = feeder.raise_cable(board, "cable-9", 88.0, at=T0)
        self.assertIs(smoke_1, smoke_2)
        self.assertEqual(smoke_1.source, "fire")
        self.assertEqual(cable.source, "cable")
        self.assertEqual({item.source for item in board.open_alarms()}, {"fire", "cable"})

    def test_repeat_after_close_raises_fresh_alarm(self) -> None:
        board, _ = make_board()
        first = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
        board.acknowledge(first.id, "alice", at=T0)
        board.close(first.id, "alice", at=add_seconds(T0, 60))
        second = feeder.raise_environment(board, "sensor-1", 95.0, at=add_seconds(T0, 300))
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(second.state, PENDING)
        self.assertEqual(second.occurrence_count, 1)
        # The closed record stays closed and its history is untouched.
        self.assertEqual(board.get(first.id).state, CLOSED)
        self.assertEqual([event["kind"] for event in board.history(first.id)][-1], EVENT_CLOSED)

    def test_unknown_source_rejected(self) -> None:
        board, _ = make_board()
        with self.assertRaises(BoardError):
            board.raise_alarm("weather", "rain", at=T0)


class LifecycleCase(unittest.TestCase):
    def test_acknowledge_suspend_resume_close(self) -> None:
        board, _ = make_board()
        alarm = feeder.raise_cable(board, "cable-1", 80.0, at=T0)
        board.acknowledge(alarm.id, "alice", at=T0)
        self.assertEqual(board.get(alarm.id).state, PROCESSING)

        board.suspend(alarm.id, "alice", "parts ordered", seconds=600, at=add_seconds(T0, 60))
        suspended = board.get(alarm.id)
        self.assertEqual(suspended.state, SUSPENDED)
        self.assertEqual(suspended.suspend_until, add_seconds(T0, 660))
        self.assertEqual(suspended.suspend_count, 1)

        board.resume(alarm.id, "alice", at=add_seconds(T0, 120))
        self.assertEqual(board.get(alarm.id).state, PROCESSING)

        board.close(alarm.id, "alice", at=add_seconds(T0, 200))
        self.assertEqual(board.get(alarm.id).state, CLOSED)

    def test_illegal_transitions_are_rejected(self) -> None:
        board, _ = make_board()
        alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
        # Cannot suspend before acknowledging.
        with self.assertRaises(BoardError):
            board.suspend(alarm.id, "alice", "x", seconds=10, at=T0)
        board.acknowledge(alarm.id, "alice", at=T0)
        board.close(alarm.id, "alice", at=T0)
        with self.assertRaises(BoardError):
            board.acknowledge(alarm.id, "alice", at=T0)
        with self.assertRaises(BoardError):
            board.close(alarm.id, "alice", at=T0)

    def test_suspend_window_is_bounded_and_limited(self) -> None:
        policy = Policy(max_suspends=1, max_suspend_seconds=600)
        board, _ = make_board(policy)
        alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
        board.acknowledge(alarm.id, "alice", at=T0)
        with self.assertRaises(BoardError):
            board.suspend(alarm.id, "alice", "too long", seconds=601, at=T0)
        board.suspend(alarm.id, "alice", "ok", seconds=300, at=T0)
        board.resume(alarm.id, "alice", at=add_seconds(T0, 10))
        with self.assertRaises(BoardError):
            board.suspend(alarm.id, "alice", "limit", seconds=10, at=T0)

    def test_close_reason_is_validated(self) -> None:
        board, _ = make_board()
        alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
        with self.assertRaises(BoardError):
            board.close(alarm.id, "alice", reason="ignored", at=T0)
        board.close(alarm.id, "alice", reason=CLOSE_FALSE, at=T0)
        self.assertEqual(board.get(alarm.id).close_reason, CLOSE_FALSE)


class SuspensionExpiryCase(unittest.TestCase):
    def test_expiry_repops_pending_never_autocloses(self) -> None:
        """Decision 1: suspend expiry returns the alarm to the active watch."""

        board, _ = make_board()
        alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
        board.acknowledge(alarm.id, "alice", at=T0)
        board.suspend(alarm.id, "alice", "wait", seconds=600, at=T0)
        self.assertEqual(board.get(alarm.id).state, SUSPENDED)

        result = board.run_due(add_seconds(T0, 601))
        self.assertEqual(result["resumed"], [alarm.id])
        bounced = board.get(alarm.id)
        self.assertEqual(bounced.state, PENDING)
        self.assertEqual(bounced.suspend_until, "")
        # Fresh acknowledgement SLA from the expiry moment.
        self.assertEqual(bounced.ack_due_at, add_seconds(add_seconds(T0, 601), 300))
        self.assertIn("suspend_expired", [event["kind"] for event in board.history(alarm.id)])

    def test_tick_before_deadline_changes_nothing(self) -> None:
        board, _ = make_board()
        alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
        board.acknowledge(alarm.id, "alice", at=T0)
        board.suspend(alarm.id, "alice", "wait", seconds=600, at=T0)
        result = board.run_due(add_seconds(T0, 599))
        self.assertEqual(result["resumed"], [])
        self.assertEqual(board.get(alarm.id).state, SUSPENDED)


class EscalationCase(unittest.TestCase):
    def test_escalation_climbs_one_rung_at_a_time(self) -> None:
        """Decision 2: gradual escalation, rung by rung to the director."""

        board, _ = make_board()
        alarm = feeder.raise_fire(board, "zone-1", at=T0)
        self.assertEqual(board.get(alarm.id).level, LEVEL_PRIMARY)
        board.escalate(alarm.id, at=add_seconds(T0, 10))
        self.assertEqual(board.get(alarm.id).level, LEVEL_SHIFT_LEAD)
        self.assertEqual(board.get(alarm.id).assignee, "bob")
        board.escalate(alarm.id, at=add_seconds(T0, 20))
        self.assertEqual(board.get(alarm.id).level, LEVEL_DUTY_MANAGER)
        self.assertEqual(board.get(alarm.id).assignee, "carol")
        board.escalate(alarm.id, at=add_seconds(T0, 30))
        self.assertEqual(board.get(alarm.id).level, LEVEL_DIRECTOR)
        self.assertEqual(board.get(alarm.id).assignee, "dave")
        # The director is the hard cap: further escalation cannot exceed it.
        board.escalate(alarm.id, at=add_seconds(T0, 40))
        self.assertEqual(board.get(alarm.id).level, LEVEL_DIRECTOR)
        kinds = [event["kind"] for event in board.history(alarm.id)]
        self.assertEqual(kinds.count("escalated"), 4)

    def test_pending_ack_timeout_escalates_automatically(self) -> None:
        policy = Policy(ack_seconds=300, escalate_seconds=300)
        board, _ = make_board(policy)
        alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
        result = board.run_due(add_seconds(T0, 300))
        self.assertEqual(result["escalated"], [alarm.id])
        self.assertEqual(board.get(alarm.id).level, LEVEL_SHIFT_LEAD)
        # Next tick inside the quiet period is a no-op...
        self.assertEqual(board.run_due(add_seconds(T0, 400))["escalated"], [])
        # ...and the next rung arrives after the escalation quiet period.
        result = board.run_due(add_seconds(T0, 601))
        self.assertEqual(board.get(alarm.id).level, LEVEL_DUTY_MANAGER)

    def test_processing_solve_timeout_escalates(self) -> None:
        policy = Policy(solve_seconds=900)
        board, _ = make_board(policy)
        alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
        board.acknowledge(alarm.id, "alice", at=T0)
        self.assertEqual(board.run_due(add_seconds(T0, 899))["escalated"], [])
        result = board.run_due(add_seconds(T0, 900))
        self.assertEqual(result["escalated"], [alarm.id])
        self.assertEqual(board.get(alarm.id).assignee, "bob")

    def test_suspended_alarms_never_escalate_while_parked(self) -> None:
        board, _ = make_board()
        alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
        board.acknowledge(alarm.id, "alice", at=T0)
        board.suspend(alarm.id, "alice", "wait", seconds=3600, at=T0)
        result = board.run_due(add_seconds(T0, 2000))
        self.assertEqual(result["escalated"], [])
        self.assertEqual(board.get(alarm.id).level, LEVEL_PRIMARY)


class HandoverCase(unittest.TestCase):
    def test_handover_reassigns_open_alarms_to_incoming_watch(self) -> None:
        clock = Clock(T0)
        roster = Roster()
        roster.put(watch("day"))
        roster.put(
            watch(
                "night",
                start="2026-10-05T20:00:00Z",
                end="2026-10-06T08:00:00Z",
                members={1: "nora", 2: "nathan", 3: "nadia", 4: "dave"},
            )
        )
        board = AlarmBoard(roster, policy=Policy(ack_seconds=100000, solve_seconds=100000), time=clock)

        alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
        board.acknowledge(alarm.id, "alice", at=T0)
        board.escalate(alarm.id, at=T0)
        self.assertEqual(board.get(alarm.id).assignee, "bob")

        # Pending alarm at primary level also carries over to the new watch.
        other = feeder.raise_cable(board, "cable-2", 80.0, at=T0)

        result = board.handover("day", "night", at="2026-10-05T20:00:00Z")
        self.assertEqual(set(result["reassigned"]), {alarm.id, other.id})
        self.assertEqual(board.get(alarm.id).assignee, "nathan")
        self.assertEqual(board.get(other.id).assignee, "nora")
        reassigned = [event for event in board.history(alarm.id) if event["kind"] == "reassigned"]
        self.assertEqual(reassigned[-1]["detail"]["to_shift"], "night")

    def test_handover_first_expires_due_suspensions(self) -> None:
        """A suspend deadline reached before handover pops at the handover."""

        board, _ = make_board()
        alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
        board.acknowledge(alarm.id, "alice", at=T0)
        board.suspend(alarm.id, "alice", "wait", seconds=600, at=T0)
        # Incoming watch replaces alice at every rung.
        roster = board._roster  # noqa: SLF001 - direct schedule swap for the test
        roster.put(
            Shift(
                id="late",
                starts_at="2026-10-05T11:00:00Z",
                ends_at="2026-10-05T20:00:00Z",
                members={1: "ivy", 2: "bob", 3: "carol", 4: "dave"},
            )
        )
        result = board.handover("day", "late", at="2026-10-05T11:00:00Z")
        self.assertEqual(result["resumed"], [alarm.id])
        self.assertEqual(board.get(alarm.id).state, PENDING)
        self.assertEqual(board.get(alarm.id).assignee, "ivy")

    def test_handover_requires_both_shifts(self) -> None:
        board, _ = make_board()
        with self.assertRaises(BoardError):
            board.handover("day", "ghost", at=T0)


class IdempotencyCase(unittest.TestCase):
    def test_replayed_command_returns_stored_alarm_without_double_event(self) -> None:
        board, _ = make_board()
        alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
        board.acknowledge(alarm.id, "alice", at=T0, request_id="req-1")
        before = len(board.history(alarm.id))
        board.acknowledge(alarm.id, "alice", at=T0, request_id="req-1")
        self.assertEqual(len(board.history(alarm.id)), before)

    def test_request_id_cannot_be_reused_for_another_action(self) -> None:
        board, _ = make_board()
        alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
        board.acknowledge(alarm.id, "alice", at=T0, request_id="req-1")
        with self.assertRaises(BoardError):
            board.close(alarm.id, "alice", at=T0, request_id="req-1")

    def test_store_round_trip(self) -> None:
        store = IdempotencyStore()
        self.assertIsNone(store.seen("x"))
        self.assertIsNone(store.seen(""))  # empty id is never treated as seen
        store.remember(
            __import__("tunnelnet.alarms", fromlist=["IdempotencyRecord"]).IdempotencyRecord(
                request_id="x", alarm_id="a", action="close", at=T0, outcome={}
            )
        )
        self.assertEqual(store.seen("x").action, "close")
        self.assertEqual(store.count(), 1)


class OutboxCase(unittest.TestCase):
    def test_failed_delivery_is_retried_until_success(self) -> None:
        class Flaky:
            def __init__(self) -> None:
                self.calls = 0

            def send(self, message: Message) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("gateway down")

        sink = Flaky()
        outbox = Outbox(sink)
        message = Message(id="m1", alarm_id="a1", channel="duty", recipient="alice", subject="x", at=T0)
        outbox.enqueue(message)
        first = outbox.deliver_pending()
        self.assertEqual(first, {"sent": 0, "failed": 1})
        self.assertFalse(outbox.get("m1").delivered)
        self.assertEqual(outbox.get("m1").attempts, 1)
        self.assertIn("gateway down", outbox.get("m1").last_error)
        second = outbox.deliver_pending()
        self.assertEqual(second, {"sent": 1, "failed": 0})
        self.assertTrue(outbox.get("m1").delivered)
        # Delivered messages are not sent again.
        self.assertEqual(outbox.deliver_pending(), {"sent": 0, "failed": 0})

    def test_escalation_enqueues_notification_to_new_assignee(self) -> None:
        board, _ = make_board()
        alarm = feeder.raise_fire(board, "zone-1", at=T0)
        board.escalate(alarm.id, at=add_seconds(T0, 10))
        pending = board.outbox().pending()
        recipients = {message.recipient for message in pending}
        self.assertIn("alice", recipients)
        self.assertIn("bob", recipients)


class PersistenceCase(unittest.TestCase):
    def test_board_rebuilds_state_and_history_from_store(self) -> None:
        class Recording:
            def send(self, message: Message) -> None:
                return None

        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(tmp)
            roster = Roster(default=watch())
            outbox = Outbox(Recording())
            board = AlarmBoard(roster, repository=AlarmRepository(store), outbox=outbox)
            alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
            feeder.raise_environment(board, "sensor-1", 91.0, at=add_seconds(T0, 10))
            board.acknowledge(alarm.id, "alice", at=T0)
            board.suspend(alarm.id, "alice", "wait", seconds=600, at=T0)
            board.flush_notifications()

            rebuilt = AlarmBoard(Roster(default=watch()), repository=AlarmRepository(store))
            recovered = rebuilt.get(alarm.id)
            self.assertEqual(recovered.state, SUSPENDED)
            self.assertEqual(recovered.occurrence_count, 2)
            self.assertEqual(recovered.suspend_until, add_seconds(T0, 600))
            self.assertTrue(rebuilt.verify_history(alarm.id))
            self.assertEqual(rebuilt.counts()["suspended"], 1)
            # Delivered notifications do not return as pending after restart.
            self.assertEqual(rebuilt.outbox().pending(), [])

    def test_undelivered_messages_are_replayed_after_restart(self) -> None:
        class Failing:
            def send(self, message: Message) -> None:
                raise RuntimeError("down")

        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(tmp)
            board = AlarmBoard(
                Roster(default=watch()),
                repository=AlarmRepository(store),
                outbox=Outbox(Failing()),
            )
            alarm = feeder.raise_fire(board, "zone-1", at=T0)
            board.flush_notifications()
            self.assertEqual(len(board.outbox().pending()), 1)

            class Succeeding:
                def __init__(self) -> None:
                    self.received: list[str] = []

                def send(self, message: Message) -> None:
                    self.received.append(message.alarm_id)

            sink = Succeeding()
            rebuilt_board = AlarmBoard(
                Roster(default=watch()),
                repository=AlarmRepository(store),
                outbox=Outbox(),
            )
            rebuilt_board.outbox().bind_sink(sink)
            rebuilt_board.flush_notifications()
            self.assertEqual(sink.received, [alarm.id])
            self.assertEqual(rebuilt_board.outbox().pending(), [])

    def test_delivered_messages_stay_delivered_after_restart(self) -> None:
        class Recording:
            def __init__(self) -> None:
                self.received: list[str] = []

            def send(self, message: Message) -> None:
                self.received.append(message.id)

        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(tmp)
            outbox = Outbox()
            board = AlarmBoard(
                Roster(default=watch()), repository=AlarmRepository(store), outbox=outbox
            )
            alarm = feeder.raise_fire(board, "zone-1", at=T0)
            sink = Recording()
            outbox.bind_sink(sink)
            board.flush_notifications()
            self.assertEqual(len(sink.received), 1)

            rebuilt = AlarmBoard(
                Roster(default=watch()), repository=AlarmRepository(store), outbox=Outbox(Recording())
            )
            self.assertEqual(rebuilt.outbox().pending(), [])
            self.assertEqual(rebuilt.flush_notifications(), {"sent": 0, "failed": 0})

    def test_open_dedupe_index_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(tmp)
            board = AlarmBoard(Roster(default=watch()), repository=AlarmRepository(store))
            alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
            rebuilt = AlarmBoard(Roster(default=watch()), repository=AlarmRepository(store))
            merged = feeder.raise_environment(rebuilt, "sensor-1", 92.0, at=add_seconds(T0, 5))
            self.assertEqual(merged.id, alarm.id)
            self.assertEqual(merged.occurrence_count, 2)


class HistoryIntegrityCase(unittest.TestCase):
    def test_history_is_append_only_and_chain_verified(self) -> None:
        board, _ = make_board()
        alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
        board.acknowledge(alarm.id, "alice", at=T0)
        board.close(alarm.id, "alice", at=T0)
        events = board.history(alarm.id)
        self.assertEqual([event["seq"] for event in events], [1, 2, 3])
        self.assertTrue(board.verify_history(alarm.id))
        # First event chains from genesis; every later one chains from prior.
        history = board._histories[alarm.id]  # noqa: SLF001
        raw = history.events()
        self.assertEqual(raw[0].chain, digest(raw[0], GENESIS))
        self.assertEqual(raw[1].chain, digest(raw[1], raw[0].chain))

    def test_edited_history_breaks_the_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(tmp)
            board = AlarmBoard(Roster(default=watch()), repository=AlarmRepository(store))
            alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
            board.acknowledge(alarm.id, "alice", at=T0)
            path = os.path.join(tmp, "alarm-journal", f"{alarm.id}.json")
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
            doc["events"][0]["detail"]["subject"] = "tampered"
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(doc, handle)
            rebuilt = AlarmBoard(Roster(default=watch()), repository=AlarmRepository(store))
            self.assertFalse(rebuilt.verify_history(alarm.id))

    def test_truncated_history_is_detected(self) -> None:
        board, _ = make_board()
        alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
        board.acknowledge(alarm.id, "alice", at=T0)
        rows = board.history(alarm.id)
        # A dropped tail is internally consistent on its own...
        prefix = History.from_dicts(alarm.id, rows[:1])
        self.assertTrue(prefix.verify())
        # ...but it no longer matches the chain anchor stored on the alarm.
        self.assertNotEqual(prefix.tail_chain(), board.get(alarm.id).history_chain)


class ProjectionCase(unittest.TestCase):
    def test_report_and_source_status_share_one_board(self) -> None:
        board, _ = make_board()
        env_alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
        fire_alarm = feeder.raise_fire(board, "zone-2", at=T0)
        board.acknowledge(fire_alarm.id, "alice", at=T0)
        report = board_report(board).as_dict()
        self.assertEqual(report["counts"]["pending"], 1)
        self.assertEqual(report["counts"]["processing"], 1)
        self.assertEqual(report["by_source"]["environment"]["open"], 1)
        self.assertEqual(report["by_source"]["fire"]["open"], 1)
        env_status = source_status(board, "environment").as_dict()
        self.assertEqual(env_status["pending"], 1)
        self.assertEqual(env_status["processing"], 0)
        # Same alarm id appears in the source projection and the board list.
        self.assertIn(env_alarm.id, [row["id"] for row in report["open_alarms"]])

    def test_filters(self) -> None:
        board, _ = make_board()
        env_alarm = feeder.raise_environment(board, "sensor-1", 90.0, at=T0)
        feeder.raise_fire(board, "zone-1", at=T0)
        self.assertEqual([a.id for a in board.list(source="environment")], [env_alarm.id])
        self.assertEqual(len(board.list(include_closed=False)), 2)
        board.close(env_alarm.id, "alice", at=T0)
        self.assertEqual(len(board.list(include_closed=False)), 1)
        self.assertEqual(len(board.list(state=CLOSED)), 1)


if __name__ == "__main__":
    unittest.main()
