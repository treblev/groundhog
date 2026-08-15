import { createHash } from "node:crypto";
import { appendFile, mkdir, readdir, rm } from "node:fs/promises";
import { join } from "node:path";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const DEFAULT_LOG_DIR = "/home/openclaw/data/groundhog/logs/request-traces";
const DEFAULT_RETENTION_DAYS = 15;

function requestId(event, context) {
  return event?.runId ?? context?.runId ?? event?.context?.runId;
}

function callId(event, fallback) {
  return event?.callId ?? event?.toolCallId ?? event?.modelCallId ?? event?.invocationId ?? fallback;
}

function binarySummary(value) {
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return {
    byte_count: Buffer.byteLength(text),
    sha256: createHash("sha256").update(text).digest("hex"),
  };
}

function safe(value, key = "", seen = new WeakSet()) {
  if (value === undefined || value === null) return null;
  if (["string", "number", "boolean"].includes(typeof value)) {
    if (typeof value === "string" && /^(?:data|image|images|image_data)$/i.test(key) && value.length > 1024) {
      return binarySummary(value);
    }
    return value;
  }
  if (typeof value === "bigint") return value.toString();
  if (value instanceof Error) return { name: value.name, message: value.message, stack: value.stack };
  if (value instanceof Date) return value.toISOString();
  if (typeof value !== "object") return String(value);
  if (seen.has(value)) return "[Circular]";
  seen.add(value);
  if (Array.isArray(value)) {
    if (/^(?:images|image_data)$/i.test(key)) return value.map(binarySummary);
    return value.map((item) => safe(item, key, seen));
  }
  return Object.fromEntries(
    Object.entries(value).map(([childKey, item]) => [childKey, safe(item, childKey, seen)]),
  );
}

function statusError(event) {
  const error = event?.error ?? event?.errorMessage ?? event?.failureReason;
  if (error) return String(error?.message ?? error);
  if (event?.outcome === "error") {
    return String(event?.failureKind ?? event?.errorCategory ?? "Model call failed.");
  }
  return null;
}

function makeWriter(logDir) {
  let writes = Promise.resolve();
  return (record) => {
    writes = writes.then(async () => {
      await mkdir(logDir, { recursive: true });
      const day = record.started_at.slice(0, 10);
      await appendFile(join(logDir, `${day}.jsonl`), `${JSON.stringify(safe(record))}\n`);
    });
    return writes;
  };
}

function newest(map) {
  return [...map.values()].at(-1);
}

