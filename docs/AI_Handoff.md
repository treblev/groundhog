# Groundhog — OpenAI Codex Migration Handoff

---

## 1. Project Purpose and Current Status

**Groundhog** is a personal data pipeline and local AI agent. It ingests health, sleep, workout, spending, and stock market data into a single local DuckDB database, runs technical analysis signals, fires alerts on trading signals, and answers natural-language questions about the data via an LLM agent.

**Current status:** Long-running service roadmap Phases 0-5 are complete, Phase 6 daemon mode is implemented with Linux restart/reboot verification still pending, and Phase 7 local agentic summarization/review work is complete. Production on Linux tracks the `main` branch under the `openclaw` service user. Telegram activity, workout, sleep, and spending screenshots are imported deterministically; Telegram text questions use the direct `/ask <question>` command to invoke the guarded LangGraph agent. `/expense` bypasses chat-model routing and invokes Groundhog's local spending importer directly. The agent has mutable todos, a 12-tool-call limit, database-grounding retries, an internal-details disclosure guard, and deterministic note prefetch when a question names a ticker with active notes. Dedicated tools cover activity and sleep summaries, exact-date workout lookup, local semantic workout-plan search, data freshness, and a market summary that includes Bitcoin. OpenClaw handles chat, commands, scheduling, and delivery; Groundhog remains the local data and analytics layer.

---

## 2. Architecture Overview

```
data sources → ingestion/ → DuckDB → analytics/ → alerts
                    ↑                     ↓
Telegram /expense → OpenClaw plugin       mcp_server/ (stdio tools)
                                              ↓
                                  langgraph_client/ (active agent)
                                  mcp_client/ (legacy reference)

workouts ──→ semantic chunk builder ──→ Ollama embeddings ──→ semantic_chunks
                                  ↑                              ↓
                         index script / search refresh ← search_documents
```

- **Ingestion**: yfinance (stocks), fitness activity screenshots via vision LLM → `activities`, SugarWOD plan screenshots → `workouts`, Sleep8 screenshots → `sleep_metrics`, and Wallet/bank transaction-list screenshots → `spending`
- **Analytics**: SMA50/200 crossover, Supertrend (daily + weekly) → `stock_signals` → `stock_alerts`
- **Agent**: MCP tool server (stdio JSON-RPC) + LangGraph client (`create_agent()`), replacing the hand-rolled loop
- **Semantic retrieval**: `search_documents` indexes and ranks stored workout plans, weekly Supertrend alert history, and active user-authored ticker notes locally. It is required for a non-date workout lookup and semantic historical-alert or ticker-note lookup; exact-date retrieval, current market facts, and structured counts/aggregates stay on MCP data tools or `run_sql`.
- **Direct commands**: OpenClaw's `groundhog-ask-router` handles `/ask`; `groundhog-spending-router` handles `/expense` and `/expense-category`; `groundhog-stock-notes-router` handles ticker-note writes. All bypass outer-model routing and call local code directly.
- **Scheduling**: OpenClaw cron under `openclaw` (daily stocks: 5 PM America/Phoenix, weekdays)
- **AI**: Ollama local only. `qwen3.6:latest` for SQL/text, `qwen3-vl:latest` for vision, `qwen3-embedding:0.6b` for memory and semantic-search embeddings. No external API calls with personal data.

---

## 3. Important Files and What They Do

