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
