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
- asynchronous activity uploads and every Qwen attempt/retry
- `/expense` vision calls
- stock-note operations and their local embedding calls

The Python tracer is Groundhog-owned because `/ask`, activity processing, and
expense processing run outside OpenClaw's model lifecycle hooks.

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
