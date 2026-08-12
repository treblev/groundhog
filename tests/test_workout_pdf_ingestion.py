import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion import schema, workout_pdfs
from ingestion.workouts import _insert


SAMPLE_TEXT = """8/9/26, 11:47 AM     Whiteboard Calendar : SugarWOD
January 2026 / Workout of the Day
MON 12
0
\uf023 Fitness

20 minute AMRAP
10 squats
https://app.sugarwod.com/workouts/calendar?week=20260112&track=workout-of-the-day 1/2
\f8/9/26, 11:47 AM     Whiteboard Calendar : SugarWOD
\uf023 Performance
5 x 3 Deadlift
12 results
TUE 13
0
\uf023 HYROX

30/30 X 8 rounds rotating
https://app.sugarwod.com/workouts/calendar?week=20260112&track=workout-of-the-day 2/2
"""


class WorkoutPdfIngestionTests(unittest.TestCase):
    def test_parser_uses_headers_not_pdf_page_boundaries(self):
        records = workout_pdfs.parse_pdf_text(SAMPLE_TEXT)
        self.assertEqual([record["date"] for record in records], ["2026-01-12", "2026-01-13"])
        self.assertEqual(records[0]["name"], "Fitness")
        self.assertEqual(records[0]["category"], "Workout of the Day")
        self.assertIn("Performance\n5 x 3 Deadlift", records[0]["description"])
        self.assertNotIn("results", records[0]["description"])
        self.assertNotIn("Whiteboard Calendar", records[0]["description"])
        self.assertEqual(records[0]["structure_type"], "amrap")
        self.assertEqual(records[1]["structure_type"], "rotating")

    def test_parser_rejects_a_day_that_does_not_match_the_calendar_week(self):
        with self.assertRaisesRegex(ValueError, "does not match week"):
            workout_pdfs.parse_pdf_text(SAMPLE_TEXT.replace("MON 12", "MON 13"))

    def test_parser_removes_a_nonzero_comment_count_before_the_plan(self):
        records = workout_pdfs.parse_pdf_text(SAMPLE_TEXT.replace("MON 12\n0", "MON 12\n1"))
        self.assertEqual(records[0]["name"], "Fitness")
        self.assertTrue(records[0]["description"].startswith("Fitness\n"))

    def test_import_skips_any_date_already_in_workouts(self):
        records = workout_pdfs.parse_pdf_text(SAMPLE_TEXT)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "groundhog.duckdb"
            source_path = temp_path / "week.pdf"
            source_path.write_bytes(b"pdf bytes")
            schema.init_db(db_path)
            con = duckdb.connect(str(db_path))
            try:
                _insert(
                    con,
                    {
                        "date": "2026-01-12",
                        "day_of_week": "MON",
                        "name": "Existing",
                        "category": "Workout of the Day",
                        "structure_type": None,
                        "description": "Keep this record",
                    },
                )
                result = workout_pdfs.import_records(con, records, source_path)
                self.assertEqual(result["inserted"], 1)
                self.assertEqual(result["skipped_existing"], 1)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM workouts").fetchone()[0], 2)
                self.assertEqual(
                    con.execute("SELECT name FROM workouts WHERE date = '2026-01-12'").fetchone()[0],
                    "Existing",
                )
                self.assertEqual(con.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
            finally:
                con.close()

    def test_dry_run_reports_without_writing(self):
        records = workout_pdfs.parse_pdf_text(SAMPLE_TEXT)
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            db_path = temp_path / "groundhog.duckdb"
            source_path = temp_path / "week.pdf"
            source_path.write_bytes(b"pdf bytes")
            schema.init_db(db_path)
            con = duckdb.connect(str(db_path))
            try:
                result = workout_pdfs.import_records(con, records, source_path, dry_run=True)
                self.assertEqual(result["inserted"], 2)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM workouts").fetchone()[0], 0)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
