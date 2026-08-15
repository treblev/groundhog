# Groundhog Linux Operations

Current Linux deployment assumptions:

- Host: `192.168.1.38`
- Service user: `openclaw`
- Repo: `/home/openclaw/apps/groundhog`
- Deployment branch: `main` (tracks `origin/main`)
- DuckDB: `/home/openclaw/data/groundhog/groundhog.duckdb`
- OpenClaw gateway: user systemd service on `127.0.0.1:18789`
- Ollama: `http://Vijays-MacBook-Pro.local:11434`
- Privacy: local Ollama only; no OpenAI or Anthropic fallback

## Ollama Access

When Groundhog runs on Linux but Ollama runs on the Mac, the Mac Ollama base URL
is configured explicitly in `config/settings.py` as
`OLLAMA_BASE_URL=http://Vijays-MacBook-Pro.local:11434`.

Verify from Linux:

```bash
curl http://Vijays-MacBook-Pro.local:11434/api/tags
venv/bin/python tests/smoke_test.py
```

## Stock Jobs

Equities run at 5 PM Phoenix time on weekdays. A separate `groundhog-crypto`
timer runs at the same time on Saturday and Sunday and fetches only `BTC-USD`;
it does not rerun equities, signals, or alerts. Install it as `openclaw`:

```bash
cp deploy/systemd/user/groundhog-crypto.service ~/.config/systemd/user/
cp deploy/systemd/user/groundhog-crypto.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now groundhog-crypto.timer
```

The stock pipeline should run as the `openclaw` user:

```bash
cd /home/openclaw/apps/groundhog
GROUNDHOG_DB_PATH=/home/openclaw/data/groundhog/groundhog.duckdb \
  GROUNDHOG_ALERT_BACKEND=none \
  scripts/daily_stocks.sh
```

`scripts/daily_stocks.sh` calls `groundhog_service.py run daily-stocks`. The
service runner records one `agent_runs` row for the complete pipeline. A fatal
pipeline error is stored with its traceback and then re-raised so systemd marks
the service as failed. The runner invokes:

1. `python ingestion/stocks.py`
2. `python analytics/signals.py`
3. `python analytics/alerts.py`

`analytics/alerts.py` records deduped rows in `stock_alerts`. On Linux, set
`GROUNDHOG_ALERT_BACKEND=none` when OpenClaw is responsible for chat,
scheduler, and delivery. For interactive desktop notifications, use
`GROUNDHOG_ALERT_BACKEND=notify-send`.

Print a machine-readable service snapshot:

```bash
venv/bin/python groundhog_service.py status
```

Inspect the most recent job runs:

```bash
venv/bin/python -c "import duckdb; from config.settings import DB_PATH; con = duckdb.connect(str(DB_PATH)); print(con.execute('SELECT job_name, status, started_at, finished_at, error_text FROM agent_runs ORDER BY started_at DESC LIMIT 10').fetchall())"
```

Inspect recent Groundhog events:

```bash
venv/bin/python -c "import duckdb; from config.settings import DB_PATH; con = duckdb.connect(str(DB_PATH)); print(con.execute('SELECT event_type, source, subject_type, subject_id, occurred_at, payload FROM events ORDER BY occurred_at DESC LIMIT 20').fetchall())"
```

Event conventions:

- `job_completed` and `job_failed` describe the complete scheduled pipeline.
- `stock_signal_flipped` records a detected SMA or weekly Supertrend direction change.
- `stock_alert_created` records an alert row after Groundhog creates it.
- Events are idempotent: a stable `dedupe_key` means rerunning a job does not duplicate the same fact.
- `payload` is JSON. It contains event-specific facts; delivery decisions belong to OpenClaw.

Stock alerts create one `pending` outbox row. `pending`, `delivered`, `failed`,
and `discarded` are the supported delivery statuses. Groundhog never sends an
outbox item itself; OpenClaw reads and updates this table through MCP.

Inspect pending delivery items with their source facts:

```bash
venv/bin/python -c "import duckdb; from config.settings import DB_PATH; con = duckdb.connect(str(DB_PATH)); print(con.execute(\"SELECT o.id, e.event_type, e.payload, o.created_at FROM outbox o JOIN events e ON e.id = o.event_id WHERE o.status = 'pending' ORDER BY o.created_at\").fetchall())"
```

## OpenClaw Telegram Delivery

OpenClaw Telegram is configured on the Linux host with:

- account: `default`
- display name: `groundhog-telegram`
- token source: `/home/openclaw/.openclaw/secrets/telegram_bot_token`
- target chat id: `8243406239`

OpenClaw's Groundhog MCP server must use the Linux venv:

```text
mcp.servers.groundhog.command=/home/openclaw/apps/groundhog/venv/bin/python
```

Verify the channel and MCP server:

```bash
openclaw channels status --deep
openclaw mcp probe groundhog
```

