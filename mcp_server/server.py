import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio

import duckdb
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from agent.memory import remember, recall
from config.settings import DB_PATH

server = Server("groundhog")
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

    return f"Unknown tool: {name}"


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
