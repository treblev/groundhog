import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { createMediaIngressHandler } from "./core.js";

function configured(value, fallback) {
  return typeof value === "string" && value.trim() ? value : fallback;
}

export default definePluginEntry({
  id: "groundhog-media-ingress",
  name: "Groundhog media ingress",
  description: "Durably queues exact Telegram activity attachments before model dispatch.",
  register(api) {
    const config = api.pluginConfig ?? {};
    const appDir = configured(config.appDir, "/home/openclaw/apps/groundhog");
    api.on("reply_dispatch", createMediaIngressHandler({
      appDir,
      python: configured(config.python, `${appDir}/venv/bin/python`),
      dbPath: configured(config.dbPath, "/home/openclaw/data/groundhog/groundhog.duckdb"),
      spoolDir: configured(config.spoolDir, "/home/openclaw/data/groundhog/media-spool"),
      logger: api.logger,
    }));
  },
});
