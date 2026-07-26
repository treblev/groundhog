import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import import_openclaw_activity_media as watcher
from ingestion import schema


class OpenClawActivityMediaTests(unittest.TestCase):
    def test_initialize_ignores_existing_attachments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inbound = root / "inbound"
            inbound.mkdir()
            (inbound / "old.jpg").write_bytes(b"old")
            state_path = root / "state.json"

            count = watcher.run(inbound, state_path, initialize=True)

            self.assertEqual(count, 0)
            state = watcher._load_state(state_path)
            self.assertEqual(next(iter(state.values()))["status"], "ignored_existing")

    def test_new_attachment_is_imported_only_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inbound = root / "inbound"
            inbound.mkdir()
            state_path = root / "state.json"
            watcher.run(inbound, state_path, initialize=True)
            image = inbound / "new.jpg"
            image.write_bytes(b"new")

            with (
                patch.object(watcher, "process_image", return_value=[{"type": "activity"}]) as process,
                patch.object(watcher, "_enqueue_confirmation"),
            ):
                self.assertEqual(watcher.run(inbound, state_path), 1)
                self.assertEqual(watcher.run(inbound, state_path), 0)

            process.assert_called_once_with(image)

    def test_failed_attachment_is_not_retried_without_a_new_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inbound = root / "inbound"
            inbound.mkdir()
            state_path = root / "state.json"
            image = inbound / "bad.jpg"
            image.write_bytes(b"bad")

            with patch.object(watcher, "process_image", side_effect=ValueError("not an activity")) as process:
                self.assertEqual(watcher.run(inbound, state_path), 0)
                self.assertEqual(watcher.run(inbound, state_path), 0)

            process.assert_called_once_with(image)

    def test_next_plan_uses_plan_importer_once_then_returns_to_activity_importer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inbound = root / "inbound"
            inbound.mkdir()
            state_path = root / "state.json"
            watcher.set_next_kind(state_path, "plan")
            plan_image = inbound / "plan.jpg"
            plan_image.write_bytes(b"plan")

            with (
                patch.object(watcher, "process_workout_plan", return_value=2) as plan,
                patch.object(watcher, "process_image", return_value=[{"type": "activity"}]) as activity,
                patch.object(watcher, "_enqueue_confirmation"),
            ):
                self.assertEqual(watcher.run(inbound, state_path), 2)
                activity_image = inbound / "activity.jpg"
                activity_image.write_bytes(b"activity")
                self.assertEqual(watcher.run(inbound, state_path), 1)

            plan.assert_called_once()
            activity.assert_called_once_with(activity_image)

    def test_force_kind_applies_to_every_currently_pending_attachment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inbound = root / "inbound"
            inbound.mkdir()
            state_path = root / "state.json"
            (inbound / "plan-one.jpg").write_bytes(b"one")
            (inbound / "plan-two.jpg").write_bytes(b"two")

            with (
                patch.object(watcher, "process_workout_plan", return_value=1) as plan,
                patch.object(watcher, "_enqueue_confirmation"),
            ):
                self.assertEqual(watcher.run(inbound, state_path, force_kind="plan"), 2)

            self.assertEqual(plan.call_count, 2)
            self.assertTrue(all(record["kind"] == "plan" for record in watcher._load_state(state_path).values()))

    def test_next_sleep_uses_sleep_importer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inbound = root / "inbound"
            inbound.mkdir()
            state_path = root / "state.json"
            watcher.set_next_kind(state_path, "sleep")
            image = inbound / "sleep.jpg"
            image.write_bytes(b"sleep")

            with (
                patch.object(watcher, "process_sleep", return_value={"hrv": 50}) as process,
                patch.object(watcher, "_enqueue_confirmation"),
            ):
                self.assertEqual(watcher.run(inbound, state_path), 1)

            process.assert_called_once()
            state = watcher._load_state(state_path)
            self.assertEqual(next(iter(state.values()))["kind"], "sleep")

    def test_caption_date_is_passed_to_the_direct_activity_importer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inbound = root / "inbound"
            inbound.mkdir()
            state_path = root / "state.json"
            image = inbound / "run.jpg"
            image.write_bytes(b"run")

            with (
                patch.object(watcher, "process_image", return_value=[{"type": "activity"}]) as process,
                patch.object(watcher, "_enqueue_confirmation"),
            ):
                records = watcher.import_captioned_activity(image, "Easy run — 7/18", state_path)

            _, reference_date, date_hint = process.call_args.args
            self.assertEqual(date_hint, "7/18")
            self.assertIsNotNone(reference_date)
            self.assertEqual(records, [{"type": "activity"}])
            state = watcher._load_state(state_path)
            record = state[watcher._file_id(image)]
            self.assertEqual(record["caption_date_hint"], "7/18")

    def test_caption_ignores_invalid_date_tokens(self):
        self.assertIsNone(watcher._date_hint_from_caption("ran on 2/30"))

    def test_caption_prefers_an_iso_date(self):
        self.assertEqual(watcher._date_hint_from_caption("date: 2025-07-18"), "2025-07-18")

    def test_activity_confirmation_includes_reviewable_metrics(self):
        message = watcher._confirmation_message(
            "activity",
            [{
                "activity_type": "running",
                "date": "2026-07-25",
                "distance_miles": 3.1,
                "duration_seconds": 1800,
                "avg_pace_seconds_per_mile": 581,
                "avg_hr": 142,
            }],
        )

        self.assertEqual(
            message,
            "Imported activity: running (2026-07-25). Distance: 3.1 mi; duration: 30:00; "
            "avg pace: 9:41/mi; avg HR: 142 bpm.",
        )

    def test_successful_import_enqueues_confirmation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inbound = root / "inbound"
            inbound.mkdir()
            state_path = root / "state.json"
            image = inbound / "run.jpg"
            image.write_bytes(b"run")
            records = [{"type": "activity", "activity_type": "running"}]

            with (
                patch.object(watcher, "process_image", return_value=records),
                patch.object(watcher, "_enqueue_confirmation") as enqueue,
            ):
                self.assertEqual(watcher.run(inbound, state_path), 1)

            enqueue.assert_called_once_with("activity", watcher._file_id(image), records, None)

    def test_confirmation_is_queued_once_in_outbox(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "groundhog.duckdb"
            schema.init_db(db_path)
            records = [{"activity_type": "walking", "date": "2026-07-25", "duration_seconds": 600}]

            with patch.object(watcher, "DB_PATH", db_path):
                watcher._enqueue_confirmation("activity", "upload-id", records)
                watcher._enqueue_confirmation("activity", "upload-id", records)

            con = duckdb.connect(str(db_path), read_only=True)
            try:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending'").fetchone()[0], 1)
                self.assertIn(
                    "Imported activity: walking (2026-07-25)",
                    con.execute("SELECT payload::VARCHAR FROM events").fetchone()[0],
                )
            finally:
                con.close()
