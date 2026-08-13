import { spawn } from "node:child_process";

const IMAGE_EXTENSION = /\.(?:png|jpe?g|webp)$/i;

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

export function currentImagePaths(ctx) {
  const paths = Array.isArray(ctx?.MediaPaths)
    ? ctx.MediaPaths.filter((value) => typeof value === "string" && value.trim())
    : text(ctx?.MediaPath) ? [ctx.MediaPath] : [];
  const types = Array.isArray(ctx?.MediaTypes)
    ? ctx.MediaTypes
    : text(ctx?.MediaType) ? [ctx.MediaType] : [];
  return paths.filter((path, index) => {
    const mediaType = text(types[index]).toLowerCase();
    return mediaType ? mediaType.startsWith("image/") : IMAGE_EXTENSION.test(path);
  });
}

export function routeDecision(event) {
  const ctx = event?.ctx ?? {};
  const channel = text(event?.originatingChannel || ctx.OriginatingChannel || ctx.Provider).toLowerCase();
  const chatType = text(event?.originatingChatType || ctx.ChatType).toLowerCase();
  const commandBody = text(ctx.BodyForCommands || ctx.CommandBody || ctx.RawBody);
  const imagePaths = currentImagePaths(ctx);
  if (channel !== "telegram" || chatType !== "direct" || ctx.CommandAuthorized !== true) {
    return { claim: false };
  }
  if (imagePaths.length === 0 || commandBody.startsWith("/")) return { claim: false };
  const messageId = text(ctx.MessageSidFull || ctx.MessageSid || ctx.MessageSidFirst || ctx.MessageSidLast);
  return {
    claim: true,
    imagePaths,
    caption: commandBody,
    messageId,
  };
}

export function runEnqueue({ python, appDir, dbPath, spoolDir, imagePath, caption, messageId }) {
  return new Promise((resolveRun, rejectRun) => {
    const args = [
      "-m", "scripts.media_ingestion", "--db-path", dbPath, "enqueue",
      "--kind", "activity", "--image", imagePath,
      "--channel", "telegram", "--message-id", messageId,
    ];
    if (caption) args.push("--caption", caption);
    if (spoolDir) args.push("--spool-dir", spoolDir);
    const child = spawn(python, args, {
      cwd: appDir,
      env: { ...process.env, GROUNDHOG_DB_PATH: dbPath },
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", rejectRun);
    child.on("close", (code) => {
      if (code !== 0) {
        const details = stderr.trim();
        const lastLine = details.split(/\r?\n/).filter(Boolean).at(-1) ?? `Groundhog exited ${code}`;
        const error = new Error(lastLine.replace(/^[A-Za-z_][\w.]*Error:\s*/, ""));
        error.details = details;
        rejectRun(error);
        return;
      }
      try {
        resolveRun(JSON.parse(stdout.trim()));
      } catch {
        rejectRun(new Error("Groundhog enqueue returned an invalid response."));
      }
    });
  });
}

async function finishHandled(context, message, outcome = "completed") {
  const queuedFinal = context.dispatcher.sendFinalReply({ text: message });
  context.dispatcher.markComplete();
  await context.dispatcher.waitForIdle();
  const counts = context.dispatcher.getQueuedCounts();
  context.recordProcessed(outcome, { reason: "groundhog_media_ingress" });
  context.markIdle("message_completed");
  return { handled: true, queuedFinal, counts };
}

export function createMediaIngressHandler(options) {
  const enqueue = options.enqueue ?? runEnqueue;
  return async (event, context) => {
    const decision = routeDecision(event);
    if (!decision.claim) return undefined;
    if (!decision.messageId) {
      return finishHandled(
        context,
        "Activity upload could not be queued because Telegram did not provide a message ID.",
        "error",
      );
    }

    const jobs = [];
    const failures = [];
    for (const imagePath of decision.imagePaths) {
      try {
        jobs.push(await enqueue({
          ...options,
          imagePath,
          caption: decision.caption,
          messageId: decision.messageId,
        }));
      } catch (error) {
        failures.push(error);
        options.logger?.error(`Groundhog media enqueue failed: ${error.details || error.message}`);
      }
    }
    if (jobs.length) {
      const ids = jobs.map((job) => job.short_id).join(", ");
      const noun = jobs.length === 1 ? "Activity" : "Activities";
      const verb = jobs.length === 1 ? "is" : "are";
      const warning = failures.length
        ? ` ${failures.length} additional attachment${failures.length === 1 ? "" : "s"} could not be queued; check the ingress logs.`
        : "";
      return finishHandled(context, `${noun} received — job ${ids} ${verb} processing.${warning}`);
    }
    return finishHandled(
      context,
      "Activity upload could not be queued. The attachment was not accepted; check the Groundhog ingress logs.",
      "error",
    );
  };
}