| File | Purpose |
|------|---------|
| `config/settings.py` | All paths, model names, and Ollama URLs. Single source of truth. |
| `config/watchlist.txt` | 105 tickers (6 custom periods + 99 Nasdaq-100 at 2y) |
| `ingestion/schema.py` | Idempotent table creation. All `ALTER TABLE ADD COLUMN IF NOT EXISTS`. |
| `ingestion/stocks.py` | yfinance OHLCV fetch → DuckDB upsert. NaN→None via `_safe()`. |
| `ingestion/sleep.py` | Drops sleep screenshots → vision LLM → sleep_metrics upsert. Date from filename. |
| `ingestion/workouts.py` | Drops SugarWOD screenshots → vision LLM → workouts upsert. Hash-based dedup ID. |
| `agent/embeddings.py` | Validates and sends batches to Ollama's configured `/api/embed` endpoint. |
| `agent/semantic_search.py` | Builds versioned workout chunks, refreshes their derived vectors idempotently, and ranks semantic search results in DuckDB. |
| `scripts/stock_notes.py` | Canonical ticker-note add/edit/delete/list CLI; preserves revisions and refreshes local note embeddings. |
| `ingestion/health.py` | Activity-result screenshot importer. Supports a direct `--image` path for OpenClaw attachments and writes to `activities`. |
| `ingestion/spending.py` | Local vision importer for Wallet and bank transaction lists. Resolves dates, filters pending rows, classifies, deduplicates, archives, and writes to `spending`. |
| `analytics/signals.py` | SMA50/200 + Supertrend (daily+weekly). Uses `ta` lib for SMA, manual pandas for Supertrend. |
| `analytics/alerts.py` | Reads signal direction flips → optional notification → stock_alerts dedup. |
| `mcp_server/server.py` | MCP stdio tool server. Core data tools plus documented service-state tools in `docs/OpenClaw_MCP.md`. |
| `mcp_client/client.py` | Old hand-rolled agent loop. Replaced. Keep for reference. |
| `langgraph_client/client.py` | Active agent. Uses LangChain's `create_agent()` with MCP tools wrapped as async Python functions. |
| `scripts/ask_groundhog.py` | One-question CLI used by Telegram `/ask`; prints only the guarded agent answer. |
| `deploy/openclaw/plugins/groundhog-ask-router/` | Direct OpenClaw `/ask` command that invokes `scripts.ask_groundhog` without outer-model routing. |
| `deploy/openclaw/plugins/groundhog-request-trace/` | Local JSONL tracing for ordinary OpenClaw request, LLM-call, and tool-call spans. Direct Groundhog subprocesses use `agent/request_trace.py`. |
| `deploy/openclaw/skills/groundhog-ask/SKILL.md` | Fallback `/ask` routing instructions when the direct plugin is unavailable. |
| `deploy/openclaw/plugins/groundhog-spending-router/` | Direct OpenClaw command plugin for `/expense` imports and `/expense-category` corrections. |
| `deploy/openclaw/plugins/groundhog-stock-notes-router/` | Direct OpenClaw command plugin for ticker-note add/edit/delete/list actions. |
| `groundhog_service.py` | Service CLI: `run daily-stocks` and `status` |
| `scripts/daily_stocks.sh` | Manual compatibility entrypoint to `groundhog_service.py run daily-stocks` |
| `scripts/openclaw_deliver_outbox.py` | OpenClaw-side delivery bridge: Groundhog MCP outbox → OpenClaw Telegram → mark delivered on success. |
| `scripts/import_openclaw_activity_media.py` | Deterministic OpenClaw media watcher. Checkpoints old files, imports each new attachment once, and has one-shot `--next-kind plan` routing. |
| `scripts/update_watchlist.py` | Scrapes Nasdaq-100 from Wikipedia, merges into watchlist.txt. |
| `scripts/index_semantic_documents.py` | Manual/dry-run refresh entry point for local workout, weekly-alert, and memory embeddings. |
| `docs/Stock_Alert_Semantic_Search.md` | Design and operating boundary for semantic retrieval of weekly Supertrend alert history. |
| `docs/Stock_Semantic_Notes.md` | Canonical user ticker notes, revision history, direct commands, and semantic retrieval boundary. |
| `deploy/systemd/user/groundhog-stocks.service` | systemd user service retained as a manual fallback for the daily stock pipeline. |
| `deploy/systemd/user/groundhog-stocks.timer` | Legacy systemd timer; disabled in production because OpenClaw cron owns the weekday 5pm Phoenix schedule. |
| `deploy/systemd/user/groundhog-daemon.service` | Optional always-on daemon service; do not enable alongside the timer. |
| `deploy/systemd/user/groundhog-openclaw-media.{service,timer}` | One-minute OpenClaw inbound-media watcher for Telegram screenshots. |
| `docs/Linux_Operations.md` | Linux host runbook for stock jobs and the OpenClaw schedule. |
| `docs/Spending_Skills_Plan.md` | Spending capability boundaries, current workflows, and future skill plan. |

---

## 4. Current TODOs and Open Bugs

