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
            "description": (
                "Get the user's most recent exercise activities (runs, walks, rides, cardio, "
                "strength sessions) from the activities table. Use this for any question about "
                "'recent workouts' or exercise history — the separate 'workouts' table stores gym "
                "class/WOD programming descriptions, not personal exercise history."
            ),
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
            "description": "Search persistent memory for the user's personal opinions, preferences, and stated beliefs. Do NOT use for factual data questions — use run_sql or other data tools instead.",
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

_DUCKDB_DIALECT = """\
-- DuckDB dialect:
-- Dates:      CURRENT_DATE (not CURDATE()), CURRENT_TIMESTAMP (not NOW())
-- Intervals:  INTERVAL '7 days' (not INTERVAL 7 DAY)
-- Truncation: DATE_TRUNC('week', date), DATE_TRUNC('month', date)
-- Subqueries: no LIMIT inside subqueries — use a CTE instead
"""


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
            note = (
                f"  -- activity_types: {', '.join(t for t in types if t)}. "
                "This is the user's personal exercise history (runs, walks, cardio, strength). "
                "For 'recent workouts' questions, use get_recent_activities or query this table."
            )
        elif table == "workouts":
            note = (
                "  -- gym class / WOD programming descriptions (e.g. SugarWOD), NOT the user's "
                "personal exercise history. Do not use this table to answer 'my recent workouts' "
                "questions — use activities instead."
            )
        parts.append(f"{table}({col_defs}){note}")
    return _DUCKDB_DIALECT + "\n".join(parts)


# --- Agentic loop ---

def _chat(messages: list, tools: bool = True) -> dict:
    payload = {
        "model": OLLAMA_SQL_MODEL,
        "messages": messages,
        "stream": False,
    }
    if tools:
        payload["tools"] = TOOLS
    response = httpx.post(OLLAMA_URL, json=payload, timeout=600.0)
    response.raise_for_status()
    return response.json()["message"]


def _chat_with_retry(messages: list, tools: bool = True, max_retries: int = 2) -> dict:
    """Call the model, retrying if it comes back with neither a tool call nor any
    content — an empty response that would otherwise be silently accepted as the
    final answer. Seen intermittently (both qwen3:32b and qwen3.6)."""
    message: dict = {}
    for attempt in range(max_retries + 1):
        message = _chat(messages, tools=tools)
        if message.get("tool_calls") or message.get("content", "").strip():
            return message
        if attempt < max_retries:
            print(f"  [retry] empty response from model, retrying ({attempt + 1}/{max_retries})")
    return message


def _plan(question: str, schema: str) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "You are a planning assistant. Given a question and a list of available tools, "
                "write a concise numbered plan describing exactly which tools to call and in what order to answer the question. "
                "Be specific: name the tool, what argument to pass, and why it's needed. "
                "Do NOT call any tools — only write the plan.\n\n"
                "Available tools: run_sql, get_latest_price, get_recent_activities, get_health_summary, remember, recall\n\n"
                f"Database schema:\n{schema}"
            ),
        },
        {"role": "user", "content": f"Plan how to answer: {question}"},
    ]
    message = _chat_with_retry(messages, tools=False)
    return message.get("content", "").strip()


def _synthesize(question: str, messages: list) -> str:
    synthesis_messages = messages + [
        {
            "role": "user",
            "content": (
                "Based only on the tool results above, give a clear and direct answer to the original question. "
                "Do not call any more tools. If a week or period returned no rows, it means zero — state that directly."
            ),
        }
    ]
    message = _chat_with_retry(synthesis_messages, tools=False)
    return message.get("content", "").strip() or "No response."


def _ask(question: str, schema: str, con: duckdb.DuckDBPyConnection) -> str:
    plan = _plan(question, schema)
    if plan:
        print(f"  [plan] {plan[:300]}")

    messages = [
        {
            "role": "system",
            "content": (
                "You are a personal data assistant with access to tools that query a local database.\n"
                "Use tools to answer the user's question. Do not guess — always call a tool to get real data.\n"
                "Call only the tools needed to answer the question. Once you have enough information, stop calling tools and give your final answer.\n"
                "When a tool returns data, interpret it directly and answer. Do not call additional tools to verify or recheck the result.\n"
                "For comparison queries (this week vs last week, this month vs last month, etc): if only one period appears in the results, the missing period has a count of zero. State the answer directly — do not say more data is needed.\n"
                "NEVER call remember() unless the user's message explicitly uses the word 'remember' or 'save'. Do not save computed results automatically.\n"
                "NEVER call recall() after remember() — if you just saved a fact you already have it, there is nothing to look up.\n"
                "Call recall() ONLY when the question is about the user's personal opinions, preferences, or stated beliefs — NOT for factual questions that can be answered with SQL data.\n"
                "When recall() returns memories, answer ONLY based on those memories. Do not add your own knowledge, reasoning, or qualifications. State the answer directly as fact.\n"
                "When the user says 'workouts', 'exercise', or 'activities' referring to things they personally did (runs, walks, cardio, strength sessions), that means the activities table / get_recent_activities tool — NOT the workouts table, which stores unrelated gym class/WOD programming text.\n\n"
                f"Database schema:\n{schema}"
                + (f"\n\nExecution plan:\n{plan}" if plan else "")
            ),
        },
        {"role": "user", "content": question},
    ]

    for _ in range(MAX_TOOL_ROUNDS):
        message = _chat_with_retry(messages)

        # native tool_calls field (Ollama >= 0.3 with supported model)
        tool_calls = message.get("tool_calls")

        # fallback: model returned tool call as JSON text in content
        if not tool_calls:
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
            answer = message.get("content", "").strip()
            return answer if answer else "The model returned an empty response after retrying — try rephrasing your question."

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

    return _synthesize(question, messages)


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
