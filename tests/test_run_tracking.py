import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import runs
from ingestion import schema
import groundhog_service


class RunTrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "groundhog.duckdb"
        schema.init_db(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _connection(self):
        return duckdb.connect(str(self.db_path))

    def test_schema_creation_is_idempotent(self):
        schema.init_db(self.db_path)

        con = self._connection()
        try:
            tables = con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name = 'agent_runs'"
            ).fetchall()
            self.assertEqual(tables, [("agent_runs",)])
        finally:
            con.close()

    def test_finish_successful_run(self):
        con = self._connection()
        try:
            run_id = runs.start_run(con, "daily_stocks")
            runs.finish_run(con, run_id, "succeeded")
            row = con.execute(
                "SELECT job_name, status, started_at, finished_at, error_text FROM agent_runs WHERE id = ?",
                [run_id],
            ).fetchone()
        finally:
            con.close()

        self.assertEqual(row[:2], ("daily_stocks", "succeeded"))
        self.assertIsNotNone(row[2])
        self.assertIsNotNone(row[3])
        self.assertIsNone(row[4])

    def test_daily_pipeline_records_failure_and_reraises(self):
        with (
            patch.object(groundhog_service, "DB_PATH", self.db_path),
            patch.object(groundhog_service.stocks, "run"),
            patch.object(groundhog_service.signals, "run", side_effect=RuntimeError("signal failure")),
            patch.object(groundhog_service.alerts, "run"),
        ):
            with self.assertRaisesRegex(RuntimeError, "signal failure"):
                groundhog_service.run_daily_stocks()

        con = self._connection()
        try:
            row = con.execute(
                "SELECT job_name, status, finished_at, error_text FROM agent_runs"
            ).fetchone()
        finally:
            con.close()

        self.assertEqual(row[:2], ("daily_stocks", "failed"))
        self.assertIsNotNone(row[2])
        self.assertIn("RuntimeError: signal failure", row[3])

    def test_daily_pipeline_records_success(self):
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
                "SELECT job_name, status, finished_at, error_text FROM agent_runs"
            ).fetchone()
        finally:
            con.close()

        self.assertEqual(row[:2], ("daily_stocks", "succeeded"))
        self.assertIsNotNone(row[2])
        self.assertIsNone(row[3])

    def test_status_command_reports_latest_run_and_pending_outbox(self):
        con = self._connection()
        try:
            run_id = runs.start_run(con, "daily_stocks")
            runs.finish_run(con, run_id, "succeeded")
        finally:
            con.close()

        output = io.StringIO()
        with (
            patch.object(groundhog_service, "DB_PATH", self.db_path),
            contextlib.redirect_stdout(output),
        ):
            exit_code = groundhog_service.main(["status"])

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(result["latest_run"]["status"], "succeeded")
        self.assertEqual(result["pending_outbox_count"], 0)

    def test_due_tasks_runs_weekday_once_after_market_close(self):
        monday_after_close = datetime(2026, 7, 20, 17, 0, tzinfo=groundhog_service.PHOENIX)
        monday_morning = datetime(2026, 7, 20, 9, 0, tzinfo=groundhog_service.PHOENIX)
        sunday_after_close = datetime(2026, 7, 19, 17, 0, tzinfo=groundhog_service.PHOENIX)
        ran_today = datetime(2026, 7, 20, 17, 1, tzinfo=groundhog_service.PHOENIX)
        ran_yesterday = datetime(2026, 7, 19, 17, 1, tzinfo=groundhog_service.PHOENIX)

        self.assertEqual(groundhog_service.due_tasks(monday_after_close, None), ["daily-stocks"])
        self.assertEqual(groundhog_service.due_tasks(monday_after_close, ran_yesterday), ["daily-stocks"])
        self.assertEqual(groundhog_service.due_tasks(monday_after_close, ran_today), [])
        self.assertEqual(groundhog_service.due_tasks(monday_morning, None), [])
        self.assertEqual(groundhog_service.due_tasks(sunday_after_close, None), [])


if __name__ == "__main__":
    unittest.main()
