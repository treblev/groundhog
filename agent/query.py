import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb
import httpx

from config.settings import DB_PATH, OLLAMA_SQL_MODEL

OLLAMA_URL = "http://localhost:11434/api/chat"


def _get_schema(con: duckdb.DuckDBPyConnection) -> str:
    tables = con.execute("SHOW TABLES").fetchdf()["name"].tolist()
    parts = []
    for table in tables:
        cols = con.execute(f"DESCRIBE {table}").fetchdf()
        col_defs = ", ".join(
            f"{row['column_name']} {row['column_type']}"
            for _, row in cols.iterrows()
        )
        note = ""
        if table == "stock_watchlist":
            tickers = con.execute("SELECT DISTINCT ticker FROM stock_watchlist").fetchdf()["ticker"].tolist()
            note = f"  -- tickers: {', '.join(tickers)}"
        elif table == "activities":
            types = con.execute("SELECT DISTINCT activity_type FROM activities").fetchdf()["activity_type"].tolist()
            note = f"  -- activity_types: {', '.join(t for t in types if t)}"
        parts.append(f"{table}({col_defs}){note}")
    return "\n".join(parts)


def _ollama(messages: list) -> str:
    response = httpx.post(
        OLLAMA_URL,
        json={"model": OLLAMA_SQL_MODEL, "messages": messages, "stream": False},
        timeout=120.0,
    )
    response.raise_for_status()
    return response.json()["message"]["content"]


def _extract_sql(raw: str) -> str:
    if "```" in raw:
        block = raw.split("```")[1]
        return block.removeprefix("sql").strip()
    return raw.strip()


def _ask(question: str, schema: str, con: duckdb.DuckDBPyConnection) -> str:
    sql_prompt = [
        {
            "role": "system",
            "content": (
                "You are a SQL expert. Given a DuckDB schema and a question, "
                "return ONLY a valid DuckDB SQL query with no explanation.\n\n"
                "Rules:\n"
                "- Use exact ticker symbols as listed in the schema comments\n"
                "- For 'latest' or 'current' price queries, use ORDER BY date DESC LIMIT 1 — do not use CURRENT_DATE\n"
                "- Use DuckDB syntax only\n\n"
                f"Schema:\n{schema}"
            ),
        },
        {"role": "user", "content": question},
    ]
    raw_sql = _ollama(sql_prompt)
    sql = _extract_sql(raw_sql)

    print(f"  [SQL] {sql}")
    try:
        results = con.execute(sql).fetchdf()
    except Exception as e:
        return f"SQL error: {e}\n\nGenerated SQL:\n{sql}"

    answer_prompt = [
        {
            "role": "system",
            "content": "You are a helpful personal data assistant. Answer the user's question conversationally based on the query results. Be concise.",
        },
        {
            "role": "user",
            "content": f"Question: {question}\n\nQuery results:\n{results.to_string(index=False)}",
        },
    ]
    return _ollama(answer_prompt)


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

            print("Agent: ", end="", flush=True)
            answer = _ask(question, schema, con)
            print(answer)
            print()
    finally:
        con.close()


if __name__ == "__main__":
    run()
