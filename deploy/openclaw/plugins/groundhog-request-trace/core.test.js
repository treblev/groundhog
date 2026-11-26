import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { registerRequestTrace } from "./core.js";

async function harness(t, options = {}) {
  const root = await mkdtemp(join(tmpdir(), "groundhog-trace-test-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const logDir = join(root, "traces");
  if (options.failWrites) await writeFile(logDir, "not a directory");
  const hooks = new Map();
  const logs = [];
  const logger = Object.fromEntries(["info", "warn", "error"].map((level) => [level, (line) => {
    if (options.failLogger) throw new Error("logger unavailable");
    logs.push({ level, ...JSON.parse(line.slice("groundhog-trace ".length)) });
  }]));
  registerRequestTrace({ pluginConfig: { logDir }, logger,
    on: (name, handler) => hooks.set(name, handler) });
  return { root, logDir, logs, hooks,
    call: (name, event = {}, context = {}) => hooks.get(name)(event, context) };
}

test("Gateway lifecycle is immediate, correlated, and excludes private content", async (t) => {
  const h = await harness(t);
  const context = { runId: "run-test", sessionKey: "agent:main:telegram:direct:PRIVATE_ID", messageId: "private-message" };
  await h.call("message_received", { content: "SECRET agenda", from: "PRIVATE_ID" }, context);
  assert.equal(h.logs.at(-1).event, "message_received");
  await h.call("before_agent_run", { prompt: "SECRET question" }, context);
  await h.call("model_call_started", { callId: "call-test", model: "qwen3.8:27b-mlx" }, context);
  assert.equal(h.logs.at(-1).event, "model_started");
  await h.call("llm_input", { systemPrompt: "SECRET", prompt: "SECRET", historyMessages: ["SECRET"], tools: ["SECRET"] }, context);
  assert.equal(h.logs.at(-1).history_messages, 1);
  await h.call("model_call_ended", { callId: "call-test", durationMs: 123 }, context);
  assert.equal(h.logs.at(-1).event, "model_finished", "Do not wait for the agent's final response");
  await h.call("llm_output", { callId: "call-test", response: "SECRET response" }, context);
  assert.equal(h.logs.filter((r) => r.event === "model_finished").length, 1);
  await h.call("before_tool_call", { callId: "tool-test", toolName: "calendar__calendar_events", params: { description: "SECRET" } }, context);
  await h.call("after_tool_call", { callId: "tool-test", result: { content: "SECRET results" } }, context);
  await h.call("agent_end", { success: true, response: "SECRET final" }, context);
  await h.call("message_sent", { success: true, content: "SECRET final" }, context);
  assert.deepEqual(h.logs.map((r) => r.event), ["trace_ready", "message_received", "request_started",
    "model_started", "context_ready", "model_finished", "tool_started", "tool_finished", "request_finished", "message_sent"]);
  assert.ok(h.logs.slice(1).every((r) => r.run_id === "run-test"));
  assert.equal(new Set(h.logs.slice(1).map((r) => r.session_ref)).size, 1);
  assert.ok(!/SECRET|PRIVATE_ID|private-message/.test(JSON.stringify(h.logs)));
  const files = await readdir(h.logDir);
  const rows = (await readFile(join(h.logDir, files[0]), "utf8")).trim().split("\n").map(JSON.parse);
  assert.deepEqual(rows.map((r) => r.type), ["request_start", "llm_call", "tool_call", "request_end"]);
  assert.match(JSON.stringify(rows), /SECRET/, "Detailed traces remain in their separate private stream");
});

test("aborted calls and failed delivery appear without leaking error text", async (t) => {
  const h = await harness(t);
  const context = { runId: "cancelled-run" };
  await h.call("model_call_started", { model: "qwen", callId: "pending-model" }, context);
  await h.call("before_tool_call", { toolName: "calendar_list", callId: "pending-tool" }, context);
  await h.call("agent_end", { success: false, error: "aborted SECRET" }, context);
  await h.call("message_sent", { success: false, error: "SECRET delivery failure" }, context);
  assert.equal(h.logs.find((r) => r.event === "model_finished").failure, "cancelled");
  assert.equal(h.logs.find((r) => r.event === "tool_finished").status, "failed");
  assert.equal(h.logs.find((r) => r.event === "request_finished").status, "failed");
  assert.equal(h.logs.at(-1).status, "failed");
  assert.ok(!JSON.stringify(h.logs).includes("SECRET"));
});

test("provider timeout is visible before agent end and without a response event", async (t) => {
  const h = await harness(t);
  const context = { runId: "timeout-run" };
  await h.call("model_call_started", { callId: "timeout-call" }, context);
  await h.call("model_call_ended", { callId: "timeout-call", durationMs: 600000, error: "provider timeout SECRET" }, context);
  assert.equal(h.logs.at(-1).event, "model_finished");
  assert.equal(h.logs.at(-1).duration_ms, 600000);
  assert.equal(h.logs.at(-1).failure, "timeout");
  await h.call("agent_end", { success: false }, context);
  assert.equal(h.logs.filter((r) => r.event === "model_finished").length, 1);
  assert.ok(!JSON.stringify(h.logs).includes("SECRET"));
});

test("trace-file failure still emits request start and does not block the agent", async (t) => {
  const h = await harness(t, { failWrites: true });
  const result = await h.call("before_agent_run", {}, { runId: "write-failure" });
  assert.deepEqual(result, { outcome: "pass" });
  assert.ok(h.logs.some((r) => r.event === "request_started"));
  assert.ok(h.logs.some((r) => r.event === "observer_error"));
});

test("Gateway logger failure cannot block private tracing or the agent", async (t) => {
  const h = await harness(t, { failLogger: true });
  assert.deepEqual(await h.call("before_agent_run", {}, { runId: "logger-failure" }), { outcome: "pass" });
  await h.call("agent_end", { success: true }, { runId: "logger-failure" });
  assert.equal((await readdir(h.logDir)).length, 1);
});
