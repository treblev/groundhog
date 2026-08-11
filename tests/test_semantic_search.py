import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.semantic_search import (
    DOMAIN_STOCK_ALERT,
    search_documents,
    split_workout_sections,
    stock_alert_chunks,
    sync_stock_alert_embeddings,
    sync_workout_embeddings,
    workout_chunks,
)
from ingestion import schema


def fake_embedder(texts: list[str]) -> list[list[float]]:
    vectors = []
    for text in texts:
        lowered = text.lower()
        if "sled" in lowered or "rowing and lower body" in lowered:
            vectors.append([1.0, 0.0, 0.0])
        elif "yoga" in lowered:
            vectors.append([0.0, 1.0, 0.0])
        else:
            vectors.append([0.0, 0.0, 1.0])
    return vectors


def fake_stock_alert_embedder(texts: list[str]) -> list[list[float]]:
    vectors = []
    for text in texts:
        lowered = text.lower()
        if "bullish" in lowered:
            vectors.append([1.0, 0.0, 0.0])
        elif "bearish" in lowered:
            vectors.append([0.0, 1.0, 0.0])
        else:
            vectors.append([0.0, 0.0, 1.0])
    return vectors


class SemanticChunkingTests(unittest.TestCase):
    def test_splits_sugarwod_and_orange_theory_sections(self):
        sugarwod = split_workout_sections(
            "Fitness\n10 DB squats\n\nPerformance\n5 barbell squats\n\nHYROX (1 pm)\nSled push"
        )
        orange_theory = split_workout_sections(
            "August Tornado Template\n\nTread Block 1: 3:30\n1:30 push\n\n"
            "Row Block 1: 3:30\n400 meter push row\n\nFloor Block 1: 3:30\n10 squat to press"
        )

        self.assertEqual([item["section_label"] for item in sugarwod], ["Fitness", "Performance", "HYROX"])
        self.assertEqual([item["section_label"] for item in orange_theory], ["Tread", "Row", "Floor"])
        self.assertIn("400 meter push row", orange_theory[1]["content"])

    def test_creates_day_and_section_chunks_with_expanded_aliases(self):
        chunks = workout_chunks(
            {
                "id": "workout-1",
                "date": None,
                "day_of_week": None,
                "name": "Tornado",
                "category": "OrangeTheory",
                "structure_type": "tornado",
                "description": "Tread Block 1: 3:30\n1:30 push\n\nFloor Block 1: 3:30\n10 DB squats",
            }
        )

        self.assertEqual([chunk["chunk_kind"] for chunk in chunks], ["day", "section", "section"])
        self.assertIsNone(chunks[0]["source_date"])
        self.assertIn("dumbbell", chunks[2]["embedding_input"])


class SemanticIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "groundhog.duckdb"
        schema.init_db(self.db_path)
        con = duckdb.connect(str(self.db_path))
        try:
            con.execute(
                """
                INSERT INTO workouts
                    (id, date, day_of_week, name, category, structure_type, description)
                VALUES
                    ('sled-plan', '2026-08-10', 'MON', 'HYROX Sled', 'Workout of the Day', 'intervals',
                     'Fitness\nDB squats\n\nHYROX (1 pm)\nSled push and rower'),
                    ('yoga-plan', NULL, NULL, 'Recovery Yoga', 'Recovery', NULL,
                     'Yoga mobility and breathing')
                """
            )
            con.execute(
                """
                INSERT INTO stock_alerts (id, date, ticker, alert_type, message)
                VALUES
                    ('alpha-weekly-bull', '2026-08-01', 'ALPHA', 'supertrend_weekly_bullish',
                     'ALPHA: Supertrend (weekly) flipped BULLISH (BUY signal)'),
                    ('beta-weekly-bear', '2026-08-08', 'BETA', 'supertrend_weekly_bearish',
                     'BETA: Supertrend (weekly) flipped BEARISH (SELL signal)'),
                    ('gamma-daily-bull', '2026-08-09', 'GAMMA', 'supertrend_daily_bullish',
                     'GAMMA: Supertrend (daily) flipped BULLISH (BUY signal)'),
                    ('alpha-sma', '2026-08-09', 'ALPHA', 'golden_cross',
                     'ALPHA: Golden Cross — SMA50 crossed above SMA200 (BUY signal)')
                """
            )
        finally:
            con.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_sync_is_idempotent_and_removes_obsolete_chunks(self):
        first = sync_workout_embeddings(self.db_path, embedder=fake_embedder)
        second = sync_workout_embeddings(self.db_path, embedder=fake_embedder)

        self.assertEqual(first["sources"], 2)
        self.assertEqual(first["chunks"], 4)
        self.assertEqual(first["embedded"], 4)
        self.assertEqual(second["embedded"], 0)
        self.assertEqual(second["unchanged"], 4)

        con = duckdb.connect(str(self.db_path))
        try:
            con.execute("DELETE FROM workouts WHERE id = 'yoga-plan'")
        finally:
            con.close()
        third = sync_workout_embeddings(self.db_path, embedder=fake_embedder)
        self.assertEqual(third["deleted"], 1)

    def test_dry_run_generates_vectors_without_writing_chunks(self):
        result = sync_workout_embeddings(
            self.db_path,
            embedder=fake_embedder,
            batch_size=2,
            dry_run=True,
        )

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["sources"], 2)
        self.assertEqual(result["would_embed"], 4)
        self.assertEqual(result["batches"], 2)
        self.assertEqual(result["dimensions"], 3)
        self.assertEqual(result["writes"], 0)
        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM semantic_chunks").fetchone()[0], 0)
        finally:
            con.close()

    def test_search_ranks_semantic_matches_and_deduplicates_chunk_levels(self):
        sync_workout_embeddings(self.db_path, embedder=fake_embedder)
        results = search_documents(
            "rowing and lower body",
            top_k=20,
            db_path=self.db_path,
            embedder=fake_embedder,
            sync=False,
        )

        self.assertEqual(results[0]["source_id"], "sled-plan")
        self.assertEqual(results[0]["section"], "HYROX")
        self.assertEqual(len({row["source_id"] for row in results}), len(results))
        self.assertLessEqual(len(results), 10)

    def test_search_applies_section_and_date_filters(self):
        sync_workout_embeddings(self.db_path, embedder=fake_embedder)
        results = search_documents(
            "sled workout",
            section="HYROX",
            start_date="2026-01-01",
            end_date="2026-12-31",
            db_path=self.db_path,
            embedder=fake_embedder,
            sync=False,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source_id"], "sled-plan")
        self.assertEqual(results[0]["match_kind"], "section")

    def test_stock_alert_chunks_include_only_weekly_supertrend_rows(self):
        self.assertEqual(stock_alert_chunks({
            "id": "daily", "date": date(2026, 8, 9), "ticker": "GAMMA",
            "alert_type": "supertrend_daily_bullish", "message": "daily alert",
        }), [])

        first = sync_stock_alert_embeddings(
            self.db_path, embedder=fake_stock_alert_embedder
        )
        second = sync_stock_alert_embeddings(
            self.db_path, embedder=fake_stock_alert_embedder
        )

        self.assertEqual(first["sources"], 2)
        self.assertEqual(first["chunks"], 2)
        self.assertEqual(first["embedded"], 2)
        self.assertEqual(second["embedded"], 0)
        con = duckdb.connect(str(self.db_path))
        try:
            con.execute(
                "UPDATE stock_alerts SET message = ? WHERE id = ?",
                ["ALPHA: revised weekly bullish Supertrend alert", "alpha-weekly-bull"],
            )
        finally:
            con.close()
        changed = sync_stock_alert_embeddings(
            self.db_path, embedder=fake_stock_alert_embedder
        )
        self.assertEqual(changed["embedded"], 1)
        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            rows = con.execute(
                "SELECT source_id FROM semantic_chunks WHERE domain = ? ORDER BY source_id",
                [DOMAIN_STOCK_ALERT],
            ).fetchall()
        finally:
            con.close()
        self.assertEqual(rows, [("alpha-weekly-bull",), ("beta-weekly-bear",)])
        con = duckdb.connect(str(self.db_path))
        try:
            con.execute("DELETE FROM stock_alerts WHERE id = ?", ["beta-weekly-bear"])
        finally:
            con.close()
        removed = sync_stock_alert_embeddings(
            self.db_path, embedder=fake_stock_alert_embedder
        )
        self.assertEqual(removed["deleted"], 1)

    def test_stock_alert_search_applies_ticker_direction_and_date_filters(self):
        sync_stock_alert_embeddings(self.db_path, embedder=fake_stock_alert_embedder)
        results = search_documents(
            "weekly bullish trend",
            domain=DOMAIN_STOCK_ALERT,
            ticker="alpha",
            direction="bullish",
            start_date="2026-08-01",
            end_date="2026-08-01",
            db_path=self.db_path,
            embedder=fake_stock_alert_embedder,
            sync=False,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source_id"], "alpha-weekly-bull")
        self.assertEqual(results[0]["ticker"], "ALPHA")
        self.assertEqual(results[0]["direction"], "bullish")
        self.assertEqual(results[0]["alert_type"], "supertrend_weekly_bullish")


if __name__ == "__main__":
    unittest.main()
