import { spawn } from "node:child_process";

const PHOENIX = "America/Phoenix";
const SATURDAY = 6;
const WEEKDAY_INDEX = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };

function phoenixParts(now) {
  const formatter = new Intl.DateTimeFormat("en-US", {
    timeZone: PHOENIX,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    weekday: "short",
  });
  const values = Object.fromEntries(
    formatter.formatToParts(now)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
  return {
    year: Number(values.year),
    month: Number(values.month),
    day: Number(values.day),
    weekday: WEEKDAY_INDEX[values.weekday],
  };
}

function isoDate(year, month, day) {
  return new Date(Date.UTC(year, month - 1, day)).toISOString().slice(0, 10);
}

/** Return the Saturday that had finished before this Phoenix-local instant. */
export function latestCompletedSaturday(now = new Date()) {
  const local = phoenixParts(now);
  let daysSinceSaturday = (local.weekday - SATURDAY + 7) % 7;
  if (daysSinceSaturday === 0) daysSinceSaturday = 7;
  return isoDate(local.year, local.month, local.day - daysSinceSaturday);
}

export function weeklyReviewDate(args, now = new Date()) {
  const supplied = (args ?? "").trim();
  if (!supplied) return latestCompletedSaturday(now);
  if (/\s/.test(supplied)) throw new Error("Usage: /weekly-review [YYYY-MM-DD]");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(supplied)) {
    throw new Error("Weekly review date must use YYYY-MM-DD format.");
  }
  const parsed = new Date(`${supplied}T00:00:00Z`);
  if (Number.isNaN(parsed.valueOf()) || parsed.toISOString().slice(0, 10) !== supplied) {
    throw new Error("Weekly review date is not a valid calendar date.");
  }
  if (parsed.getUTCDay() !== SATURDAY) throw new Error("Weekly review date must be a Saturday.");
  return supplied;
}

function failureFrom(code, stderr) {
  const lastLine = stderr.trim().split(/\r?\n/).filter(Boolean).at(-1);
  if (lastLine) return new Error(lastLine.replace(/^[A-Za-z_][\w.]*Error:\s*/, ""));
  return new Error(`Groundhog weekly review exited with code ${code}.`);
}

export function runWeeklyReview({
  python,
  appDir,
  dbPath,
  date,
  timeoutMs = 12 * 60 * 1000,
  spawnProcess = spawn,
}) {
  return new Promise((resolveRun, rejectRun) => {
    const child = spawnProcess(python, ["groundhog_service.py", "summarize", "weekly", "--date", date], {
      cwd: appDir,
      env: {
        ...process.env,
        GROUNDHOG_DB_PATH: dbPath,
        GROUNDHOG_REQUEST_SOURCE: "telegram",
      },
    });
    let stdout = "";
    let stderr = "";
    let settled = false;
    const settle = (callback, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      callback(value);
    };
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      settle(rejectRun, new Error(`Groundhog weekly review exceeded the ${Math.ceil(timeoutMs / 1000)} second timeout.`));
    }, timeoutMs);

    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", (error) => settle(rejectRun, error));
    child.on("close", (code) => {
      if (code === 0 && stdout.trim()) {
        settle(resolveRun, stdout.trim());
        return;
      }
      if (code === 0) {
        settle(rejectRun, new Error("Groundhog produced an empty weekly review."));
        return;
      }
      settle(rejectRun, failureFrom(code, stderr));
    });
  });
}

export function createWeeklyReviewHandler(options) {
  const run = options.run ?? runWeeklyReview;
  const now = options.now ?? (() => new Date());
  return async (ctx = {}) => {
    let date;
    try {
      date = weeklyReviewDate(ctx.args, now());
    } catch (error) {
      return { text: error.message };
    }
    try {
      return { text: await run({ ...options, date }) };
    } catch (error) {
      options.logger?.error(`Groundhog weekly review failed: ${error.message}`);
      return { text: `Weekly review failed: ${error.message}` };
    }
  };
}
