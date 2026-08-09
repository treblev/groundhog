import { spawn } from "node:child_process";
import { readdirSync, statSync } from "node:fs";
import { resolve } from "node:path";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const DATE = /^\d{4}-\d{2}-\d{2}$/;

function textFrom(value) {
  return typeof value === "string" ? value : "";
}

function findRecentMediaPath(mediaRoot, timestamp) {
  const target = typeof timestamp === "number"
    ? (timestamp < 1_000_000_000_000 ? timestamp * 1000 : timestamp)
    : Date.now();
  const candidates = [];
  const visit = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = `${directory}/${entry.name}`;
      if (entry.isDirectory()) visit(path);
      else if (/\.(?:png|jpe?g)$/i.test(entry.name)) {
        const modifiedAt = statSync(path).mtimeMs;
        if (Math.abs(modifiedAt - target) <= 5 * 60 * 1000) candidates.push({ path, modifiedAt });
      }
    }
  };
  try {
    visit(mediaRoot);
  } catch {
    return undefined;
  }
  candidates.sort((left, right) => Math.abs(left.modifiedAt - target) - Math.abs(right.modifiedAt - target));
  return candidates[0]?.path;
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
    child.on("close", (code) => {
      if (code === 0) {
        resolveRun(stdout.trim());
        return;
      }
      const details = stderr.trim();
      const lastLine = details.split(/\r?\n/).filter(Boolean).at(-1) ?? `Groundhog exited ${code}`;
      const error = new Error(lastLine.replace(/^[A-Za-z_][\w.]*Error:\s*/, ""));
      error.details = details;
      rejectRun(error);
    });
  });
}

function countLabel(count, singular, plural = `${singular}s`) {
  return `${count} ${count === 1 ? singular : plural}`;
}

function formatImport(result) {
  const rows = Array.isArray(result) ? result : (result.transactions ?? []);
  const pending = Number(result.skipped_pending ?? 0);
  const duplicates = Number(result.skipped_duplicates ?? 0);
  const invalid = Number(result.skipped_invalid ?? 0);
  const skipped = [];
  if (pending) skipped.push(countLabel(pending, "pending charge"));
  if (duplicates) skipped.push(countLabel(duplicates, "existing duplicate"));
  if (invalid) skipped.push(countLabel(invalid, "invalid row"));
  if (rows.length === 0) {
    if (pending && !duplicates && !invalid) {
      return `No posted transactions imported; skipped ${countLabel(pending, "pending charge")}.`;
    }
    if (duplicates && !pending && !invalid) {
      return `No new transactions; skipped ${countLabel(duplicates, "existing duplicate")}.`;
    }
    if (skipped.length) return `No new transactions imported; skipped ${skipped.join(", ")}.`;
    return "Could not identify any supported transactions with a merchant, amount, and date.";
  }
  const total = rows.reduce((sum, row) => sum + Number(row.amount), 0).toFixed(2);
  const lines = rows.map((row) => `${row.id.slice(0, 8)} — ${row.merchant}: $${Number(row.amount).toFixed(2)} (${row.category})`);
  const skippedText = skipped.length ? `\nSkipped ${skipped.join(", ")}.` : "";
  return `Imported ${rows.length} spending transaction${rows.length === 1 ? "" : "s"} — $${total}\n${lines.join("\n")}${skippedText}`;
}

export default definePluginEntry({
  id: "groundhog-spending-router",
  name: "Groundhog spending router",
  description: "Deterministic Wallet and bank transaction screenshot import and category corrections.",
  register(api) {
    const config = api.pluginConfig ?? {};
    const appDir = textFrom(config.appDir) || "/home/openclaw/apps/groundhog";
    const python = textFrom(config.python) || `${appDir}/venv/bin/python`;
    const mediaRoot = resolve(textFrom(config.mediaRoot) || "/home/openclaw/.openclaw/media/inbound");
    const mediaStatePath = textFrom(config.mediaStatePath) || "/home/openclaw/data/groundhog/openclaw_activity_media_state.json";

    api.registerCommand({
      name: "expense",
      description: "Import an attached Wallet or bank transaction screenshot.",
      acceptsArgs: false,
      channels: ["telegram"],
      handler: async () => {
        const startedAt = Date.now();
        const imagePath = findRecentMediaPath(mediaRoot, startedAt);
        if (!imagePath) {
          return { text: "No recent screenshot was found. Attach the transaction screenshot and use /expense as its caption." };
        }
        const referenceDate = new Date(startedAt).toLocaleDateString("en-CA", { timeZone: "America/Phoenix" });
        if (!DATE.test(referenceDate)) return { text: "Could not determine the upload date for this transaction screenshot." };
        try {
          const output = await run(python, appDir, [
            "import", "--image", imagePath, "--reference-date", referenceDate,
            "--media-state-path", mediaStatePath,
          ]);
          const result = JSON.parse(output);
          api.logger.info(`Groundhog spending import completed: rows=${result.transactions?.length ?? 0} pending=${result.skipped_pending ?? 0} duplicates=${result.skipped_duplicates ?? 0} invalid=${result.skipped_invalid ?? 0} durationMs=${Date.now() - startedAt}`);
          return { text: formatImport(result) };
        } catch (error) {
          api.logger.error(`Groundhog spending import failed: ${error.details || error.message}`);
          return { text: `Expense import failed: ${error.message}` };
        }
      },
    });

    api.registerCommand({
      name: "expense-category",
      description: "Correct an expense category: /expense-category <transaction-id> <category>",
      acceptsArgs: true,
      channels: ["telegram"],
      handler: async (ctx) => {
        const tokens = (ctx.args ?? "").trim().split(/\s+/);
        if (tokens.length !== 2) {
          return { text: "Usage: /expense-category <transaction-id> <groceries|dining|shopping|entertainment|beer|other>" };
        }
        try {
          const output = await run(python, appDir, ["category", tokens[0], tokens[1]]);
          const row = JSON.parse(output);
          return { text: `Updated ${row.id.slice(0, 8)} — ${row.merchant} ($${row.amount}) to ${row.category}.` };
        } catch (error) {
          return { text: `Spending category update failed: ${error.message}` };
        }
      },
    });
  },
});
