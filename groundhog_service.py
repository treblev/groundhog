"""Operational command surface for Groundhog's scheduled service tasks."""
import argparse
import json
import signal
import sys
import traceback
from datetime import date, datetime
from pathlib import Path
from threading import Event
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import duckdb

from agent.events import record_event
from agent.runs import finish_run, start_run
from agent.summaries import generate_daily_summary, generate_weekly_review, prioritize_pending_outbox
from analytics import alerts, signals
from config.settings import DB_PATH
from ingestion import stocks
from ingestion.schema import init_db

DAILY_STOCKS_JOB = "daily_stocks"
PHOENIX = ZoneInfo("America/Phoenix")


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


def due_tasks(now: datetime, last_daily_stocks_run: datetime | None) -> list[str]:
    """Return scheduled tasks due at a Phoenix-local time."""
    if now.tzinfo is None:
        raise ValueError("now must include a timezone.")
    local_now = now.astimezone(PHOENIX)
    if last_daily_stocks_run is not None and last_daily_stocks_run.tzinfo is None:
        last_daily_stocks_run = last_daily_stocks_run.replace(tzinfo=PHOENIX)
    has_run_today = (
        last_daily_stocks_run is not None
        and last_daily_stocks_run.astimezone(PHOENIX).date() == local_now.date()
    )
    if local_now.weekday() < 5 and local_now.hour >= 17 and not has_run_today:
        return ["daily-stocks"]
    return []


def _last_daily_stocks_run() -> datetime | None:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        row = con.execute(
            """
            SELECT MAX(started_at)
            FROM agent_runs
            WHERE job_name = ?
            """,
            [DAILY_STOCKS_JOB],
        ).fetchone()
        return row[0] if row and row[0] else None
    finally:
        con.close()


def run_daemon(poll_seconds: int = 60) -> None:
    """Poll for due work until systemd asks the process to stop."""
    if poll_seconds < 1:
        raise ValueError("poll_seconds must be at least 1.")

    init_db(DB_PATH)
    stop_event = Event()

    def stop(_signum, _frame) -> None:
        print("Groundhog daemon stopping.")
        stop_event.set()

    previous_term = signal.signal(signal.SIGTERM, stop)
    previous_int = signal.signal(signal.SIGINT, stop)
    print(f"Groundhog daemon started; polling every {poll_seconds} seconds.")
    try:
        while not stop_event.is_set():
            for task in due_tasks(datetime.now(PHOENIX), _last_daily_stocks_run()):
                if task == "daily-stocks":
                    run_daily_stocks()
            stop_event.wait(poll_seconds)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run and inspect Groundhog service tasks.")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="Run a scheduled Groundhog task.")
    run_parser.add_argument("job", choices=["daily-stocks"])
    commands.add_parser("status", help="Print the latest service status as JSON.")
    daemon_parser = commands.add_parser("daemon", help="Poll for and run due Groundhog tasks.")
    daemon_parser.add_argument("--poll-seconds", type=int, default=60)
    summary_parser = commands.add_parser("summarize", help="Generate a local derived summary.")
    summary_parser.add_argument("kind", choices=["daily", "weekly"])
    summary_parser.add_argument("--date", required=True, type=date.fromisoformat)
    args = parser.parse_args(argv)

    if args.command == "run":
        run_daily_stocks()
        return 0

    if args.command == "daemon":
        run_daemon(args.poll_seconds)
        return 0

    if args.command == "summarize":
        init_db(DB_PATH)
        con = duckdb.connect(str(DB_PATH))
        try:
            prioritize_pending_outbox(con)
            content = (
                generate_daily_summary(con, args.date)
                if args.kind == "daily"
                else generate_weekly_review(con, args.date)
            )
        finally:
            con.close()
        print(content)
        return 0

    print(json.dumps(get_status(), default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
