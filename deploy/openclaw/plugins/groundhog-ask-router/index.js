import { spawn } from "node:child_process";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

function textFrom(value) {
  return typeof value === "string" ? value : "";
}

function run(python, appDir, question) {
  return new Promise((resolveRun, rejectRun) => {
    const child = spawn(python, ["-m", "scripts.ask_groundhog", question], {
      cwd: appDir,
      env: { ...process.env, GROUNDHOG_DB_PATH: "/home/openclaw/data/groundhog/groundhog.duckdb" },
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", rejectRun);
    child.on("close", (code) => {
      const answer = stdout.trim();
      if (code === 0 && answer) return resolveRun(answer);
      const details = stderr.trim();
      const error = new Error(details || (code === 0 ? "Groundhog returned an empty answer." : `Groundhog exited ${code}`));
      error.details = details;
      rejectRun(error);
    });
  });
}

export default definePluginEntry({
  id: "groundhog-ask-router",
  name: "Groundhog ask router",
  description: "Deterministically routes /ask questions to the guarded local Groundhog agent.",
  register(api) {
    const config = api.pluginConfig ?? {};
    const appDir = textFrom(config.appDir) || "/home/openclaw/apps/groundhog";
    const python = textFrom(config.python) || `${appDir}/venv/bin/python`;

    api.registerCommand({
      name: "ask",
      description: "Ask a question about local Groundhog data: /ask <question>",
      acceptsArgs: true,
      channels: ["telegram"],
      handler: async (ctx) => {
        const question = (ctx.args ?? "").trim();
        if (!question) return { text: "Usage: /ask <question about your Groundhog data>" };
        try {
          return { text: await run(python, appDir, question) };
        } catch (error) {
          api.logger.error(`Groundhog ask failed: ${error.details || error.message}`);
          return { text: "Groundhog could not answer that question right now. Please try again." };
        }
      },
    });
  },
});