async function removeExpiredLogs(logDir, retentionDays, now = new Date()) {
  const cutoff = new Date(now);
  cutoff.setUTCHours(0, 0, 0, 0);
  cutoff.setUTCDate(cutoff.getUTCDate() - retentionDays);
  let entries;
  try {
    entries = await readdir(logDir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const entry of entries) {
    if (!entry.isFile() || !/^\d{4}-\d{2}-\d{2}\.jsonl$/.test(entry.name)) continue;
    if (new Date(`${entry.name.slice(0, 10)}T00:00:00Z`) < cutoff) {
      await rm(join(logDir, entry.name));
    }
  }
}

export default definePluginEntry({
  id: "groundhog-request-trace",
  name: "Groundhog request trace",
  description: "Writes local request, LLM-call, and tool-call spans for every OpenClaw agent run.",
  register(api) {
    const config = api.pluginConfig ?? {};
    const logDir = config.logDir ?? DEFAULT_LOG_DIR;
    const retentionDays = config.retentionDays ?? DEFAULT_RETENTION_DAYS;
    const append = makeWriter(logDir);
    const runs = new Map();
    const safeHook = (fn) => Promise.resolve().then(fn).catch((error) => {
      api.logger.error(`Groundhog request trace failed: ${error.message}`);
    });

    const finishLlm = async (run, pending) => {
      run.llm.delete(pending.id);
      const ended = pending.ended ?? {};
      const error = ended.error ?? null;
      await append({
        schema_version: 1,
        request_id: run.id,
        type: "llm_call",
        started_at: pending.startedAt.toISOString(),
        duration_ms: ended.durationMs ?? Math.max(0, Date.now() - pending.startedMs),
        status: error ? "failed" : "passed",
        model: pending.model,
        prompt: pending.prompt,
        response: pending.response,
        error,
      });
    };

    const ensureRun = async (event, context) => {
      const id = requestId(event, context);
      if (!id) return null;
      let run = runs.get(id);
      if (run) return run;
      const startedAt = new Date();
      run = {
        id,
        startedAt,
        llm: new Map(),
        tools: new Map(),
        sequence: 0,
      };
      runs.set(id, run);
      await append({
        schema_version: 1,
        request_id: id,
        type: "request_start",
        started_at: startedAt.toISOString(),
        source: event?.channelId ?? context?.channelId ?? "openclaw",
        operation: "openclaw_agent_run",
        metadata: {
          prompt: event?.prompt,
          sender_id: event?.senderId,
          sender_is_owner: event?.senderIsOwner,
          account_id: event?.accountId,
        },
      });
      return run;
    };

    api.on("before_agent_run", (event, context) => safeHook(() => ensureRun(event, context)));

    api.on("model_call_started", (event, context) => safeHook(async () => {
      const run = await ensureRun(event, context);
      if (!run) return;
      const id = callId(event, `llm-${++run.sequence}`);
      run.llm.set(id, {
        id,
        startedAt: new Date(),
        startedMs: Date.now(),
        model: event?.model ?? event?.modelId ?? event?.resolvedModel,
        prompt: event?.prompt ?? event?.messages ?? run.lastLlmInput,
      });
    }));

    api.on("llm_input", (event, context) => safeHook(async () => {
      const run = await ensureRun(event, context);
      if (!run) return;
      run.lastLlmInput = {
        system_prompt: event?.systemPrompt,
        prompt: event?.prompt,
        history_messages: event?.historyMessages,
        tools: event?.tools,
        images_count: event?.imagesCount,
      };
      const pending = run.llm.get(callId(event)) ?? newest(run.llm);
      if (pending) pending.prompt = run.lastLlmInput;
    }));

    api.on("llm_output", (event, context) => safeHook(async () => {
      const run = await ensureRun(event, context);
      if (!run) return;
      const pending = run.llm.get(callId(event)) ?? newest(run.llm);
      if (pending) {
        pending.response = event?.response ?? event?.output ?? event;
        if (pending.ended) await finishLlm(run, pending);
      }
    }));

    api.on("model_call_ended", (event, context) => safeHook(async () => {
      const run = await ensureRun(event, context);
      if (!run) return;
      const id = callId(event);
      const pending = run.llm.get(id) ?? newest(run.llm);
      if (!pending) return;
      const error = statusError(event);
      pending.model ??= event?.model ?? event?.modelId;
      pending.ended = {
        durationMs: event?.durationMs ?? Math.max(0, Date.now() - pending.startedMs),
        error,
      };
      if (pending.response !== undefined || error) await finishLlm(run, pending);
    }));

    api.on("before_tool_call", (event, context) => safeHook(async () => {
      const run = await ensureRun(event, context);
      if (!run) return;
      const id = callId(event, `tool-${++run.sequence}`);
      run.tools.set(id, {
        id,
        startedAt: new Date(),
        startedMs: Date.now(),
        tool: event?.toolName ?? event?.name,
        arguments: event?.params ?? event?.arguments ?? event?.input,
      });
    }));

    api.on("after_tool_call", (event, context) => safeHook(async () => {
      const run = await ensureRun(event, context);
      if (!run) return;
      const id = callId(event);
      const pending = run.tools.get(id) ?? newest(run.tools);
      if (!pending) return;
      run.tools.delete(pending.id);
      const error = statusError(event);
      await append({
        schema_version: 1,
        request_id: run.id,
        type: "tool_call",
        started_at: pending.startedAt.toISOString(),
        duration_ms: Math.max(0, Date.now() - pending.startedMs),
        status: error ? "failed" : "passed",
        tool: pending.tool ?? event?.toolName ?? event?.name,
        arguments: pending.arguments,
        result: event?.result ?? event?.output,
        error,
      });
    }));

    api.on("agent_end", (event, context) => safeHook(async () => {
      const run = await ensureRun(event, context);
      if (!run) return;
      const error = statusError(event);
      for (const pending of run.llm.values()) {
        const pendingError = pending.ended?.error ?? error ?? (
          pending.ended ? null : "Request ended before the LLM call completed."
        );
        await append({
          schema_version: 1,
          request_id: run.id,
          type: "llm_call",
          started_at: pending.startedAt.toISOString(),
          duration_ms: pending.ended?.durationMs ?? Math.max(0, Date.now() - pending.startedMs),
          status: pendingError ? "failed" : "passed",
          model: pending.model,
          prompt: pending.prompt,
          response: pending.response,
          error: pendingError,
        });
      }
      for (const pending of run.tools.values()) {
        await append({
          schema_version: 1,
          request_id: run.id,
          type: "tool_call",
          started_at: pending.startedAt.toISOString(),
          duration_ms: Math.max(0, Date.now() - pending.startedMs),
          status: "failed",
          tool: pending.tool,
          arguments: pending.arguments,
          result: null,
          error: error ?? "Request ended before the tool call completed.",
        });
      }
      await append({
        schema_version: 1,
        request_id: run.id,
        type: "request_end",
        started_at: new Date().toISOString(),
        duration_ms: Math.max(0, Date.now() - run.startedAt.getTime()),
        status: error || event?.success === false ? "failed" : "passed",
        error,
        metadata: { final_response: event?.response ?? event?.output },
      });
      runs.delete(run.id);
      await removeExpiredLogs(logDir, retentionDays);
    }));
  },
});
