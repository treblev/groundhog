import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio

from langchain.agents import create_agent
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

from config.settings import OLLAMA_SQL_MODEL

SERVER_SCRIPT = str(Path(__file__).resolve().parent.parent / "mcp_server" / "server.py")

_DUCKDB_DIALECT = """\
-- DuckDB dialect:
-- Dates:      CURRENT_DATE (not CURDATE()), CURRENT_TIMESTAMP (not NOW())
-- Intervals:  INTERVAL '7 days' (not INTERVAL 7 DAY)
-- Truncation: DATE_TRUNC('week', date), DATE_TRUNC('month', date)
-- Subqueries: no LIMIT inside subqueries — use a CTE instead
"""


def _make_tools(session: ClientSession) -> list:
    async def run_sql(query: str) -> str:
        """Run a SQL query against the local DuckDB database."""
        result = await session.call_tool("run_sql", {"query": query})
        return result.content[0].text if result.content else "No result."

    async def get_latest_price(ticker: str) -> str:
        """Get the latest closing price for a stock ticker."""
        result = await session.call_tool("get_latest_price", {"ticker": ticker})
        return result.content[0].text if result.content else "No result."

    async def get_recent_activities(limit: int = 5) -> str:
        """Get recent Garmin activities."""
        result = await session.call_tool("get_recent_activities", {"limit": limit})
        return result.content[0].text if result.content else "No result."

    async def get_health_summary() -> str:
        """Get a summary of recent health metrics."""
        result = await session.call_tool("get_health_summary", {})
        return result.content[0].text if result.content else "No result."

    async def remember(key: str, value: str) -> str:
        """Save a personal note or preference to memory."""
        result = await session.call_tool("remember", {"key": key, "value": value})
        return result.content[0].text if result.content else "Saved."

    async def recall(key: str) -> str:
        """Recall a previously saved personal note or preference."""
        result = await session.call_tool("recall", {"key": key})
        return result.content[0].text if result.content else "Nothing found."

    return [run_sql, get_latest_price, get_recent_activities, get_health_summary, remember, recall]


async def _build_schema(session: ClientSession) -> str:
    schema_result = await session.call_tool("run_sql", {"query": "SHOW TABLES"})
    tables_raw = schema_result.content[0].text if schema_result.content else ""
    tables = [line.strip() for line in tables_raw.splitlines() if line.strip() and line.strip() != "name"]

    schema_parts = []
    for table in tables:
        desc = await session.call_tool("run_sql", {"query": f"DESCRIBE {table}"})
        col_text = desc.content[0].text if desc.content else ""
        note = ""
        if table == "stock_signals":
            note = "  -- signal_type: 'sma_cross' or 'supertrend'; timeframe: 'daily' or 'weekly'; direction: 'bullish' or 'bearish'; value: SMA gap (sma_cross) or supertrend line price (supertrend)"
        elif table == "stock_alerts":
            note = "  -- alert_type: 'golden_cross', 'death_cross', 'supertrend_daily_bullish', 'supertrend_daily_bearish', 'supertrend_weekly_bullish', 'supertrend_weekly_bearish'"
        elif table == "workouts":
            types_result = await session.call_tool("run_sql", {"query": "SELECT DISTINCT structure_type FROM workouts WHERE structure_type IS NOT NULL"})
            types = [line.strip() for line in types_result.content[0].text.splitlines() if line.strip() and line.strip() != "structure_type"]
            cats_result = await session.call_tool("run_sql", {"query": "SELECT DISTINCT category FROM workouts WHERE category IS NOT NULL"})
            cats = [line.strip() for line in cats_result.content[0].text.splitlines() if line.strip() and line.strip() != "category"]
            note = f"  -- structure_types: {', '.join(types)}; categories: {', '.join(cats)}"
        schema_parts.append(f"{table}({col_text[:200]}){note}")

    return _DUCKDB_DIALECT + "\n".join(schema_parts)


async def run():
    params = StdioServerParameters(
        command=sys.executable,
        args=[SERVER_SCRIPT],
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            schema = await _build_schema(session)
            tools = _make_tools(session)

            agent = create_agent(
                model=f"ollama:{OLLAMA_SQL_MODEL}",
                tools=tools,
                system_prompt=(
                    "You are a personal data assistant with access to tools that query a local database.\n"
                    "Use tools to answer the user's question. Do not guess — always call a tool to get real data.\n"
                    "Call only the tools needed. Once you have enough information, stop and give your final answer.\n"
                    "When a tool returns data, interpret it directly. Do not call additional tools to verify.\n"
                    "For comparison queries: if only one period appears, the missing period is zero.\n"
                    "NEVER call remember() unless the user explicitly says 'remember' or 'save'.\n"
                    "Call recall() ONLY for questions about personal opinions, preferences, or stated beliefs.\n"
                    f"\nDatabase schema:\n{schema}"
                ),
            )

            print(f"Groundhog Agent — {len(tools)} tools loaded. Type 'exit' to quit.\n")

            while True:
                try:
                    question = input("You: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nBye.")
                    break

                if not question:
                    continue
                if question.lower() in {"exit", "quit"}:
                    print("Bye.")
                    break

                result = await agent.ainvoke({"messages": [{"role": "user", "content": question}]})
                answer = result["messages"][-1].content
                print(f"Agent: {answer}\n")


if __name__ == "__main__":
    asyncio.run(run())
