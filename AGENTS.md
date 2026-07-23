# Groundhog — Agent Instructions

Personal data pipeline + local AI agent. Ingests health, sleep, workout, and stock data into a local DuckDB database. Runs technical analysis signals and fires macOS alerts. Answers natural-language questions via an LLM agent backed by MCP tools.

**Full handoff context**: `docs/AI_Handoff.md`

---

## Repo Layout

```
config/          settings.py (all paths/models), watchlist.txt
ingestion/       schema.py, stocks.py, sleep.py, workouts.py, health.py
analytics/       signals.py (SMA+Supertrend), alerts.py (notifications)
mcp_server/      server.py — stdio MCP tool server (DO NOT MODIFY)
mcp_client/      client.py — old hand-rolled agent loop (reference only)
langgraph_client/client.py — new LangGraph agent (IN PROGRESS, step 2/7)
scripts/         daily_stocks.sh, update_watchlist.py
notebooks/       vision and agent prompt eval experiments
docs/            AI_Handoff.md
data/            gitignored — DB, logs, drop folders
```

---

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python ingestion/schema.py   # create tables (idempotent)
# Ollama must be running: ollama serve
# Models needed: qwen3:32b, qwen3-vl:latest
```

No environment variables needed. All config in `config/settings.py`.

---

## Run Commands

```bash
python ingestion/stocks.py        # fetch OHLCV for all 105 watchlist tickers
python ingestion/sleep.py         # process screenshots from data/drop/sleep8/
python ingestion/workouts.py      # process screenshots from data/drop/workouts/
python analytics/signals.py       # compute SMA50/200 + Supertrend signals
python analytics/alerts.py        # check direction flips, record deduped alerts, optionally notify
python scripts/update_watchlist.py  # refresh Nasdaq-100 tickers from Wikipedia
python mcp_client/client.py       # run old agent (REPL)
python langgraph_client/client.py # run new agent (incomplete)
python -m unittest discover -s tests -p 'test_*.py'  # offline regression tests
python tests/smoke_test.py        # live DB, MCP imports, and Ollama memory checks
```

No linter configured.

---

## Coding Conventions

- All paths and model names live in `config/settings.py` — no hardcoded values anywhere else
- `data/` is gitignored — never commit personal data or the `.duckdb` file
- AI is local-only via Ollama — never call OpenAI/Anthropic with personal data
- Every script must be idempotent (safe to re-run)
- `sys.path.insert(0, str(Path(__file__).resolve().parent.parent))` at top of every script below root
- DuckDB upserts: use `ON CONFLICT DO NOTHING` or `ON CONFLICT DO UPDATE SET` — not `INSERT OR REPLACE` (SQLite-only)
- Never trust `result.rowcount` after `ON CONFLICT DO NOTHING` in DuckDB — it returns -1. Use before/after `COUNT(*)` instead
- Vision prompts: request JSON output; parse content between ` ```json ``` ` fences
- Date for sleep/workout DB rows comes from the **filename**, not screenshot content

---

## Architecture Decisions

| Decision | Choice | Reason |
|----------|--------|--------|
| Database | DuckDB | Analytical queries, local file, no server |
| AI runtime | Ollama (local) | Personal data must not leave the machine |
| Scheduling | systemd user timer | Runs under the `openclaw` service user; linger is enabled |
| Supertrend bands | Manual pandas | pandas-ta fails on Python 3.14 (numba won't build) |
| SMA | `ta` library | Same reason — pandas-ta broken |
| Weekly signals | Resample daily OHLCV with `resample("W-FRI")` | Don't fetch weekly bars from yfinance |
| Workout/sleep dates | From filename | Screenshot OCR is unreliable for dates |
| Watchlist default period | `"2y"` | SMA200 needs 200+ rows; `"1d"` was not enough |

---

## Known Gotchas

1. `ON CONFLICT DO NOTHING` in DuckDB returns `rowcount = -1` — never use it to count inserts
2. pandas-ta is broken on Python 3.14 (numba compile failure) — don't try to install it
3. Wikipedia blocks urllib/pandas `read_html` — use `httpx` with a browser User-Agent
4. Wikipedia Nasdaq-100 table: ticker is `cells[0]`, company name is `cells[1]` — easy to mix up
5. macOS screenshot filenames contain ` ` (narrow no-break space) before AM/PM — not a regular space
6. Supertrend uses Wilder's RMA: `ewm(alpha=1/period, adjust=False)` — not EMA (`alpha=2/(period+1)`)
7. Supertrend direction flip fires on the **previous bar's** band value (Pine Script `up1`/`dn1` pattern)
8. yfinance returns NaN for some tickers (ADI, LIN) — always run through `_safe()` before inserting
9. Vision LLM (`qwen3-vl`) is slow — 14+ min for complex screenshots; only process daily images

---

## Do Not Change Without Asking

- `mcp_server/server.py` — stable tool interface; LangGraph client calls into it
- `config/settings.py` — single source of truth for all paths/models
- `config/watchlist.txt` custom periods — `INTC 7y`, `BTC-USD max`, `MSFT 10y`, `V 10y`, `NET 7y`, `SNOW 5y`
- Supertrend implementation in `analytics/signals.py` — verified correct against Pine Script
- Scheduling mechanism — systemd user timer under `openclaw`, `TZ=America/Phoenix`, 5pm MST

---

## How to Verify Work Is Done

- **Regression suite**: `python -m unittest discover -s tests -p 'test_*.py'`
- **Live smoke suite**: `python tests/smoke_test.py`
- **Ingestion**: run the script, check printed row counts; query DuckDB directly
- **Signals**: `SELECT COUNT(*) FROM stock_signals WHERE date = (SELECT MAX(date) FROM stock_signals);`
- **Alerts**: `SELECT * FROM stock_alerts ORDER BY notified_at DESC LIMIT 10;`
- **Schema changes**: re-run `ingestion/schema.py` — must be idempotent (no errors on second run)
- **Agent**: ask "what is the latest closing price for AAPL?" — should return a number without errors
- **Anything touching Supertrend**: spot-check AAPL or MSFT direction against TradingView Supertrend (period=10, multiplier=3)
