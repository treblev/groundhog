import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import events
from agent import outbox
from analytics import alerts
from ingestion import schema
import groundhog_service


class EventTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "groundhog.duckdb"
        schema.init_db(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _connection(self):
        return duckdb.connect(str(self.db_path))

    def test_event_recording_is_idempotent_and_keeps_json_payload(self):
        con = self._connection()
        try:
            created = events.record_event(
                con,
                event_type="job_completed",
                source="tests",
                subject_type="agent_run",
                subject_id="run-1",
                payload={"job_name": "daily_stocks", "status": "succeeded"},
                dedupe_key="agent_run:run-1:job_completed",
            )
            duplicate = events.record_event(
                con,
                event_type="job_completed",
                source="tests",
                subject_type="agent_run",
                subject_id="run-1",
                payload={"job_name": "daily_stocks", "status": "succeeded"},
                dedupe_key="agent_run:run-1:job_completed",
            )
            row = con.execute(
                "SELECT event_type, source, subject_type, subject_id, payload::VARCHAR FROM events"
            ).fetchone()
        finally:
            con.close()

        self.assertTrue(created)
        self.assertFalse(duplicate)
        self.assertEqual(row[:4], ("job_completed", "tests", "agent_run", "run-1"))
        self.assertEqual(
            json.loads(row[4]), {"job_name": "daily_stocks", "status": "succeeded"}
        )

    def test_daily_pipeline_records_completed_event(self):
        with (
            patch.object(groundhog_service, "DB_PATH", self.db_path),
            patch.object(groundhog_service.stocks, "run"),
            patch.object(groundhog_service.signals, "run"),
            patch.object(groundhog_service.alerts, "run"),
        ):
            groundhog_service.run_daily_stocks()

        con = self._connection()
        try:
            row = con.execute(
                "SELECT event_type, source, subject_type, payload::VARCHAR FROM events"
            ).fetchone()
        finally:
            con.close()

        self.assertEqual(row[:3], ("job_completed", "groundhog_service", "agent_run"))
        self.assertEqual(json.loads(row[3])["status"], "succeeded")

    def test_daily_pipeline_records_failed_event(self):
        with (
            patch.object(groundhog_service, "DB_PATH", self.db_path),
            patch.object(groundhog_service.stocks, "run", side_effect=RuntimeError("fetch failed")),
            patch.object(groundhog_service.signals, "run"),
            patch.object(groundhog_service.alerts, "run"),
        ):
            with self.assertRaisesRegex(RuntimeError, "fetch failed"):
                groundhog_service.run_daily_stocks()

        con = self._connection()
        try:
            row = con.execute(
                "SELECT event_type, payload::VARCHAR FROM events"
            ).fetchone()
        finally:
            con.close()

        self.assertEqual(row[0], "job_failed")
        self.assertIn("RuntimeError: fetch failed", json.loads(row[1])["error_text"])

    def test_sma_signal_flip_creates_signal_and_alert_events_once(self):
        con = self._connection()
        try:
            con.execute(
                """
                INSERT INTO stock_signals (id, date, ticker, signal_type, timeframe, value, direction)
                VALUES
                    ('old', '2026-07-20', 'TEST', 'sma_cross', 'daily', 1.0, 'bearish'),
                    ('new', '2026-07-21', 'TEST', 'sma_cross', 'daily', 2.0, 'bullish')
                """
            )
            with patch.object(alerts, "_notify"):
                alerts._check_sma_cross(con, "TEST")
                alerts._check_sma_cross(con, "TEST")

            event_types = con.execute(
                "SELECT event_type FROM events ORDER BY event_type"
            ).fetchall()
            event_payloads = con.execute(
                "SELECT payload::VARCHAR FROM events WHERE event_type = 'stock_signal_flipped'"
            ).fetchall()
            outbox_rows = con.execute("SELECT status FROM outbox").fetchall()
        finally:
            con.close()

        self.assertEqual(event_types, [("stock_alert_created",), ("stock_signal_flipped",)])
        self.assertEqual(outbox_rows, [("pending",)])
        self.assertEqual(
            json.loads(event_payloads[0][0]),
            {
                "date": date(2026, 7, 21).isoformat(),
                "direction": "bullish",
                "previous_direction": "bearish",
                "signal_type": "sma_cross",
                "ticker": "TEST",
                "timeframe": "daily",
            },
        )

    def test_outbox_is_idempotent_and_tracks_delivery_status(self):
        con = self._connection()
        try:
            event_key = "stock_alert:test-alert"
            events.record_event(
                con,
                event_type="stock_alert_created",
                source="tests",
                subject_type="stock_alert",
                subject_id="test-alert",
                payload={"ticker": "TEST"},
                dedupe_key=event_key,
            )
            event_id = events.event_id_for(event_key)
            created = outbox.enqueue_event(con, event_id)
            duplicate = outbox.enqueue_event(con, event_id)
            outbox_id = con.execute(
                "SELECT id FROM outbox WHERE event_id = ?", [event_id]
            ).fetchone()[0]
            outbox.set_outbox_status(con, outbox_id, "failed", "gateway unavailable")
            failed = con.execute(
                "SELECT status, delivered_at, delivery_error FROM outbox WHERE id = ?", [outbox_id]
            ).fetchone()
            outbox.set_outbox_status(con, outbox_id, "delivered")
            delivered = con.execute(
                "SELECT status, delivered_at, delivery_error FROM outbox WHERE id = ?", [outbox_id]
            ).fetchone()
        finally:
            con.close()

        self.assertTrue(created)
        self.assertFalse(duplicate)
        self.assertEqual(failed, ("failed", None, "gateway unavailable"))
        self.assertEqual(delivered[0], "delivered")
        self.assertIsNotNone(delivered[1])
        self.assertIsNone(delivered[2])


if __name__ == "__main__":
    unittest.main()
