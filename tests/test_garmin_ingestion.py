import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion import garmin, schema


class GarminIngestionTests(unittest.TestCase):
    def _source_db(self, path: Path) -> None:
        con = sqlite3.connect(path)
        con.executescript(
            """
            CREATE TABLE activity (
                activity_id INTEGER PRIMARY KEY, user_id INTEGER, activity_name TEXT,
                activity_type_key TEXT, event_type_key TEXT, start_ts TEXT, end_ts TEXT,
                timezone_offset_hours REAL, duration REAL, moving_duration REAL,
                distance REAL, lap_count INTEGER, average_speed REAL, max_speed REAL,
                average_hr REAL, max_hr REAL, calories REAL, has_polyline INTEGER,
                ts_data_available INTEGER
            );
            CREATE TABLE activity_lap_metric (
                activity_id INTEGER, lap_idx INTEGER, name TEXT, value REAL, units TEXT
            );
            CREATE TABLE activity_ts_metric (
                activity_id INTEGER, timestamp TEXT, name TEXT, value REAL, units TEXT
            );
            CREATE TABLE swimming_agg_metrics (
                activity_id INTEGER, pool_length REAL, active_lengths INTEGER,
                strokes REAL, avg_stroke_distance REAL, avg_strokes REAL,
                avg_swim_cadence REAL, avg_swolf REAL
            );
            CREATE TABLE activity_path (
                activity_id INTEGER, path_json TEXT, point_count INTEGER
            );
            CREATE TABLE steps (
                user_id INTEGER, timestamp TEXT, value INTEGER,
                activity_level TEXT, activity_level_constant INTEGER
            );
            """
        )
        con.execute(
            "INSERT INTO activity VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [123, 9, "Morning swim", "lap_swimming", "other", "2026-09-05T18:00:00", "2026-09-05T18:10:00", -7, 600, 590, 1000, 40, 1.67, 2.1, 130, 155, 300, 0, 1],
        )
        con.execute("INSERT INTO activity_lap_metric VALUES (123, 1, 'distance', 25, 'm')")
        con.execute("INSERT INTO activity_ts_metric VALUES (123, '2026-09-06T01:00:01', 'heart_rate', 130, 'bpm')")
        con.execute("INSERT INTO swimming_agg_metrics VALUES (123, 2500, 40, 800, 1.25, 20, 30, 45)")
        con.execute("INSERT INTO activity_path VALUES (123, ?, 2)", ['[[1.0, 2.0], [1.1, 2.1]]'])
        con.executemany(
            "INSERT INTO steps VALUES (?, ?, ?, ?, ?)",
            [
                (9, "2026-09-05T08:00:00+00:00", 1200, "active", 0),
                (9, "2026-09-05T08:15:00+00:00", 800, "active", 0),
                (9, "2026-09-06T08:00:00+00:00", 5000, "active", 0),
            ],
        )
        con.commit()
        con.close()

    def test_imports_scalar_and_repeating_garmin_data_idempotently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_db = root / "garmin_data.db"
            target_db = root / "groundhog.duckdb"
            self._source_db(source_db)

            first = garmin.import_garmin(source_db, target_db, date(2026, 9, 5), date(2026, 9, 7))
            second = garmin.import_garmin(source_db, target_db, date(2026, 9, 5), date(2026, 9, 7))
            self.assertEqual(first["activities_imported"], 1)
            self.assertEqual(second["activities_imported"], 1)

            con = duckdb.connect(str(target_db), read_only=True)
            try:
                self.assertEqual(
                    con.execute(
                        "SELECT id, date, activity_type, distance_meters, avg_hr, pool_distance, laps, swim_pace_unit FROM activities"
                    ).fetchone(),
                    ("garmin:123", date(2026, 9, 5), "pool swim", 1000.0, 130, 1000.0, 40, "meters"),
                )
                self.assertEqual(con.execute("SELECT COUNT(*) FROM garmin_activity_lap_metrics").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM garmin_activity_samples").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM garmin_activity_paths").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM garmin_swimming_aggregates").fetchone()[0], 1)
                self.assertEqual(
                    con.execute("SELECT date, steps FROM health_metrics ORDER BY date").fetchall(),
                    [(date(2026, 9, 5), 2000), (date(2026, 9, 6), 5000)],
                )
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
