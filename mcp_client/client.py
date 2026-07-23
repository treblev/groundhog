import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio
import json
import re

import httpx
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

from config.settings import OLLAMA_CHAT_URL, OLLAMA_SQL_MODEL

OLLAMA_URL = OLLAMA_CHAT_URL
MAX_TOOL_ROUNDS = 8
SERVER_SCRIPT = str(Path(__file__).resolve().parent.parent / "mcp_server" / "server.py")

_DUCKDB_DIALECT = """\
-- DuckDB dialect:
-- Dates:      CURRENT_DATE (not CURDATE()), CURRENT_TIMESTAMP (not NOW())
-- Intervals:  INTERVAL '7 days' (not INTERVAL 7 DAY)
-- Truncation: DATE_TRUNC('week', date), DATE_TRUNC('month', date)
-- Subqueries: no LIMIT inside subqueries — use a CTE instead
"""


def _mcp_tool_to_ollama(tool) -> dict:
    """Convert an MCP Tool object to Ollama's tool format."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.inputSchema,
        },
    }


def _chat(messages: list, tools: list | None = None) -> dict:
    payload = {"model": OLLAMA_SQL_MODEL, "messages": messages, "stream": False}
    if tools:
        payload["tools"] = tools
    response = httpx.post(OLLAMA_URL, json=payload, timeout=120.0)
    response.raise_for_status()
    return response.json()["message"]


async def _ask(question: str, session: ClientSession, ollama_tools: list, schema: str) -> str:
    # planning step
    plan_messages = [
        {
            "role": "system",
            "content": (
                "You are a planning assistant. Write a concise numbered plan: which tools to call, "
                "in what order, and why. Do NOT call any tools — only write the plan.\n\n"
                f"Available tools: {', '.join(t['function']['name'] for t in ollama_tools)}\n\n"
                f"Database schema:\n{schema}"
            ),
        },
        {"role": "user", "content": f"Plan how to answer: {question}"},
    ]
    plan = _chat(plan_messages, tools=None).get("content", "").strip()
    if plan:
        print(f"  [plan] {plan[:300]}")

    messages = [
        {
            "role": "system",
            "content": (
                "You are a personal data assistant with access to tools that query a local database.\n"
                "Use tools to answer the user's question. Do not guess — always call a tool to get real data.\n"
                "Call only the tools needed. Once you have enough information, stop and give your final answer.\n"
                "When a tool returns data, interpret it directly. Do not call additional tools to verify.\n"
                "For comparison queries: if only one period appears, the missing period is zero.\n"
                "NEVER call remember() unless the user explicitly says 'remember' or 'save'.\n"
                "Call recall() ONLY for questions about personal opinions, preferences, or stated beliefs.\n"
                "When recall() returns memories, answer ONLY based on those memories.\n\n"
                f"Database schema:\n{schema}"
                + (f"\n\nExecution plan:\n{plan}" if plan else "")
            ),
        },
        {"role": "user", "content": question},
    ]

    for _ in range(MAX_TOOL_ROUNDS):
        message = _chat(messages, tools=ollama_tools)
        tool_calls = message.get("tool_calls")

        if not tool_calls:
            content = message.get("content", "")
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group())
                    tool_names = {t["function"]["name"] for t in ollama_tools}
                    if "name" in parsed and parsed["name"] in tool_names:
                        tool_calls = [{"function": {"name": parsed["name"], "arguments": parsed.get("arguments", {})}}]
                except (json.JSONDecodeError, TypeError):
                    pass

        if not tool_calls:
            return message.get("content", "No response.")

        messages.append(message)
        for call in tool_calls:
            name = call["function"]["name"]
            args = call["function"]["arguments"]
            if isinstance(args, str):
                args = json.loads(args)

            # call the tool via MCP
            mcp_result = await session.call_tool(name, args)
            result = mcp_result.content[0].text if mcp_result.content else "No result."
            print(f"  [tool] {name}({args}) → {result[:120]}")
            messages.append({"role": "tool", "content": result})

    # synthesize if max rounds hit
    synthesis_messages = messages + [{
        "role": "user",
        "content": "Based only on the tool results above, give a clear direct answer. No more tools. Missing periods mean zero.",
    }]
    return _chat(synthesis_messages, tools=None).get("content", "No response.")


async def run():
    params = StdioServerParameters(
        command=sys.executable,
        args=[SERVER_SCRIPT],
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            mcp_tools = await session.list_tools()
            ollama_tools = [_mcp_tool_to_ollama(t) for t in mcp_tools.tools]

            # build enriched schema by calling run_sql via MCP
            schema_result = await session.call_tool("run_sql", {"query": "SHOW TABLES"})
            tables_raw = schema_result.content[0].text if schema_result.content else ""
            tables = [line.strip() for line in tables_raw.splitlines() if line.strip() and line.strip() != "name"]

            schema_parts = []
            for table in tables:
                desc = await session.call_tool("run_sql", {"query": f"DESCRIBE {table}"})
                col_text = desc.content[0].text if desc.content else ""
                note = ""
                if table == "stock_watchlist":
                    tickers_result = await session.call_tool("run_sql", {"query": "SELECT DISTINCT ticker FROM stock_watchlist"})
                    tickers = [line.strip() for line in tickers_result.content[0].text.splitlines() if line.strip() and line.strip() != "ticker"]
                    note = f"  -- tickers: {', '.join(tickers)}"
                elif table == "activities":
                    types_result = await session.call_tool("run_sql", {"query": "SELECT DISTINCT activity_type FROM activities"})
                    types = [line.strip() for line in types_result.content[0].text.splitlines() if line.strip() and line.strip() != "activity_type"]
                    note = f"  -- activity_types: {', '.join(t for t in types if t)}"
                elif table == "stock_signals":
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
            schema = _DUCKDB_DIALECT + "\n".join(schema_parts)

            print(f"Groundhog MCP Client — {len(ollama_tools)} tools loaded via MCP. Type 'exit' to quit.\n")

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

                answer = await _ask(question, session, ollama_tools, schema)
                print(f"Agent: {answer}\n")


if __name__ == "__main__":
    asyncio.run(run())
