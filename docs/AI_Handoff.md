# Groundhog — OpenAI Codex Migration Handoff

---

## 1. Project Purpose and Current Status

**Groundhog** is a personal data pipeline and local AI agent. It ingests health, sleep, workout, and stock market data into a single local DuckDB database, runs technical analysis signals, fires macOS alerts on trading signals, and answers natural-language questions about the data via an LLM agent.

**Current status:** Milestone 5 complete. Core data pipelines have been migrated from Mac to Linux under the `openclaw` service user. Stock jobs are ready to run via a systemd user timer. OpenClaw handles chat, scheduling, and delivery; Groundhog remains the local data and analytics layer. `langgraph_client/client.py` has replaced the hand-rolled `mcp_client/client.py` as the active agent — it uses LangChain's `create_agent()` directly rather than a custom `StateGraph`. `mcp_client/client.py` is kept for reference only.

---

## 2. Architecture Overview

```
data sources → ingestion/ → DuckDB → analytics/ → alerts
                                          ↓
                               mcp_server/ (tool server, stdio)
                                          ↓
                          langgraph_client/ (active — create_agent())
                          mcp_client/      (legacy hand-rolled loop, kept for reference)
```

- **Ingestion**: yfinance (stocks), Garmin screenshots via vision LLM (health/sleep), SugarWOD screenshots via vision LLM (workouts)
- **Analytics**: SMA50/200 crossover, Supertrend (daily + weekly) → `stock_signals` → `stock_alerts`
- **Agent**: MCP tool server (stdio JSON-RPC) + LangGraph client (`create_agent()`), replacing the hand-rolled loop
- **Scheduling**: Linux systemd user timer under `openclaw`
- **AI**: Ollama local only. `qwen3:32b` for SQL/text, `qwen3-vl:latest` for vision. No external API calls with personal data.

---

## 3. Important Files and What They Do

| File | Purpose |
|------|---------|
| `config/settings.py` | All paths and model names. Single source of truth. |
| `config/watchlist.txt` | 105 tickers (6 custom periods + 99 Nasdaq-100 at 2y) |
| `ingestion/schema.py` | Idempotent table creation. All `ALTER TABLE ADD COLUMN IF NOT EXISTS`. |
| `ingestion/stocks.py` | yfinance OHLCV fetch → DuckDB upsert. NaN→None via `_safe()`. |
| `ingestion/sleep.py` | Drops sleep screenshots → vision LLM → sleep_metrics upsert. Date from filename. |
| `ingestion/workouts.py` | Drops SugarWOD screenshots → vision LLM → workouts upsert. Hash-based dedup ID. |
| `analytics/signals.py` | SMA50/200 + Supertrend (daily+weekly). Uses `ta` lib for SMA, manual pandas for Supertrend. |
| `analytics/alerts.py` | Reads signal direction flips → optional notification → stock_alerts dedup. |
| `mcp_server/server.py` | MCP stdio tool server. Tools: run_sql, get_latest_price, get_recent_activities, get_health_summary, remember, recall. **Do not modify.** |
| `mcp_client/client.py` | Old hand-rolled agent loop. Replaced. Keep for reference. |
| `langgraph_client/client.py` | Active agent. Uses LangChain's `create_agent()` with MCP tools wrapped as async Python functions. |
| `scripts/daily_stocks.sh` | Chains: stocks.py → signals.py → alerts.py |
| `scripts/update_watchlist.py` | Scrapes Nasdaq-100 from Wikipedia, merges into watchlist.txt. |
| `deploy/systemd/user/groundhog-stocks.service` | systemd user service for the daily stock pipeline. |
| `deploy/systemd/user/groundhog-stocks.timer` | systemd user timer, runs 5pm America/Phoenix on weekdays. |
| `docs/Linux_Operations.md` | Linux host runbook for stock jobs and the systemd user timer. |

---

## 4. Current TODOs and Open Bugs

**In progress:**
- See `TODO.md` for current `langgraph_client` work: `ToolRetryMiddleware` for malformed tool calls, a `write_todos` mutable planning tool, and prompting the agent to revisit its plan after each tool result.

**Planned features:**
- Cross-source insights: "how does sleep affect workout performance?" (requires JOIN across sleep_metrics + workouts)
- Agent querying workouts table (already in DB, just needs schema hints in context)
- Advanced RAG: entity-aware memory, retrieval evaluation
- M6 Production hardening: evals, observability, prompt versioning, guardrails

**Known open items:**
- Sleep data has only a few test rows; no automated ingestion trigger yet (manual drop-and-run)
- Workouts ingestion is manual (drop screenshots, run script); no Linux timer yet
- `notebooks/agent_prompt_evals.ipynb` has uncommitted changes (visible in git status)

---

