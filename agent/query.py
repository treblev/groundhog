import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import re

import duckdb
import httpx

from config.settings import DB_PATH, OLLAMA_SQL_MODEL
from agent.memory import remember, recall

OLLAMA_URL = "http://localhost:11434/api/chat"
MAX_TOOL_ROUNDS = 8

# --- Tool definitions sent to the model ---

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": "Run a DuckDB SQL query and return the results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "A valid DuckDB SQL query."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_latest_price",
            "description": "Get the latest closing price for a stock ticker.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Exact ticker symbol e.g. INTC, BTC-USD."}
                },
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_activities",
            "description": "Get the most recent workout activities.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Number of activities to return. Default 5."}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_health_summary",
            "description": "Get the health metrics (steps, avg HR, active minutes) for a specific date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Date in YYYY-MM-DD format."}
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Save a fact or preference to persistent memory for future recall.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string", "description": "The fact or preference to remember."}
                },
                "required": ["fact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "Search persistent memory for facts relevant to a query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The topic or question to search memory for."},
                    "top_k": {"type": "integer", "description": "Number of memories to return. Default 3."},
                },
                "required": ["query"],
            },
        },
    },
]

# --- Tool implementations ---

def _run_sql(con: duckdb.DuckDBPyConnection, query: str) -> str:
    try:
        df = con.execute(query).fetchdf()
        return df.to_string(index=False) if not df.empty else "No results."
    except Exception as e:
        return f"SQL error: {e}"


def _get_latest_price(con: duckdb.DuckDBPyConnection, ticker: str) -> str:
    try:
        row = con.execute(
            "SELECT date, closing_price FROM stock_watchlist WHERE ticker = ? ORDER BY date DESC LIMIT 1",
            [ticker],
        ).fetchone()
        if not row:
            return f"No data found for ticker '{ticker}'."
        return f"{ticker} closing price on {row[0]}: ${row[1]:,.2f}"
    except Exception as e:
        return f"Error: {e}"


def _get_recent_activities(con: duckdb.DuckDBPyConnection, limit: int = 5) -> str:
    try:
        df = con.execute(
            "SELECT date, activity_type, duration_seconds, avg_hr, max_hr, calories FROM activities ORDER BY date DESC LIMIT ?",
            [limit],
        ).fetchdf()
        return df.to_string(index=False) if not df.empty else "No activities found."
    except Exception as e:
        return f"Error: {e}"


def _get_health_summary(con: duckdb.DuckDBPyConnection, date: str) -> str:
    try:
        row = con.execute(
            "SELECT date, steps, avg_hr, active_minutes FROM health_metrics WHERE date = ?",
            [date],
        ).fetchone()
        if not row:
            return f"No health data for {date}."
        return f"Date: {row[0]}, Steps: {row[1]}, Avg HR: {row[2]}, Active minutes: {row[3]}"
    except Exception as e:
        return f"Error: {e}"


def _dispatch(con: duckdb.DuckDBPyConnection, name: str, args: dict) -> str:
    if name == "run_sql":
        return _run_sql(con, args["query"])
    elif name == "get_latest_price":
        return _get_latest_price(con, args["ticker"])
    elif name == "get_recent_activities":
        return _get_recent_activities(con, args.get("limit", 5))
    elif name == "get_health_summary":
        return _get_health_summary(con, args["date"])
    elif name == "remember":
        return remember(con, args["fact"])
    elif name == "recall":
        return recall(con, args["query"], args.get("top_k", 3))
    return f"Unknown tool: {name}"


# --- Schema context ---

def _get_schema(con: duckdb.DuckDBPyConnection) -> str:
    tables = con.execute("SHOW TABLES").fetchdf()["name"].tolist()
    parts = []
    for table in tables:
        cols = con.execute(f"DESCRIBE {table}").fetchdf()
        col_defs = ", ".join(f"{r['column_name']} {r['column_type']}" for _, r in cols.iterrows())
        note = ""
        if table == "stock_watchlist":
            tickers = con.execute("SELECT DISTINCT ticker FROM stock_watchlist").fetchdf()["ticker"].tolist()
            note = f"  -- tickers: {', '.join(tickers)}"
        elif table == "activities":
            types = con.execute("SELECT DISTINCT activity_type FROM activities").fetchdf()["activity_type"].tolist()
            note = f"  -- activity_types: {', '.join(t for t in types if t)}"
        parts.append(f"{table}({col_defs}){note}")
    return "\n".join(parts)


# --- Agentic loop ---

def _chat(messages: list, tools: bool = True) -> dict:
    payload = {
        "model": OLLAMA_SQL_MODEL,
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = TOOLS
    response = httpx.post(OLLAMA_URL, json=payload, timeout=120.0)
    response.raise_for_status()
    return response.json()["message"]


def _ask(question: str, schema: str, con: duckdb.DuckDBPyConnection) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "You are a personal data assistant with access to tools that query a local database.\n"
                "Use tools to answer the user's question. Do not guess — always call a tool to get real data.\n"
                "Call only the tools needed to answer the question. Once you have enough information, stop calling tools and give your final answer.\n"
                "For simple requests like remembering a fact, call remember() once and immediately confirm to the user.\n"
                "Before answering any question about the user's opinions, preferences, or beliefs, always call recall() first.\n"
                "When recall() returns memories, answer ONLY based on those memories. Do not add your own knowledge, reasoning, or qualifications. State the answer directly as fact.\n\n"
                f"Database schema:\n{schema}"
            ),
        },
        {"role": "user", "content": question},
    ]

    for _ in range(MAX_TOOL_ROUNDS):
        message = _chat(messages)

        # native tool_calls field (Ollama >= 0.3 with supported model)
        tool_calls = message.get("tool_calls")

        # fallback: model returned tool call as JSON text in content
        if not tool_calls:
            import re
            content = message.get("content", "")
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group())
                    if "name" in parsed and parsed["name"] in {t["function"]["name"] for t in TOOLS}:
                        tool_calls = [{"function": {"name": parsed["name"], "arguments": parsed.get("arguments", {})}}]
                except (json.JSONDecodeError, TypeError):
                    pass

        # no tool call — model has a final answer
        if not tool_calls:
            return message.get("content", "No response.")

        # execute each tool call and feed results back
        messages.append(message)
        for call in tool_calls:
            name = call["function"]["name"]
            args = call["function"]["arguments"]
            if isinstance(args, str):
                args = json.loads(args)
            result = _dispatch(con, name, args)
            print(f"  [tool] {name}({args}) → {result[:120]}")
            messages.append({"role": "tool", "content": result})

    return "Agent reached max tool rounds without a final answer."


def run() -> None:
    con = duckdb.connect(str(DB_PATH))
    schema = _get_schema(con)

    print("Groundhog Agent — ask anything about your data. Type 'exit' to quit.\n")

    try:
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

            answer = _ask(question, schema, con)
            print(f"Agent: {answer}\n")
    finally:
        con.close()


if __name__ == "__main__":
    run()
