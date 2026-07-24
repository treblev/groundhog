"""Durable lifecycle records for Groundhog jobs."""
from uuid import uuid4

import duckdb


def start_run(con: duckdb.DuckDBPyConnection, job_name: str) -> str:
    """Create a running job record and return its ID."""
    run_id = str(uuid4())
    con.execute(
        """
        INSERT INTO agent_runs (id, job_name, status)
        VALUES (?, ?, 'running')
        """,
        [run_id, job_name],
    )
    return run_id


def finish_run(
    con: duckdb.DuckDBPyConnection,
    run_id: str,
    status: str,
    error_text: str | None = None,
) -> None:
    """Finalize a running job record as succeeded or failed."""
    if status not in {"succeeded", "failed"}:
        raise ValueError("Run status must be 'succeeded' or 'failed'.")

    con.execute(
        """
        UPDATE agent_runs
        SET status = ?, finished_at = CURRENT_TIMESTAMP, error_text = ?
        WHERE id = ? AND status = 'running'
        """,
        [status, error_text, run_id],
    )
