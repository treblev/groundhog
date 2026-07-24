"""Operational command surface for Groundhog's scheduled service tasks."""
import argparse
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import duckdb

from agent.events import record_event
from agent.runs import finish_run, start_run
from analytics import alerts, signals
from config.settings import DB_PATH
from ingestion import stocks
from ingestion.schema import init_db

DAILY_STOCKS_JOB = "daily_stocks"


def _finish_run(run_id: str, status: str, error_text: str | None = None) -> None:
    con = duckdb.connect(str(DB_PATH))
    try:
        con.execute("BEGIN")
        finish_run(con, run_id, status, error_text)
        event_type = "job_completed" if status == "succeeded" else "job_failed"
        record_event(
            con,
            event_type=event_type,
            source="groundhog_service",
            subject_type="agent_run",
            subject_id=run_id,
            payload={
                "job_name": DAILY_STOCKS_JOB,
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


def run_daily_stocks() -> None:
    """Run daily stock ingestion, analytics, and alert generation."""
    init_db(DB_PATH)
    con = duckdb.connect(str(DB_PATH))
    try:
        run_id = start_run(con, DAILY_STOCKS_JOB)
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
        _finish_run(run_id, "failed", error_text)
        raise
    else:
        _finish_run(run_id, "succeeded")


def get_status() -> dict:
    """Return a compact operational snapshot without contacting any model."""
    init_db(DB_PATH)
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        latest = con.execute(
            """
            SELECT job_name, status, started_at, finished_at, error_text
            FROM agent_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        pending_outbox_count = con.execute(
            "SELECT COUNT(*) FROM outbox WHERE status = 'pending'"
        ).fetchone()[0]
        recent_event_count = con.execute(
            "SELECT COUNT(*) FROM events WHERE occurred_at >= CURRENT_TIMESTAMP - INTERVAL 1 DAY"
        ).fetchone()[0]
    finally:
        con.close()

    return {
        "latest_run": (
            {
                "job_name": latest[0],
                "status": latest[1],
                "started_at": latest[2],
                "finished_at": latest[3],
                "error_text": latest[4],
            }
            if latest
            else None
        ),
        "pending_outbox_count": pending_outbox_count,
        "recent_event_count": recent_event_count,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run and inspect Groundhog service tasks.")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="Run a scheduled Groundhog task.")
    run_parser.add_argument("job", choices=["daily-stocks"])
    commands.add_parser("status", help="Print the latest service status as JSON.")
    args = parser.parse_args(argv)

    if args.command == "run":
        run_daily_stocks()
        return 0

    print(json.dumps(get_status(), default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
