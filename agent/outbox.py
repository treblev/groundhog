"""Queue Groundhog events for delivery without owning the delivery channel."""
import hashlib

import duckdb

OUTBOX_STATUSES = {"pending", "delivered", "failed", "discarded"}


def _outbox_id(event_id: str) -> str:
    return hashlib.sha256(f"outbox:{event_id}".encode()).hexdigest()


def enqueue_event(con: duckdb.DuckDBPyConnection, event_id: str) -> bool:
    """Create one pending delivery item for an existing event."""
    exists = con.execute("SELECT 1 FROM events WHERE id = ?", [event_id]).fetchone()
    if exists is None:
        raise ValueError(f"Cannot enqueue missing event: {event_id}")

    before = con.execute(
        "SELECT COUNT(*) FROM outbox WHERE event_id = ?", [event_id]
    ).fetchone()[0]
    con.execute(
        """
        INSERT INTO outbox (id, event_id, status)
        VALUES (?, ?, 'pending')
        ON CONFLICT (event_id) DO NOTHING
        """,
        [_outbox_id(event_id), event_id],
    )
    after = con.execute(
        "SELECT COUNT(*) FROM outbox WHERE event_id = ?", [event_id]
    ).fetchone()[0]
    return after > before


def set_outbox_status(
    con: duckdb.DuckDBPyConnection,
    outbox_id: str,
    status: str,
    delivery_error: str | None = None,
) -> None:
    """Set an outbox delivery status and timestamp successful deliveries."""
    if status not in OUTBOX_STATUSES:
        raise ValueError(f"Unsupported outbox status: {status}")

    if status == "delivered":
        con.execute(
            """
            UPDATE outbox
            SET status = ?, delivered_at = COALESCE(delivered_at, CURRENT_TIMESTAMP), delivery_error = NULL
            WHERE id = ?
            """,
            [status, outbox_id],
        )
    else:
        con.execute(
            """
            UPDATE outbox
            SET status = ?, delivery_error = ?
            WHERE id = ?
            """,
            [status, delivery_error, outbox_id],
        )