## 5. Decisions Already Made

- **Local AI only**: Ollama, never OpenAI/Anthropic API for personal data
- **DuckDB not SQLite**: chosen for analytical query performance
- **systemd user timers on Linux**: `openclaw` has linger enabled, so timers run without an active login
- **Date from filename, not screenshot**: screenshot OCR for dates is unreliable (workouts + sleep)
- **`ta` library for SMA, manual pandas for Supertrend**: `pandas-ta` fails on Python 3.14 (numba won't build)
- **Weekly Supertrend**: resample daily OHLCV to weekly with `resample("W-FRI")` — do not fetch weekly bars from yfinance
- **Hash-based workout IDs**: `SHA256(date|name|description[:50])[:16]` for safe re-runs
- **Watchlist default period**: `"2y"` (changed from `"1d"` — 1d wasn't enough for SMA200)
- **Tickers that leave Nasdaq-100 stay in watchlist**: intentional, you may still want to track them
- **Blog posts**: always commit AND push in one step; never commit without pushing

---

## 6. Coding Conventions

- All config (paths, model names) in `config/settings.py`. No hardcoded paths elsewhere.
- `data/` is gitignored. Never commit personal data or the DuckDB file.
- All DB operations: `ON CONFLICT DO NOTHING` or `ON CONFLICT DO UPDATE SET` (not `INSERT OR REPLACE` — that's SQLite syntax, doesn't exist in DuckDB)
- Idempotent scripts: safe to re-run any ingestion or analytics script
- `sys.path.insert(0, ...)` at top of every script that lives below project root (needed for `config` imports)
- No `rowcount` after DuckDB `ON CONFLICT DO NOTHING` — use before/after `COUNT(*)` instead (DuckDB returns -1 for skipped rows)
- Vision prompts: always request JSON output with explicit field names; parse with `json.loads()` on extracted content between ` ```json ``` ` fences
- Blog posts: Jekyll at `~/Projects/treblev.github.io/_posts/`, format `YYYY-MM-DD-slug.md`

---

## 7. Build, Run, Test, Lint Commands

```bash
# Setup
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Initialize DB schema
python ingestion/schema.py

# Run ingestion
python ingestion/stocks.py          # fetch OHLCV for all watchlist tickers
python ingestion/sleep.py           # process sleep screenshots from data/drop/sleep8/
python ingestion/workouts.py        # process workout screenshots from data/drop/workouts/

# Analytics
python analytics/signals.py         # compute SMA + Supertrend signals
python analytics/alerts.py          # check flips, record deduped alerts, optionally notify

# Update watchlist
python scripts/update_watchlist.py  # scrape Nasdaq-100 from Wikipedia, merge

# Agent (old hand-rolled)
python mcp_client/client.py

# Agent (new LangGraph — incomplete)
python langgraph_client/client.py

# No test suite yet. No linter configured.
```

---

## 8. Environment Variables Needed

Required:

```bash
GROUNDHOG_DB_PATH=/home/openclaw/data/groundhog/groundhog.duckdb
```

Recommended for Linux stock jobs when OpenClaw handles delivery:

```bash
GROUNDHOG_ALERT_BACKEND=none
```

The systemd timer is pinned to `America/Phoenix`.

---

## 9. Database Schema Assumptions

Database file: `data/db/groundhog.duckdb`

```sql
health_metrics       -- daily grain: steps, avg_hr, active_minutes, date
stock_watchlist      -- daily OHLCV: date, ticker, open, high, low, closing_price, volume
                     -- PK: (date, ticker)
stock_signals        -- id, date, ticker, signal_type, timeframe, value, direction
                     -- signal_type: 'sma_cross' or 'supertrend'
                     -- timeframe: 'daily' or 'weekly'
                     -- direction: 'bullish' or 'bearish'
                     -- value: SMA gap (sma_cross) or supertrend line price (supertrend)
stock_alerts         -- id, date, ticker, alert_type, message, notified_at
                     -- alert_type: 'golden_cross','death_cross',
                     --   'supertrend_daily_bullish/bearish','supertrend_weekly_bullish/bearish'
sleep_metrics        -- date, resting_hr, hrv, breath_rate,
                     --   time_to_fall_asleep_minutes (nullable), deep_sleep_minutes (nullable)
workouts             -- id, date, day_of_week, name, category, structure_type, description
reminders            -- SCD Type 2: valid_from, valid_to, is_current
activities           -- Garmin activity summary (legacy)
memory               -- agent memory store: key, value, updated_at
```

All tables created idempotently by `ingestion/schema.py`.

---

## 10. External Services / APIs

| Service | Used for | Notes |
|---------|---------|-------|
| yfinance | Stock OHLCV data | Free, no auth needed |
| Wikipedia | Nasdaq-100 ticker list | Requires httpx + browser User-Agent; urllib gets 403 |
| Ollama | LLM inference | Must be running locally; models: qwen3:32b, qwen3-vl:latest |
| notify-send / osascript / stdout | Optional local notification backend | `GROUNDHOG_ALERT_BACKEND=auto|none|notify-send|osascript|stdout` |

No external APIs receive personal data. No API keys needed.

---

## 11. Known Gotchas

1. **DuckDB `rowcount` is -1** for `ON CONFLICT DO NOTHING` skipped rows. Always use before/after `COUNT(*)`.
2. **pandas-ta fails on Python 3.14** — numba won't build. Use `ta` library for SMA; implement Supertrend manually.
3. **Wikipedia 403 with urllib** — `pd.read_html()` is blocked. Use `httpx` with `User-Agent: Mozilla/5.0`.
4. **Wikipedia Nasdaq-100 table**: ticker is `cells[0]`, NOT `cells[1]` (which is company name). Wrong column silently writes garbage tickers.
5. **macOS screenshot filenames use ` `** (narrow no-break space) before AM/PM. Must use the actual unicode character in path strings, not a regular space.
6. **Supertrend direction flip logic**: fires on the PREVIOUS bar's band value (Pine Script's `up1`/`dn1` pattern). Bands must be tracked separately — do not read direction from the supertrend value itself.
7. **Supertrend uses Wilder's RMA**: `ewm(alpha=1/period, adjust=False)` — NOT `alpha=2/(period+1)` (that's EMA).
8. **Weekly Supertrend**: resample daily OHLCV with `resample("W-FRI")`. Do not try to fetch weekly bars from yfinance.
9. **yfinance NaN rows**: some tickers (ADI, LIN) have NaN in OHLCV fields. Must convert with `_safe()` before inserting.
10. **`INSERT OR REPLACE` doesn't exist in DuckDB** — SQLite only. Use `ON CONFLICT DO UPDATE SET` or `ON CONFLICT DO NOTHING`.
11. **Linux timers run under `openclaw`** — keep linger enabled so user services continue without login.
12. **Vision LLM is slow**: `qwen3-vl:latest` can take 14+ minutes for complex screenshots. Workouts ingestion only processes daily screenshots, not weekly calendar views.

