import { spawn } from "node:child_process";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

function textFrom(value) {
  return typeof value === "string" ? value : "";
}

function run(python, appDir, args) {
  return new Promise((resolveRun, rejectRun) => {
    const child = spawn(python, ["scripts/stock_notes.py", ...args], {
      cwd: appDir,
      env: { ...process.env, GROUNDHOG_DB_PATH: "/home/openclaw/data/groundhog/groundhog.duckdb" },
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", rejectRun);
    child.on("close", (code) => {
      if (code === 0) return resolveRun(stdout.trim());
      const details = stderr.trim();
      const lastLine = details.split(/\r?\n/).filter(Boolean).at(-1) ?? `Groundhog exited ${code}`;
      const error = new Error(lastLine.replace(/^[A-Za-z_][\w.]*Error:\s*/, ""));
      error.details = details;
      rejectRun(error);
    });
  });
}

function splitRequired(args, command) {
  const trimmed = (args ?? "").trim();
  const [first, ...rest] = trimmed.split(/\s+/);
  if (!first || rest.length === 0) return undefined;
  return [command, first, rest.join(" ")];
}

export default definePluginEntry({
  id: "groundhog-stock-notes-router",
  name: "Groundhog stock notes router",
  description: "Direct commands for durable, locally embedded ticker notes.",
  register(api) {
    const config = api.pluginConfig ?? {};
    const appDir = textFrom(config.appDir) || "/home/openclaw/apps/groundhog";
    const python = textFrom(config.python) || `${appDir}/venv/bin/python`;

    api.registerCommand({
      name: "stocks-add-notes",
      description: "Add a semantic note: /stocks-add-notes <ticker> <note>",
      acceptsArgs: true,
      channels: ["telegram"],
      handler: async (ctx) => {
        const command = splitRequired(ctx.args, "add");
        if (!command) return { text: "Usage: /stocks-add-notes <ticker> <note>" };
        try {
          const note = JSON.parse(await run(python, appDir, command));
          return { text: `Saved ${note.ticker} note ${note.id.slice(0, 8)} (revision ${note.revision}). It is ready for semantic search.` };
        } catch (error) {
          api.logger.error(`Groundhog stock-note add failed: ${error.details || error.message}`);
          return { text: `Could not save stock note: ${error.message}` };
        }
      },
    });

    api.registerCommand({
      name: "stocks-edit-notes",
      description: "Edit a semantic note: /stocks-edit-notes <note-id> <replacement>",
      acceptsArgs: true,
      channels: ["telegram"],
      handler: async (ctx) => {
        const command = splitRequired(ctx.args, "edit");
        if (!command) return { text: "Usage: /stocks-edit-notes <note-id> <replacement>" };
        try {
          const note = JSON.parse(await run(python, appDir, command));
          return { text: `Updated ${note.ticker} note ${note.id.slice(0, 8)} to revision ${note.revision}. Semantic search now uses the replacement.` };
        } catch (error) {
          api.logger.error(`Groundhog stock-note edit failed: ${error.details || error.message}`);
          return { text: `Could not edit stock note: ${error.message}` };
        }
      },
    });

    api.registerCommand({
      name: "stocks-delete-notes",
      description: "Delete a semantic note: /stocks-delete-notes <note-id>",
      acceptsArgs: true,
      channels: ["telegram"],
      handler: async (ctx) => {
        const noteId = (ctx.args ?? "").trim();
        if (!noteId || /\s/.test(noteId)) return { text: "Usage: /stocks-delete-notes <note-id>" };
        try {
          const note = JSON.parse(await run(python, appDir, ["delete", noteId]));
          return { text: `Deleted ${note.ticker} note ${note.id.slice(0, 8)} (revision ${note.revision}). Its semantic chunk was removed.` };
        } catch (error) {
          api.logger.error(`Groundhog stock-note delete failed: ${error.details || error.message}`);
          return { text: `Could not delete stock note: ${error.message}` };
        }
      },
    });

    api.registerCommand({
      name: "stocks-notes",
      description: "List active notes for a ticker: /stocks-notes <ticker>",
      acceptsArgs: true,
      channels: ["telegram"],
      handler: async (ctx) => {
        const ticker = (ctx.args ?? "").trim();
        if (!ticker || /\s/.test(ticker)) return { text: "Usage: /stocks-notes <ticker>" };
        try {
          const notes = JSON.parse(await run(python, appDir, ["list", ticker]));
          if (notes.length === 0) return { text: `No active notes for ${ticker.toUpperCase()}.` };
          return { text: notes.map((note) => `${note.id.slice(0, 8)} — ${note.note}`).join("\n") };
        } catch (error) {
          api.logger.error(`Groundhog stock-note list failed: ${error.details || error.message}`);
          return { text: `Could not list stock notes: ${error.message}` };
        }
      },
    });
  },
});
