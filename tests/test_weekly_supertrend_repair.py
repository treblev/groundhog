import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import events, outbox
from analytics.signals import _completed_weekly_bars, _upsert_signal
from ingestion import schema
from scripts.repair_weekly_supertrend import repair


class CompletedWeeklyBarsTests(unittest.TestCase):
    def test_excludes_friday_label_until_that_friday_arrives(self):
        dates = pd.to_datetime(["2026-08-07", "2026-08-10", "2026-08-11"])
        frame = pd.DataFrame(
            {
                "date": dates,
                "open": [100.0, 101.0, 102.0],
                "high": [103.0, 104.0, 105.0],
                "low": [99.0, 100.0, 101.0],
                "closing_price": [102.0, 103.0, 104.0],
                "volume": [1, 2, 3],
            }
        )

        partial = _completed_weekly_bars(frame, date(2026, 8, 11))
        completed = _completed_weekly_bars(frame, date(2026, 8, 14))

        self.assertEqual(partial["date"].dt.date.tolist(), [date(2026, 8, 7)])
        self.assertEqual(
            completed["date"].dt.date.tolist(),
            [date(2026, 8, 7), date(2026, 8, 14)],
        )

    def test_completed_week_can_replace_an_earlier_partial_signal(self):
        con = duckdb.connect(":memory:")
        try:
            con.execute(
                """
                CREATE TABLE stock_signals (
                    id VARCHAR PRIMARY KEY, date DATE, ticker VARCHAR, signal_type VARCHAR,
                    timeframe VARCHAR, value DECIMAL(10, 4), direction VARCHAR
                )
                """
            )
            _upsert_signal(
                con, date(2026, 8, 14), "SHOP", "supertrend", "weekly", 100, "bullish"
            )
            _upsert_signal(
                con, date(2026, 8, 14), "SHOP", "supertrend", "weekly", 99, "bearish", replace=True
            )
            self.assertEqual(
                con.execute("SELECT value, direction FROM stock_signals").fetchone(),
                (99, "bearish"),
            )
        finally:
            con.close()


class WeeklySupertrendRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "groundhog.duckdb"
        schema.init_db(self.db_path)
        self.con = duckdb.connect(str(self.db_path))
        self.con.execute(
            """
            INSERT INTO stock_signals (id, date, ticker, signal_type, timeframe, value, direction)
            VALUES
                ('valid-signal', '2026-08-07', 'SHOP', 'supertrend', 'weekly', 100, 'bearish'),
                ('future-signal', '2026-08-14', 'SHOP', 'supertrend', 'weekly', 101, 'bullish')
            """
        )
        self.con.execute(
            """
            INSERT INTO stock_alerts (id, date, ticker, alert_type, message)
            VALUES ('future-alert', '2026-08-14', 'SHOP', 'supertrend_weekly_bullish', 'SHOP flipped')
            """
        )
        self.con.execute(
            """
            INSERT INTO semantic_chunks
                (id, domain, source_id, chunk_kind, chunk_index, content, metadata,
                 content_hash, embedding_model, embedding)
            VALUES ('future-chunk', 'stock_alert', 'future-alert', 'alert', 0,
                    'SHOP flipped', '{}', 'hash', 'test', [1.0])
            """
        )
        alert_key = "stock_alert:future-alert"
        events.record_event(
            self.con, "stock_alert_created", "tests", "stock_alert", "future-alert",
            {"ticker": "SHOP"}, alert_key,
        )
        outbox.enqueue_event(self.con, events.event_id_for(alert_key))
        events.record_event(
            self.con, "stock_signal_flipped", "tests", "stock_signal", "SHOP:supertrend:weekly:2026-08-14",
            {"signal_type": "supertrend", "timeframe": "weekly", "date": "2026-08-14"},
            "flip:future",
        )

    def tearDown(self):
        self.con.close()
        self.temp_dir.cleanup()

    def test_dry_run_reports_without_changing_rows(self):
        result = repair(self.db_path, as_of_date=date(2026, 8, 11), dry_run=True)

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["stock_alerts"], 1)
        self.assertEqual(result["stock_signals"], 1)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM stock_alerts").fetchone()[0], 1)

    def test_apply_removes_only_future_weekly_artifacts(self):
        result = repair(self.db_path, as_of_date=date(2026, 8, 11))

        self.assertFalse(result["dry_run"])
        self.assertEqual(
            self.con.execute("SELECT id FROM stock_signals ORDER BY id").fetchall(),
            [("valid-signal",)],
        )
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM stock_alerts").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM semantic_chunks").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
