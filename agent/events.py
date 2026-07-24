"""Write idempotent, durable facts to Groundhog's event stream."""
import hashlib
import json
from collections.abc import Mapping

import duckdb

EVENT_TYPES = {
    "job_completed",
    "job_failed",
    "stock_signal_flipped",
    "stock_alert_created",
    "sleep_data_imported",
    "workout_data_imported",
    "health_metric_changed",
}


def event_id_for(dedupe_key: str) -> str:
    """Return the deterministic ID assigned to a dedupe key."""
    return hashlib.sha256(dedupe_key.encode()).hexdigest()


def record_event(
    con: duckdb.DuckDBPyConnection,
    event_type: str,
    source: str,
    subject_type: str,
    subject_id: str,
    payload: Mapping,
    dedupe_key: str,
) -> bool:
    """Record one event, returning True only when it was newly inserted."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unsupported event type: {event_type}")
    if not isinstance(payload, Mapping):
        raise TypeError("Event payload must be a mapping.")

    before = con.execute(
        "SELECT COUNT(*) FROM events WHERE dedupe_key = ?", [dedupe_key]
    ).fetchone()[0]
    con.execute(
        """
        INSERT INTO events
            (id, event_type, source, subject_type, subject_id, dedupe_key, payload)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (dedupe_key) DO NOTHING
        """,
        [
            event_id_for(dedupe_key),
            event_type,
            source,
            subject_type,
            subject_id,
            dedupe_key,
            json.dumps(dict(payload), sort_keys=True, default=str),
        ],
    )
    after = con.execute(
        "SELECT COUNT(*) FROM events WHERE dedupe_key = ?", [dedupe_key]
    ).fetchone()[0]
    return after > before
