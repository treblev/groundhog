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
from agent.weekly_summaries import health_summary, market_summary
from config.settings import DB_PATH
from mcp_server.tool_docs import ToolDocumentationError, load_tool_documentation


_TOOL_NAMES = frozenset(
    {
        "run_sql",
        "get_latest_price",
        "get_stock_symbols",
        "query_stock_notes",
        "query_stock_alerts",
        "get_recent_activities",
        "get_activity_summary",
        "get_sleep_summary",
        "get_workout_for_date",
        "search_documents",
        "get_data_freshness",
        "get_market_summary",
        "get_health_summary",
        "get_weekly_health_summary",
        "get_weekly_market_summary",
        "remember",
        "recall",
        "get_recent_events",
        "get_pending_outbox",
        "get_agent_run_status",
        "get_latest_alerts",
        "mark_outbox_delivered",
    }
)
_TOOL_DOCUMENTATION = load_tool_documentation(expected_tool_names=_TOOL_NAMES)


def _description(tool_name: str) -> str:
    return _TOOL_DOCUMENTATION[tool_name].description


def _argument_description(tool_name: str, argument_name: str) -> str:
    try:
        return _TOOL_DOCUMENTATION[tool_name].arguments[argument_name]
    except KeyError as error:
        raise ToolDocumentationError(
            f"Missing documentation for argument '{tool_name}.{argument_name}'"
        ) from error


