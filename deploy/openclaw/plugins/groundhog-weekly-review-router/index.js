import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { createWeeklyReviewHandler } from "./core.js";

function textFrom(value) {
  return typeof value === "string" ? value : "";
}

function timeoutFrom(value) {
  return Number.isFinite(value) && value > 0 ? value : 12 * 60 * 1000;
}

export default definePluginEntry({
  id: "groundhog-weekly-review-router",
  name: "Groundhog weekly review router",
  description: "Runs local weekly reviews directly from the registered Telegram command.",
  register(api) {
    const config = api.pluginConfig ?? {};
    const appDir = textFrom(config.appDir) || "/home/openclaw/apps/groundhog";
    const python = textFrom(config.python) || `${appDir}/venv/bin/python`;
    const dbPath = textFrom(config.dbPath) || "/home/openclaw/data/groundhog/groundhog.duckdb";
    const timeoutMs = timeoutFrom(config.timeoutMs);

    api.registerCommand({
      name: "weekly-review",
      description: "Create a local weekly review: /weekly-review [YYYY-MM-DD Saturday]",
      acceptsArgs: true,
      channels: ["telegram"],
      handler: createWeeklyReviewHandler({ python, appDir, dbPath, timeoutMs, logger: api.logger }),
    });
  },
});
