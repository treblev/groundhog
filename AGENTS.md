# Groundhog — Agent Instructions

Personal data pipeline + local AI agent. Ingests health, sleep, workout, spending, and stock data into a local DuckDB database. Runs technical analysis signals and fires alerts. Answers natural-language questions via an LLM agent backed by MCP tools.

**Full handoff context**: `docs/AI_Handoff.md`

---

## Repo Layout

```
config/          settings.py (all paths/models), watchlist.txt
ingestion/       schema.py, stocks.py, sleep.py, workouts.py, health.py, spending.py
analytics/       signals.py (SMA+Supertrend), alerts.py (notifications)
mcp_server/      server.py — stdio MCP tool server (DO NOT MODIFY)
mcp_client/      client.py — old hand-rolled agent loop (reference only)
langgraph_client/client.py — active LangGraph agent
scripts/         daily_stocks.sh, update_watchlist.py
deploy/openclaw/ OpenClaw skills, plugins, and deployment assets
notebooks/       vision and agent prompt eval experiments
docs/            architecture, operations, and feature plans
data/            gitignored — DB, logs, drop folders
```

---

## Setup

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python ingestion/schema.py   # create tables (idempotent)
# Ollama must be running: ollama serve
# Models needed: qwen3.6:latest, qwen3-vl:latest, nomic-embed-text
```

`GROUNDHOG_DB_PATH` is required. Other paths and models are configured in
`config/settings.py`, with environment-variable overrides where defined.

---

## Run Commands

```bash
python ingestion/stocks.py        # fetch OHLCV for all 105 watchlist tickers
python ingestion/sleep.py         # process screenshots from data/drop/sleep8/
python ingestion/workouts.py      # process screenshots from data/drop/workouts/
python -m ingestion.spending import --image <path> --reference-date YYYY-MM-DD
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

- All paths, model names, and Ollama URLs live in `config/settings.py` — no hardcoded values anywhere else
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
| Scheduling | OpenClaw cron | Runs as `openclaw`; daily stocks run at 5pm America/Phoenix on weekdays |
| Supertrend bands | Manual pandas | pandas-ta fails on Python 3.14 (numba won't build) |
| SMA | `ta` library | Same reason — pandas-ta broken |
| Weekly signals | Resample daily OHLCV with `resample("W-FRI")` | Don't fetch weekly bars from yfinance |
| Workout/sleep dates | From filename | Screenshot OCR is unreliable for dates |
| Spending intake | Direct OpenClaw `/expense` command | Bypasses model routing and deterministically invokes Groundhog ingestion |
| Spending dates | Visible transaction label + Phoenix upload date | Wallet uses relative labels; bank screenshots may contain calendar dates |
| Spending deduplication | Same merchant and amount within three days | Covers posting-date shifts between Wallet and bank views |
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
10. Spending screenshots may show a charge and a running balance — import only the charge amount
11. Pending spending rows are skipped; posted rows require a merchant, amount, and resolvable date

---

## Do Not Change Without Asking

- `mcp_server/server.py` — stable tool interface; LangGraph client calls into it
- `config/settings.py` — single source of truth for all paths/models
- `config/watchlist.txt` custom periods — `INTC 7y`, `BTC-USD max`, `MSFT 10y`, `V 10y`, `NET 7y`, `SNOW 5y`
- Supertrend implementation in `analytics/signals.py` — verified correct against Pine Script
- Scheduling mechanism — OpenClaw cron under `openclaw`, `America/Phoenix`, 5pm weekdays

---

## How to Verify Work Is Done

- **Regression suite**: `python -m unittest discover -s tests -p 'test_*.py'`
- **Live smoke suite**: `python tests/smoke_test.py`
- **Ingestion**: run the script, check printed row counts; query DuckDB directly
- **Signals**: `SELECT COUNT(*) FROM stock_signals WHERE date = (SELECT MAX(date) FROM stock_signals);`
- **Alerts**: `SELECT * FROM stock_alerts ORDER BY notified_at DESC LIMIT 10;`
- **Schema changes**: re-run `ingestion/schema.py` — must be idempotent (no errors on second run)
- **Agent**: ask "what is the latest closing price for AAPL?" — should return a number without errors
- **Spending**: run `python -m unittest tests.test_spending_ingestion`; verify `/expense` imports posted rows and `/expense-category` updates one short transaction ID
- **Anything touching Supertrend**: spot-check AAPL or MSFT direction against TradingView Supertrend (period=10, multiplier=3)

## Issue Tracking

- GitHub Issues is the durable backlog for features and bugs. Keep `TODO.md` only for temporary local notes that are not ready to become issues.
- Use the project GitHub MCP server for issue and pull-request operations. Fall back to `gh` only when MCP is unavailable.
- Before creating an issue, search open and closed issues for duplicates. Include the problem or outcome, relevant context, acceptance criteria, and verification notes.
- Use `[Feature]` or `[Bug]` at the start of an issue title when an equivalent repository label is not available.
- Read the relevant issue before starting issue-backed work. Update or close it only after the requested change is verified, and link the pull request or commit when one exists.
- Never put personal health, sleep, workout, spending, account, portfolio, screenshot, database, log, token, or secret data in GitHub issues, comments, or pull requests. Describe behavior using sanitized examples only.
- Creating, editing, commenting on, or closing an issue is an external write. Do it only when the user asks to track or update the work; MCP write tools are configured to request approval.

## Communication

- Explain issues directly and concisely. State what happened, why it happened, and what changed. Avoid jargon, unnecessary implementation detail, and restating prior context.
- If the user asks a question, answer the question only. Do not start implementing changes unless the user explicitly asks for implementation.
