# Groundhog MCP Service Tools

OpenClaw connects to the `groundhog` stdio MCP server. Groundhog exposes local
facts and state; OpenClaw chooses the user-facing wording and delivery channel.

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

## Local LLM Boundary

Groundhog may use the configured local Ollama model to create daily summaries
and weekly reviews over stored facts. It may rank pending outbox items, but it
must not write raw health, activity, stock, signal, alert, or memory records;
change systemd configuration; or mark an item delivered. Generated summaries
are stored in `derived_artifacts` and require OpenClaw or user review before
any user-facing delivery.

Workout semantic search also uses local Ollama embeddings. The index contains
derived copies of stored workout-plan text and may be rebuilt safely; source
workout rows remain authoritative and are never changed by search.
