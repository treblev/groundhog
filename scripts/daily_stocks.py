"""Run the daily stock pipeline and record one durable job lifecycle."""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb

from agent.events import record_event
from agent.runs import finish_run, start_run
from analytics import alerts, signals
from config.settings import DB_PATH
from ingestion.schema import init_db
from ingestion import stocks

JOB_NAME = "daily_stocks"


def _finish(run_id: str, status: str, error_text: str | None = None) -> None:
    con = duckdb.connect(str(DB_PATH))
    try:
        con.execute("BEGIN")
        finish_run(con, run_id, status, error_text)
        event_type = "job_completed" if status == "succeeded" else "job_failed"
        record_event(
            con,
            event_type=event_type,
            source="scripts.daily_stocks",
            subject_type="agent_run",
            subject_id=run_id,
            payload={
                "job_name": JOB_NAME,
                "status": status,
                "error_text": error_text,
            },
            dedupe_key=f"agent_run:{run_id}:{event_type}",
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def run() -> None:
    init_db(DB_PATH)
    con = duckdb.connect(str(DB_PATH))
    try:
        run_id = start_run(con, JOB_NAME)
    finally:
        con.close()

    try:
        print("--- Fetching prices ---")
        stocks.run()
        print("--- Computing signals ---")
        signals.run()
        print("--- Checking alerts ---")
        alerts.run()
    except Exception:
        error_text = traceback.format_exc()
        _finish(run_id, "failed", error_text)
        raise
    else:
        _finish(run_id, "succeeded")


if __name__ == "__main__":
    run()
