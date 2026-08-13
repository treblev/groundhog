# Media Ingestion Rebuild

## Status

Proposed architecture on `codex/rebuilding`. This document is a design review;
it does not change the deployed ingestion path.

## Problem

Activity screenshots currently have two intake paths:

1. An OpenClaw skill that depends on the chat model recognizing an activity
   upload and selecting the correct attachment.
2. A systemd timer that scans an inbound directory and defaults unseen images
   to activities.

The paths share a JSON checkpoint but do not share durable jobs. They can
disagree about the media directory, select an older attachment, permanently
checkpoint a transient failure, or produce chat replies unrelated to an actual
import result. The timer can stop scheduling without making ingestion visibly
unhealthy.

The August 12 incident demonstrated all important weaknesses: Telegram staged
the correct image, the model skipped the skill, the timer had been elapsed for
five days, and the model's recovery attempt selected an older image and failed
to wait for local Qwen.

## Design principles

- The current Telegram event supplies the attachment identity. Never search for
  “the most recent” file.
- The outer chat model never decides whether or how an activity is imported.
- Receipt and processing are separate. A slow or restarted Qwen call must not
  lose the upload.
- DuckDB is the source of truth for job state. JSON files are not queues.
- Every state transition is idempotent and inspectable.
- User-facing messages come from recorded job outcomes, not model narration.
- A broken worker must be observable and recoverable without resending media.

## User contract

For the first version of the rebuild:

- A bare image sent by the owner in the Telegram direct chat is an activity
  upload.
- An optional caption is metadata only. A date token may fill a date that is
  missing or unreadable in the image; it does not trigger the route.
- `/expense` continues through the existing explicit expense command and is not
  claimed as a bare activity.
- Workout-plan and sleep ingestion retain their explicit workflows until they
  are migrated to the same durable job system. The activity rebuild must not
  silently classify a bare image as plan, sleep, or spending.

The bot sends exactly two possible messages for one accepted upload:

1. Immediate receipt: `Activity received — job <short-id> is processing.`
2. One terminal result: an imported metric summary or a concise failure with
   the job ID and retry instruction.

There are no “let me check,” vision interpretations, speculative causes, or
requests to resend while the original job still exists.

This is intentionally one intake plugin, one table, and one worker. The durable
job boundary is necessary because local vision inference can outlive a Telegram
turn or Gateway process. Running Qwen synchronously inside the plugin would be
shorter code, but a Gateway restart would lose the only record of the upload.

## Architecture

```text
Telegram image
    |
    v
OpenClaw groundhog-media-ingress plugin
  reply_dispatch hook, before chat-model dispatch
  - owner + Telegram DM + image guard
  - exact ctx.MediaPaths attachment
  - copy into Groundhog-owned spool
  - enqueue one DuckDB job
  - send receipt and mark turn handled
    |
    v
media_ingestion_jobs (durable queue)
    |
    v
groundhog-media-worker.service
  - claims one job with a lease
  - invokes the activity importer
  - waits for local Qwen to finish
  - records imported / needs_review / retry_wait
  - queues one terminal outbox event
    |
    v
existing OpenClaw outbox delivery bridge -> Telegram
```

### OpenClaw ingress plugin

Create `deploy/openclaw/plugins/groundhog-media-ingress`.

Use the installed OpenClaw `reply_dispatch` hook because it:

- runs before normal agent dispatch;
- exposes the finalized inbound context, including exact `MediaPath` and
  `MediaPaths` values for the current Telegram update;
- can queue a final reply through the provided dispatcher; and
- can return `handled: true`, preventing the chat model from seeing or
  answering the upload.

The plugin must not scan directories. It calls a narrow enqueue CLI with an
argument array:

```text
python -m scripts.media_ingestion enqueue
  --kind activity
  --image <exact-current-media-path>
  --caption <current-caption>
  --channel telegram
  --message-id <current-message-id>
```

The CLI synchronously copies the image into a configured Groundhog spool before
returning. The copied file name is the content hash plus the original extension.
This removes any dependency on OpenClaw's later media cleanup or staging paths.

The plugin returns `handled: true` only after enqueue succeeds. If enqueue
fails, it still returns `handled: true` with a factual error message so the chat
model cannot invent a recovery path.

### Durable job table

Add an idempotent schema migration:

```sql
CREATE TABLE IF NOT EXISTS media_ingestion_jobs (
    id VARCHAR PRIMARY KEY,
    content_hash VARCHAR NOT NULL,
    source_channel VARCHAR NOT NULL,
    source_message_id VARCHAR NOT NULL,
    kind VARCHAR NOT NULL CHECK (kind IN ('activity')),
    spool_path VARCHAR NOT NULL,
    caption TEXT,
    status VARCHAR NOT NULL CHECK (
        status IN ('queued', 'processing', 'retry_wait', 'imported', 'needs_review')
    ),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    available_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    lease_owner VARCHAR,
    lease_expires_at TIMESTAMP,
    error_code VARCHAR,
    error_text TEXT,
    result_json JSON,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    UNIQUE (source_channel, source_message_id, content_hash, kind)
);
```

Allowed statuses:

