import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import json
from decimal import Decimal

import duckdb
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from agent.memory import remember, recall
from agent.outbox import set_outbox_status
from agent.semantic_search import search_documents
from config.settings import DB_PATH

server = Server("groundhog")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="run_sql",
            description="Run a DuckDB SQL query and return the results.",
            inputSchema={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "A valid DuckDB SQL query."}},
                "required": ["query"],
            },
        ),
        Tool(
            name="get_latest_price",
            description="Get the latest closing price for a stock ticker.",
            inputSchema={
                "type": "object",
                "properties": {"ticker": {"type": "string", "description": "Exact ticker symbol e.g. INTC, BTC-USD."}},
                "required": ["ticker"],
            },
        ),
        Tool(
            name="get_recent_activities",
            description="Get the most recent workout activities.",
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "Number of activities to return. Default 5."}},
                "required": [],
            },
        ),
        Tool(
            name="get_activity_summary",
            description="Summarize activities for an optional inclusive YYYY-MM-DD date range.",
            inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}, "required": []},
        ),
        Tool(
            name="get_sleep_summary",
            description="Summarize sleep metrics for an optional inclusive YYYY-MM-DD date range.",
            inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}, "required": []},
        ),
        Tool(
            name="get_workout_for_date",
            description="Get the planned workout for a YYYY-MM-DD date.",
            inputSchema={"type": "object", "properties": {"date": {"type": "string"}}, "required": ["date"]},
        ),
        Tool(
            name="search_documents",
            description=(
                "Search stored workout plans, historical weekly Supertrend alerts, or user-authored ticker notes by meaning or "
                "similarity. Use domain='workout' for non-date workout lookup and domain='stock_alert' "
                "for historical weekly flips; use domain='stock_note' for semantic note retrieval. "
                "Use structured tools for exact dates, counts, and market facts."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language semantic search query."},
                    "domain": {"type": "string", "enum": ["workout", "stock_alert", "stock_note"], "default": "workout"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                    "start_date": {"type": "string", "description": "Optional inclusive YYYY-MM-DD lower bound."},
                    "end_date": {"type": "string", "description": "Optional inclusive YYYY-MM-DD upper bound."},
                    "section": {"type": "string", "description": "Optional track filter, such as Fitness, HYROX, Tread, Row, or Floor."},
                    "structure_type": {"type": "string", "description": "Optional exact workout structure filter."},
                    "ticker": {"type": "string", "description": "Optional exact ticker filter for stock-alert retrieval."},
                    "direction": {"type": "string", "enum": ["bullish", "bearish"], "description": "Optional weekly Supertrend direction filter."},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_data_freshness",
            description="Get the latest available date for every Groundhog data source.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_market_summary",
            description="Summarize the latest tracked market data, including BTC-USD price, change, and signals.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_health_summary",
            description="Get the health metrics (steps, avg HR, active minutes) for a specific date.",
            inputSchema={
                "type": "object",
                "properties": {"date": {"type": "string", "description": "Date in YYYY-MM-DD format."}},
                "required": ["date"],
            },
        ),
        Tool(
            name="remember",
            description="Save a fact or preference to persistent memory for future recall.",
            inputSchema={
                "type": "object",
                "properties": {"fact": {"type": "string", "description": "The fact or preference to remember."}},
                "required": ["fact"],
            },
        ),
        Tool(
            name="recall",
            description="Search persistent memory for the user's personal opinions, preferences, and stated beliefs. Do NOT use for factual data questions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The topic or question to search memory for."},
                    "top_k": {"type": "integer", "description": "Number of memories to return. Default 3."},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_recent_events",
            description="Get recent durable Groundhog service events.",
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "Number of events to return. Default 20."}},
                "required": [],
            },
        ),
        Tool(
            name="get_pending_outbox",
            description="Get pending Groundhog delivery items with their source event data.",
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "Number of items to return. Default 20."}},
                "required": [],
            },
        ),
        Tool(
            name="get_agent_run_status",
            description="Get the most recent Groundhog scheduled job run and its outcome.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_latest_alerts",
            description="Get recent deduplicated stock alerts recorded by Groundhog.",
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "Number of alerts to return. Default 10."}},
                "required": [],
            },
        ),
        Tool(
            name="mark_outbox_delivered",
            description="Mark one pending Groundhog outbox item as delivered after OpenClaw sends it.",
            inputSchema={
                "type": "object",
                "properties": {"outbox_id": {"type": "string", "description": "The outbox item ID to mark delivered."}},
                "required": ["outbox_id"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    result = _dispatch(name, arguments)
    return [TextContent(type="text", text=result)]


def _dispatch(
    name: str,
    args: dict,
    connection: duckdb.DuckDBPyConnection | None = None,
) -> str:
    """Handle one MCP call without retaining a DuckDB lock between requests."""
    if name == "search_documents":
        db_path = DB_PATH
        if connection is not None:
            database_rows = connection.execute("PRAGMA database_list").fetchall()
            database_file = next((row[2] for row in database_rows if row[1] == "memory"), None)
            if database_file:
                db_path = database_file
        results = search_documents(
            args["query"],
            domain=args.get("domain", "workout"),
            top_k=args.get("top_k", 5),
            start_date=args.get("start_date"),
            end_date=args.get("end_date"),
            section=args.get("section"),
            structure_type=args.get("structure_type"),
            ticker=args.get("ticker"),
            direction=args.get("direction"),
            db_path=db_path,
        )
        return json.dumps(results, default=_json_default)

    owns_connection = connection is None
    con = connection or duckdb.connect(str(DB_PATH))
    try:
        return _dispatch_with_connection(con, name, args)
    finally:
        if owns_connection:
            con.close()


def _dispatch_with_connection(
    con: duckdb.DuckDBPyConnection, name: str, args: dict
) -> str:
    if name == "run_sql":
        try:
            df = con.execute(args["query"]).fetchdf()
            return df.to_string(index=False) if not df.empty else "No results."
        except Exception as e:
            return f"SQL error: {e}"

    if name == "get_latest_price":
        ticker = args["ticker"]
        row = con.execute(
            "SELECT date, closing_price FROM stock_watchlist WHERE ticker = ? ORDER BY date DESC LIMIT 1",
            [ticker],
        ).fetchone()
        return f"{ticker} closing price on {row[0]}: ${row[1]:,.2f}" if row else f"No data for '{ticker}'."

    if name == "get_recent_activities":
        df = con.execute(
            "SELECT date, activity_type, duration_seconds, avg_hr, max_hr, calories FROM activities ORDER BY date DESC LIMIT ?",
            [args.get("limit", 5)],
        ).fetchdf()
        return df.to_string(index=False) if not df.empty else "No activities found."

    if name == "get_activity_summary":
        where, parameters = _date_range(args, "date")
        return _query_json(con, f"""
            SELECT MIN(date) AS first_date, MAX(date) AS last_date, COUNT(*) AS activity_count,
                   COUNT(*) FILTER (WHERE activity_type = 'running') AS run_count,
                   COUNT(*) FILTER (WHERE activity_type = 'pool swim') AS swim_count,
                   SUM(distance_miles) AS total_distance_miles,
                   SUM(duration_seconds) AS total_duration_seconds,
                   AVG(avg_hr) AS average_hr
            FROM activities {where}
        """, parameters)

    if name == "get_sleep_summary":
        where, parameters = _date_range(args, "date")
        return _query_json(con, f"""
            SELECT MIN(date) AS first_date, MAX(date) AS last_date, COUNT(*) AS night_count,
                   AVG(resting_hr) AS average_resting_hr, AVG(hrv) AS average_hrv,
                   AVG(breath_rate) AS average_breath_rate,
                   AVG(deep_sleep_minutes) AS average_deep_sleep_minutes
            FROM sleep_metrics {where}
        """, parameters)

    if name == "get_workout_for_date":
        return _query_json(con, """
            SELECT date, day_of_week, name, category, structure_type, description
            FROM workouts WHERE date = ? ORDER BY created_at DESC
        """, [args["date"]])

    if name == "get_data_freshness":
        return _query_json(con, """
            SELECT 'activities' AS source, MAX(date) AS latest_date FROM activities
            UNION ALL SELECT 'health_metrics', MAX(date) FROM health_metrics
            UNION ALL SELECT 'sleep_metrics', MAX(date) FROM sleep_metrics
            UNION ALL SELECT 'stock_prices', MAX(date) FROM stock_watchlist
            UNION ALL SELECT 'stock_signals', MAX(date) FROM stock_signals
            UNION ALL SELECT 'workouts', MAX(date) FROM workouts
            ORDER BY source
        """)

    if name == "get_market_summary":
        btc = con.execute("""
            WITH prices AS (
                SELECT date, closing_price,
                       LAG(closing_price) OVER (ORDER BY date) AS previous_close
                FROM stock_watchlist WHERE ticker = 'BTC-USD'
            )
            SELECT date, closing_price, previous_close,
                   closing_price - previous_close AS change_amount,
                   CASE WHEN previous_close = 0 THEN NULL
                        ELSE 100 * (closing_price - previous_close) / previous_close END AS change_percent
            FROM prices ORDER BY date DESC LIMIT 1
        """).fetchone()
        signals = _query_json(con, """
            SELECT timeframe, direction, value, date
            FROM stock_signals
            WHERE ticker = 'BTC-USD' AND signal_type = 'supertrend'
            QUALIFY ROW_NUMBER() OVER (PARTITION BY timeframe ORDER BY date DESC) = 1
            ORDER BY timeframe
        """)
        latest_signal_date = con.execute("SELECT MAX(date) FROM stock_signals").fetchone()[0]
        flips = _query_json(con, """
            SELECT ticker, alert_type, date, message FROM stock_alerts
            ORDER BY notified_at DESC LIMIT 5
        """)
        return json.dumps({
            "bitcoin": {"date": btc[0], "price": btc[1], "previous_close": btc[2], "change_amount": btc[3], "change_percent": btc[4]} if btc else None,
            "bitcoin_supertrend": json.loads(signals),
            "latest_signal_date": latest_signal_date,
            "recent_alerts": json.loads(flips),
        }, default=_json_default)

    if name == "get_health_summary":
        row = con.execute(
            "SELECT date, steps, avg_hr, active_minutes FROM health_metrics WHERE date = ?",
            [args["date"]],
        ).fetchone()
        return (
            f"Date: {row[0]}, Steps: {row[1]}, Avg HR: {row[2]}, Active minutes: {row[3]}"
            if row
            else f"No health data for {args['date']}."
        )

    if name == "remember":
        return remember(con, args["fact"])

    if name == "recall":
        return recall(con, args["query"], args.get("top_k", 3))

    if name == "get_recent_events":
        return _query_json(con,
            """
            SELECT event_type, source, subject_type, subject_id, occurred_at, payload
            FROM events
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            [args.get("limit", 20)],
        )

    if name == "get_pending_outbox":
        return _query_json(con,
            """
            SELECT o.id, e.event_type, e.subject_type, e.subject_id, e.payload, o.created_at
            FROM outbox o
            JOIN events e ON e.id = o.event_id
            WHERE o.status = 'pending'
            ORDER BY o.created_at
            LIMIT ?
            """,
            [args.get("limit", 20)],
        )

    if name == "get_agent_run_status":
        return _query_json(con,
            """
            SELECT job_name, status, started_at, finished_at, error_text
            FROM agent_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        )

    if name == "get_latest_alerts":
        return _query_json(con,
            """
            SELECT date, ticker, alert_type, message, notified_at
            FROM stock_alerts
            ORDER BY notified_at DESC
            LIMIT ?
            """,
            [args.get("limit", 10)],
        )

    if name == "mark_outbox_delivered":
        outbox_id = args["outbox_id"]
        row = con.execute("SELECT id FROM outbox WHERE id = ?", [outbox_id]).fetchone()
        if row is None:
            return json.dumps({"error": f"No outbox item found for '{outbox_id}'."})
        set_outbox_status(con, outbox_id, "delivered")
        return _query_json(con,
            "SELECT id, status, delivered_at FROM outbox WHERE id = ?", [outbox_id]
        )

    return f"Unknown tool: {name}"


def _query_json(
    con: duckdb.DuckDBPyConnection, query: str, parameters: list | None = None
) -> str:
    cursor = con.execute(query, parameters or [])
    columns = [column[0] for column in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    return json.dumps(rows, default=_json_default)


def _json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def _date_range(args: dict, column: str) -> tuple[str, list]:
    clauses, parameters = [], []
    if args.get("start_date"):
        clauses.append(f"{column} >= ?")
        parameters.append(args["start_date"])
    if args.get("end_date"):
        clauses.append(f"{column} <= ?")
        parameters.append(args["end_date"])
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", parameters


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
