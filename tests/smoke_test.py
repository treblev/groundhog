"""
Smoke tests for Groundhog.

Not a substitute for the eval notebooks (notebooks/vision_prompt_evals.ipynb,
notebooks/agent_prompt_evals.ipynb) -- those test LLM/prompt behavior against
labeled data. This only checks the deterministic layer underneath: that every
module still imports cleanly (e.g. after a config.settings change like
requiring GROUNDHOG_DB_PATH), and that the agent's tool functions run against
the live DB without raising.

Run: python tests/smoke_test.py
"""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb

PASS: list[str] = []
FAIL: list[tuple[str, Exception]] = []

MODULES = [
    "config.settings",
    "ingestion.schema",
    "ingestion.health",
    "ingestion.workouts",
    "ingestion.sleep",
    "ingestion.stocks",
    "agent.query",
    "agent.memory",
    "analytics.signals",
    "analytics.alerts",
    "mcp_server.server",
]


def check(name: str, fn) -> None:
    try:
        result = fn()
        PASS.append(name)
        print(f"  PASS  {name}" + (f"  -> {result}" if result else ""))
    except Exception as e:
        FAIL.append((name, e))
        print(f"  FAIL  {name}: {e}")


def _first_ticker(con: duckdb.DuckDBPyConnection) -> str:
    row = con.execute("SELECT ticker FROM stock_watchlist LIMIT 1").fetchone()
    return row[0] if row else "AAPL"


def run() -> None:
    print("=== Module imports ===")
    for mod in MODULES:
        check(f"import {mod}", lambda mod=mod: importlib.import_module(mod))

    from config.settings import DB_PATH
    import agent.query as q
    import agent.memory as mem

    con = duckdb.connect(str(DB_PATH))

    print("\n=== Tool functions (against the live DB) ===")
    check("run_sql", lambda: q._run_sql(con, "SELECT 1"))
    check("get_latest_price", lambda: q._get_latest_price(con, _first_ticker(con)))
    check("get_recent_activities", lambda: q._get_recent_activities(con, 3))
    check("get_health_summary", lambda: q._get_health_summary(con, "2026-01-01"))

    print("\n=== Memory tools (write + read, cleaned up after) ===")
    test_fact = "SMOKE_TEST_FACT -- safe to ignore, deleted automatically by tests/smoke_test.py"
    check("remember", lambda: mem.remember(con, test_fact))
    check("recall", lambda: mem.recall(con, "smoke test"))
    con.execute("DELETE FROM memory WHERE fact = ?", [test_fact])

    con.close()

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    run()
