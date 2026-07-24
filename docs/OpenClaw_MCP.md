# Groundhog MCP Service Tools

OpenClaw connects to the `groundhog` stdio MCP server. Groundhog exposes local
facts and state; OpenClaw chooses the user-facing wording and delivery channel.

## Service Tool Contract

| Tool | Input | Result | Ownership |
| --- | --- | --- | --- |
| `get_recent_events` | optional `limit` | JSON list of durable events | Groundhog reads facts |
| `get_pending_outbox` | optional `limit` | JSON list of pending delivery items and source event data | Groundhog exposes pending facts |
| `get_agent_run_status` | none | JSON list with the most recent job run | Groundhog exposes job health |
| `get_latest_alerts` | optional `limit` | JSON list of recent stock alerts | Groundhog exposes analytics results |
| `mark_outbox_delivered` | `outbox_id` | JSON list with the updated delivery record | OpenClaw confirms it delivered an item |

`mark_outbox_delivered` is idempotent. It does not send any message and does
not change the original event. OpenClaw must only call it after successful
delivery through its own configured channel.
