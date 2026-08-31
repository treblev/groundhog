import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";

import { createWeeklyReviewHandler, latestCompletedSaturday, runWeeklyReview, weeklyReviewDate } from "./core.js";

test("the default selects the most recently completed Phoenix Saturday", () => {
  assert.equal(latestCompletedSaturday(new Date("2026-08-31T03:30:00Z")), "2026-08-29");
  assert.equal(latestCompletedSaturday(new Date("2026-08-29T20:00:00Z")), "2026-08-22");
});

test("an explicit weekly-review date must be one Saturday", () => {
  assert.equal(weeklyReviewDate("2026-08-29"), "2026-08-29");
  assert.throws(() => weeklyReviewDate("2026-08-30"), /must be a Saturday/);
  assert.throws(() => weeklyReviewDate("2026-02-29"), /valid calendar date/);
  assert.throws(() => weeklyReviewDate("2026-08-29 extra"), /Usage/);
});

test("handler invokes the local weekly command with its resolved Saturday", async () => {
  const calls = [];
  const handler = createWeeklyReviewHandler({
    now: () => new Date("2026-08-31T03:30:00Z"),
    async run(options) {
      calls.push(options);
      return "Weekly review from local facts.";
    },
  });
  assert.deepEqual(await handler({ args: "" }), { text: "Weekly review from local facts." });
  assert.equal(calls[0].date, "2026-08-29");
});

test("handler returns factual validation and command errors", async () => {
  const invalid = createWeeklyReviewHandler({ async run() { throw new Error("should not run"); } });
  assert.deepEqual(await invalid({ args: "2026-08-30" }), { text: "Weekly review date must be a Saturday." });
  const failed = createWeeklyReviewHandler({ async run() { throw new Error("local database is unavailable"); } });
  assert.deepEqual(await failed({ args: "2026-08-29" }), { text: "Weekly review failed: local database is unavailable" });
});

test("subprocess boundary runs the fixed local summarize command", async () => {
  const calls = [];
  const result = await runWeeklyReview({
    python: "/test/python",
    appDir: "/test/app",
    dbPath: "/test/groundhog.duckdb",
    date: "2026-08-29",
    timeoutMs: 1_000,
    spawnProcess(python, args, options) {
      calls.push({ python, args, options });
      const child = new EventEmitter();
      child.stdout = new EventEmitter();
      child.stderr = new EventEmitter();
      child.kill = () => true;
      queueMicrotask(() => {
        child.stdout.emit("data", "Weekly review from local facts.\n");
        child.emit("close", 0);
      });
      return child;
    },
  });
  assert.equal(result, "Weekly review from local facts.");
  assert.deepEqual(calls[0].args, ["groundhog_service.py", "summarize", "weekly", "--date", "2026-08-29"]);
  assert.equal(calls[0].options.cwd, "/test/app");
  assert.equal(calls[0].options.env.GROUNDHOG_DB_PATH, "/test/groundhog.duckdb");
});
