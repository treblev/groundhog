import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";

import { createExpenseHandler, expenseMessageId, runEnqueue } from "./core.js";

test("expense command queues the selected image and returns immediately", async () => {
  const calls = [];
  const handler = createExpenseHandler({
    mediaRoot: "/media",
    findMedia() { return "/media/current-wallet.jpg"; },
    async enqueue(args) {
      calls.push(args);
      return { short_id: "cafebabe", status: "queued", created: true };
    },
    now() { return Date.parse("2026-08-13T20:14:54-07:00"); },
  });

  const result = await handler({ commandBody: "/expense" });

  assert.deepEqual(result, { text: "Expense received — job cafebabe is processing." });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].imagePath, "/media/current-wallet.jpg");
  assert.equal(calls[0].messageId, "expense:current-wallet.jpg");
});

test("expense attachment identity prefers an explicit message id", () => {
  assert.equal(expenseMessageId({ messageId: "telegram-42" }, "/media/wallet.jpg"), "telegram-42");
});

test("expense enqueue failure returns a factual receipt error", async () => {
  const handler = createExpenseHandler({
    mediaRoot: "/media",
    findMedia() { return "/media/current-wallet.jpg"; },
    async enqueue() { throw new Error("database unavailable"); },
  });

  const result = await handler({ commandBody: "/expense" });

  assert.deepEqual(result, { text: "Expense upload could not be queued: database unavailable" });
});

test("spawn boundary creates an idempotent expense job", async () => {
  const root = await mkdtemp(join(tmpdir(), "groundhog-expense-plugin-"));
  try {
    const imagePath = join(root, "wallet.jpg");
    const dbPath = join(root, "groundhog.duckdb");
    const spoolDir = join(root, "spool");
    await writeFile(imagePath, "exact wallet attachment");
    const options = {
      python: resolve("venv/bin/python"),
      appDir: process.cwd(),
      dbPath,
      spoolDir,
      imagePath,
      messageId: "telegram-expense-1",
    };

    const first = await runEnqueue(options);
    const duplicate = await runEnqueue(options);

    assert.equal(first.created, true);
    assert.equal(duplicate.created, false);
    assert.equal(first.id, duplicate.id);
    assert.equal(first.status, "queued");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
