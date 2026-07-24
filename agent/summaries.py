"""Local-only LLM summaries over durable Groundhog facts."""
import hashlib
from datetime import date, timedelta

import duckdb
import httpx

from config.settings import OLLAMA_CHAT_URL, OLLAMA_SQL_MODEL


def _artifact_id(artifact_type: str, period_start: date, period_end: date) -> str:
    key = f"{artifact_type}:{period_start}:{period_end}"
    return hashlib.sha256(key.encode()).hexdigest()


def _ask_local_model(prompt: str) -> str:
    response = httpx.post(
        OLLAMA_CHAT_URL,
        json={
            "model": OLLAMA_SQL_MODEL,
            "stream": False,
            "messages": [
                {
                    "role": "system",
                    "content": "Summarize only the supplied local facts. Do not give financial advice or invent missing facts.",
                },
                {"role": "user", "content": prompt},
            ],
        },
        timeout=120.0,
    )
    response.raise_for_status()
    return response.json()["message"]["content"].strip()


def _store_artifact(
    con: duckdb.DuckDBPyConnection,
    artifact_type: str,
    period_start: date,
    period_end: date,
    content: str,
) -> str:
    artifact_id = _artifact_id(artifact_type, period_start, period_end)
    con.execute(
        """
        INSERT INTO derived_artifacts (id, artifact_type, period_start, period_end, content, model)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content, model = EXCLUDED.model
        """,
        [artifact_id, artifact_type, period_start, period_end, content, OLLAMA_SQL_MODEL],
    )
    return artifact_id


def generate_daily_summary(con: duckdb.DuckDBPyConnection, summary_date: date) -> str:
    rows = con.execute(
        """
        SELECT event_type, subject_type, subject_id, payload
        FROM events
        WHERE CAST(occurred_at AS DATE) = ?
        ORDER BY occurred_at
        """,
        [summary_date],
    ).fetchall()
    facts = "\n".join(str(row) for row in rows) or "No Groundhog events were recorded."
    content = _ask_local_model(f"Write a concise daily Groundhog summary for {summary_date}.\nFacts:\n{facts}")
    _store_artifact(con, "daily_summary", summary_date, summary_date, content)
    return content


def generate_weekly_review(con: duckdb.DuckDBPyConnection, week_end: date) -> str:
    week_start = week_end - timedelta(days=6)
    events = con.execute(
        """
        SELECT event_type, payload FROM events
        WHERE CAST(occurred_at AS DATE) BETWEEN ? AND ? ORDER BY occurred_at
        """,
        [week_start, week_end],
    ).fetchall()
    activities = con.execute(
        """
        SELECT activity_type, COUNT(*) FROM activities
        WHERE date BETWEEN ? AND ? GROUP BY activity_type ORDER BY activity_type
        """,
        [week_start, week_end],
    ).fetchall()
    facts = f"Events: {events}\nActivities: {activities}"
    content = _ask_local_model(f"Write a concise weekly Groundhog review for {week_start} through {week_end}.\nFacts:\n{facts}")
    _store_artifact(con, "weekly_review", week_start, week_end, content)
    return content


def prioritize_pending_outbox(con: duckdb.DuckDBPyConnection) -> int:
    """Apply a deterministic priority; delivery still requires OpenClaw review."""
    con.execute(
        """
        UPDATE outbox AS o
        SET priority = CASE
                WHEN e.event_type = 'stock_alert_created' THEN 100
                WHEN e.event_type = 'job_failed' THEN 80
                ELSE 10
            END,
            priority_reason = CASE
                WHEN e.event_type = 'stock_alert_created' THEN 'stock alert'
                WHEN e.event_type = 'job_failed' THEN 'job failure'
                ELSE 'standard event'
            END
        FROM events AS e
        WHERE o.event_id = e.id AND o.status = 'pending'
        """
    )
    return con.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending'").fetchone()[0]
