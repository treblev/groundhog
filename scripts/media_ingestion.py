"""Durable, asynchronous ingestion for media received by OpenClaw."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo

import duckdb
import httpx

from agent.events import event_id_for, record_event
from agent.outbox import enqueue_event
from agent.request_trace import RequestTrace, use_trace
from config.settings import DB_PATH, OPENCLAW_MEDIA_STATE_PATH
from ingestion.health import IMAGE_EXTS, PROCESSED_DIR, process_image
from ingestion import spending
from ingestion.schema import init_db

PHOENIX = ZoneInfo("America/Phoenix")
JOB_STATUSES = {"queued", "processing", "retry_wait", "imported", "needs_review"}
JOB_KINDS = {"activity", "expense"}
RETRY_DELAYS_SECONDS = (60, 300, 900)
DEFAULT_LEASE_SECONDS = 20 * 60
HEARTBEAT_SECONDS = 60
PROCESSED_IMAGE_RETENTION_DAYS = 15
CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _default_spool_dir() -> Path:
    return OPENCLAW_MEDIA_STATE_PATH.parent / "media_spool"


def _content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _job_id(channel: str, message_id: str, kind: str, content_hash: str) -> str:
    identity = f"{channel}\0{message_id}\0{kind}\0{content_hash}"
    return hashlib.sha256(identity.encode()).hexdigest()


def _copy_to_spool(image_path: Path, spool_dir: Path, content_hash: str) -> Path:
    spool_dir.mkdir(parents=True, exist_ok=True)
    destination = spool_dir / f"{content_hash}{image_path.suffix.lower()}"
    if destination.exists():
        return destination

    temporary = spool_dir / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copy2(image_path, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def enqueue_media(
    kind: str,
    image_path: Path,
    caption: str | None,
    source_channel: str,
    source_message_id: str,
    db_path: Path = DB_PATH,
    spool_dir: Path | None = None,
) -> dict:
    """Spool an exact attachment and idempotently create its durable job."""
    if kind not in JOB_KINDS:
        raise ValueError(f"Unsupported media job kind: {kind}")
    image_path = image_path.resolve()
    if not image_path.is_file():
        raise FileNotFoundError(image_path)
    if image_path.suffix.lower() not in IMAGE_EXTS:
        raise ValueError(f"Unsupported image type: {image_path.suffix or '(none)'}")
    if not source_channel.strip() or not source_message_id.strip():
        raise ValueError("Source channel and message ID are required.")

    content_hash = _content_hash(image_path)
    job_id = _job_id(source_channel, source_message_id, kind, content_hash)
    resolved_spool_dir = spool_dir or _default_spool_dir()
    spool_path = resolved_spool_dir / f"{content_hash}{image_path.suffix.lower()}"

    try:
        init_db(db_path)
    except Exception as error:
        trace = RequestTrace(
            operation=f"{kind}_import",
            source=source_channel,
            request_id=job_id,
        ).start(
            job_id=job_id,
            content_hash=content_hash,
            source_message_id=source_message_id,
            source_image_path=image_path,
            caption=caption,
        )
        trace.end("failed", str(error), phase="enqueue")
        raise

    con = duckdb.connect(str(db_path))
    trace = None
    try:
        existing = con.execute(
            "SELECT status FROM media_ingestion_jobs WHERE id = ?", [job_id]
        ).fetchone()
        if existing is None:
            trace = RequestTrace(
                operation=f"{kind}_import",
                source=source_channel,
                request_id=job_id,
            ).start(
                job_id=job_id,
                content_hash=content_hash,
                source_message_id=source_message_id,
                source_image_path=image_path,
                caption=caption,
            )
            spool_path = _copy_to_spool(image_path, resolved_spool_dir, content_hash)
            con.execute(
                """
                INSERT INTO media_ingestion_jobs (
                    id, content_hash, source_channel, source_message_id,
                    kind, spool_path, caption, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued')
                ON CONFLICT DO NOTHING
                """,
                [
                    job_id,
                    content_hash,
                    source_channel,
                    source_message_id,
                    kind,
                    str(spool_path),
                    caption,
                ],
            )
        row = con.execute(
            "SELECT status FROM media_ingestion_jobs WHERE id = ?", [job_id]
        ).fetchone()
        if row is None:
            raise RuntimeError("Job enqueue did not produce a durable row.")
        return {
            "id": job_id,
            "short_id": job_id[:8],
            "status": row[0],
            "created": existing is None,
            "spool_path": str(spool_path),
        }
    except Exception as error:
        if trace is not None:
            trace.end("failed", str(error), phase="enqueue")
        raise
    finally:
        con.close()


def enqueue_activity(
    image_path: Path,
    caption: str | None,
    source_channel: str,
    source_message_id: str,
    db_path: Path = DB_PATH,
    spool_dir: Path | None = None,
) -> dict:
    """Backward-compatible activity enqueue entry point."""
    return enqueue_media(
        "activity",
        image_path,
        caption,
        source_channel,
        source_message_id,
        db_path,
        spool_dir,
    )


def _row_to_job(description, row) -> dict | None:
    if row is None:
        return None
    return {column[0]: value for column, value in zip(description, row)}


def claim_next_job(
    db_path: Path = DB_PATH,
    worker_id: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> dict | None:
    """Claim the oldest ready job, including work abandoned after lease expiry."""
    worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    now = _utcnow()
    lease_expires_at = now + timedelta(seconds=lease_seconds)
    con = duckdb.connect(str(db_path))
    try:
        con.execute("BEGIN TRANSACTION")
        con.execute(
            """
            UPDATE media_ingestion_jobs
            SET status = 'queued', lease_owner = NULL, lease_expires_at = NULL,
                updated_at = ?
            WHERE status = 'processing' AND lease_expires_at <= ?
            """,
            [now, now],
        )
        candidate = con.execute(
            """
            SELECT id FROM media_ingestion_jobs
            WHERE status IN ('queued', 'retry_wait') AND available_at <= ?
            ORDER BY available_at, created_at, id
            LIMIT 1
            """,
            [now],
        ).fetchone()
        if candidate is None:
            con.execute("COMMIT")
            return None
        job_id = candidate[0]
        con.execute(
            """
            UPDATE media_ingestion_jobs
            SET status = 'processing', attempt_count = attempt_count + 1,
                lease_owner = ?, lease_expires_at = ?, updated_at = ?,
                error_code = NULL, error_text = NULL
            WHERE id = ?
            """,
            [worker_id, lease_expires_at, now, job_id],
        )
        cursor = con.execute("SELECT * FROM media_ingestion_jobs WHERE id = ?", [job_id])
        job = _row_to_job(cursor.description, cursor.fetchone())
        con.execute("COMMIT")
        return job
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


class LeaseHeartbeat:
    """Keep a claimed job owned while a local vision call is still running."""

    def __init__(
        self,
        db_path: Path,
        job_id: str,
        worker_id: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        interval_seconds: int = HEARTBEAT_SECONDS,
    ):
        self.db_path = db_path
        self.job_id = job_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join(timeout=self.interval_seconds + 1)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            now = _utcnow()
            try:
                con = duckdb.connect(str(self.db_path))
                try:
                    con.execute(
                        """
                        UPDATE media_ingestion_jobs
                        SET lease_expires_at = ?, updated_at = ?
                        WHERE id = ? AND status = 'processing' AND lease_owner = ?
                        """,
                        [
                            now + timedelta(seconds=self.lease_seconds),
                            now,
                            self.job_id,
                            self.worker_id,
                        ],
                    )
                finally:
                    con.close()
            except duckdb.Error:
                # A later heartbeat can recover from a short-lived writer conflict.
                continue


def _date_hint_from_caption(caption: str | None) -> str | None:
    if not caption:
        return None
    tokens = re.findall(r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2})(?!\d)", caption)
    for token in tokens:
        try:
            date.fromisoformat(token)
            return token
        except ValueError:
            try:
                month, day = (int(part) for part in re.split(r"[/-]", token))
                date(2000, month, day)
                return token
            except ValueError:
                continue
    return None


def _format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "not detected"
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _format_pace(seconds: int | None, unit: str) -> str:
    if seconds is None:
        return "not detected"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}:{seconds:02d}/{unit}"


def _activity_success_message(records: list[dict]) -> str:
    summaries = []
    for activity in records:
        activity_type = activity.get("activity_type", "other")
        date_text = activity.get("date", "unknown date")
        if activity_type == "pool swim":
            distance = activity.get("pool_distance")
            distance_unit = activity.get("pool_distance_unit") or "units"
            distance_text = f"{distance} {distance_unit}" if distance is not None else "not detected"
            pace_unit = f"100 {activity.get('swim_pace_unit') or 'units'}"
            pace = _format_pace(activity.get("swim_pace_seconds_per_100"), pace_unit)
        else:
            distance = activity.get("distance_miles")
            distance_text = f"{distance} mi" if distance is not None else "not detected"
            pace = _format_pace(activity.get("avg_pace_seconds_per_mile"), "mi")
        summaries.append(
            f"Imported activity: {activity_type} ({date_text}). Distance: {distance_text}; "
            f"duration: {_format_duration(activity.get('duration_seconds'))}; "
            f"avg pace: {pace}; avg HR: {activity.get('avg_hr', 'not detected')} bpm."
        )
    return "\n".join(summaries) or "Activity import completed, but no activity details were returned."


def _count_label(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or f'{singular}s')}"


def _expense_success_message(result: dict) -> str:
    rows = result.get("transactions") if isinstance(result, dict) else []
    rows = rows if isinstance(rows, list) else []
    pending = int(result.get("skipped_pending", 0))
    duplicates = int(result.get("skipped_duplicates", 0))
    invalid = int(result.get("skipped_invalid", 0))
    skipped = []
    if pending:
        skipped.append(_count_label(pending, "pending charge"))
    if duplicates:
        skipped.append(_count_label(duplicates, "existing duplicate"))
    if invalid:
        skipped.append(_count_label(invalid, "invalid row"))
    if not rows:
        if pending and not duplicates and not invalid:
            return f"No posted transactions imported; skipped {_count_label(pending, 'pending charge')}."
        if duplicates and not pending and not invalid:
            return f"No new transactions; skipped {_count_label(duplicates, 'existing duplicate')}."
        if skipped:
            return f"No new transactions imported; skipped {', '.join(skipped)}."
        return "Could not identify any supported transactions with a merchant, amount, and date."
    total = sum(Decimal(str(row["amount"])) for row in rows)
    lines = [
        f"{row['id'][:8]} — {row['merchant']}: ${Decimal(str(row['amount'])):.2f} ({row['category']})"
        for row in rows
    ]
    transaction_lines = "\n".join(lines)
    skipped_text = f"\nSkipped {', '.join(skipped)}." if skipped else ""
    return (
        f"Imported {len(rows)} spending transaction{'s' if len(rows) != 1 else ''} — ${total:.2f}\n"
        f"{transaction_lines}{skipped_text}"
    )


def _success_message(job: dict, result: list[dict] | dict) -> str:
    if job["kind"] == "expense":
        return _expense_success_message(result)
    return _activity_success_message(result)


def _result_count(job: dict, result: list[dict] | dict) -> int:
    if job["kind"] == "expense":
        return len(result.get("transactions", []))
    return len(result)


def _validate_result(job: dict, result: list[dict] | dict) -> None:
    if job["kind"] == "expense":
        if not isinstance(result, dict):
            raise ValueError("Spending importer returned an invalid result.")
        rows = result.get("transactions")
        if not isinstance(rows, list):
            raise ValueError("Spending importer returned an invalid transaction list.")
        skipped = sum(
            int(result.get(key, 0))
            for key in ("skipped_pending", "skipped_duplicates", "skipped_invalid")
        )
        if not rows and not skipped:
            raise ValueError(
                "Could not identify any supported transactions with a merchant, amount, and date."
            )
    elif not isinstance(result, list) or not result:
        raise ValueError("Activity importer returned no activity records.")


def _retryable_error(error: Exception) -> bool:
    if isinstance(error, (httpx.TransportError, TimeoutError, ConnectionError)):
        return True
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code in {408, 429, 502, 503, 504}
    if isinstance(error, duckdb.Error):
        text = str(error).lower()
        return any(token in text for token in ("lock", "conflict", "temporar", "busy"))
    return False


def _record_terminal_event(
    con: duckdb.DuckDBPyConnection,
    job: dict,
    message: str,
    success: bool,
) -> None:
    dedupe_key = f"media_ingestion:{job['id']}:attempt:{job['attempt_count']}:terminal"
    event_type = "upload_imported" if success else "job_failed"
    record_event(
        con,
        event_type=event_type,
        source="scripts.media_ingestion",
        subject_type="media_ingestion_job",
        subject_id=job["id"],
        payload={"kind": job["kind"], "job_id": job["id"], "message": message},
        dedupe_key=dedupe_key,
    )
    enqueue_event(con, event_id_for(dedupe_key))


def _finish_success(db_path: Path, job: dict, result: list[dict] | dict) -> None:
    now = _utcnow()
    message = _success_message(job, result)
    con = duckdb.connect(str(db_path))
    try:
        con.execute("BEGIN TRANSACTION")
        con.execute(
            """
            UPDATE media_ingestion_jobs
            SET status = 'imported', result_json = ?, completed_at = ?, updated_at = ?,
                lease_owner = NULL, lease_expires_at = NULL,
                error_code = NULL, error_text = NULL
            WHERE id = ? AND status = 'processing' AND lease_owner = ?
            """,
            [json.dumps(result, default=str, sort_keys=True), now, now, job["id"], job["lease_owner"]],
        )
        _record_terminal_event(con, job, message, success=True)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def _finish_failure(db_path: Path, job: dict, error: Exception) -> str:
    now = _utcnow()
    retryable = _retryable_error(error)
    attempt = int(job["attempt_count"])
    has_retry = retryable and attempt <= len(RETRY_DELAYS_SECONDS)
    status = "retry_wait" if has_retry else "needs_review"
    delay = RETRY_DELAYS_SECONDS[attempt - 1] if has_retry else 0
    available_at = now + timedelta(seconds=delay)
    error_code = "infrastructure_error" if retryable else "input_or_parse_error"
    error_text = str(error)[:2000]

    con = duckdb.connect(str(db_path))
    try:
        con.execute("BEGIN TRANSACTION")
        con.execute(
            """
            UPDATE media_ingestion_jobs
            SET status = ?, available_at = ?, updated_at = ?, completed_at = ?,
                lease_owner = NULL, lease_expires_at = NULL,
                error_code = ?, error_text = ?
            WHERE id = ? AND status = 'processing' AND lease_owner = ?
            """,
            [
                status,
                available_at,
                now,
                now if status == "needs_review" else None,
                error_code,
                error_text,
                job["id"],
                job["lease_owner"],
            ],
        )
        if status == "needs_review":
            label = "Expense" if job["kind"] == "expense" else "Activity"
            message = (
                f"{label} job {job['id'][:8]} could not be imported. "
                f"It is saved for review; retry with `python -m scripts.media_ingestion retry --job {job['id'][:8]}`."
            )
            _record_terminal_event(con, job, message, success=False)
        con.execute("COMMIT")
        return status
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def process_one_job(
    db_path: Path = DB_PATH,
    worker_id: str | None = None,
    processor: Callable[[Path, date | None, str | None], list[dict] | dict] | None = None,
    heartbeat_interval: int = HEARTBEAT_SECONDS,
) -> dict | None:
    """Claim and process one job; return its resulting state for tests and operators."""
    worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    job = claim_next_job(db_path, worker_id)
    if job is None:
        return None

    trace = RequestTrace(
        operation=f"{job['kind']}_import",
        source=job["source_channel"],
        request_id=job["id"],
        started_at=job["created_at"],
    )
    spool_path = Path(job["spool_path"])
    with use_trace(trace):
        try:
            reference_date = datetime.fromtimestamp(spool_path.stat().st_mtime, PHOENIX).date()
            date_hint = _date_hint_from_caption(job.get("caption"))
            selected_processor = processor
            if selected_processor is None:
                if job["kind"] == "expense":
                    selected_processor = lambda path, upload_date, _: spending.process_image(
                        path,
                        upload_date,
                        db_path=db_path,
                        processed_dir=path.parent,
                    )
                else:
                    selected_processor = process_image
            with LeaseHeartbeat(
                db_path,
                job["id"],
                worker_id,
                interval_seconds=heartbeat_interval,
            ):
                result = selected_processor(spool_path, reference_date, date_hint)
            _validate_result(job, result)
            _finish_success(db_path, job, result)
            record_count = _result_count(job, result)
            trace.end("passed", job_id=job["id"], records=record_count)
            return {"id": job["id"], "status": "imported", "records": record_count}
        except Exception as error:
            status = _finish_failure(db_path, job, error)
            if status == "needs_review":
                trace.end("failed", str(error), job_id=job["id"], job_status=status)
            return {"id": job["id"], "status": status, "error": str(error)}


def retry_job(job_prefix: str, db_path: Path = DB_PATH) -> dict:
    con = duckdb.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT id, status FROM media_ingestion_jobs WHERE id LIKE ? ORDER BY id",
            [f"{job_prefix}%"],
        ).fetchall()
        if not rows:
            raise ValueError(f"No job matches {job_prefix!r}.")
        if len(rows) > 1:
            raise ValueError(f"Job prefix {job_prefix!r} is ambiguous.")
        job_id, status = rows[0]
        if status != "needs_review":
            raise ValueError(f"Job {job_id[:8]} is {status}, not needs_review.")
        con.execute(
            """
            UPDATE media_ingestion_jobs
            SET status = 'queued', available_at = CURRENT_TIMESTAMP,
                completed_at = NULL, updated_at = CURRENT_TIMESTAMP,
                error_code = NULL, error_text = NULL
            WHERE id = ?
            """,
            [job_id],
        )
        return {"id": job_id, "short_id": job_id[:8], "status": "queued"}
    finally:
        con.close()


def job_status(db_path: Path = DB_PATH, job_prefix: str | None = None) -> list[dict]:
    init_db(db_path)
    con = duckdb.connect(str(db_path))
    try:
        where = "WHERE id LIKE ?" if job_prefix else ""
        parameters = [f"{job_prefix}%"] if job_prefix else []
        cursor = con.execute(
            f"""
            SELECT id, kind, status, attempt_count, available_at, lease_owner,
                   lease_expires_at, error_code, error_text, created_at, updated_at, completed_at
            FROM media_ingestion_jobs {where}
            ORDER BY created_at DESC
            """,
            parameters,
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        con.close()


def queue_health(db_path: Path = DB_PATH) -> dict:
    """Return queue facts suitable for an operator or health check."""
    init_db(db_path)
    now = _utcnow()
    con = duckdb.connect(str(db_path))
    try:
        counts = {
            status: count
            for status, count in con.execute(
                "SELECT status, COUNT(*) FROM media_ingestion_jobs GROUP BY status"
            ).fetchall()
        }
        oldest_ready = con.execute(
            """
            SELECT MIN(created_at) FROM media_ingestion_jobs
            WHERE status IN ('queued', 'retry_wait')
            """
        ).fetchone()[0]
        expired_leases = con.execute(
            """
            SELECT COUNT(*) FROM media_ingestion_jobs
            WHERE status = 'processing' AND lease_expires_at <= ?
            """,
            [now],
        ).fetchone()[0]
        last_completion = con.execute(
            "SELECT MAX(completed_at) FROM media_ingestion_jobs WHERE status = 'imported'"
        ).fetchone()[0]
        return {
            "counts": {status: counts.get(status, 0) for status in sorted(JOB_STATUSES)},
            "oldest_ready_at": oldest_ready,
            "oldest_ready_age_seconds": (
                max(0, int((now - oldest_ready).total_seconds())) if oldest_ready else None
            ),
            "expired_processing_leases": expired_leases,
            "last_successful_completion_at": last_completion,
        }
    finally:
        con.close()


def cleanup_processed_images(
    db_path: Path = DB_PATH,
    processed_dir: Path = PROCESSED_DIR,
    retention_days: int = PROCESSED_IMAGE_RETENTION_DAYS,
    now: datetime | None = None,
) -> dict:
    """Remove old spool/archive copies only when every matching job succeeded."""
    if retention_days < 1:
        raise ValueError("Processed-image retention must be at least one day.")
    init_db(db_path)
    cutoff = (now or _utcnow()) - timedelta(days=retention_days)
    con = duckdb.connect(str(db_path))
    try:
        rows = con.execute(
            """
            SELECT content_hash, LIST(DISTINCT spool_path) AS spool_paths
            FROM media_ingestion_jobs
            GROUP BY content_hash
            HAVING COUNT(*) FILTER (WHERE status != 'imported') = 0
               AND MAX(completed_at) < ?
            """,
            [cutoff],
        ).fetchall()
    finally:
        con.close()

    removed = []
    errors = []
    for content_hash, spool_paths in rows:
        candidates = {Path(path) for path in spool_paths}
        if processed_dir.is_dir():
            candidates.update(
                path
                for path in processed_dir.glob(f"{content_hash}.*")
                if path.suffix.lower() in IMAGE_EXTS
            )
        for path in sorted(candidates):
            try:
                if path.is_file():
                    path.unlink()
                    removed.append(str(path))
            except OSError as error:
                errors.append({"path": str(path), "error": str(error)})
    return {
        "eligible_content_hashes": len(rows),
        "removed_files": removed,
        "errors": errors,
        "cutoff": cutoff,
    }


def run_worker(db_path: Path, once: bool, poll_seconds: float) -> None:
    init_db(db_path)
    next_cleanup_at = 0.0
    while True:
        monotonic_now = time.monotonic()
        if monotonic_now >= next_cleanup_at:
            cleanup = cleanup_processed_images(db_path)
            if cleanup["removed_files"] or cleanup["errors"]:
                print(json.dumps({"cleanup": cleanup}, default=str, sort_keys=True), flush=True)
            next_cleanup_at = monotonic_now + CLEANUP_INTERVAL_SECONDS
        result = process_one_job(db_path)
        if result is not None:
            print(json.dumps(result, default=str, sort_keys=True), flush=True)
        if once:
            return
        if result is None:
            time.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Durable Groundhog media-ingestion queue.")
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    enqueue_parser = subparsers.add_parser("enqueue")
    enqueue_parser.add_argument("--kind", choices=sorted(JOB_KINDS), default="activity")
    enqueue_parser.add_argument("--image", type=Path, required=True)
    enqueue_parser.add_argument("--caption")
    enqueue_parser.add_argument("--channel", required=True)
    enqueue_parser.add_argument("--message-id", required=True)
    enqueue_parser.add_argument("--spool-dir", type=Path)

    worker_parser = subparsers.add_parser("worker")
    worker_parser.add_argument("--once", action="store_true")
    worker_parser.add_argument("--poll-seconds", type=float, default=2.0)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--job")

    retry_parser = subparsers.add_parser("retry")
    retry_parser.add_argument("--job", required=True)

    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument(
        "--retention-days", type=int, default=PROCESSED_IMAGE_RETENTION_DAYS
    )

    args = parser.parse_args()
    if args.command == "enqueue":
        result = enqueue_media(
            args.kind,
            args.image,
            args.caption,
            args.channel,
            args.message_id,
            args.db_path,
            args.spool_dir,
        )
    elif args.command == "worker":
        run_worker(args.db_path, args.once, args.poll_seconds)
        return
    elif args.command == "retry":
        result = retry_job(args.job, args.db_path)
    elif args.command == "cleanup":
        result = cleanup_processed_images(
            args.db_path, retention_days=args.retention_days
        )
    else:
        result = {
            "health": queue_health(args.db_path),
            "jobs": job_status(args.db_path, args.job),
        }
    print(json.dumps(result, default=str, sort_keys=True))


if __name__ == "__main__":
    main()
