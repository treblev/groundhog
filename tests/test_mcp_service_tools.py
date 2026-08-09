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
        self.con.execute("INSERT INTO activities (id, date, activity_type, distance_miles, duration_seconds, avg_hr) VALUES ('activity-1', '2026-07-24', 'running', 2.5, 1800, 140)")
        self.con.execute("INSERT INTO sleep_metrics (date, resting_hr, hrv, breath_rate, deep_sleep_minutes) VALUES ('2026-07-24', 52, 60, 14.2, 90)")
        self.con.execute("INSERT INTO workouts (id, date, day_of_week, name, category, structure_type, description) VALUES ('workout-1', '2026-07-24', 'THU', 'Intervals', 'running', 'intervals', 'Run intervals')")
        self.con.execute("INSERT INTO stock_watchlist (date, ticker, closing_price) VALUES ('2026-07-23', 'BTC-USD', 100000), ('2026-07-24', 'BTC-USD', 101000)")
        self.con.execute("INSERT INTO stock_signals (id, date, ticker, signal_type, timeframe, value, direction) VALUES ('signal-1', '2026-07-24', 'BTC-USD', 'supertrend', 'daily', 99000, 'bullish')")

    def tearDown(self):
        self.con.close()
        self.temp_dir.cleanup()

    def _dispatch(self, name: str, args: dict) -> str:
        return server._dispatch(name, args, self.con)

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

    def test_personal_data_summary_tools_return_expected_data(self):
        activities = json.loads(self._dispatch("get_activity_summary", {}))
        sleep = json.loads(self._dispatch("get_sleep_summary", {}))
        workout = json.loads(self._dispatch("get_workout_for_date", {"date": "2026-07-24"}))
        freshness = json.loads(self._dispatch("get_data_freshness", {}))

        self.assertEqual(activities[0]["activity_count"], 1)
        self.assertEqual(activities[0]["total_distance_miles"], 2.5)
        self.assertEqual(sleep[0]["average_hrv"], 60.0)
        self.assertEqual(workout[0]["name"], "Intervals")
        self.assertEqual({row["source"] for row in freshness}, {"activities", "health_metrics", "sleep_metrics", "stock_prices", "stock_signals", "workouts"})

    def test_market_summary_includes_bitcoin(self):
        summary = json.loads(self._dispatch("get_market_summary", {}))

        self.assertEqual(summary["bitcoin"]["price"], 101000.0)
        self.assertEqual(summary["bitcoin"]["change_percent"], 1.0)
        self.assertEqual(summary["bitcoin_supertrend"][0]["direction"], "bullish")

    def test_semantic_search_tool_returns_json_results(self):
        matches = [{"source_id": "workout-1", "name": "Intervals", "score": 0.91}]
        with patch.object(server, "search_documents", return_value=matches) as search:
            result = json.loads(
                self._dispatch(
                    "search_documents",
                    {"query": "running intervals", "top_k": 3},
                )
            )

        self.assertEqual(result, matches)
        search.assert_called_once()
        self.assertEqual(search.call_args.args[0], "running intervals")
        self.assertEqual(search.call_args.kwargs["domain"], "workout")
        self.assertEqual(search.call_args.kwargs["top_k"], 3)


if __name__ == "__main__":
    unittest.main()
