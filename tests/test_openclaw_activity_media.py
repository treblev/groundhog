import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import import_openclaw_activity_media as watcher


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

            with patch.object(watcher, "process_image", return_value=[{"type": "activity"}]) as process:
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

            with patch.object(watcher, "process_workout_plan", return_value=1) as plan:
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

            with patch.object(watcher, "process_sleep", return_value={"hrv": 50}) as process:
                self.assertEqual(watcher.run(inbound, state_path), 1)

            process.assert_called_once()
            state = watcher._load_state(state_path)
            self.assertEqual(next(iter(state.values()))["kind"], "sleep")
