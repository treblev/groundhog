import { appendFile, mkdir } from "node:fs/promises";
import { join } from "node:path";

const traceDir = process.env.GROUNDHOG_REQUEST_TRACE_DIR
  ?? "/home/openclaw/apps/groundhog/data/logs/request-traces";

function safe(value) {
  const seen = new WeakSet();
  return JSON.parse(JSON.stringify(value, (_key, item) => {
    if (typeof item === "bigint") return item.toString();
    if (typeof item === "object" && item !== null) {
      if (seen.has(item)) return "[Circular]";
      seen.add(item);
    }
    return item;
  }));
}

async function record(eventType, event, ctx) {
  try {
    const now = new Date();
    await mkdir(traceDir, { recursive: true });
    const runId = event?.runId ?? ctx?.runId ?? ctx?.sessionId ?? "unscoped";
    const line = JSON.stringify({
      trace_id: String(runId),
      timestamp: now.toISOString(),
      event_type: eventType,
      component: "openclaw",
      payload: safe({ event, context: ctx }),
    });
    await appendFile(join(traceDir, `${now.toISOString().slice(0, 10)}.jsonl`), `${line}\n`, "utf8");
  } catch {
    // Observability never changes delivery of the user's request.
  }
}

export default {
  id: "groundhog-request-trace",
  name: "Groundhog Request Trace",
  register(api) {
    for (const name of [
      "before_agent_run", "model_call_started", "model_call_ended", "llm_input",
      "llm_output", "before_tool_call", "after_tool_call", "agent_end",
    ]) {
      api.on(name, (event, ctx) => record(name, event, ctx));
    }
  },
};
