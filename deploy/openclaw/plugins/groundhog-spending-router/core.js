import { spawn } from "node:child_process";
import { readdirSync, statSync } from "node:fs";
import { basename } from "node:path";

export function findRecentMediaPath(mediaRoot, timestamp) {
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

function runChild(python, appDir, args, env = {}) {
  return new Promise((resolveRun, rejectRun) => {
    const child = spawn(python, args, {
      cwd: appDir,
      env: { ...process.env, ...env },
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

export async function runEnqueue({
  python,
  appDir,
  dbPath,
  spoolDir,
  imagePath,
  messageId,
}) {
  const args = [
    "-m", "scripts.media_ingestion", "--db-path", dbPath, "enqueue",
    "--kind", "expense", "--image", imagePath,
    "--caption", "/expense", "--channel", "telegram", "--message-id", messageId,
  ];
  if (spoolDir) args.push("--spool-dir", spoolDir);
  const output = await runChild(python, appDir, args, { GROUNDHOG_DB_PATH: dbPath });
  try {
    return JSON.parse(output);
  } catch {
    throw new Error("Groundhog expense enqueue returned an invalid response.");
  }
}

export function runSpendingCommand(python, appDir, dbPath, args) {
  return runChild(
    python,
    appDir,
    ["-m", "ingestion.spending", ...args],
    {
      GROUNDHOG_DB_PATH: dbPath,
      GROUNDHOG_REQUEST_SOURCE: "telegram",
    },
  );
}

export function expenseMessageId(ctx, imagePath) {
  const explicit = ctx?.messageId ?? ctx?.messageSid ?? ctx?.MessageSid;
  return String(explicit || `expense:${basename(imagePath)}`);
}

export function createExpenseHandler(options) {
  const enqueue = options.enqueue ?? runEnqueue;
  const findMedia = options.findMedia ?? findRecentMediaPath;
  const now = options.now ?? Date.now;
  return async (ctx = {}) => {
    const startedAt = now();
    const imagePath = findMedia(options.mediaRoot, startedAt);
    if (!imagePath) {
      return { text: "No recent screenshot was found. Attach the transaction screenshot and use /expense as its caption." };
    }
    try {
      const job = await enqueue({
        ...options,
        imagePath,
        messageId: expenseMessageId(ctx, imagePath),
      });
      options.logger?.info(`Groundhog expense queued: job=${job.short_id} status=${job.status} created=${job.created}`);
      if (job.status === "imported") {
        return { text: `Expense job ${job.short_id} was already processed.` };
      }
      if (job.status === "needs_review") {
        return { text: `Expense job ${job.short_id} is saved for review.` };
      }
      return { text: `Expense received — job ${job.short_id} is processing.` };
    } catch (error) {
      options.logger?.error(`Groundhog expense enqueue failed: ${error.details || error.message}`);
      return { text: `Expense upload could not be queued: ${error.message}` };
    }
  };
}
