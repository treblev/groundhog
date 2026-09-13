import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.activity_progress import (
    activity_progress,
    activity_progress_facts,
    generate_activity_progress_notes,
    period_bounds,
)
from agent.semantic_search import (
    DOMAIN_ACTIVITY_PROGRESS,
    search_documents,
    sync_activity_progress_embeddings,
)
from ingestion.schema import init_db
from mcp_server.server import _dispatch
import groundhog_service


WEEK_END = date(2026, 9, 12)


class ActivityProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "groundhog.duckdb"
        init_db(self.db_path)
        self.con = duckdb.connect(str(self.db_path))

    def tearDown(self):
        self.con.close()
        self.temp_dir.cleanup()

    def _activity(
        self,
        activity_id: str,
        activity_date: str,
        activity_key: str | None,
        *,
        source: str | None = "garmin",
        duration: int = 1800,
        moving: int | None = None,
        distance: float | None = None,
        avg_hr: int | None = 120,
        max_hr: int | None = 150,
        calories: int | None = 200,
        training_load: float | None = 30,
        pool_unit: str | None = None,
    ) -> None:
        self.con.execute(
            """
            INSERT INTO activities
                (id, date, activity_type, activity_source, activity_type_key,
                 duration_seconds, moving_duration_seconds, distance_meters,
                 avg_hr, max_hr, calories, activity_training_load,
                 pool_distance_unit)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [activity_id, activity_date, (activity_key or "running").replace("_", " "),
             source, activity_key, duration, moving, distance, avg_hr, max_hr,
             calories, training_load, pool_unit],
        )

    def test_period_boundaries_use_completed_saturday_and_calendar_month(self):
        self.assertEqual(
            period_bounds("weekly", today=date(2026, 9, 13)),
            (date(2026, 9, 6), WEEK_END),
        )
        self.assertEqual(
            period_bounds("monthly", today=date(2026, 9, 13)),
            (date(2026, 8, 1), date(2026, 8, 31)),
        )
        self.assertEqual(
            period_bounds("weekly", current=True, today=date(2026, 9, 16)),
            (date(2026, 9, 13), date(2026, 9, 16)),
        )

    def test_garmin_only_and_stairs_ignore_daily_and_generic_steps(self):
        self._activity("stairs", "2026-09-12", "stair_climbing", duration=1980)
        self._activity("legacy", "2026-09-12", None, source=None, duration=7200, avg_hr=199)
        self.con.execute(
            "INSERT INTO health_metrics (date, steps) VALUES ('2026-09-12', 20000)"
        )
        self.con.execute(
            "INSERT INTO garmin_activity_metrics VALUES ('stairs', 'supplemental', 'steps', 10, NULL, NULL)"
        )
        self.con.execute(
            "INSERT INTO garmin_activity_lap_metrics VALUES ('stairs', 1, 'total_cycles', 5, 'cycles')"
        )

        facts = activity_progress_facts(self.con, "weekly", WEEK_END)
        stairs = next(item for item in facts["activities"] if item["activity_key"] == "stair_climbing")

        self.assertEqual(stairs["target"]["sessions"], 1)
        self.assertEqual(stairs["target"]["duration_minutes"], 33.0)
        self.assertEqual(stairs["target"]["stairs_climbed"], 1056)
        self.assertEqual(stairs["target"]["moving_minutes"], 0.0)
        self.assertNotIn("running", {item["activity_key"] for item in facts["activities"]})

    def test_swim_25_yard_under_five_seconds_contributes_distance_only(self):
        self._activity(
            "swim", "2026-09-10", "lap_swimming", duration=22, moving=22,
            distance=45.72, avg_hr=125, pool_unit="meters",
        )
        self.con.execute(
            "INSERT INTO garmin_swimming_aggregates VALUES ('swim', 2286, 2, 20, 1, 10, 20, 30)"
        )
        lap_rows = [
            (1, "total_distance", 22.86, "m"),
            (1, "total_timer_time", 2, "s"),
            (1, "avg_heart_rate", 180, "bpm"),
            (2, "total_distance", 22.86, "m"),
            (2, "total_timer_time", 20, "s"),
            (2, "avg_heart_rate", 110, "bpm"),
        ]
        self.con.executemany(
            "INSERT INTO garmin_activity_lap_metrics VALUES ('swim', ?, ?, ?, ?)",
            lap_rows,
        )

        facts = activity_progress_facts(self.con, "weekly", WEEK_END)
        swim = next(item for item in facts["activities"] if item["activity_key"] == "lap_swimming")["target"]

        self.assertEqual(swim["distance_meters"], 46.0)
        self.assertEqual(swim["duration_minutes"], 0.3)
        self.assertEqual(swim["moving_minutes"], 0.3)
        self.assertEqual(swim["average_hr"], 110.0)
        self.assertEqual(swim["forgotten_timer_lengths"], 1)
        self.assertIsNone(swim["average_swolf"])

    def test_previous_and_four_week_baseline_are_returned(self):
        self._activity("target", "2026-09-09", "treadmill_running", moving=1800, distance=3218.688)
        self._activity("previous", "2026-09-02", "treadmill_running", moving=1800, distance=1609.344)
        self._activity("baseline", "2026-08-12", "treadmill_running", moving=1800, distance=1609.344)

        facts = activity_progress_facts(self.con, "weekly", WEEK_END)
        treadmill = facts["activities"][0]

        self.assertEqual(treadmill["target"]["distance_miles"], 2.0)
        self.assertEqual(treadmill["previous"]["distance_miles"], 1.0)
        self.assertEqual(treadmill["baseline_per_period"]["sessions"], 0.5)
        self.assertEqual(treadmill["change_from_previous"]["distance_percent"], 100.0)

    def test_whole_forgotten_timer_swim_keeps_only_distance(self):
        self._activity(
            "forgotten", "2026-09-10", "lap_swimming", duration=3, moving=3,
            distance=22.86, avg_hr=190, max_hr=200, calories=99,
            training_load=88, pool_unit="yards",
        )
        self.con.execute(
            "INSERT INTO garmin_swimming_aggregates VALUES ('forgotten', 2286, 1, 1, 1, 1, 1, 1)"
        )

        facts = activity_progress_facts(self.con, "weekly", WEEK_END)
        swim = facts["activities"][0]["target"]

        self.assertEqual(swim["sessions"], 1)
        self.assertEqual(swim["distance_meters"], 23.0)
        self.assertEqual(swim["duration_minutes"], 0.0)
        self.assertIsNone(swim["average_hr"])
        self.assertIsNone(swim["max_hr"])
        self.assertEqual(swim["calories"], 0)
        self.assertEqual(swim["training_load"], 0.0)

    def test_generation_is_idempotent_and_mcp_returns_stored_note(self):
        self._activity("stairs", "2026-09-12", "stair_climbing", duration=1980)
        calls = []

        def empty_model(prompt):
            calls.append(prompt)
            return ""

        first = generate_activity_progress_notes(
            self.con, "weekly", WEEK_END, model_call=empty_model
        )
        second = generate_activity_progress_notes(
            self.con, "weekly", WEEK_END, model_call=empty_model
        )
        result = json.loads(_dispatch(
            "get_activity_progress",
            {"period_kind": "weekly", "period_end": "2026-09-12", "activity_type": "stair_climbing"},
            self.con,
        ))

        self.assertEqual(first["activities"][0]["target"]["stairs_climbed"], 1056)
        self.assertEqual(second["activities"][0]["target"]["stairs_climbed"], 1056)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM activity_progress_notes").fetchone()[0], 2)
        self.assertIn("1,056 stairs", result["activities"][0]["narrative"])

    def test_semantic_index_filters_activity_and_period(self):
        self._activity("stairs", "2026-09-12", "stair_climbing", duration=1980)
        generate_activity_progress_notes(
            self.con, "weekly", WEEK_END, model_call=lambda _prompt: ""
        )
        self.con.close()
        embedder = lambda texts: [[1.0, 0.0] for _ in texts]
        sync_activity_progress_embeddings(self.db_path, embedder=embedder)

        results = search_documents(
            "stairs",
            domain=DOMAIN_ACTIVITY_PROGRESS,
            activity_type="stair_climbing",
            period_kind="weekly",
            db_path=self.db_path,
            embedder=embedder,
            sync=False,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["activity_type"], "stair_climbing")
        self.con = duckdb.connect(str(self.db_path))

    def test_activity_summary_cli_accepts_period_and_date(self):
        output = StringIO()
        generated = {
            "period_kind": "weekly",
            "period_start": date(2026, 9, 6),
            "period_end": WEEK_END,
        }
        with (
            patch.object(groundhog_service, "DB_PATH", self.db_path),
            patch.object(groundhog_service, "generate_activity_progress_notes", return_value=generated) as generate,
            redirect_stdout(output),
        ):
            exit_code = groundhog_service.main([
                "summarize", "activity", "--period", "weekly", "--date", "2026-09-12"
            ])

        self.assertEqual(exit_code, 0)
        generate.assert_called_once()
        self.assertEqual(json.loads(output.getvalue())["period_end"], "2026-09-12")


if __name__ == "__main__":
    unittest.main()
