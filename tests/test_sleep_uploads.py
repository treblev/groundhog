import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion import schema, sleep


class SleepUploadTests(unittest.TestCase):
    def test_process_image_uses_supplied_date_and_archives_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image = root / "upload.jpg"
            image.write_bytes(b"sleep")
            db_path = root / "test.duckdb"
            con = duckdb.connect(str(db_path))
            schema.init_db(db_path)
            con.close()

            with (
                patch.object(sleep, "DB_PATH", db_path),
                patch.object(sleep, "PROCESSED_DIR", root / "processed"),
                patch.object(sleep, "_query_ollama", return_value='{"hrv": 48, "resting_hr": 54}'),
            ):
                metrics = sleep.process_image(image, date(2026, 7, 25))

            self.assertEqual(metrics["date"], "2026-07-25")
            con = duckdb.connect(str(db_path), read_only=True)
            self.assertEqual(con.execute("SELECT resting_hr, hrv FROM sleep_metrics").fetchone(), (54, 48))
            con.close()
            self.assertEqual(len(list((root / "processed").glob("*.jpg"))), 1)
