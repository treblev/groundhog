# Local Request Tracing

Groundhog writes local request traces as daily JSONL files under:

```text
<Groundhog data directory>/logs/request-traces/YYYY-MM-DD.jsonl
```

On Linux this resolves to:

```text
/home/openclaw/data/groundhog/logs/request-traces/
```

Trace files are retained for 15 days. They never contain image bytes. Image
calls record the local path and the enclosing activity request records its
content hash. Prompt and response text, tool arguments and results, errors,
models, start times, and durations are retained locally.

## Live lifecycle in OpenClaw

The OpenClaw trace plugin also emits metadata-only lifecycle summaries through
`api.logger`. In the dashboard, open the avatar/name menu at the bottom-left,
then **Settings → System → Logs**. Enable **Auto-follow** and filter for
`groundhog-trace`.

Events include `message_received`, `request_started`, `model_started`,
`context_ready`, `model_finished`, `tool_started`, `tool_finished`,
`request_finished`, and `message_sent`. `trace_ready` confirms that the plugin
loaded. `observer_error` reports a tracing failure without blocking the agent.

Use `run_id` to follow an agent turn. `session_ref` and `message_ref` are hashed
references for correlation without exposing channel identities. Inbound events
may arrive before a run ID is assigned; unavailable fields are omitted rather
than guessed. Delivery outcomes are separate from agent completion and do not
prove that the recipient read the message.

Summaries contain stage names, identifiers, model/tool names, durations, status,
and context-size counts. They exclude prompts, replies, tool arguments/results,
raw error messages, and credentials. The detailed private JSONL contract below
is unchanged. `model_finished` is emitted when the model call ends, even if the
agent's final response has not arrived. A long model call does not produce token
progress here; Ollama's own server log reports prompt-processing progress.

Run the offline plugin tests with:

```bash
node --test deploy/openclaw/plugins/groundhog-request-trace/core.test.js
```

## Local-model context policy

Telegram's channel-scoped DM sessions use `channels.telegram.dmHistoryLimit: 1`.
This bounds model replay to roughly one or two recent user turns, including
their assistant/tool messages; OpenClaw evicts turns in batches. It does not
erase stored transcripts. A value of zero means no session-turn trimming.

The configured `qwen3.8:27b-mlx` provider-model entry uses
`contextTokens: 32768` as its practical input budget. Its native
`contextWindow: 262144` remains unchanged. Merge the budget into that existing
model entry rather than replacing the provider's model list. The installed
runtime bounds its normal compaction reserve to a quarter of the budget, so
token-pressure maintenance starts earlier under the smaller budget.

Apply these controls before testing the same channel session again. Refresh an
oversized session through the supported `sessions.reset` Gateway operation when
a clean context is desired. Verify actual model input size and MCP tool usage;
a fresh headless test alone does not establish Telegram performance. Scheduled
jobs and the Groundhog data MCP interface are independent of this channel replay
setting.

## Record contract

Every request is ordered by one `request_id`:

1. `request_start`
2. zero or more `llm_call` and `tool_call` spans
3. `request_end`

An LLM or tool call is one record containing both sides of the call. There are
no separate call-start/call-completed records and no `completed_at` field.

```json
{"request_id":"abc","type":"request_start","started_at":"2026-08-12T19:20:00Z","source":"telegram","operation":"ask","metadata":{"question":"..."}}
{"request_id":"abc","type":"llm_call","started_at":"2026-08-12T19:20:01Z","duration_ms":842311,"status":"passed","model":"qwen3-vl:latest","prompt":"...","response":"...","error":null}
{"request_id":"abc","type":"tool_call","started_at":"2026-08-12T19:34:04Z","duration_ms":18,"status":"passed","tool":"get_recent_activities","arguments":{"limit":5},"result":"...","error":null}
{"request_id":"abc","type":"request_end","started_at":"2026-08-12T19:34:05Z","duration_ms":845000,"status":"passed","error":null,"metadata":{}}
```

`request_end.status` is always `passed` or `failed`. Failed call spans carry
their error in the same combined record.

## Covered paths

- OpenClaw agent turns through `groundhog-request-trace`
- `/ask`, including LangGraph model calls, verifier calls, and MCP tool calls
- asynchronous activity and `/expense` uploads, including every Qwen attempt/retry
- stock-note operations and their local embedding calls

The Python tracer is Groundhog-owned because `/ask`, activity processing, and
media-queue processing runs outside OpenClaw's model lifecycle hooks.

All `/ask` paths use one combined grounding/disclosure review per final answer.
The reviewer disables reasoning and limits JSON output to 128 tokens. A failed
review can produce an answer correction followed by one combined recheck;
there are no separate grounding and disclosure LLM passes. Review calls remain
ordinary `llm_call` spans, identifiable by the combined review system prompt.

## Inspection

Show one request in chronological file order:

```bash
jq -c 'select(.request_id == "REQUEST_ID")' \
  /home/openclaw/data/groundhog/logs/request-traces/*.jsonl
```

Show failed requests:

```bash
jq -c 'select(.type == "request_end" and .status == "failed")' \
  /home/openclaw/data/groundhog/logs/request-traces/*.jsonl
```

The trace plugin must be installed and loaded for ordinary OpenClaw agent
turns. Direct Groundhog commands are traced by their Python process regardless
of the OpenClaw plugin.
