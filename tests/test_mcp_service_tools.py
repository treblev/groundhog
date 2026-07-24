import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import events, outbox, runs
from ingestion import schema
from mcp_server import server


class McpServiceToolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "groundhog.duckdb"
        schema.init_db(self.db_path)
        self.con = duckdb.connect(str(self.db_path))

        run_id = runs.start_run(self.con, "daily_stocks")
        runs.finish_run(self.con, run_id, "succeeded")
        alert_id = "alert-1"
        self.con.execute(
            """
            INSERT INTO stock_alerts (id, date, ticker, alert_type, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            [alert_id, date(2026, 7, 23), "TEST", "golden_cross", "TEST crossed up"],
        )
        event_key = f"stock_alert:{alert_id}"
        events.record_event(
            self.con,
            event_type="stock_alert_created",
            source="tests",
            subject_type="stock_alert",
            subject_id=alert_id,
            payload={"ticker": "TEST", "alert_type": "golden_cross"},
            dedupe_key=event_key,
        )
        self.event_id = events.event_id_for(event_key)
        outbox.enqueue_event(self.con, self.event_id)
        self.outbox_id = self.con.execute(
            "SELECT id FROM outbox WHERE event_id = ?", [self.event_id]
        ).fetchone()[0]

    def tearDown(self):
        self.con.close()
        self.temp_dir.cleanup()

    def _dispatch(self, name: str, args: dict) -> str:
        with patch.object(server, "con", self.con):
            return server._dispatch(name, args)

    def test_read_service_tools_return_expected_data(self):
        recent_events = json.loads(self._dispatch("get_recent_events", {}))
        pending_outbox = json.loads(self._dispatch("get_pending_outbox", {}))
        latest_run = json.loads(self._dispatch("get_agent_run_status", {}))
        latest_alerts = json.loads(self._dispatch("get_latest_alerts", {}))

        self.assertEqual(recent_events[0]["event_type"], "stock_alert_created")
        self.assertEqual(pending_outbox[0]["id"], self.outbox_id)
        self.assertEqual(latest_run[0]["status"], "succeeded")
        self.assertEqual(latest_alerts[0]["ticker"], "TEST")

    def test_mark_outbox_delivered_is_idempotent(self):
        first = json.loads(
            self._dispatch("mark_outbox_delivered", {"outbox_id": self.outbox_id})
        )
        delivered_at = first[0]["delivered_at"]
        second = json.loads(
            self._dispatch("mark_outbox_delivered", {"outbox_id": self.outbox_id})
        )

        self.assertEqual(first[0]["status"], "delivered")
        self.assertIsNotNone(delivered_at)
        self.assertEqual(second[0]["delivered_at"], delivered_at)

    def test_mark_outbox_delivered_reports_missing_item(self):
        result = json.loads(
            self._dispatch("mark_outbox_delivered", {"outbox_id": "missing"})
        )
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
