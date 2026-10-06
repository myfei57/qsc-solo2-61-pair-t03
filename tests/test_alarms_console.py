"""End to end HTTP tests for the alarm disposition closed loop."""

from __future__ import annotations

import json
import tempfile
import unittest

from tunnelnet.console.server import Server
from tunnelnet.store import Store

T0 = "2026-10-05T10:00:00Z"
T5 = "2026-10-05T10:05:00Z"
T6 = "2026-10-05T10:06:00Z"


class AlarmConsoleCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/data")
        self.server = Server(self.store, web_dir="web")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None, query: dict | None = None):
        response = self.server.dispatch(method, path, payload, query)
        body = response.body.decode("utf-8")
        parsed = json.loads(body) if response.content_type.startswith("application/json") else body
        return response.status, parsed

    def test_board_starts_empty_and_appears_in_snapshot(self) -> None:
        status, report = self.call("GET", "/api/alarms/board")
        self.assertEqual(status, 200)
        self.assertEqual(report["counts"]["total"], 0)
        status, snapshot = self.call("GET", "/api/snapshot")
        self.assertIn("alarms", snapshot)

    def test_full_lifecycle_over_http(self) -> None:
        _, raised = self.call(
            "POST",
            "/api/alarms/raise",
            {"source": "environment", "subject": "sensor-7 hot", "dedupe_key": "env:s7", "at": T0},
        )
        alarm_id = raised["alarm"]["id"]
        self.assertEqual(raised["alarm"]["state"], "pending")
        self.assertEqual(raised["alarm"]["assignee"], "operator")
        self.assertFalse(raised["merged"])

        # Repeated report with the same key merges instead of raising again.
        _, duplicate = self.call(
            "POST",
            "/api/alarms/raise",
            {"source": "environment", "subject": "sensor-7 hot", "dedupe_key": "env:s7", "at": T0},
        )
        self.assertEqual(duplicate["alarm"]["id"], alarm_id)
        self.assertTrue(duplicate["merged"])
        self.assertEqual(duplicate["alarm"]["occurrence_count"], 2)

        _, acked = self.call(
            "POST", "/api/alarms/acknowledge", {"id": alarm_id, "actor": "operator", "at": T0}
        )
        self.assertEqual(acked["alarm"]["state"], "processing")

        _, suspended = self.call(
            "POST",
            "/api/alarms/suspend",
            {"id": alarm_id, "actor": "operator", "reason": "parts", "until": "2026-10-05T10:30:00Z", "at": T0},
        )
        self.assertEqual(suspended["alarm"]["state"], "suspended")

        _, resumed = self.call(
            "POST", "/api/alarms/resume", {"id": alarm_id, "actor": "operator", "at": "2026-10-05T10:10:00Z"}
        )
        self.assertEqual(resumed["alarm"]["state"], "processing")

        _, closed = self.call(
            "POST",
            "/api/alarms/close",
            {"id": alarm_id, "actor": "operator", "reason": "resolved", "at": "2026-10-05T10:20:00Z"},
        )
        self.assertEqual(closed["alarm"]["state"], "closed")

        _, detail = self.call("GET", "/api/alarms/detail", query={"id": [alarm_id]})
        kinds = [event["kind"] for event in detail["history"]]
        self.assertEqual(
            kinds,
            ["raised", "duplicate_merged", "acknowledged", "suspended", "resumed", "closed"],
        )
        self.assertTrue(detail["history_intact"])

    def test_invalid_transitions_return_400(self) -> None:
        _, raised = self.call(
            "POST", "/api/alarms/raise", {"source": "fire", "subject": "x", "at": T0}
        )
        alarm_id = raised["alarm"]["id"]
        status, body = self.call(
            "POST", "/api/alarms/suspend", {"id": alarm_id, "reason": "x", "until": "2027-01-01T00:00:00Z"}
        )
        self.assertEqual(status, 400)
        self.assertIn("pending", body["error"])
        status, body = self.call("POST", "/api/alarms/acknowledge", {"id": "ghost"})
        self.assertEqual(status, 400)
        self.assertIn("not found", body["error"])

    def test_idempotent_close_replay(self) -> None:
        _, raised = self.call(
            "POST", "/api/alarms/raise", {"source": "cable", "subject": "x", "at": T0}
        )
        alarm_id = raised["alarm"]["id"]
        payload = {"id": alarm_id, "actor": "operator", "request_id": "close-1", "at": T0}
        status, first = self.call("POST", "/api/alarms/close", payload)
        self.assertEqual(status, 200)
        status, replay = self.call("POST", "/api/alarms/close", payload)
        self.assertEqual(status, 200)
        self.assertEqual(replay["alarm"]["state"], "closed")
        _, detail = self.call("GET", "/api/alarms/detail", query={"id": [alarm_id]})
        self.assertEqual([event["kind"] for event in detail["history"]].count("closed"), 1)

    def test_suspend_expiry_and_escalation_via_tick(self) -> None:
        _, raised = self.call(
            "POST",
            "/api/alarms/raise",
            {"source": "fire", "subject": "smoke zone-9", "at": T0},
        )
        alarm_id = raised["alarm"]["id"]
        # Pending at T0; five minutes later the acknowledgement SLA fires one
        # gradual escalation rung.
        _, ticked = self.call("POST", "/api/alarms/tick", {"now": T5})
        self.assertEqual(ticked["escalated"], [alarm_id])
        _, detail = self.call("GET", "/api/alarms/detail", query={"id": [alarm_id]})
        self.assertEqual(detail["alarm"]["level"], 2)
        self.assertEqual(detail["alarm"]["assignee"], "shift-lead")

        # Quiet period: another minute without a new window does nothing.
        _, quiet = self.call("POST", "/api/alarms/tick", {"now": T6})
        self.assertEqual(quiet["escalated"], [])

        # Suspension expiry pops the alarm back to pending rather than closing.
        self.call("POST", "/api/alarms/acknowledge", {"id": alarm_id, "actor": "shift-lead", "at": T6})
        self.call(
            "POST",
            "/api/alarms/suspend",
            {"id": alarm_id, "actor": "shift-lead", "until": "2026-10-05T10:30:00Z", "at": T6},
        )
        _, expired = self.call("POST", "/api/alarms/tick", {"now": "2026-10-05T10:31:00Z"})
        self.assertEqual(expired["resumed"], [alarm_id])
        _, detail = self.call("GET", "/api/alarms/detail", query={"id": [alarm_id]})
        self.assertEqual(detail["alarm"]["state"], "pending")
        self.assertIn("suspend_expired", [event["kind"] for event in detail["history"]])

    def test_roster_and_handover_reassigns_open_alarms(self) -> None:
        self.call(
            "POST",
            "/api/alarms/roster/shift",
            {
                "id": "day",
                "starts_at": "2026-10-05T08:00:00Z",
                "ends_at": "2026-10-05T20:00:00Z",
                "members": {"primary": "alice", "shift_lead": "bob", "duty_manager": "carol", "director": "dave"},
            },
        )
        self.call(
            "POST",
            "/api/alarms/roster/shift",
            {
                "id": "night",
                "starts_at": "2026-10-05T20:00:00Z",
                "ends_at": "2026-10-06T08:00:00Z",
                "members": {"primary": "nora", "shift_lead": "nathan", "duty_manager": "nadia", "director": "dave"},
            },
        )
        status, roster = self.call("GET", "/api/alarms/roster")
        self.assertEqual(status, 200)
        self.assertEqual(roster["count"], 2)
        status, active = self.call("GET", "/api/alarms/roster/active", query={"at": ["2026-10-05T21:00:00Z"]})
        self.assertEqual(active["shift"]["id"], "night")

        _, raised = self.call(
            "POST",
            "/api/alarms/raise",
            {"source": "environment", "subject": "sensor-1 hot", "at": "2026-10-05T19:59:00Z"},
        )
        alarm_id = raised["alarm"]["id"]
        self.assertEqual(raised["alarm"]["assignee"], "alice")

        _, handed = self.call(
            "POST",
            "/api/alarms/handover",
            {"from_shift": "day", "to_shift": "night", "at": "2026-10-05T20:00:00Z"},
        )
        self.assertIn(alarm_id, handed["reassigned"])
        _, detail = self.call("GET", "/api/alarms/detail", query={"id": [alarm_id]})
        self.assertEqual(detail["alarm"]["assignee"], "nora")

    def test_filters_and_source_status(self) -> None:
        for source, subject in (("environment", "e"), ("fire", "f"), ("cable", "c")):
            self.call(
                "POST",
                "/api/alarms/raise",
                {"source": source, "subject": subject, "dedupe_key": f"{source}:1", "at": T0},
            )
        status, only_env = self.call("GET", "/api/alarms", query={"source": ["environment"]})
        self.assertEqual(status, 200)
        self.assertEqual(only_env["count"], 1)
        status, open_only = self.call("GET", "/api/alarms", query={"closed": ["0"]})
        self.assertEqual(open_only["count"], 3)

        status, env_status = self.call("GET", "/api/alarms/by-source", query={"source": ["environment"]})
        self.assertEqual(env_status["pending"], 1)
        status, bad = self.call("GET", "/api/alarms/by-source", query={"source": ["bogus"]})
        self.assertEqual(status, 400)

    def test_source_feeds_share_the_board(self) -> None:
        # The environment filter is a shared moving window; feed three hot
        # samples so the filtered value clears the alarm threshold.
        for at in ("2026-10-05T10:00:00Z", "2026-10-05T10:00:01Z", "2026-10-05T10:00:02Z"):
            _, sample = self.call("POST", "/api/env/sample", {"sensor_id": "sensor-3", "value": 90.0, "at": at})
        self.assertTrue(sample["alarmed"])
        # ...and a smoke report...
        self.call("POST", "/api/fire/smoke", {"zone": "zone-1"})
        # ...and an over-temperature cable all land on the same board.
        self.call("POST", "/api/cables/temp", {"cable_id": "cable-1", "temp": 90.0})
        _, board = self.call("GET", "/api/alarms/board")
        self.assertEqual(board["counts"]["pending"], 3)
        sources = {alarm["source"] for alarm in board["open_alarms"]}
        self.assertEqual(sources, {"environment", "fire", "cable"})
        # Environment and fire pages read the same disposition projection.
        _, env_page = self.call("GET", "/api/env")
        self.assertEqual(env_page["disposition"]["pending"], 1)
        _, fire_page = self.call("GET", "/api/fire/report")
        self.assertEqual(fire_page["disposition"]["pending"], 1)
        _, cable_page = self.call("GET", "/api/cables/health")
        self.assertEqual(cable_page["disposition"]["pending"], 1)

    def test_history_verify_endpoint(self) -> None:
        _, raised = self.call(
            "POST", "/api/alarms/raise", {"source": "fire", "subject": "x", "at": T0}
        )
        status, verify = self.call("GET", "/api/alarms/verify")
        self.assertEqual(status, 200)
        self.assertTrue(verify["all_intact"])
        self.assertIn(raised["alarm"]["id"], verify["intact"])

    def test_history_csv_export(self) -> None:
        _, raised = self.call(
            "POST", "/api/alarms/raise", {"source": "fire", "subject": "x", "at": T0}
        )
        alarm_id = raised["alarm"]["id"]
        response = self.server.dispatch(
            "GET", "/api/export/alarms/history", None, {"id": [alarm_id]}
        )
        text = response.body.decode("utf-8")
        self.assertTrue(text.startswith("seq,kind,at,actor,detail,chain"))
        self.assertIn("raised", text)

    def test_outbox_enqueues_and_flushes(self) -> None:
        self.call(
            "POST", "/api/alarms/raise", {"source": "fire", "subject": "x", "at": T0}
        )
        _, outbox = self.call("GET", "/api/alarms/outbox")
        self.assertGreaterEqual(outbox["pending"], 1)
        # No sink is bound in the console runtime: flush reports no failures
        # but messages stay pending until a real notifier is attached.
        _, flush = self.call("POST", "/api/alarms/flush")
        self.assertEqual(flush["failed"], 0)

    def test_audit_summary_reads_same_board(self) -> None:
        self.call(
            "POST", "/api/alarms/raise", {"source": "fire", "subject": "x", "at": T0}
        )
        status, audit = self.call("GET", "/api/audit/summary")
        self.assertEqual(status, 200)
        self.assertEqual(audit["alarm_dispositions"]["pending"], 1)
        self.assertTrue(audit["alarm_history_intact"])

    def test_roster_shifts_survive_restart(self) -> None:
        self.call(
            "POST",
            "/api/alarms/roster/shift",
            {
                "id": "night",
                "starts_at": "2026-10-05T20:00:00Z",
                "ends_at": "2026-10-06T08:00:00Z",
                "members": {"primary": "nora", "shift_lead": "nathan", "duty_manager": "nadia", "director": "dave"},
            },
        )
        restarted = Server(self.store, web_dir="web")
        response = restarted.dispatch(
            "GET", "/api/alarms/roster/active", None, {"at": ["2026-10-05T21:00:00Z"]}
        )
        active = json.loads(response.body.decode("utf-8"))
        self.assertEqual(active["shift"]["id"], "night")
        self.assertEqual(active["shift"]["members"]["primary"], "nora")

    def test_board_state_survives_server_restart(self) -> None:
        _, raised = self.call(
            "POST",
            "/api/alarms/raise",
            {"source": "environment", "subject": "sensor-1 hot", "dedupe_key": "env:1", "at": T0},
        )
        alarm_id = raised["alarm"]["id"]
        self.call("POST", "/api/alarms/acknowledge", {"id": alarm_id, "actor": "operator", "at": T0})
        self.call(
            "POST",
            "/api/alarms/suspend",
            {"id": alarm_id, "actor": "operator", "until": "2026-10-05T10:30:00Z", "at": T0},
        )
        # A new Server on the same store rehydrates the disposition board.
        restarted = Server(self.store, web_dir="web")
        response = restarted.dispatch(
            "GET", "/api/alarms/detail", None, {"id": [alarm_id]}
        )
        detail = json.loads(response.body.decode("utf-8"))
        self.assertEqual(detail["alarm"]["state"], "suspended")
        self.assertTrue(detail["history_intact"])
        self.assertEqual(detail["history"][0]["kind"], "raised")


if __name__ == "__main__":
    unittest.main()
