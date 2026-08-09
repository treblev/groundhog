import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { definePluginEntry } from "openclaw/plugin-sdk";

const DATE = /^\d{4}-\d{2}-\d{2}$/;

function textFrom(value) {
  return typeof value === "string" ? value : "";
}

function findMediaPath(value, mediaRoot) {
  if (typeof value === "string") {
    const candidates = value.match(/\/[\w./-]+\.(?:png|jpe?g)/gi) ?? [];
    return candidates.find((candidate) => candidate.startsWith(mediaRoot) && existsSync(candidate));
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findMediaPath(item, mediaRoot);
      if (found) return found;
    }
  }
  if (value && typeof value === "object") {
    for (const item of Object.values(value)) {
      const found = findMediaPath(item, mediaRoot);
      if (found) return found;
    }
  }
  return undefined;
}

function run(python, appDir, args) {
  return new Promise((resolveRun, rejectRun) => {
    const child = spawn(python, ["-m", "ingestion.spending", ...args], {
      cwd: appDir,
      env: { ...process.env, GROUNDHOG_DB_PATH: "/home/openclaw/data/groundhog/groundhog.duckdb" },
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", rejectRun);
    child.on("close", (code) => code === 0 ? resolveRun(stdout.trim()) : rejectRun(new Error(stderr.trim() || `Groundhog exited ${code}`)));
  });
}

function formatImport(rows) {
  if (rows.length === 0) return "That Wallet screenshot was already imported.";
  const total = rows.reduce((sum, row) => sum + Number(row.amount), 0).toFixed(2);
  const lines = rows.map((row) => `${row.id.slice(0, 8)} — ${row.merchant}: $${Number(row.amount).toFixed(2)} (${row.category})`);
  return `Imported ${rows.length} spending transaction${rows.length === 1 ? "" : "s"} — $${total}\n${lines.join("\n")}`;
}

export default definePluginEntry({
  id: "groundhog-spending-router",
  name: "Groundhog spending router",
  description: "Deterministic Apple Wallet screenshot import and category corrections.",
  register(api) {
    const config = api.pluginConfig ?? {};
    const appDir = textFrom(config.appDir) || "/home/openclaw/apps/groundhog";
    const python = textFrom(config.python) || `${appDir}/venv/bin/python`;
    const mediaRoot = resolve(textFrom(config.mediaRoot) || "/home/openclaw/media/inbound");
    const mediaStatePath = textFrom(config.mediaStatePath) || "/home/openclaw/data/groundhog/openclaw_activity_media_state.json";

    api.registerHook("inbound_claim", async (event) => {
      if (event.channel !== "telegram" || !/(?:^|\s)\/expense\b/i.test(event.content ?? event.body ?? "")) return;
      const imagePath = findMediaPath(event, mediaRoot);
      if (!imagePath) return;
      const referenceDate = new Date(event.timestamp ?? Date.now()).toLocaleDateString("en-CA", { timeZone: "America/Phoenix" });
      if (!DATE.test(referenceDate)) return { handled: true, reply: { text: "Could not determine the upload date for this Wallet screenshot." } };
      try {
        const output = await run(python, appDir, [
          "import", "--image", imagePath, "--reference-date", referenceDate,
          "--media-state-path", mediaStatePath,
        ]);
        return { handled: true, reply: { text: formatImport(JSON.parse(output)) } };
      } catch (error) {
        api.logger.error(`Groundhog spending import failed: ${error.message}`);
        return { handled: true, reply: { text: `Wallet import failed: ${error.message}` } };
      }
    });

    api.registerCommand({
      name: "expense",
      description: "Correct an expense category: /expense category <transaction-id> <category>",
      acceptsArgs: true,
      channels: ["telegram"],
      handler: async (ctx) => {
        const tokens = (ctx.args ?? "").trim().split(/\s+/);
        if (tokens.length !== 3 || tokens[0].toLowerCase() !== "category") {
          return { text: "Usage: /expense category <transaction-id> <groceries|dining|shopping|entertainment|beer|other>" };
        }
        try {
          const output = await run(python, appDir, ["category", tokens[1], tokens[2]]);
          const row = JSON.parse(output);
          return { text: `Updated ${row.id.slice(0, 8)} — ${row.merchant} ($${row.amount}) to ${row.category}.` };
        } catch (error) {
          return { text: `Spending category update failed: ${error.message}` };
        }
      },
    });
  },
});
