import { appendFile, mkdir, readdir, rm } from "node:fs/promises";
import { join } from "node:path";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const DEFAULT_LOG_DIR = "/home/openclaw/apps/groundhog/data/logs/request-traces";
const DEFAULT_RETENTION_DAYS = 15;

function runId(event, context) {
  return event?.runId ?? context?.runId ?? event?.context?.runId;
}

function json(value, seen = new WeakSet()) {
  if (value === undefined) return null;
  if (value === null || ["string", "number", "boolean"].includes(typeof value)) return value;
  if (typeof value === "bigint") return value.toString();
  if (value instanceof Error) return { name: value.name, message: value.message, stack: value.stack };
  if (value instanceof Date) return value.toISOString();
  if (typeof value !== "object") return String(value);
  if (seen.has(value)) return "[Circular]";
  seen.add(value);
  if (Array.isArray(value)) return value.map((item) => json(item, seen));
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, json(item, seen)]));
}

function serialize(value) {
  return JSON.parse(JSON.stringify(json(value)));
}

export default definePluginEntry({
  id: "groundhog-request-trace",
  name: "Groundhog request trace",
  description: "Writes local JSONL traces for agent runs that execute Groundhog MCP tools.",
  register(api) {
    const config = api.pluginConfig ?? {};
    const logDir = config.logDir ?? DEFAULT_LOG_DIR;
    const retentionDays = config.retentionDays ?? DEFAULT_RETENTION_DAYS;
    const runs = new Map();
    const capture = (event, context, type) => {
      const id = runId(event, context);
      if (!id) return;
      const trace = runs.get(id) ?? { runId: id, startedAt: new Date().toISOString(), groundhog: false, events: [] };
      trace.events.push({ type, timestamp: new Date().toISOString(), payload: serialize(event) });
      if (type === "after_tool_call" && String(event.toolName ?? "").startsWith("groundhog__")) trace.groundhog = true;
      runs.set(id, trace);
    };
    const safe = (fn) => Promise.resolve().then(fn).catch(() => undefined);
    for (const hook of ["before_agent_run", "model_call_started", "llm_input", "llm_output", "model_call_ended", "before_tool_call", "after_tool_call"]) {
      api.on(hook, (event, context) => safe(() => capture(event, context, hook)));
    }
    api.on("agent_end", (event, context) => safe(async () => {
      const id = runId(event, context);
      if (!id) return;
      capture(event, context, "agent_end");
      const trace = runs.get(id);
      runs.delete(id);
      if (!trace?.groundhog) return;
      const completedAt = new Date().toISOString();
      const day = completedAt.slice(0, 10);
      await mkdir(logDir, { recursive: true });
      await appendFile(join(logDir, `${day}.jsonl`), `${JSON.stringify({ schemaVersion: 1, ...trace, completedAt })}\n`);
      const cutoff = new Date(completedAt);
      cutoff.setUTCHours(0, 0, 0, 0);
      cutoff.setUTCDate(cutoff.getUTCDate() - retentionDays);
      for (const entry of await readdir(logDir, { withFileTypes: true })) {
        if (!entry.isFile() || !/^\d{4}-\d{2}-\d{2}\.jsonl$/.test(entry.name)) continue;
        if (new Date(`${entry.name.slice(0, 10)}T00:00:00Z`) < cutoff) await rm(join(logDir, entry.name));
      }
    }));
  },
});
