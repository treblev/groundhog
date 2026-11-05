/** Read-only health check through the installed OpenClaw stdio transport.
 * Reports counts and timings only; no calendar content or credentials.
 */
import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import { createRequire } from "node:module";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

const { values } = parseArgs({ options: {
  "openclaw-root": { type: "string" },
  config: { type: "string" },
  start: { type: "string" },
  end: { type: "string" },
} });
for (const key of ["openclaw-root", "config", "start", "end"]) {
  assert.ok(values[key], `Supply --${key}`);
}
const root = values["openclaw-root"];
const require = createRequire(join(root, "package.json"));
const { Client } = await import(pathToFileURL(require.resolve("@modelcontextprotocol/sdk/client/index.js")));
let Transport;
for (const name of (await readdir(join(root, "dist"))).filter((name) =>
  name.startsWith("mcp-client-lifecycle-") && name.endsWith(".mjs"))) {
  const module = await import(pathToFileURL(join(root, "dist", name)));
  Transport = Object.values(module).find((value) =>
    typeof value === "function" && value.name === "OpenClawStdioClientTransport");
  if (Transport) break;
}
assert.ok(Transport, "Inspect this OpenClaw version: stdio transport export not found");
const config = JSON.parse(await readFile(values.config, "utf8"));
const entry = config.mcp?.servers?.calendar;
assert.ok(entry?.enabled && entry.transport === "stdio", "Enable the calendar stdio entry first");
const client = new Client({ name: "groundhog-calendar-health", version: "1" }, { capabilities: {} });
const transport = new Transport({
  command: entry.command, args: entry.args, cwd: entry.cwd,
  env: entry.env, stderr: "ignore",
});
const started = performance.now();
async function call(name, args) {
  const result = await client.callTool({ name, arguments: args }, undefined, { timeout: 60000 });
  const data = result.structuredContent ?? JSON.parse(result.content.find((item) => item.type === "text").text);
  assert.ok(!result.isError && data.status === "ok", `${name} failed; inspect the private service audit`);
  return data;
}
try {
  await client.connect(transport);
  const catalog = await client.listTools();
  const discovered = await call("calendar_list", {});
  const selected = discovered.calendars.filter((row) => row.read_enabled);
  assert.ok(selected.length, "Select at least one readable calendar");
  for (const row of selected) {
    const agenda = await call("calendar_events", {
      calendar_id: row.calendar_id, start: values.start, end: values.end,
    });
    assert.ok(!agenda.truncated, "Narrow the health-check window: agenda truncated");
  }
  await call("calendar_find_free_slots", {
    start: values.start, end: values.end, duration_minutes: 30,
  });
  console.log(JSON.stringify({ status: "ok", transport: "installed OpenClaw stdio",
    tools: catalog.tools.length, selectedCalendars: selected.length,
    durationMs: Math.round(performance.now() - started), writesAttempted: 0 }));
} finally {
  await client.close();
}