The delivery bridge is:

```bash
GROUNDHOG_DB_PATH=/home/openclaw/data/groundhog/groundhog.duckdb \
  OPENCLAW_TELEGRAM_TARGET=8243406239 \
  venv/bin/python scripts/openclaw_deliver_outbox.py
```

It calls `get_pending_outbox`, sends each pending message with
`openclaw message send --channel telegram`, and calls `mark_outbox_delivered`
only after Telegram delivery succeeds.

OpenClaw cron runs the bridge every 30 seconds so completed jobs reach Telegram
promptly while retaining durable outbox delivery:

```text
name: groundhog-outbox-telegram
id: 201d4f1c-6ad5-41b3-859f-2b8ab70f3ab3
schedule: every 30s
delivery.mode: none
env: GROUNDHOG_DB_PATH, OPENCLAW_TELEGRAM_TARGET
```

Useful checks:

```bash
openclaw cron show groundhog-outbox-telegram --json
openclaw cron run 201d4f1c-6ad5-41b3-859f-2b8ab70f3ab3 --wait --wait-timeout 2m
openclaw cron runs
```

Verified delivery path:

- EXC stock alert delivered through Telegram and marked `delivered`.
- Synthetic `Groundhog Telegram delivery test -- ignore` outbox row delivered
  through the cron job and marked `delivered`.
- Pending outbox count returned to `0`.

## OpenClaw Activity Screenshot Intake

`groundhog-openclaw-media.timer` checks OpenClaw's inbound media directory every
minute. On first run, it records all existing attachments without importing
them. Later images are parsed by the local vision model and upserted into the
existing `activities` table. The checkpoint is stored outside the repository at
`/home/openclaw/data/groundhog/openclaw_activity_media_state.json`.

To arm exactly the next Telegram image as a SugarWOD workout plan instead of an
activity result, run:

```bash
GROUNDHOG_OPENCLAW_MEDIA_STATE_PATH=/home/openclaw/data/groundhog/openclaw_activity_media_state.json \
venv/bin/python -m scripts.import_openclaw_activity_media --next-kind plan
```

The plan importer uses the upload's Phoenix-local date and then automatically
returns to activity-result mode.

## Spending Screenshot Intake

The `groundhog-spending-router` plugin lives at
`deploy/openclaw/plugins/groundhog-spending-router`. It registers `/expense` as
a direct Telegram command that runs before the chat model. It copies the image
into the durable media spool, creates a `kind=expense` job, and immediately
replies with the short job ID. `groundhog-media-worker.service` later invokes
the local Wallet and bank transaction importer; its terminal result enters the
same outbox used by activity jobs and is delivered by the 30-second Telegram
bridge.

Correct a category later with:

```text
/expense-category <transaction-id> <groceries|dining|shopping|entertainment|beer|other>
```

The importer resolves Wallet relative dates and bank calendar dates, skips
pending charges, ignores running balances, and deduplicates matching merchant
and amount pairs within a three-day posting window. Circle K merchant names are
always categorized as `beer`. It does not change the default activity, plan, or
sleep image routes.

Install and verify it as `openclaw`:

```bash
cd /home/openclaw/apps/groundhog
openclaw plugins install /home/openclaw/apps/groundhog/deploy/openclaw/plugins/groundhog-spending-router
openclaw plugins enable groundhog-spending-router
systemctl --user restart openclaw-gateway.service
systemctl --user is-active openclaw-gateway.service
openclaw plugins inspect groundhog-spending-router --runtime --json
```

After future deployments, the plugin is path-backed, so pulling `main` and
restarting `openclaw-gateway.service` loads the new code. Runtime inspection
must show `status: loaded`, commands `expense` and `expense-category`, and no
diagnostics.

Useful database checks:

```bash
venv/bin/python -c "import duckdb; from config.settings import DB_PATH; con=duckdb.connect(str(DB_PATH)); print(con.execute('SELECT transaction_date, merchant, amount, category FROM spending ORDER BY transaction_date DESC, created_at DESC LIMIT 20').fetchall())"
venv/bin/python -m unittest tests.test_spending_ingestion tests.test_media_ingestion
node --test deploy/openclaw/plugins/groundhog-spending-router/core.test.js
```

## Ticker Semantic Notes

The `groundhog-stock-notes-router` plugin provides direct Telegram writes for
user-authored ticker research notes. It is separate from the stock alert system:
notes are canonical, revisioned context; their Ollama embeddings are derived.

Install and verify it as `openclaw` after deploying the feature branch:

```bash
cd /home/openclaw/apps/groundhog
openclaw plugins install /home/openclaw/apps/groundhog/deploy/openclaw/plugins/groundhog-stock-notes-router
openclaw plugins enable groundhog-stock-notes-router
systemctl --user restart openclaw-gateway.service
openclaw plugins inspect groundhog-stock-notes-router --runtime --json
```

