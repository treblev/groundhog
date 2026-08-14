import { resolve } from "node:path";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { createExpenseHandler, runSpendingCommand } from "./core.js";

function textFrom(value) {
  return typeof value === "string" ? value : "";
}


export default definePluginEntry({
  id: "groundhog-spending-router",
  name: "Groundhog spending router",
  description: "Queued Wallet and bank transaction screenshot imports and deterministic category corrections.",
  register(api) {
    const config = api.pluginConfig ?? {};
    const appDir = textFrom(config.appDir) || "/home/openclaw/apps/groundhog";
    const python = textFrom(config.python) || `${appDir}/venv/bin/python`;
    const mediaRoot = resolve(textFrom(config.mediaRoot) || "/home/openclaw/.openclaw/media/inbound");
    const dbPath = textFrom(config.dbPath) || "/home/openclaw/data/groundhog/groundhog.duckdb";
    const spoolDir = textFrom(config.spoolDir) || "/home/openclaw/data/groundhog/media-spool";

    api.registerCommand({
      name: "expense",
      description: "Import an attached Wallet or bank transaction screenshot.",
      acceptsArgs: false,
      channels: ["telegram"],
      handler: createExpenseHandler({ python, appDir, dbPath, spoolDir, mediaRoot, logger: api.logger }),
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
          const output = await runSpendingCommand(python, appDir, dbPath, ["category", tokens[0], tokens[1]]);
          const row = JSON.parse(output);
          return { text: `Updated ${row.id.slice(0, 8)} — ${row.merchant} ($${row.amount}) to ${row.category}.` };
        } catch (error) {
          return { text: `Spending category update failed: ${error.message}` };
        }
      },
    });
  },
});
