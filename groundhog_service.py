"""Operational command surface for Groundhog's scheduled service tasks."""
import argparse
import json
import signal
import sys
import traceback
from datetime import date, datetime
from decimal import Decimal
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
WEEKEND_CRYPTO_JOB = "weekend_crypto_prices"
PHOENIX = ZoneInfo("America/Phoenix")


def _json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def query_data(sql: str, max_rows: int = 100) -> dict:
    """Run one read-only DuckDB query and return JSON-ready rows."""
    if max_rows < 1:
        raise ValueError("max_rows must be at least 1.")
    statement = sql.strip()
    if not statement:
        raise ValueError("SQL query cannot be empty.")
    if not statement.upper().startswith(("SELECT", "WITH", "EXPLAIN")):
        raise ValueError("Only read-only SELECT, WITH, and EXPLAIN queries are allowed.")

    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        result = con.execute(statement)
        columns = [column[0] for column in result.description]
        rows = result.fetchmany(max_rows + 1)
    finally:
        con.close()

    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    return {
        "columns": columns,
        "rows": [dict(zip(columns, row)) for row in rows],
        "truncated": truncated,
    }


def inspect_schema(table: str | None = None) -> list[dict]:
    """Return table and column metadata for building Groundhog data queries."""
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        if table:
            rows = con.execute(
                """
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'main' AND table_name = ?
                ORDER BY ordinal_position
                """,
                [table],
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'main'
                ORDER BY table_name, ordinal_position
                """
            ).fetchall()
    finally:
        con.close()
    return [
        {"table": row[0], "column": row[1], "type": row[2]}
        for row in rows
    ]


def _finish_run(
    run_id: str, job_name: str, status: str, error_text: str | None = None
) -> None:
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
                "job_name": job_name,
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
        _finish_run(run_id, DAILY_STOCKS_JOB, "failed", error_text)
        raise
    else:
        _finish_run(run_id, DAILY_STOCKS_JOB, "succeeded")


def run_weekend_crypto_prices() -> None:
    """Fetch the weekend BTC-USD price without rerunning equities or alerts."""
    init_db(DB_PATH)
    con = duckdb.connect(str(DB_PATH))
    try:
        run_id = start_run(con, WEEKEND_CRYPTO_JOB)
    finally:
        con.close()

    try:
        print("--- Fetching weekend BTC-USD price ---")
        quote = stocks.fetch_latest_intraday_price("BTC-USD")
        if quote is None:
            print("No intraday BTC-USD quote returned; falling back to daily history.")
            stocks.run(tickers={"BTC-USD"})
        else:
            con = duckdb.connect(str(DB_PATH))
            try:
                stocks.upsert_current_price(con, quote)
            finally:
                con.close()
            print(f"Stored BTC-USD price for {quote[0]}.")
    except Exception:
        error_text = traceback.format_exc()
        _finish_run(run_id, WEEKEND_CRYPTO_JOB, "failed", error_text)
        raise
    else:
        _finish_run(run_id, WEEKEND_CRYPTO_JOB, "succeeded")


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


def due_tasks(
    now: datetime,
    last_daily_stocks_run: datetime | None,
    last_weekend_crypto_run: datetime | None = None,
) -> list[str]:
    """Return scheduled tasks due at a Phoenix-local time."""
    if now.tzinfo is None:
        raise ValueError("now must include a timezone.")
    local_now = now.astimezone(PHOENIX)
    def ran_today(last_run: datetime | None) -> bool:
        if last_run is None:
            return False
        if last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=PHOENIX)
        return last_run.astimezone(PHOENIX).date() == local_now.date()

    due = []
    if local_now.weekday() < 5 and local_now.hour >= 17 and not ran_today(last_daily_stocks_run):
        due.append("daily-stocks")
    if local_now.weekday() >= 5 and local_now.hour >= 17 and not ran_today(last_weekend_crypto_run):
        due.append("weekend-crypto-prices")
    return due


def _last_run(job_name: str) -> datetime | None:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        row = con.execute(
            """
            SELECT MAX(started_at)
            FROM agent_runs
            WHERE job_name = ?
            """,
            [job_name],
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
            for task in due_tasks(
                datetime.now(PHOENIX),
                _last_run(DAILY_STOCKS_JOB),
                _last_run(WEEKEND_CRYPTO_JOB),
            ):
                if task == "daily-stocks":
                    run_daily_stocks()
                elif task == "weekend-crypto-prices":
                    run_weekend_crypto_prices()
            stop_event.wait(poll_seconds)
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run and inspect Groundhog service tasks.")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="Run a scheduled Groundhog task.")
    run_parser.add_argument("job", choices=["daily-stocks", "weekend-crypto-prices"])
    commands.add_parser("status", help="Print the latest service status as JSON.")
    query_parser = commands.add_parser("query", help="Run one read-only SQL query and print JSON.")
    query_parser.add_argument("--sql", required=True, help="A SELECT, WITH, or EXPLAIN query.")
    query_parser.add_argument("--max-rows", type=int, default=100, help="Maximum returned rows (default: 100).")
    schema_parser = commands.add_parser("schema", help="Print table and column metadata as JSON.")
    schema_parser.add_argument("--table", help="Optional table name to inspect.")
    daemon_parser = commands.add_parser("daemon", help="Poll for and run due Groundhog tasks.")
    daemon_parser.add_argument("--poll-seconds", type=int, default=60)
    summary_parser = commands.add_parser("summarize", help="Generate a local derived summary.")
    summary_parser.add_argument("kind", choices=["daily", "weekly"])
    summary_parser.add_argument("--date", required=True, type=date.fromisoformat)
    args = parser.parse_args(argv)

    if args.command == "run":
        if args.job == "daily-stocks":
            run_daily_stocks()
        else:
            run_weekend_crypto_prices()
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

    if args.command == "query":
        try:
            result = query_data(args.sql, args.max_rows)
        except (duckdb.Error, ValueError) as error:
            print(json.dumps({"error": str(error)}), file=sys.stderr)
            return 2
        print(json.dumps(result, default=_json_default, sort_keys=True))
        return 0

    if args.command == "schema":
        print(json.dumps(inspect_schema(args.table), sort_keys=True))
        return 0

    print(json.dumps(get_status(), default=_json_default, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
