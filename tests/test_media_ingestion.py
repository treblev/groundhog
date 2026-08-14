import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import duckdb
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion import schema
from ingestion import health
from ingestion import spending
from agent import request_trace
from scripts import media_ingestion


class MediaIngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "groundhog.duckdb"
        self.spool_dir = self.root / "spool"
        self.trace_dir_patch = patch.object(request_trace, "TRACE_DIR", self.root / "traces")
        self.trace_dir_patch.start()
        schema.init_db(self.db_path)

    def tearDown(self):
        self.trace_dir_patch.stop()
        self.temp_dir.cleanup()

    def _image(self, name: str = "activity.jpg", content: bytes = b"image") -> Path:
        image = self.root / name
        image.write_bytes(content)
        return image

    def _enqueue(self, image: Path | None = None, message_id: str = "message-1") -> dict:
        return media_ingestion.enqueue_activity(
            image or self._image(),
            "Morning run 8/12",
            "telegram",
            message_id,
            self.db_path,
            self.spool_dir,
        )

    def _row(self):
        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            return con.execute(
                """
                SELECT status, attempt_count, lease_owner, error_code, result_json::VARCHAR
                FROM media_ingestion_jobs
                """
            ).fetchone()
        finally:
            con.close()

    def _trace_records(self):
        paths = list((self.root / "traces").glob("*.jsonl"))
        return [
            json.loads(line)
            for path in paths
            for line in path.read_text().splitlines()
        ]

    def test_schema_migration_is_idempotent(self):
        schema.init_db(self.db_path)
        schema.init_db(self.db_path)
        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            table_count = con.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'media_ingestion_jobs'"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(table_count, 1)

    def test_schema_migration_preserves_activity_jobs_and_allows_expenses(self):
        legacy_path = self.root / "legacy.duckdb"
        con = duckdb.connect(str(legacy_path))
        try:
            con.execute("""
                CREATE TABLE media_ingestion_jobs (
                    id VARCHAR PRIMARY KEY,
                    content_hash VARCHAR NOT NULL,
                    source_channel VARCHAR NOT NULL,
                    source_message_id VARCHAR NOT NULL,
                    kind VARCHAR NOT NULL CHECK (kind IN ('activity')),
                    spool_path VARCHAR NOT NULL,
                    caption TEXT,
                    status VARCHAR NOT NULL CHECK (
                        status IN ('queued', 'processing', 'retry_wait', 'imported', 'needs_review')
                    ),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    available_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    lease_owner VARCHAR,
                    lease_expires_at TIMESTAMP,
                    error_code VARCHAR,
                    error_text TEXT,
                    result_json JSON,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    UNIQUE (source_channel, source_message_id, content_hash, kind)
                )
            """)
            con.execute("""
                INSERT INTO media_ingestion_jobs (
                    id, content_hash, source_channel, source_message_id,
                    kind, spool_path, caption, status
                ) VALUES ('legacy-job', 'hash', 'telegram', 'message',
                          'activity', '/spool/activity.jpg', NULL, 'queued')
            """)
        finally:
            con.close()

        schema.init_db(legacy_path)
        schema.init_db(legacy_path)
        con = duckdb.connect(str(legacy_path))
        try:
            self.assertEqual(
                con.execute("SELECT id, kind, status FROM media_ingestion_jobs").fetchall(),
                [("legacy-job", "activity", "queued")],
            )
            con.execute("""
                INSERT INTO media_ingestion_jobs (
                    id, content_hash, source_channel, source_message_id,
                    kind, spool_path, caption, status
                ) VALUES ('expense-job', 'expense-hash', 'telegram', 'expense-message',
                          'expense', '/spool/expense.jpg', '/expense', 'queued')
            """)
        finally:
            con.close()

    def test_enqueue_copies_exact_image_and_duplicate_returns_same_job(self):
        image = self._image(content=b"original attachment")

        first = self._enqueue(image)
        image.write_bytes(b"source changed later")
        duplicate_source = self._image("duplicate.jpg", b"original attachment")
        second = self._enqueue(duplicate_source)

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(Path(first["spool_path"]).read_bytes(), b"original attachment")
        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM media_ingestion_jobs").fetchone()[0], 1)
        finally:
            con.close()

    def test_same_content_from_distinct_message_creates_distinct_job(self):
        image = self._image(content=b"same screenshot")
        first = self._enqueue(image, "message-1")
        second = self._enqueue(image, "message-2")
        self.assertNotEqual(first["id"], second["id"])

    def test_successful_worker_run_records_result_and_one_outbox_event(self):
        result = self._enqueue()
        calls = []

        def processor(path, reference_date, date_hint):
            calls.append((path, reference_date, date_hint))
            return [{
                "type": "activity",
                "activity_type": "running",
                "date": "2026-08-12",
                "distance_miles": 3.1,
                "duration_seconds": 1800,
                "avg_pace_seconds_per_mile": 581,
                "avg_hr": 142,
            }]

        outcome = media_ingestion.process_one_job(
            self.db_path, "test-worker", processor, heartbeat_interval=1
        )

        self.assertEqual(outcome, {"id": result["id"], "status": "imported", "records": 1})
        self.assertEqual(calls[0][0], Path(result["spool_path"]))
        self.assertEqual(calls[0][2], "8/12")
        status, attempts, owner, error_code, result_json = self._row()
        self.assertEqual((status, attempts, owner, error_code), ("imported", 1, None, None))
        self.assertEqual(json.loads(result_json)[0]["activity_type"], "running")

        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            event = con.execute("SELECT event_type, payload::VARCHAR FROM events").fetchone()
            self.assertEqual(con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)
        finally:
            con.close()
        self.assertEqual(event[0], "upload_imported")
        self.assertIn("Imported activity: running", json.loads(event[1])["message"])
        lifecycle = [
            record for record in self._trace_records() if record["request_id"] == result["id"]
        ]
        self.assertEqual([record["type"] for record in lifecycle], [
            "request_start", "request_end"
        ])
        self.assertEqual(lifecycle[-1]["status"], "passed")

    def test_offline_end_to_end_uses_canonical_parser_and_inserts_activity(self):
        job = self._enqueue()
        response = json.dumps([{
            "type": "activity",
            "month_day": "08-12",
            "activity_type": "running",
            "distance_miles": 2.5,
            "duration_seconds": 1500,
            "avg_pace_seconds_per_mile": 600,
            "avg_hr": 135,
            "max_hr": 150,
            "calories": 250,
        }])

        with (
            patch.object(health, "DB_PATH", self.db_path),
            patch.object(health, "PROCESSED_DIR", self.root / "archive"),
            patch.object(health, "_query_ollama", return_value=response),
        ):
            outcome = media_ingestion.process_one_job(
                self.db_path,
                "test-worker",
                health.process_image,
                heartbeat_interval=1,
            )

        self.assertEqual(outcome, {"id": job["id"], "status": "imported", "records": 1})
        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            activity = con.execute(
                "SELECT date, activity_type, distance_miles, duration_seconds FROM activities"
            ).fetchone()
            outbox_count = con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(str(activity[0]), "2026-08-12")
        self.assertEqual(activity[1:], ("running", 2.5, 1500))
        self.assertEqual(outbox_count, 1)

    def test_expense_job_dispatches_spending_import_and_formats_outbox_result(self):
        image = self._image("wallet.jpg", b"wallet screenshot")
        job = media_ingestion.enqueue_media(
            "expense",
            image,
            "/expense",
            "telegram",
            "expense-message-1",
            self.db_path,
            self.spool_dir,
        )
        spending_result = {
            "transactions": [{
                "id": "abc123def4567890",
                "merchant": "Cafe",
                "amount": "4.46",
                "category": "dining",
            }],
            "skipped_pending": 0,
            "skipped_duplicates": 0,
            "skipped_invalid": 0,
        }

        with patch.object(
            media_ingestion.spending,
            "process_image",
            return_value=spending_result,
        ) as process_expense:
            outcome = media_ingestion.process_one_job(
                self.db_path,
                "test-worker",
                heartbeat_interval=1,
            )

        self.assertEqual(outcome, {"id": job["id"], "status": "imported", "records": 1})
        call = process_expense.call_args
        self.assertEqual(call.args[0], Path(job["spool_path"]))
        self.assertEqual(call.kwargs["db_path"], self.db_path)
        self.assertEqual(call.kwargs["processed_dir"], self.spool_dir)
        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            kind, result_json = con.execute(
                "SELECT kind, result_json::VARCHAR FROM media_ingestion_jobs WHERE id = ?",
                [job["id"]],
            ).fetchone()
            payload = json.loads(con.execute("SELECT payload::VARCHAR FROM events").fetchone()[0])
        finally:
            con.close()
        self.assertEqual(kind, "expense")
        self.assertEqual(json.loads(result_json), spending_result)
        self.assertIn("Imported 1 spending transaction — $4.46", payload["message"])
        self.assertIn("abc123de — Cafe: $4.46 (dining)", payload["message"])

    def test_unrecognized_expense_result_goes_to_review(self):
        job = media_ingestion.enqueue_media(
            "expense",
            self._image("wallet.jpg", b"not a transaction list"),
            "/expense",
            "telegram",
            "expense-message-2",
            self.db_path,
            self.spool_dir,
        )
        empty_result = {
            "transactions": [],
            "skipped_pending": 0,
            "skipped_duplicates": 0,
            "skipped_invalid": 0,
        }

        outcome = media_ingestion.process_one_job(
            self.db_path,
            "test-worker",
            lambda *_: empty_result,
        )

        self.assertEqual(outcome["status"], "needs_review")
        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            payload = json.loads(con.execute("SELECT payload::VARCHAR FROM events").fetchone()[0])
        finally:
            con.close()
        self.assertIn(f"Expense job {job['short_id']} could not be imported", payload["message"])

    def test_offline_expense_queue_inserts_spending_and_emits_outbox(self):
        job = media_ingestion.enqueue_media(
            "expense",
            self._image("wallet.jpg", b"wallet screenshot"),
            "/expense",
            "telegram",
            "expense-message-3",
            self.db_path,
            self.spool_dir,
        )
        response = json.dumps([{
            "merchant": "Cafe",
            "amount": 4.46,
            "visible_date_label": "Today",
            "payment_method": "Apple Pay",
            "category": "dining",
            "status": "posted",
        }])

        with patch.object(spending, "_query_ollama", return_value=response):
            outcome = media_ingestion.process_one_job(
                self.db_path,
                "test-worker",
                heartbeat_interval=1,
            )

        self.assertEqual(outcome, {"id": job["id"], "status": "imported", "records": 1})
        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            transaction = con.execute(
                "SELECT merchant, amount, payment_method, category FROM spending"
            ).fetchone()
            outbox_count = con.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(transaction, ("Cafe", Decimal("4.46"), "Apple Pay", "dining"))
        self.assertEqual(outbox_count, 1)

    def test_parse_failure_goes_directly_to_review_and_records_factual_event(self):
        result = self._enqueue()

        def processor(*_):
            raise ValueError("Could not resolve screenshot date")

        outcome = media_ingestion.process_one_job(self.db_path, "test-worker", processor)

        self.assertEqual(outcome["status"], "needs_review")
        self.assertEqual(self._row()[:4], ("needs_review", 1, None, "input_or_parse_error"))
        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            payload = json.loads(con.execute("SELECT payload::VARCHAR FROM events").fetchone()[0])
        finally:
            con.close()
        self.assertIn(result["short_id"], payload["message"])
        self.assertNotIn("Could not resolve screenshot date", payload["message"])

    def test_transport_failure_schedules_retry_without_terminal_message(self):
        self._enqueue()

        def processor(*_):
            raise httpx.ConnectError("Ollama offline")

        outcome = media_ingestion.process_one_job(self.db_path, "test-worker", processor)

        self.assertEqual(outcome["status"], "retry_wait")
        self.assertEqual(self._row()[:4], ("retry_wait", 1, None, "infrastructure_error"))
        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)
            available_at = con.execute("SELECT available_at FROM media_ingestion_jobs").fetchone()[0]
        finally:
            con.close()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        self.assertGreater(available_at, now + timedelta(seconds=40))

    def test_expired_processing_lease_is_reclaimed(self):
        result = self._enqueue()
        con = duckdb.connect(str(self.db_path))
        try:
            con.execute(
                """
                UPDATE media_ingestion_jobs
                SET status = 'processing', lease_owner = 'dead-worker',
                    lease_expires_at = CURRENT_TIMESTAMP - INTERVAL 1 MINUTE
                WHERE id = ?
                """,
                [result["id"]],
            )
        finally:
            con.close()

        job = media_ingestion.claim_next_job(self.db_path, "replacement-worker")

        self.assertEqual(job["id"], result["id"])
        self.assertEqual(job["lease_owner"], "replacement-worker")
        self.assertEqual(job["attempt_count"], 1)

    def test_heartbeat_prevents_duplicate_claim_then_stopped_worker_recovers(self):
        result = self._enqueue()
        first = media_ingestion.claim_next_job(self.db_path, "first-worker", lease_seconds=1)
        self.assertEqual(first["id"], result["id"])

        with media_ingestion.LeaseHeartbeat(
            self.db_path,
            result["id"],
            "first-worker",
            lease_seconds=1,
            interval_seconds=0.1,
        ):
            time.sleep(1.2)
            self.assertIsNone(
                media_ingestion.claim_next_job(self.db_path, "second-worker", lease_seconds=1)
            )

        time.sleep(1.1)
        recovered = media_ingestion.claim_next_job(
            self.db_path, "second-worker", lease_seconds=1
        )
        self.assertEqual(recovered["id"], result["id"])
        self.assertEqual(recovered["lease_owner"], "second-worker")

    def test_operator_retry_reuses_saved_job(self):
        result = self._enqueue()

        def processor(*_):
            raise ValueError("not an activity")

        media_ingestion.process_one_job(self.db_path, "test-worker", processor)
        retried = media_ingestion.retry_job(result["short_id"], self.db_path)

        self.assertEqual(retried["id"], result["id"])
        self.assertEqual(retried["status"], "queued")
        self.assertEqual(self._row()[0], "queued")

    def test_queue_health_reports_ready_age_and_expired_leases(self):
        self._enqueue()
        health = media_ingestion.queue_health(self.db_path)

        self.assertEqual(health["counts"]["queued"], 1)
        self.assertEqual(health["counts"]["processing"], 0)
        self.assertIsNotNone(health["oldest_ready_at"])
        self.assertGreaterEqual(health["oldest_ready_age_seconds"], 0)
        self.assertEqual(health["expired_processing_leases"], 0)
        self.assertIsNone(health["last_successful_completion_at"])

    def test_cleanup_removes_spool_and_archive_after_fifteen_days(self):
        result = self._enqueue()
        content_hash = Path(result["spool_path"]).stem
        archive_dir = self.root / "archive"
        archive_dir.mkdir()
        archive_path = archive_dir / f"{content_hash}.jpg"
        archive_path.write_bytes(b"archived image")
        con = duckdb.connect(str(self.db_path))
        try:
            con.execute(
                """
                UPDATE media_ingestion_jobs
                SET status = 'imported', completed_at = TIMESTAMP '2026-07-01 00:00:00'
                WHERE id = ?
                """,
                [result["id"]],
            )
        finally:
            con.close()

        cleanup = media_ingestion.cleanup_processed_images(
            self.db_path,
            archive_dir,
            now=datetime(2026, 8, 12),
        )

        self.assertEqual(cleanup["eligible_content_hashes"], 1)
        self.assertFalse(Path(result["spool_path"]).exists())
        self.assertFalse(archive_path.exists())
        self.assertEqual(cleanup["errors"], [])

    def test_cleanup_keeps_recent_and_unfinished_images(self):
        old = self._enqueue(message_id="old-import")
        recent = self._enqueue(self._image("recent.jpg", b"recent"), "recent-import")
        unfinished = self._enqueue(self._image("failed.jpg", b"failed"), "failed-import")
        con = duckdb.connect(str(self.db_path))
        try:
            con.execute(
                """
                UPDATE media_ingestion_jobs
                SET status = 'imported', completed_at = CASE
                    WHEN id = ? THEN TIMESTAMP '2026-07-01 00:00:00'
                    ELSE TIMESTAMP '2026-08-10 00:00:00'
                END
                WHERE id IN (?, ?)
                """,
                [old["id"], old["id"], recent["id"]],
            )
            con.execute(
                """
                UPDATE media_ingestion_jobs
                SET status = 'needs_review', completed_at = TIMESTAMP '2026-07-01 00:00:00'
                WHERE id = ?
                """,
                [unfinished["id"]],
            )
        finally:
            con.close()

        media_ingestion.cleanup_processed_images(
            self.db_path,
            self.root / "archive",
            now=datetime(2026, 8, 12),
        )

        self.assertFalse(Path(old["spool_path"]).exists())
        self.assertTrue(Path(recent["spool_path"]).exists())
        self.assertTrue(Path(unfinished["spool_path"]).exists())

    def test_cleanup_keeps_shared_image_while_any_matching_job_is_unfinished(self):
        image = self._image(content=b"shared content")
        imported = self._enqueue(image, "imported-message")
        review = self._enqueue(image, "review-message")
        self.assertEqual(imported["spool_path"], review["spool_path"])
        con = duckdb.connect(str(self.db_path))
        try:
            con.execute(
                """
                UPDATE media_ingestion_jobs
                SET status = 'imported', completed_at = TIMESTAMP '2026-07-01 00:00:00'
                WHERE id = ?
                """,
                [imported["id"]],
            )
            con.execute(
                """
                UPDATE media_ingestion_jobs
                SET status = 'needs_review', completed_at = TIMESTAMP '2026-07-01 00:00:00'
                WHERE id = ?
                """,
                [review["id"]],
            )
        finally:
            con.close()

        cleanup = media_ingestion.cleanup_processed_images(
            self.db_path,
            self.root / "archive",
            now=datetime(2026, 8, 12),
        )

        self.assertEqual(cleanup["eligible_content_hashes"], 0)
        self.assertTrue(Path(imported["spool_path"]).exists())


if __name__ == "__main__":
    unittest.main()
