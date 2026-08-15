import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { createMediaIngressHandler, currentImagePaths, routeDecision, runEnqueue } from "./core.js";

const REPOSITORY_ROOT = resolve(fileURLToPath(new URL("../../../..", import.meta.url)));

function event(overrides = {}) {
  return {
    originatingChannel: "telegram",
    originatingChatType: "direct",
    ctx: {
      CommandAuthorized: true,
      MessageSid: "1234",
      RawBody: "<media:image>",
      MediaPaths: ["/exact/current/upload.jpg"],
      MediaTypes: ["image/jpeg"],
      ...overrides,
    },
  };
}

function hookContext() {
  const replies = [];
  const processed = [];
  return {
    replies,
    processed,
    dispatcher: {
      sendFinalReply(payload) { replies.push(payload); return true; },
      markComplete() {},
      async waitForIdle() {},
      getQueuedCounts() { return { tool: 0, block: 0, final: replies.length }; },
    },
    recordProcessed(outcome, details) { processed.push({ outcome, details }); },
    markIdle() {},
  };
}

test("uses only exact current image paths from the finalized event", () => {
  assert.deepEqual(
    currentImagePaths(event().ctx),
    ["/exact/current/upload.jpg"],
  );
  assert.equal(routeDecision(event()).messageId, "1234");
});

test("does not claim expense commands", () => {
  assert.equal(routeDecision(event({ RawBody: "/expense" })).claim, false);
});

test("does not claim groups, non-owners, or non-Telegram media", () => {
  assert.equal(routeDecision({ ...event(), originatingChatType: "group" }).claim, false);
  assert.equal(routeDecision(event({ CommandAuthorized: false })).claim, false);
  assert.equal(routeDecision({ ...event(), originatingChannel: "signal" }).claim, false);
});

test("queues the exact attachment, sends receipt, and prevents model dispatch", async () => {
  const calls = [];
  const context = hookContext();
  const handler = createMediaIngressHandler({
    appDir: "/app",
    python: "/app/venv/bin/python",
    dbPath: "/data/groundhog.duckdb",
    spoolDir: "/data/media-spool",
    async enqueue(args) {
      calls.push(args);
      return { short_id: "deadbeef", status: "queued" };
    },
  });

  const result = await handler(event({ RawBody: "Morning run 8/12" }), context);

  assert.equal(result.handled, true);
  assert.equal(result.queuedFinal, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].imagePath, "/exact/current/upload.jpg");
  assert.equal(calls[0].messageId, "1234");
  assert.equal(calls[0].caption, "Morning run 8/12");
  assert.deepEqual(context.replies, [{ text: "Activity received — job deadbeef is processing." }]);
});

test("enqueue failure is factual and still prevents model narration", async () => {
  const context = hookContext();
  const handler = createMediaIngressHandler({
    async enqueue() { throw new Error("database unavailable"); },
  });

  const result = await handler(event(), context);

  assert.equal(result.handled, true);
  assert.deepEqual(context.replies, [{
    text: "Activity upload could not be queued. The attachment was not accepted; check the Groundhog ingress logs.",
  }]);
  assert.equal(context.processed[0].outcome, "error");
});

test("spawn boundary invokes the durable Python enqueue CLI idempotently", async () => {
  const root = await mkdtemp(join(tmpdir(), "groundhog-media-plugin-"));
  try {
    const imagePath = join(root, "activity.jpg");
    const dbPath = join(root, "groundhog.duckdb");
    const spoolDir = join(root, "spool");
    await writeFile(imagePath, "exact attachment");
    const options = {
      python: join(REPOSITORY_ROOT, "venv/bin/python"),
      appDir: REPOSITORY_ROOT,
      dbPath,
      spoolDir,
      imagePath,
      caption: "Run 8/12",
      messageId: "telegram-1234",
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
