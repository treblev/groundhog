import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion import schema, workouts


class WorkoutIngestionTests(unittest.TestCase):
    def test_date_from_filename_requires_a_valid_iso_date(self):
        self.assertEqual(
            workouts._date_from_filename(Path("SugarWOD 2026-07-21.png")),
            date(2026, 7, 21),
        )
        self.assertIsNone(workouts._date_from_filename(Path("SugarWOD.png")))
        self.assertIsNone(workouts._date_from_filename(Path("SugarWOD 2026-02-30.png")))

    def test_parse_workouts_accepts_a_fenced_json_array(self):
        parsed = workouts._parse_workouts(
            '```json\n[{"name": "AMRAP", "structure_type": "amrap"}]\n```'
        )
        self.assertEqual(parsed, [{"name": "AMRAP", "structure_type": "amrap"}])
        self.assertEqual(workouts._parse_workouts('{"name": "single plan"}'), [{"name": "single plan"}])

    def test_multiple_cards_are_combined_into_one_workout_plan(self):
        merged = workouts._combine_workout_cards(
            [
                {"name": "Conditioning", "category": "Fitness", "structure_type": "amrap", "description": "10 min AMRAP"},
                {"name": "Deadlift", "category": "Fitness", "structure_type": "strength", "description": "5 x 3"},
            ]
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["name"], "Conditioning")
        self.assertIsNone(merged[0]["structure_type"])
        self.assertEqual(merged[0]["description"], "Conditioning\n10 min AMRAP\n\nDeadlift\n5 x 3")

    def test_filename_date_overrides_vision_dates_and_sets_weekday(self):
        parsed = [{"date": "2025-01-01", "date_day": 1, "day_of_week": "WED"}]
        workouts._apply_filename_date(parsed, date(2026, 7, 21))
        self.assertEqual(
            parsed,
            [{"date": "2026-07-21", "date_day": 21, "day_of_week": "TUE"}],
        )

    def test_workout_insert_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "groundhog.duckdb"
            schema.init_db(db_path)
            con = duckdb.connect(str(db_path))
            try:
                workout = {
                    "date": "2026-07-21",
                    "day_of_week": "TUE",
                    "name": "AMRAP",
                    "category": "Fitness",
                    "structure_type": "amrap",
                    "description": "10 minutes",
                }
                workouts._insert(con, workout)
                workouts._insert(con, workout)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM workouts").fetchone()[0], 1)
            finally:
                con.close()

    def test_direct_upload_accepts_an_undated_template(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "groundhog.duckdb"
            image_path = temp_path / "orange-theory.png"
            image_path.write_bytes(b"image bytes")
            schema.init_db(db_path)
            with (
                patch.object(workouts, "DB_PATH", db_path),
                patch.object(workouts, "WORKOUTS_DROP_FOLDER", temp_path / "drop"),
                patch.object(workouts, "PROCESSED_DIR", temp_path / "processed"),
                patch.object(
                    workouts,
                    "_query_ollama",
                    return_value=(
                        '[{"name":"August Tornado Template #1",'
                        '"category":"OrangeTheory","structure_type":"tornado",'
                        '"description":"Tread Block 1: 3:30\\n1:30 push"}]'
                    ),
                ),
            ):
                self.assertEqual(workouts.process_image(image_path), 1)

            con = duckdb.connect(str(db_path), read_only=True)
            try:
                row = con.execute(
                    "SELECT date, day_of_week, category, structure_type FROM workouts"
                ).fetchone()
                self.assertEqual(row, (None, None, "OrangeTheory", "tornado"))
            finally:
                con.close()

    def test_direct_upload_archives_without_moving_the_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "groundhog.duckdb"
            archive_dir = temp_path / "processed"
            image_path = temp_path / "telegram-upload.png"
            image_path.write_bytes(b"image bytes")
            schema.init_db(db_path)
            with (
                patch.object(workouts, "DB_PATH", db_path),
                patch.object(workouts, "WORKOUTS_DROP_FOLDER", temp_path / "drop"),
                patch.object(workouts, "PROCESSED_DIR", archive_dir),
                patch.object(workouts, "_query_ollama", return_value='[{"name": "AMRAP"}]'),
            ):
                count = workouts.process_image(image_path, date(2026, 7, 21))

            self.assertEqual(count, 1)
            self.assertTrue(image_path.exists())
            self.assertEqual(len(list(archive_dir.glob("*.png"))), 1)
            con = duckdb.connect(str(db_path), read_only=True)
            try:
                self.assertEqual(con.execute("SELECT date, day_of_week FROM workouts").fetchone(), (date(2026, 7, 21), "TUE"))
                self.assertEqual(con.execute("SELECT event_type FROM events").fetchone(), ("workout_data_imported",))
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
