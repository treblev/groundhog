import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import json

import duckdb
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from agent.memory import remember, recall
from agent.outbox import set_outbox_status
from config.settings import DB_PATH
from ingestion.schema import init_db

server = Server("groundhog")
init_db(DB_PATH)
con = duckdb.connect(str(DB_PATH))


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


def _dispatch(name: str, args: dict) -> str:
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
        return _query_json(
            """
            SELECT event_type, source, subject_type, subject_id, occurred_at, payload
            FROM events
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            [args.get("limit", 20)],
        )

    if name == "get_pending_outbox":
        return _query_json(
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
        return _query_json(
            """
            SELECT job_name, status, started_at, finished_at, error_text
            FROM agent_runs
            ORDER BY started_at DESC
            LIMIT 1
            """
        )

    if name == "get_latest_alerts":
        return _query_json(
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
        return _query_json(
            "SELECT id, status, delivered_at FROM outbox WHERE id = ?", [outbox_id]
        )

    return f"Unknown tool: {name}"


def _query_json(query: str, parameters: list | None = None) -> str:
    cursor = con.execute(query, parameters or [])
    columns = [column[0] for column in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    return json.dumps(rows, default=str)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
