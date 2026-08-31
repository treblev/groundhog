"""Local-only LLM summaries over durable Groundhog facts."""
import hashlib
import time
from datetime import date, datetime, timezone

import duckdb
import httpx

from config.settings import OLLAMA_CHAT_URL, OLLAMA_SQL_MODEL
from agent.request_trace import record_llm_call


def _artifact_id(artifact_type: str, period_start: date, period_end: date) -> str:
    key = f"{artifact_type}:{period_start}:{period_end}"
    return hashlib.sha256(key.encode()).hexdigest()


def _ask_local_model(prompt: str) -> str:
    system_prompt = "Summarize only the supplied local facts. Do not give financial advice or invent missing facts."
    started_at = datetime.now(timezone.utc)
    monotonic_started = time.monotonic()
    try:
        response = httpx.post(
            OLLAMA_CHAT_URL,
            json={
                "model": OLLAMA_SQL_MODEL,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=120.0,
        )
        response.raise_for_status()
        content = response.json()["message"]["content"].strip()
    except Exception as error:
        record_llm_call(
            started_at=started_at,
            monotonic_started=monotonic_started,
            model=OLLAMA_SQL_MODEL,
            prompt={"system": system_prompt, "user": prompt},
            error=error,
            metadata={"operation": "summary_generation"},
        )
        raise
    record_llm_call(
        started_at=started_at,
        monotonic_started=monotonic_started,
        model=OLLAMA_SQL_MODEL,
        prompt={"system": system_prompt, "user": prompt},
        response=content,
        metadata={"operation": "summary_generation"},
    )
    return content


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
    """Compatibility wrapper for the structured weekly reviewer."""
    from agent.weekly_reviewer import generate_weekly_review as _generate_weekly_review

    return _generate_weekly_review(con, week_end)


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
