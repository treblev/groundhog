# Groundhog MCP Service Tools

OpenClaw connects to the `groundhog` stdio MCP server. Groundhog exposes local
facts, derived retrieval results, and state; OpenClaw chooses the user-facing
wording and delivery channel.

## Semantic Retrieval Architecture

`search_documents` is the read-only semantic retrieval boundary for stored
workout plans and historical weekly Supertrend alerts. Its supported domains are
`workout` and `stock_alert`.

```text
workouts (authoritative rows)
  → whole-plan + recognized section chunks
  → local Ollama /api/embed batches
  → semantic_chunks (derived DuckDB rows)
  → query embedding + DuckDB list_cosine_similarity
  → one best chunk per workout, ranked evidence returned to the agent

stock_alerts (authoritative rows; weekly Supertrend flips only)
  → one derived alert chunk with ticker/direction metadata
  → local Ollama /api/embed batches → semantic_chunks
  → query embedding + DuckDB list_cosine_similarity
```

The derived index stores the source workout ID and date, chunk kind/index,
optional section label, title, original chunk text, metadata, content hash,
embedding model, vector, and timestamps. A whole-plan `day` chunk is created
for every non-empty plan. Additional `section` chunks are created for recognized
Fitness, Performance, HYROX, Tread, Row, and Floor headings. The chunk text is
enriched before embedding with plan metadata and expansions for common workout
abbreviations (for example, `DB` → dumbbell and `EMOM` → every minute on the
minute).

The index is safe to rebuild: synchronization reads source rows, calls Ollama
outside a DuckDB write lock, then upserts only chunks whose content hash or
embedding model changed and deletes stale chunks. Workout rows are never
modified. Searches refresh the workout index by default; operators can also run
`python scripts/index_semantic_documents.py [--domain workout|stock_alert|memory|all]` or
add `--dry-run` to validate without writing.

## Direct Command Boundary

Spending screenshot ingestion does not use MCP. The registered OpenClaw
commands `/expense` and `/expense-category` are provided by
`deploy/openclaw/plugins/groundhog-spending-router` and invoke
`ingestion.spending` directly. This deterministic route prevents the chat model
from treating an expense upload as a general image question or repeatedly
deciding whether to call a tool. Do not add an overlapping spending-write MCP
tool unless the direct-command design is intentionally replaced.

## Service Tool Contract

| Tool | Input | Result | Ownership |
| --- | --- | --- | --- |
| `search_documents` | semantic query, workout domain, optional result/date/section/structure filters | JSON list of ranked workout-plan evidence | Groundhog refreshes its derived local index and reads workout facts |
| `get_recent_events` | optional `limit` | JSON list of durable events | Groundhog reads facts |
| `get_pending_outbox` | optional `limit` | JSON list of pending delivery items and source event data | Groundhog exposes pending facts |
| `get_agent_run_status` | none | JSON list with the most recent job run | Groundhog exposes job health |
| `get_latest_alerts` | optional `limit` | JSON list of recent stock alerts | Groundhog exposes analytics results |
| `mark_outbox_delivered` | `outbox_id` | JSON list with the updated delivery record | OpenClaw confirms it delivered an item |

`mark_outbox_delivered` is idempotent. It does not send any message and does
not change the original event. OpenClaw must only call it after successful
delivery through its own configured channel.

The Linux deployment uses `scripts/openclaw_deliver_outbox.py` for Telegram
delivery. It reads pending rows through `get_pending_outbox`, sends through
OpenClaw's Telegram channel, and calls `mark_outbox_delivered` only after a
successful send response.

### `search_documents` contract

Required input: `query`. Optional inputs are `domain` (`workout` or
`stock_alert`),
`top_k` (1–10, default 5), inclusive `start_date` and `end_date`, exact
case-insensitive `section`, and exact case-insensitive `structure_type`.
Stock-alert retrieval also accepts exact case-insensitive `ticker` and
`direction` (`bullish` or `bearish`) filters.

Groundhog embeds the query with the configured local embedding model, limits
candidate chunks to the same embedding model, applies supplied filters, and
uses DuckDB cosine similarity. Multiple matching chunks from the same workout
are deduplicated so the response contains one highest-scoring evidence item per
workout. Each result includes the stable `source_id`, workout date and metadata,
the matching chunk/section, its text, the full authoritative workout description,
and a similarity score.

Use this tool for requests by meaning, movements, equipment, format, similarity,
or training focus, plus historical weekly Supertrend alerts by meaning or
similarity. Use `get_workout_for_date` for a known date and `run_sql` for
counts, aggregates, current stock prices, current signal state, and exact alert
listings. The LangGraph system prompt enforces this distinction. Do not embed
OHLCV bars or use semantic search for deterministic market calculations.

## Local LLM Boundary

Groundhog may use the configured local Ollama model to create daily summaries
and weekly reviews over stored facts. It may rank pending outbox items, but it
must not write raw health, activity, stock, signal, alert, or memory records;
change systemd configuration; or mark an item delivered. Generated summaries
are stored in `derived_artifacts` and require OpenClaw or user review before
any user-facing delivery.

Workout semantic search uses only local Ollama embeddings (`qwen3-embedding:0.6b`
in the current configuration). No workout text or query is sent to an external
embedding service. Embeddings rank stored evidence; they do not create workout
facts, schedule a workout, save a selection, or alter a plan.

The same local-only boundary applies to weekly Supertrend alert retrieval. The
canonical `stock_alerts` row supplies each derived chunk; only
`supertrend_weekly_bullish` and `supertrend_weekly_bearish` rows are indexed.