---

## 12. Recent Work Completed

In order (most recent last):

- Added `open`, `high`, `low`, `volume` columns to `stock_watchlist`
- Implemented SMA50/200 crossover signals (`analytics/signals.py`)
- Implemented Supertrend (daily + weekly), verified against Pine Script
- Implemented `analytics/alerts.py` with optional platform notification backends and dedup
- Added Linux systemd user timer templates for `openclaw`
- Expanded watchlist from 6 to 105 tickers (Nasdaq-100 via `scripts/update_watchlist.py`)
- Fixed DuckDB rowcount -1 bug
- Fixed NaN handling for ADI/LIN
- Added sleep ingestion (`ingestion/sleep.py`, `sleep_metrics` table)
- Added workout ingestion (`ingestion/workouts.py`, `workouts` table)
- Enriched MCP agent schema context (stock_signals, stock_alerts, workouts hints)
- Rewrote `langgraph_client/client.py` to use `create_agent()` instead of a hand-built `StateGraph`
- Fixed broken tool wrappers in `langgraph_client`

---

## 13. Things NOT to Change Without Asking

- **`mcp_server/server.py`**: stable tool layer; the LangGraph client will call into it
- **`config/settings.py`**: single source of truth; no hardcoded paths anywhere else
- **`config/watchlist.txt`**: custom periods (`INTC 7y`, `BTC-USD max`, `MSFT 10y`, `V 10y`, `NET 7y`, `SNOW 5y`) must be preserved across any watchlist updates
- **Supertrend implementation**: verified correct against Pine Script; do not "simplify"
- **Scheduling**: Linux systemd user timer under `openclaw`, pinned to `America/Phoenix`.
- **AI model selection**: local Ollama only. Do not add OpenAI/Anthropic calls.
- **Date source for sleep/workout ingestion**: date comes from filename, not from screenshot content

---

## 14. Suggested Next Task for Codex

See `TODO.md` for the current punch list on `langgraph_client/client.py`:

1. Add `ToolRetryMiddleware` (from `langchain.agents.middleware`) — `qwen3:32b` occasionally emits a tool call as literal text content instead of a structured `tool_calls` entry, and nothing currently catches it.
2. Add a `write_todos`-style mutable planning tool the agent can call and revise mid-loop, instead of a frozen upfront plan.
3. Add replanning: prompt the agent to check each new tool result against its current plan before proceeding, rather than assuming a mutable todo list alone fixes plan-drift bugs.