Runtime inspection must show `status: loaded`, commands `stocks-add-notes`,
`stocks-edit-notes`, `stocks-delete-notes`, and `stocks-notes`, and no
diagnostics. See `docs/Stock_Semantic_Notes.md` for commands and retrieval.

## Groundhog Ask Router

The `groundhog-ask-router` plugin owns Telegram `/ask` directly. It invokes the
guarded local agent without letting OpenClaw's outer chat model answer, emit an
intermediate response, or independently choose tools.

```bash
cd /home/openclaw/apps/groundhog
openclaw plugins install /home/openclaw/apps/groundhog/deploy/openclaw/plugins/groundhog-ask-router
openclaw plugins enable groundhog-ask-router
systemctl --user restart openclaw-gateway.service
openclaw plugins inspect groundhog-ask-router --runtime --json
```

Runtime inspection must show `status: loaded`, command `ask`, and no
diagnostics. Ticker-specific and broader stock questions deterministically
retrieve active user notes before the local model plans its answer.

## Local Request Tracing

Install the request tracer to capture ordinary OpenClaw agent turns. Direct
Groundhog subprocesses such as `/ask`, `/expense`, stock notes, and asynchronous
activity imports also write the same format themselves.

```bash
cd /home/openclaw/apps/groundhog
openclaw plugins install /home/openclaw/apps/groundhog/deploy/openclaw/plugins/groundhog-request-trace
openclaw plugins enable groundhog-request-trace
systemctl --user restart openclaw-gateway.service
openclaw plugins inspect groundhog-request-trace --runtime --json
```

Runtime inspection must show `status: loaded`, the LLM/tool lifecycle hooks,
and no diagnostics. Logs are local at
`/home/openclaw/data/groundhog/logs/request-traces/` and are retained for 15
days. See `docs/Request_Tracing.md` for the record contract and `jq` examples.

## OpenClaw Stock Schedule

OpenClaw is the production scheduler for weekday stock jobs. The deployed job
is `groundhog-daily-stocks`; it runs exactly at 5:00 PM in
`America/Phoenix` on Monday through Friday, with no stagger. It invokes the
same `groundhog_service.py run daily-stocks` command as the manual entrypoint.
Every successful daily run queues a Telegram completion summary through the
Groundhog outbox, including the new-alert count, latest price date, and any
tickers that returned no data or errors. Individual stock alerts remain separate
outbox messages.

Useful checks and a manual trigger:

```bash
openclaw cron show groundhog-daily-stocks --json
openclaw cron runs
openclaw cron run 9ca75bbb-6e16-45db-a2eb-473bc13547ce --wait --wait-timeout 30m
```

The former `groundhog-stocks.timer` systemd timer is disabled to prevent a
duplicate 5 PM run. Keep its service installed only as a manual fallback:

```bash
systemctl --user start groundhog-stocks.service
```

## Weekly Supertrend Repair

Weekly bars are labelled with Friday by `resample("W-FRI")`; Groundhog excludes
that labelled bar until Friday arrives. If an earlier version recorded weekly
signals or alerts dated after the current Phoenix date, inspect and remove only
those premature artifacts before the next completed-week run:

```bash
cd /home/openclaw/apps/groundhog
venv/bin/python scripts/repair_weekly_supertrend.py --dry-run
venv/bin/python scripts/repair_weekly_supertrend.py
```

The repair removes dependent stock-alert semantic chunks, events, and outbox
rows together with invalid weekly signals and alerts. It leaves valid historical
records unchanged.

## Optional Daemon Mode

OpenClaw cron is the default deployment. Daemon mode is for a continuous
Groundhog process that polls for due tasks and runs `daily-stocks` once per
Phoenix business day after 5pm. Do not enable both modes: near 5pm they can
race to start the same job.

To switch from OpenClaw cron to daemon mode as `openclaw`:

```bash
openclaw cron disable 9ca75bbb-6e16-45db-a2eb-473bc13547ce
cp deploy/systemd/user/groundhog-daemon.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now groundhog-daemon.service
systemctl --user status groundhog-daemon.service
journalctl --user -u groundhog-daemon.service -f
```

The daemon handles `SIGTERM` and `SIGINT` cleanly. Its `Restart=on-failure`
policy lets systemd restart it after an unexpected process failure.

## Agent Direction

Keep OpenClaw as the chat, scheduling, and delivery layer. Groundhog should stay
focused on local data capture, analytics, and query tools.

A future long-running Groundhog agent should use an append-only `events` table
as its boundary with OpenClaw:

- Groundhog writes detected facts and signal events.
- OpenClaw reads or is triggered by those events.
- Delivery state remains outside Groundhog, except for local dedupe tables such
  as `stock_alerts`.

Groundhog's service-state MCP contract is documented in `docs/OpenClaw_MCP.md`.
Changes to those tool names or JSON result shapes require an OpenClaw contract
review.