**In progress:**
- Optional daemon lifecycle verification: restart behavior and reboot behavior under linger.

**Planned features:**
- Cross-source insights: "how does sleep affect workout performance?" (requires JOIN across sleep_metrics + workouts)
- Cross-source readiness and coaching insights using the dedicated activity, sleep, and workout tools
- Advanced RAG: entity-aware memory, retrieval evaluation
- M6 Production hardening: evals, observability, prompt versioning, guardrails

**Known open items:**
- The media watcher treats incoming images as activity results by default. Before a SugarWOD plan upload, arm exactly one plan with `python -m scripts.import_openclaw_activity_media --next-kind plan` on Linux; it resets after that one file.
- Before a sleep screenshot upload, arm exactly one sleep import with `python -m scripts.import_openclaw_activity_media --next-kind sleep` on Linux; it also resets after one file.

---

## 5. Decisions Already Made

- **Local AI only**: Ollama, never OpenAI/Anthropic API for personal data
- **DuckDB not SQLite**: chosen for analytical query performance
- **OpenClaw cron on Linux**: the Gateway owns the weekday stock schedule; the legacy systemd stock timer is disabled to prevent duplicate runs
- **Date from filename, not screenshot**: screenshot OCR for dates is unreliable (workouts + sleep)
- **`ta` library for SMA, manual pandas for Supertrend**: `pandas-ta` fails on Python 3.14 (numba won't build)
- **Weekly Supertrend**: resample daily OHLCV to weekly with `resample("W-FRI")` — do not fetch weekly bars from yfinance
- **Weekly Supertrend only uses completed weeks**: Friday-labelled resampled bars are excluded until that Friday arrives; this prevents Monday–Thursday data from producing future-dated weekly signals or alerts.
- **Hash-based workout IDs**: `SHA256(date|name|description[:50])[:16]` for safe re-runs
- **Workout semantic index is derived, local, and versioned**: index one whole-plan chunk plus recognized Fitness, Performance, HYROX, Tread, Row, and Floor sections. Store source metadata, content hashes, model name, and vectors in `semantic_chunks`; refresh changed/model-mismatched chunks and delete stale chunks. `workouts` remains the source of truth.
- **Semantic retrieval is for workout intent, weekly Supertrend alert history, and ticker-note research context**: `search_documents` embeds a query locally and uses DuckDB cosine similarity. Workout search returns the best chunk per workout; stock-alert search returns qualifying weekly bullish/bearish flips; stock-note search returns active user-authored notes. Ticker filters are supported for both stock domains. Do not use it for exact-date workout retrieval, current stock facts, aggregates, OHLCV bars, or signal-state queries.
- **Telegram screenshots are deterministic**: do not rely on the OpenClaw chat model to interpret an image. `groundhog-openclaw-media.timer` scans `/home/openclaw/media/inbound` once per minute and calls the local `qwen3-vl:latest` importer.
- **Spending uses a registered command, not model routing**: `/expense` is owned by the OpenClaw spending plugin and directly invokes `python -m ingestion.spending`. This prevents generic chat responses and repeated tool-selection attempts.
- **Ticker notes use registered commands, not model routing**: `/stocks-add-notes`, `/stocks-edit-notes`, and `/stocks-delete-notes` invoke the canonical local note CLI. Each write updates an append-only revision record and refreshes only derived local semantic chunks.
- **Spending dates come from the transaction row**: explicit bank dates are parsed directly; relative Wallet labels are resolved against the Phoenix-local upload date.
- **Spending imports are idempotent**: an identical image hash is skipped, and the same merchant/amount within a three-day window is treated as a duplicate across screenshots.
- **Merchant rules override vision guesses**: normalized Circle K merchants are always categorized as `beer`; manual category correction remains available.
- **One SugarWOD screenshot is one plan**: multiple visible cards/sections are combined into one `workouts` record, with all card text in `description`.
- **MCP DuckDB lifecycle**: `mcp_server/server.py` must open and close its DuckDB connection per tool call. A persistent write connection blocks ingestion in another process.
- **Watchlist default period**: `"2y"` (changed from `"1d"` — 1d wasn't enough for SMA200)
- **Tickers that leave Nasdaq-100 stay in watchlist**: intentional, you may still want to track them
- **Blog posts**: always commit AND push in one step; never commit without pushing

---

## 6. Coding Conventions

- All config (paths, model names, and Ollama URLs) in `config/settings.py`. No hardcoded paths elsewhere.
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
python -m ingestion.spending import --image <path> --reference-date YYYY-MM-DD

# Build/inspect the derived local embedding index. Search refreshes workout
# chunks automatically, so this is primarily useful after a bulk import or for checks.
python scripts/index_semantic_documents.py
python scripts/index_semantic_documents.py --domain all
python scripts/index_semantic_documents.py --domain stock_alert
python scripts/index_semantic_documents.py --domain stock_note
python scripts/index_semantic_documents.py --dry-run

# Inspect or remove accidental future-dated weekly Supertrend artifacts.
python scripts/repair_weekly_supertrend.py --dry-run
python scripts/repair_weekly_supertrend.py

# Analytics
python analytics/signals.py         # compute SMA + Supertrend signals
python analytics/alerts.py          # check flips, record deduped alerts, optionally notify

# Update watchlist
python scripts/update_watchlist.py  # scrape Nasdaq-100 from Wikipedia, merge

# Agent (old hand-rolled)
python mcp_client/client.py

# Agent (active LangGraph client)
python langgraph_client/client.py

# Tests
python -m unittest discover -s tests -p 'test_*.py'  # offline regression tests
python tests/smoke_test.py                           # live DB and Ollama checks

# No linter configured.
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

Ollama base URL is configured in `config/settings.py`:

```python
OLLAMA_BASE_URL = "http://Vijays-MacBook-Pro.local:11434"
```

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
stock_notes          -- current user-authored ticker notes; soft-deleted notes are excluded from retrieval
stock_note_revisions -- append-only stock-note text and lifecycle history
sleep_metrics        -- date, resting_hr, hrv, breath_rate,
                     --   time_to_fall_asleep_minutes (nullable), deep_sleep_minutes (nullable)
workouts             -- id, date, day_of_week, name, category, structure_type, description
semantic_chunks      -- derived local vector index: workout chunks and weekly Supertrend alerts;
                       active ticker-note chunks use domain stock_note
                     -- id, domain, source_id, source_date,
                     -- chunk_kind/index, section_label, title, content, metadata,
                     -- content_hash, embedding_model, embedding, timestamps
reminders            -- SCD Type 2: valid_from, valid_to, is_current
activities           -- Garmin activity summary (legacy)
spending             -- id, transaction_date, visible_date_label, merchant, amount,
                     --   payment_method, category, source_image_hash, source_row,
                     --   created_at, updated_at
memory               -- agent memory store: key, value, updated_at
```

All tables created idempotently by `ingestion/schema.py`.

---

## 10. External Services / APIs

| Service | Used for | Notes |
|---------|---------|-------|
| yfinance | Stock OHLCV data | Free, no auth needed |
| Wikipedia | Nasdaq-100 ticker list | Requires httpx + browser User-Agent; urllib gets 403 |
| Ollama | LLM inference | Must be reachable at `OLLAMA_BASE_URL`; models: qwen3.6:latest, qwen3-vl:latest, qwen3-embedding:0.6b |
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
13. **Transaction screenshots may show two amounts**: bank rows often show the charge plus a smaller running balance. Only the charge belongs in `spending`.
14. **Pending transactions are not imported**: a row explicitly labeled pending is skipped. Posted rows need a merchant, charge amount, and resolvable date.
15. **Semantic chunks are not user data to edit directly**: they are rebuildable derived copies of workout text. Use `scripts/index_semantic_documents.py` to refresh them; do not treat them as the canonical workout record.
16. **Embedding model changes trigger reindexing**: `sync_workout_embeddings()` only reuses a chunk when both its content hash and `embedding_model` match `OLLAMA_EMBEDDING_MODEL`. All compared vectors therefore come from one model/dimension.
17. **Weekly signals must not be future-dated**: `W-FRI` labels a partial week with that Friday. Keep only labels on or before the Phoenix current date; use `scripts/repair_weekly_supertrend.py` for any premature historical rows.

---

## 12. Recent Work Completed

In order (most recent last):

- Added `open`, `high`, `low`, `volume` columns to `stock_watchlist`
- Implemented SMA50/200 crossover signals (`analytics/signals.py`)
- Implemented Supertrend (daily + weekly), verified against Pine Script
- Implemented `analytics/alerts.py` with optional platform notification backends and dedup
- Added Linux systemd user timer templates for `openclaw`
- Verified the Linux timer path completes successfully through `groundhog-stocks.service`
- Configured OpenClaw Telegram delivery and scheduled `groundhog-outbox-telegram` to deliver Groundhog outbox rows every 15 minutes
- Expanded watchlist from 6 to 105 tickers (Nasdaq-100 via `scripts/update_watchlist.py`)
- Fixed DuckDB rowcount -1 bug
- Fixed NaN handling for ADI/LIN
- Added sleep ingestion (`ingestion/sleep.py`, `sleep_metrics` table)
- Added workout ingestion (`ingestion/workouts.py`, `workouts` table)
- Added deterministic Telegram screenshot intake on Linux. The initial watcher checkpoint ignored existing Telegram images; it processes later files only and records its state at `/home/openclaw/data/groundhog/openclaw_activity_media_state.json`.
- Added direct activity screenshot ingestion into the existing `activities` table and verified three Telegram uploads end-to-end.
- Added one-shot SugarWOD plan routing, and corrected the importer so one multi-card screenshot creates one combined plan record.
- Fixed MCP DuckDB locking by removing the persistent connection and import-time schema write.
- Enriched MCP agent schema context (stock_signals, stock_alerts, workouts hints)
- Rewrote `langgraph_client/client.py` to use `create_agent()` instead of a hand-built `StateGraph`
- Fixed broken tool wrappers in `langgraph_client`
- Added Groundhog service run tracking, events, outbox rows, service-state MCP tools, local summary artifacts, and optional daemon mode
- Added deterministic Telegram sleep uploads, pool-swim metrics, and Telegram import confirmations
- Added guarded Telegram `/ask` queries through LangGraph and a deterministic OpenClaw command plugin
- Added tool-call limits, grounding retries, and internal-details response protection
- Added dedicated activity summary, sleep summary, workout lookup, data freshness, and Bitcoin-inclusive market-summary tools
- Promoted the tested `long-running-agent` history into `main`; Linux production now tracks `main`
- Added deterministic `/expense` and `/expense-category` commands through the OpenClaw spending plugin
- Added Wallet and bank transaction-list vision ingestion, the `spending` table, pending-row filtering, date parsing, image and cross-screenshot deduplication, and source-image archiving
- Added fixed merchant categorization so Circle K spending is always classified as `beer`
- Added local workout semantic search: Ollama embeddings, idempotent DuckDB chunk index, semantic MCP tool, query filters, and LangGraph guidance requiring it for non-date workout requests
- Added local semantic retrieval for historical weekly Supertrend alerts, derived from `stock_alerts` with ticker, direction, and date filters; prices and current signal state remain structured queries
- Added editable canonical ticker notes with append-only revisions, direct Telegram commands, and local semantic retrieval in the `stock_note` domain
- Fixed partial-week weekly Supertrend handling, added repair tooling for premature future-dated alerts, and queued a Telegram completion summary after every daily stock run

---

## 13. Things NOT to Change Without Asking

- **`mcp_server/server.py`**: stable tool layer; the LangGraph client will call into it
- **`config/settings.py`**: single source of truth; no hardcoded paths anywhere else
- **`config/watchlist.txt`**: custom periods (`INTC 7y`, `BTC-USD max`, `MSFT 10y`, `V 10y`, `NET 7y`, `SNOW 5y`) must be preserved across any watchlist updates
- **Supertrend implementation**: verified correct against Pine Script; do not "simplify"
- **Scheduling**: OpenClaw cron under `openclaw`, pinned to `America/Phoenix`.
- **AI model selection**: local Ollama only. Do not add OpenAI/Anthropic calls.
- **Date source**: sleep and workout plans use upload/filename metadata as a
  placeholder date; completed activities always use the date visible in their
  screenshot (the upload date is only a year-resolution reference).

---

## 14. Suggested Next Task for Codex

The only current TODO is the optional notebook prompt-override workflow in `TODO.md`. Keep the production branch on `main`; use short-lived feature branches for future work and promote them only after tests pass.
