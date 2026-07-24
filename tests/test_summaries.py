import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import events, outbox, summaries
from ingestion import schema


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "groundhog.duckdb"
        schema.init_db(self.db_path)
        self.con = duckdb.connect(str(self.db_path))

    def tearDown(self):
        self.con.close()
        self.temp_dir.cleanup()

    def test_daily_summary_uses_local_client_and_stores_artifact(self):
        events.record_event(self.con, "job_completed", "tests", "agent_run", "run-1", {"status": "succeeded"}, "run-1")
        with patch.object(summaries, "_ask_local_model", return_value="Daily summary") as ask:
            content = summaries.generate_daily_summary(self.con, date.today())

        artifact = self.con.execute("SELECT artifact_type, content, model FROM derived_artifacts").fetchone()
        self.assertEqual(content, "Daily summary")
        self.assertTrue(ask.called)
        self.assertEqual(artifact[0], "daily_summary")
        self.assertEqual(artifact[1], "Daily summary")
        self.assertEqual(artifact[2], summaries.OLLAMA_SQL_MODEL)

    def test_priority_labels_pending_stock_alerts_without_delivery(self):
        key = "stock-alert"
        events.record_event(self.con, "stock_alert_created", "tests", "stock_alert", "alert-1", {}, key)
        event_id = events.event_id_for(key)
        outbox.enqueue_event(self.con, event_id)

        pending = summaries.prioritize_pending_outbox(self.con)
        row = self.con.execute("SELECT status, priority, priority_reason FROM outbox").fetchone()

        self.assertEqual(pending, 1)
        self.assertEqual(row, ("pending", 100, "stock alert"))
