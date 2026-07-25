import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion import health, schema


class HealthUploadTests(unittest.TestCase):
    def test_prompt_allows_pool_swim_activities(self):
        self.assertIn('"pool swim"', health.PROMPT)

    def test_direct_upload_writes_to_existing_activities_table(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "groundhog.duckdb"
            image_path = temp_path / "telegram-activity.png"
            processed_dir = temp_path / "processed"
            image_path.write_bytes(b"activity screenshot")
            schema.init_db(db_path)
            response = '[{"type":"activity","month_day":"07-24","activity_type":"strength training","distance_miles":null,"duration_seconds":1800,"avg_pace_seconds_per_mile":null,"avg_hr":140,"max_hr":165,"calories":350}]'
            with (
                patch.object(health, "DB_PATH", db_path),
                patch.object(health, "PROCESSED_DIR", processed_dir),
                patch.object(health, "_query_ollama", return_value=response),
            ):
                records = health.process_image(image_path, date(2026, 7, 24))

            self.assertEqual(len(records), 1)
            self.assertTrue(image_path.exists())
            con = duckdb.connect(str(db_path), read_only=True)
            try:
                self.assertEqual(
                    con.execute("SELECT date, activity_type, duration_seconds, avg_hr FROM activities").fetchone(),
                    (date(2026, 7, 24), "strength training", 1800, 140),
                )
                self.assertEqual(con.execute("SELECT COUNT(*) FROM workouts").fetchone()[0], 0)
            finally:
                con.close()
