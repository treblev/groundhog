import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import runs
from ingestion import schema
from scripts import daily_stocks


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
            patch.object(daily_stocks, "DB_PATH", self.db_path),
            patch.object(daily_stocks.stocks, "run"),
            patch.object(daily_stocks.signals, "run", side_effect=RuntimeError("signal failure")),
            patch.object(daily_stocks.alerts, "run"),
        ):
            with self.assertRaisesRegex(RuntimeError, "signal failure"):
                daily_stocks.run()

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
            patch.object(daily_stocks, "DB_PATH", self.db_path),
            patch.object(daily_stocks.stocks, "run"),
            patch.object(daily_stocks.signals, "run"),
            patch.object(daily_stocks.alerts, "run"),
        ):
            daily_stocks.run()

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


if __name__ == "__main__":
    unittest.main()
