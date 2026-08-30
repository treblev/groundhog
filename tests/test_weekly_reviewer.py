"""Contract tests for the local-only Weekly Reviewer workflow."""

from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import summaries
import groundhog_service
from ingestion import schema


WEEK_END = date(2026, 8, 29)  # Saturday
WEEK_START = date(2026, 8, 23)


class WeeklyReviewerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "groundhog.duckdb"
        schema.init_db(self.db_path)
        self.con = duckdb.connect(str(self.db_path))
        # Imported here so this file remains syntax-checkable until production
        # implementation is added.
        self.reviewer = importlib.import_module("agent.weekly_reviewer")

    def tearDown(self):
        self.con.close()
        self.temp_dir.cleanup()

    def _seed_all_domains(self):
        self.con.execute(
            "INSERT INTO health_metrics (date, steps, avg_hr, active_minutes) VALUES (?, ?, ?, ?)",
            [WEEK_START, 8500, 62, 45],
        )
        self.con.execute(
            """
            INSERT INTO sleep_metrics
                (date, resting_hr, hrv, breath_rate, time_to_fall_asleep_minutes, deep_sleep_minutes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [WEEK_START, 54, 71, 14.2, 11, 96],
        )
        self.con.execute(
            """
            INSERT INTO activities (id, date, activity_type, duration_seconds, calories)
            VALUES ('activity-1', ?, 'run', 1800, 320)
            """,
            [WEEK_START],
        )
        self.con.execute(
            """
            INSERT INTO stock_watchlist (date, ticker, open, high, low, closing_price, volume)
            VALUES (?, 'BTC-USD', 110000, 112000, 109000, 111000, 123456)
            """,
            [WEEK_END],
        )
        self.con.execute(
            """
            INSERT INTO stock_signals (id, date, ticker, signal_type, timeframe, value, direction)
            VALUES ('signal-1', ?, 'BTC-USD', 'supertrend', 'daily', 100000, 'bullish')
            """,
            [WEEK_END],
        )
        self.con.execute(
            """
            INSERT INTO stock_alerts (id, date, ticker, alert_type, message)
            VALUES ('alert-1', ?, 'BTC-USD', 'supertrend_daily_bullish', 'BTC trend flipped')
            """,
            [WEEK_END],
        )
        self.con.execute(
            """
            INSERT INTO spending
                (id, transaction_date, merchant, amount, category, source_image_hash, source_row)
            VALUES ('spend-1', ?, 'Grocer', 42.50, 'groceries', 'image-1', 1)
            """,
            [WEEK_END],
        )
        self.con.execute(
            """
            INSERT INTO events (id, event_type, source, subject_type, subject_id, dedupe_key, payload)
            VALUES ('event-1', 'job_completed', 'tests', 'agent_run', 'run-1', 'weekly-review-test', '{}')
            """
        )

    def test_runs_four_specialists_then_coordinator_using_local_boundary(self):
        self._seed_all_domains()
        replies = [
            "Readiness evidence review",
            "Market evidence review",
            "Spending evidence review",
            "Operations evidence review",
            "Coordinator final review",
        ]

        with patch.object(self.reviewer, "_ask_local_model", side_effect=replies) as ask:
            content = self.reviewer.generate_weekly_review(self.con, WEEK_END)

        self.assertEqual(content, replies[-1])
        self.assertEqual(ask.call_count, 5)
        prompts = [call.args[0].lower() for call in ask.call_args_list]
        for expected, prompt in zip(
            ("readiness", "market", "spending", "operations"), prompts[:4], strict=True
        ):
            self.assertIn(expected, prompt)
        self.assertIn("readiness evidence review", prompts[4])
        self.assertIn("market evidence review", prompts[4])
        self.assertIn("spending evidence review", prompts[4])
        self.assertIn("operations evidence review", prompts[4])

        artifact = self.con.execute(
            """
            SELECT artifact_type, period_start, period_end, content
            FROM derived_artifacts
            WHERE artifact_type = 'weekly_review'
            """
        ).fetchone()
        self.assertEqual(artifact, ("weekly_review", WEEK_START, WEEK_END, replies[-1]))

    def test_upserts_one_weekly_artifact_on_repeat(self):
        first = ["first readiness", "first market", "first spending", "first operations", "first final"]
        second = ["second readiness", "second market", "second spending", "second operations", "second final"]

        with patch.object(self.reviewer, "_ask_local_model", side_effect=first + second) as ask:
            self.assertEqual(self.reviewer.generate_weekly_review(self.con, WEEK_END), "first final")
            self.assertEqual(self.reviewer.generate_weekly_review(self.con, WEEK_END), "second final")

        self.assertEqual(ask.call_count, 10)
        rows = self.con.execute(
            """
            SELECT COUNT(*), MIN(content), MAX(content)
            FROM derived_artifacts
            WHERE artifact_type = 'weekly_review' AND period_start = ? AND period_end = ?
            """,
            [WEEK_START, WEEK_END],
        ).fetchone()
        self.assertEqual(rows, (1, "second final", "second final"))

    def test_empty_domains_still_produce_a_review(self):
        replies = ["readiness", "market", "spending", "operations", "review despite gaps"]

        with patch.object(self.reviewer, "_ask_local_model", side_effect=replies) as ask:
            content = self.reviewer.generate_weekly_review(self.con, WEEK_END)

        self.assertEqual(content, "review despite gaps")
        self.assertEqual(ask.call_count, 5)
        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM derived_artifacts WHERE artifact_type = 'weekly_review'"
            ).fetchone()[0],
            1,
        )

    def test_empty_coordinator_response_is_not_stored(self):
        replies = ["readiness", "market", "spending", "operations", "   "]

        with patch.object(self.reviewer, "_ask_local_model", side_effect=replies):
            with self.assertRaisesRegex(RuntimeError, "coordinator returned an empty response"):
                self.reviewer.generate_weekly_review(self.con, WEEK_END)

        self.assertEqual(
            self.con.execute(
                "SELECT COUNT(*) FROM derived_artifacts WHERE artifact_type = 'weekly_review'"
            ).fetchone()[0],
            0,
        )

    def test_rejects_non_saturday_week_end_before_model_calls(self):
        with patch.object(self.reviewer, "_ask_local_model") as ask:
            with self.assertRaisesRegex(ValueError, "Saturday"):
                self.reviewer.generate_weekly_review(self.con, date(2026, 8, 28))

        ask.assert_not_called()
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM derived_artifacts").fetchone()[0], 0)

    def test_default_week_end_is_the_last_fully_completed_saturday(self):
        self.assertEqual(
            self.reviewer.latest_completed_week_end(date(2026, 8, 30)),
            WEEK_END,
        )
        self.assertEqual(
            self.reviewer.latest_completed_week_end(WEEK_END),
            date(2026, 8, 22),
        )

    def test_legacy_summaries_entrypoint_delegates_to_weekly_reviewer(self):
        with patch.object(self.reviewer, "generate_weekly_review", return_value="delegated review") as generate:
            content = summaries.generate_weekly_review(self.con, WEEK_END)

        self.assertEqual(content, "delegated review")
        generate.assert_called_once_with(self.con, WEEK_END)

    def test_service_notification_is_explicit_and_deduplicated(self):
        with (
            patch.object(groundhog_service, "DB_PATH", self.db_path),
            patch.object(groundhog_service, "generate_weekly_review", return_value="Weekly result"),
        ):
            self.assertEqual(
                groundhog_service.main(
                    ["summarize", "weekly", "--date", WEEK_END.isoformat()]
                ),
                0,
            )
            self.assertEqual(self.con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

            command = [
                "summarize",
                "weekly",
                "--date",
                WEEK_END.isoformat(),
                "--notify",
            ]
            self.assertEqual(groundhog_service.main(command), 0)
            self.assertEqual(groundhog_service.main(command), 0)

        event = self.con.execute(
            "SELECT event_type, payload FROM events WHERE event_type = 'weekly_review_generated'"
        ).fetchone()
        self.assertEqual(event[0], "weekly_review_generated")
        self.assertEqual(json.loads(event[1])["message"], "Weekly result")
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