- `queued`
- `processing`
- `retry_wait`
- `imported`
- `needs_review`

Validate all transitions centrally in application code as well as constraining
stored values in the schema.

### Worker

Create one long-running `groundhog-media-worker.service` with
`Restart=always`. Do not use a systemd timer.

The worker:

1. Atomically claims one available job and sets a lease.
2. Increments `attempt_count` before processing.
3. Calls the existing activity parser with the spooled image and optional date
   hint.
4. Waits for the local LAN Ollama/Qwen request to complete. The HTTP client
   timeout remains explicit and long enough for the configured model.
5. Writes the result and terminal event in one database transaction.
6. Recovers expired `processing` leases after a worker or host restart.

Only infrastructure failures are retried automatically:

- Ollama connection refused/unavailable
- transport errors
- configured request timeout
- transient database lock

Use bounded retry delays such as 1, 5, and 15 minutes. After the final
infrastructure attempt, move the job to `needs_review`.

Parsing, unsupported-image, and missing-date failures go directly to
`needs_review`; repeating the same model call is not expected to repair them.
They remain retryable through an explicit operator command using the same job
and same spooled image.

### Idempotency

- The source message ID plus content hash prevents duplicate Telegram delivery
  from creating duplicate jobs.
- The content-addressed spool prevents duplicate file copies.
- Existing activity upserts prevent duplicate canonical rows.
- The terminal event uses `media_ingestion:<job-id>:terminal` as its dedupe key,
  so only one final Telegram message is delivered.
- Re-running enqueue returns the existing job and its current status.

### Status and recovery

Provide local operator commands:

```text
python -m scripts.media_ingestion status [--job <id>]
python -m scripts.media_ingestion retry --job <id>
```

Expose worker health through the existing service/status path:

- worker service active state;
- age of the oldest queued job;
- count by status;
- expired processing leases;
- last successful completion time.

Alert when a queued job is older than the expected local-Qwen processing
window or the worker heartbeat is stale. Do not treat a long active lease as a
failure until its configured deadline passes.

## Failure contract

| Failure | Required behavior |
| --- | --- |
| Duplicate Telegram delivery | Return the existing job; do not process twice. |
| Gateway restarts after receipt | Job and spooled image survive; worker resumes it. |
| Worker restarts during Qwen | Lease expires; the same job becomes claimable again. |
| Ollama unavailable | Record retryable failure and schedule a bounded retry. |
| Qwen takes several minutes | Keep the same lease alive; do not launch another model call. |
| Image is not an activity | Mark `needs_review`; send one factual terminal message. |
| Screenshot date is unreadable | Use a valid caption hint or mark `needs_review`; never guess. |
| Telegram reply delivery fails | Existing outbox retains the terminal event for retry. |
| Spool copy fails | Reject enqueue factually; never invoke the chat model. |
| Database unavailable at receipt | Return one factual enqueue failure; attachment is not claimed as imported. |

## Components to remove

Once end-to-end verification succeeds:

- Disable and remove `groundhog-openclaw-media.timer`.
- Remove directory-scanning and `_next_kind` behavior from
  `scripts/import_openclaw_activity_media.py`.
- Remove the JSON state file as an activity queue/dedupe authority.
- Remove `groundhog-activity-upload` model-selected skill routing.
- Forbid chat-agent manual calls to activity ingestion; status/retry are the
  only supported recovery operations.

Keep the existing canonical activity parser and database upsert logic. The
rebuild changes intake, durability, and reporting—not metric extraction rules.

## Verification gates

The rebuild is not deployable until all of these pass:

1. Unit tests for enqueue idempotency, atomic spool copies, every state
   transition, lease recovery, retry classification, and terminal-event dedupe.
2. Plugin tests proving it uses the exact current `MediaPaths` value and returns
   `handled: true` for accepted and rejected activity uploads.
3. Offline integration test: enqueue -> worker -> mocked Qwen -> activity row ->
   one outbox result.
4. Restart test: terminate the worker during processing, expire the lease, and
   verify recovery without resending the image.
5. Linux smoke test with a sanitized test screenshot.
6. Telegram end-to-end test showing the received job ID, one final metric
   summary, the same content hash throughout, and no outer-model session turn.
7. Only after those checks: disable the old timer and skill, then verify reboot
   behavior.

## Implementation sequence

1. Add the durable table, spool configuration, and enqueue/status/retry CLI.
2. Add the worker and offline tests using a fake processor.
3. Add the `reply_dispatch` ingress plugin and plugin tests.
4. Add service health reporting and deployment units.
5. Run local regression and Linux non-production smoke checks.
6. Deploy the new path alongside the old watcher but leave only the new plugin
   able to claim bare activity images.
7. Verify Telegram end to end, then disable and remove the old timer and skill.

No step should require the chat model to select a file, classify a route, wait
for a process, or explain an importer result.

## Non-goals for the first implementation

- Do not build a general AI image classifier.
- Do not merge expense, sleep, and workout-plan extraction into one prompt.
- Do not replace the existing activity metric parser or canonical tables.
- Do not add another polling timer as a fallback.
- Do not deploy from this branch until the user approves this design and the
  verification gates pass.