server = Server("groundhog")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="run_sql",
            description=_description("run_sql"),
            inputSchema={
                "type": "object",
                "properties": {"query": {"type": "string", "description": _argument_description("run_sql", "query")}},
                "required": ["query"],
            },
        ),
        Tool(
            name="get_latest_price",
            description=_description("get_latest_price"),
            inputSchema={
                "type": "object",
                "properties": {"ticker": {"type": "string", "description": _argument_description("get_latest_price", "ticker")}},
                "required": ["ticker"],
            },
        ),
        Tool(
            name="get_stock_symbols",
            description=_description("get_stock_symbols"),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="query_stock_notes",
            description=_description("query_stock_notes"),
            inputSchema={
                "type": "object",
                "properties": {
                    "tickers": {"type": "array", "items": {"type": "string"}},
                    "start_date": {"type": "string", "description": _argument_description("query_stock_notes", "start_date")},
                    "end_date": {"type": "string", "description": _argument_description("query_stock_notes", "end_date")},
                    "active_only": {"type": "boolean", "default": True},
                    "order": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
                    "cursor": {"type": "integer", "minimum": 0, "default": 0},
                },
                "required": [],
            },
        ),
        Tool(
            name="query_stock_alerts",
            description=_description("query_stock_alerts"),
            inputSchema={
                "type": "object",
                "properties": {
                    "tickers": {"type": "array", "items": {"type": "string"}},
                    "start_date": {"type": "string", "description": _argument_description("query_stock_alerts", "start_date")},
                    "end_date": {"type": "string", "description": _argument_description("query_stock_alerts", "end_date")},
                    "timeframe": {"type": "string", "enum": ["daily", "weekly"]},
                    "direction": {"type": "string", "enum": ["bullish", "bearish"]},
                    "alert_type": {"type": "string"},
                    "latest_per_ticker": {"type": "boolean", "default": False},
                    "order": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 100},
                    "cursor": {"type": "integer", "minimum": 0, "default": 0},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_recent_activities",
            description=_description("get_recent_activities"),
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": _argument_description("get_recent_activities", "limit")}},
                "required": [],
            },
        ),
        Tool(
            name="get_activity_summary",
            description=_description("get_activity_summary"),
            inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}, "required": []},
        ),
        Tool(
            name="get_sleep_summary",
            description=_description("get_sleep_summary"),
            inputSchema={"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}, "required": []},
        ),
        Tool(
            name="get_workout_for_date",
            description=_description("get_workout_for_date"),
            inputSchema={"type": "object", "properties": {"date": {"type": "string"}}, "required": ["date"]},
        ),
        Tool(
            name="search_documents",
            description=_description("search_documents"),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": _argument_description("search_documents", "query")},
                    "domain": {"type": "string", "enum": ["workout", "stock_alert", "stock_note"], "default": "workout"},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                    "start_date": {"type": "string", "description": _argument_description("search_documents", "start_date")},
                    "end_date": {"type": "string", "description": _argument_description("search_documents", "end_date")},
                    "section": {"type": "string", "description": _argument_description("search_documents", "section")},
                    "structure_type": {"type": "string", "description": _argument_description("search_documents", "structure_type")},
                    "ticker": {"type": "string", "description": _argument_description("search_documents", "ticker")},
                    "direction": {"type": "string", "enum": ["bullish", "bearish"], "description": _argument_description("search_documents", "direction")},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_data_freshness",
            description=_description("get_data_freshness"),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_market_summary",
            description=_description("get_market_summary"),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_health_summary",
            description=_description("get_health_summary"),
            inputSchema={
                "type": "object",
                "properties": {"date": {"type": "string", "description": _argument_description("get_health_summary", "date")}},
                "required": ["date"],
            },
        ),
        Tool(
            name="get_weekly_health_summary",
            description=_description("get_weekly_health_summary"),
            inputSchema={
                "type": "object",
                "properties": {
                    "week_end": {
                        "type": "string",
                        "description": _argument_description("get_weekly_health_summary", "week_end"),
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="get_weekly_market_summary",
            description=_description("get_weekly_market_summary"),
            inputSchema={
                "type": "object",
                "properties": {
                    "week_end": {
                        "type": "string",
                        "description": _argument_description("get_weekly_market_summary", "week_end"),
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="remember",
            description=_description("remember"),
            inputSchema={
                "type": "object",
                "properties": {"fact": {"type": "string", "description": _argument_description("remember", "fact")}},
                "required": ["fact"],
            },
        ),
        Tool(
            name="recall",
            description=_description("recall"),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": _argument_description("recall", "query")},
                    "top_k": {"type": "integer", "description": _argument_description("recall", "top_k")},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_recent_events",
            description=_description("get_recent_events"),
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": _argument_description("get_recent_events", "limit")}},
                "required": [],
            },
        ),
        Tool(
            name="get_pending_outbox",
            description=_description("get_pending_outbox"),
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": _argument_description("get_pending_outbox", "limit")}},
                "required": [],
            },
        ),
        Tool(
            name="get_agent_run_status",
            description=_description("get_agent_run_status"),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_latest_alerts",
            description=_description("get_latest_alerts"),
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": _argument_description("get_latest_alerts", "limit")}},
                "required": [],
            },
        ),
        Tool(
            name="mark_outbox_delivered",
            description=_description("mark_outbox_delivered"),
            inputSchema={
                "type": "object",
                "properties": {"outbox_id": {"type": "string", "description": _argument_description("mark_outbox_delivered", "outbox_id")}},
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

    if name == "get_stock_symbols":
        return _query_envelope(con, """
            SELECT DISTINCT upper(ticker) AS ticker
            FROM (
                SELECT ticker FROM stock_watchlist
                UNION ALL SELECT ticker FROM stock_signals
                UNION ALL SELECT ticker FROM stock_alerts
                UNION ALL SELECT ticker FROM stock_notes
            ) symbols
            WHERE ticker IS NOT NULL AND trim(ticker) <> ''
            ORDER BY ticker
        """, limit=1000, cursor=0)

    if name == "query_stock_notes":
        clauses, parameters = [], []
        tickers = [str(item).strip().upper() for item in args.get("tickers", []) if str(item).strip()]
        if tickers:
            clauses.append(f"upper(ticker) IN ({', '.join('?' for _ in tickers)})")
            parameters.extend(tickers)
        if args.get("active_only", True):
            clauses.append("NOT is_deleted")
        if args.get("start_date"):
            clauses.append("CAST(created_at AS DATE) >= ?")
            parameters.append(args["start_date"])
        if args.get("end_date"):
            clauses.append("CAST(created_at AS DATE) <= ?")
            parameters.append(args["end_date"])
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        order = _order(args)
        limit, cursor = _page(args)
        return _query_envelope(
            con,
            f"""
            SELECT id, ticker, note, is_deleted, created_at, updated_at
            FROM stock_notes
            {where}
            ORDER BY created_at {order}, id {order}
            """,
            parameters,
            limit=limit,
            cursor=cursor,
        )

    if name == "query_stock_alerts":
        clauses, parameters = [], []
        tickers = [str(item).strip().upper() for item in args.get("tickers", []) if str(item).strip()]
        if tickers:
            clauses.append(f"upper(ticker) IN ({', '.join('?' for _ in tickers)})")
            parameters.extend(tickers)
        if args.get("start_date"):
            clauses.append("date >= ?")
            parameters.append(args["start_date"])
        if args.get("end_date"):
            clauses.append("date <= ?")
            parameters.append(args["end_date"])
        if args.get("timeframe"):
            clauses.append("alert_type LIKE ?")
            parameters.append(f"supertrend_{args['timeframe']}_%")
        if args.get("direction"):
            clauses.append("alert_type LIKE ?")
            parameters.append(f"%_{args['direction']}")
        if args.get("alert_type"):
            clauses.append("alert_type = ?")
            parameters.append(args["alert_type"])
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        order = _order(args)
        limit, cursor = _page(args)
        if args.get("latest_per_ticker"):
            query = f"""
                SELECT id, date, ticker, alert_type, message, notified_at
                FROM (
                    SELECT id, date, ticker, alert_type, message, notified_at,
                           ROW_NUMBER() OVER (
                               PARTITION BY ticker ORDER BY date DESC, notified_at DESC, id
                           ) AS ticker_rank
                    FROM stock_alerts
                    {where}
                ) ranked
                WHERE ticker_rank = 1
                ORDER BY date {order}, notified_at {order}, ticker
            """
        else:
            query = f"""
                SELECT id, date, ticker, alert_type, message, notified_at
                FROM stock_alerts
                {where}
                ORDER BY date {order}, notified_at {order}, ticker
            """
        return _query_envelope(
            con,
            query,
            parameters,
            limit=limit,
            cursor=cursor,
        )

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

    if name == "get_weekly_health_summary":
        return json.dumps(
            health_summary(con, args.get("week_end")), default=_json_default
        )

    if name == "get_weekly_market_summary":
        return json.dumps(
            market_summary(con, args.get("week_end")), default=_json_default
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


def _query_envelope(
    con: duckdb.DuckDBPyConnection,
    query: str,
    parameters: list | None = None,
    *,
    limit: int,
    cursor: int,
) -> str:
    """Return stable structured rows with explicit truncation metadata."""
    cursor_result = con.execute(
        f"SELECT * FROM ({query}) routed_query LIMIT ? OFFSET ?",
        [*(parameters or []), limit + 1, cursor],
    )
    columns = [column[0] for column in cursor_result.description]
    fetched = cursor_result.fetchall()
    truncated = len(fetched) > limit
    rows = [dict(zip(columns, row)) for row in fetched[:limit]]
    return json.dumps(
        {
            "rows": rows,
            "count": len(rows),
            "truncated": truncated,
            "next_cursor": cursor + limit if truncated else None,
        },
        default=_json_default,
    )


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


def _order(args: dict) -> str:
    value = str(args.get("order", "desc")).lower()
    if value not in {"asc", "desc"}:
        raise ValueError("order must be 'asc' or 'desc'")
    return value.upper()


def _page(args: dict) -> tuple[int, int]:
    limit = int(args.get("limit", 100))
    cursor = int(args.get("cursor", 0))
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if cursor < 0:
        raise ValueError("cursor must be non-negative")
    return limit, cursor


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
