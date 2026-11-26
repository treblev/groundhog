from contextlib import redirect_stdout
import io
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import sync_garmin


class GarminSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "garmin.db"
        self.argv = ["--extractor", str(Path(self.temp.name) / "garmin"),
                     "--source-db", str(self.source)]

    def test_overlapping_sync_is_rejected_and_lock_is_reusable(self):
        with sync_garmin._sync_lock(self.source):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                with sync_garmin._sync_lock(self.source):
                    self.fail("Overlapping sync acquired the lock")
        with sync_garmin._sync_lock(self.source):
            pass

    def test_heartbeat_reports_silent_work_and_stops_after_exit(self):
        output = io.StringIO()
        with redirect_stdout(output):
            with sync_garmin._progress("test", interval=0.01):
                time.sleep(0.06)
            finished = output.getvalue()
            time.sleep(0.03)
            self.assertEqual(output.getvalue(), finished)
        self.assertIn("test started", finished)
        self.assertIn("test still running", finished)

    def test_extractor_timeout_terminates_and_reaps_child(self):
        real_popen = subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child
        with patch.object(sync_garmin.subprocess, "Popen", side_effect=spawn):
            with self.assertRaises(subprocess.TimeoutExpired):
                sync_garmin._run_extractor(
                    [sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.1)
        self.assertIsNotNone(children[0].poll())

    def test_failed_extraction_does_not_import_or_leave_sync_locked(self):
        with patch.object(sync_garmin, "_run_extractor", side_effect=subprocess.CalledProcessError(1, ["garmin"])), patch.object(sync_garmin, "import_garmin") as importer, redirect_stdout(io.StringIO()):
            with self.assertRaises(subprocess.CalledProcessError):
                sync_garmin.main(self.argv)
        importer.assert_not_called()
        with sync_garmin._sync_lock(self.source):
            pass

    def test_default_is_incremental_and_refresh_remains_explicit(self):
        for extra, missing in (([], True), (["--refresh-existing"], False)):
            with patch.object(sync_garmin, "_run_extractor") as extractor, patch.object(sync_garmin, "import_garmin", return_value={"activities_imported": 0}) as importer, redirect_stdout(io.StringIO()) as output:
                self.assertEqual(sync_garmin.main(self.argv + extra), 0)
            self.assertEqual(extractor.call_args.args[1], 900)
            self.assertEqual(importer.call_args.kwargs["only_missing"], missing)
            self.assertIn('"activities_imported": 0', output.getvalue())

    def test_sigterm_unwinds_context_and_restores_handler(self):
        previous = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(SystemExit) as raised:
            with sync_garmin._handle_termination():
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        self.assertEqual(raised.exception.code, 143)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)


if __name__ == "__main__":
    unittest.main()
