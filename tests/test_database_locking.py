import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

import duckdb

from config.database import connect, DatabaseLockTimeout


class DatabaseLockingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.duckdb"
        with connect(self.path) as con:
            con.execute("CREATE TABLE counter (value INTEGER)")
            con.execute("INSERT INTO counter VALUES (0)")

    def child(self, body):
        code = "from config.database import connect\nimport sys\npath = sys.argv[1]\n" + body
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(self.path)],
            cwd=Path(__file__).resolve().parent.parent,
            env=os.environ.copy(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: child.poll() is None and child.kill())
        return child

    def test_processes_serialize_updates_without_losing_writes(self):
        body = "for _ in range(5):\n with connect(path, timeout=10) as c:\n  c.execute('UPDATE counter SET value = value + 1')\n"
        children = [self.child(body) for _ in range(3)]
        for child in children:
            out, err = child.communicate(timeout=20)
            self.assertEqual(child.returncode, 0, out + err)
        with connect(self.path, read_only=True) as con:
            self.assertEqual(con.execute("SELECT value FROM counter").fetchone(), (15,))

    def test_reader_waits_for_writer_in_another_thread(self):
        started, finished = threading.Event(), threading.Event()
        errors = []
        def read():
            started.set()
            try:
                with connect(self.path, read_only=True, timeout=5) as con:
                    self.assertEqual(con.execute("SELECT value FROM counter").fetchone(), (1,))
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()
        with connect(self.path) as writer:
            thread = threading.Thread(target=read)
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(0.1))
            writer.execute("UPDATE counter SET value = 1")
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_timeout_does_not_release_another_process_lock(self):
        child = self.child("c = connect(path)\nprint('ready', flush=True)\nimport time\ntime.sleep(30)\n")
        self.assertEqual(child.stdout.readline().strip(), "ready")
        with self.assertRaises(DatabaseLockTimeout):
            connect(self.path, timeout=0.1)
        child.kill()
        child.communicate(timeout=5)
        # The OS releases advisory locks even after an abrupt process exit.
        with connect(self.path, timeout=2) as con:
            self.assertEqual(con.execute("SELECT 1").fetchone(), (1,))

    def test_exception_closes_connection_and_rolls_back(self):
        with self.assertRaisesRegex(ValueError, "abort"):
            with connect(self.path) as con:
                con.execute("BEGIN")
                con.execute("UPDATE counter SET value = 9")
                raise ValueError("abort")
        with connect(self.path) as con:
            self.assertEqual(con.execute("SELECT value FROM counter").fetchone(), (0,))

    def test_symlink_cannot_bypass_lock_or_deadlock_same_thread(self):
        alias = self.path.with_name("alias.duckdb")
        alias.symlink_to(self.path)
        with connect(self.path):
            with self.assertRaisesRegex(RuntimeError, "same thread"):
                connect(alias, timeout=0.1)
        with connect(alias) as con:
            self.assertEqual(con.execute("SELECT 1").fetchone(), (1,))

    def test_failed_open_releases_lock(self):
        missing = self.path.with_name("missing.duckdb")
        for _ in range(2):
            with self.assertRaises(duckdb.IOException):
                connect(missing, read_only=True, timeout=0.1)
        with connect(missing, timeout=0.1) as con:
            self.assertEqual(con.execute("SELECT 1").fetchone(), (1,))

    def test_unmanaged_writer_reports_timeout_and_recovers(self):
        child = self.child("import duckdb, time\nc = duckdb.connect(path)\nprint('ready', flush=True)\ntime.sleep(30)\n")
        self.assertEqual(child.stdout.readline().strip(), "ready")
        with self.assertRaisesRegex(DatabaseLockTimeout, "unmanaged"):
            connect(self.path, timeout=0.1)
        child.kill()
        child.communicate(timeout=5)
        with connect(self.path, timeout=2) as con:
            self.assertEqual(con.execute("SELECT 1").fetchone(), (1,))

    def test_schema_failure_releases_connection(self):
        from ingestion import schema
        from unittest.mock import patch
        with patch.object(schema, "_init_schema", side_effect=ValueError("schema failed")):
            with self.assertRaisesRegex(ValueError, "schema failed"):
                schema.init_db(self.path)
        with connect(self.path, timeout=0.1) as con:
            self.assertEqual(con.execute("SELECT 1").fetchone(), (1,))

    def test_stock_download_does_not_hold_database_lock(self):
        from ingestion import stocks, schema
        from unittest.mock import patch
        schema.init_db(self.path)
        def fetch(*args):
            # A nested managed connection would fail if stocks retained its
            # connection while waiting on the network.
            with connect(self.path, timeout=0.1) as con:
                self.assertEqual(con.execute("SELECT 1").fetchone(), (1,))
            return []
        with patch.object(stocks, "DB_PATH", self.path), patch.object(stocks, "load_watchlist", return_value=[("TEST", "2y")]), patch.object(stocks, "_fetch_history", side_effect=fetch):
            result = stocks.run()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["no_data"], ["TEST"])


if __name__ == "__main__":
    unittest.main()
