# Groundhog Linux Operations

Current Linux deployment assumptions:

- Host: `192.168.1.38`
- Service user: `openclaw`
- Repo: `/home/openclaw/apps/groundhog`
- Deployment branch: `main` (tracks `origin/main`)
- DuckDB: `/home/openclaw/data/groundhog/groundhog.duckdb`
- OpenClaw gateway: user systemd service on `127.0.0.1:18789`
- Ollama: `http://192.168.1.13:11434`
- Privacy: local Ollama only; no OpenAI or Anthropic fallback

## Ollama Access

When Groundhog runs on Linux but Ollama runs on the Mac, the Mac Ollama base URL
is configured explicitly in `config/settings.py` as
`OLLAMA_BASE_URL=http://192.168.1.13:11434`.

Verify from Linux:

```bash
curl http://192.168.1.13:11434/api/tags
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

OpenClaw cron runs the bridge every 15 minutes:

```text
name: groundhog-outbox-telegram
id: 201d4f1c-6ad5-41b3-859f-2b8ab70f3ab3
schedule: every 15m
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

## Apple Wallet Spending Screenshot Intake

Install the `groundhog-spending-router` plugin from
`deploy/openclaw/plugins/groundhog-spending-router` with `openclaw plugins install`.
It claims only Telegram
image attachments whose caption contains `/expense`, before the chat model runs.
It invokes the local Apple Wallet importer, archives the screenshot under
Groundhog data, marks the media watcher checkpoint so it cannot be re-imported
as an activity, and replies with imported transactions and their short IDs.

Correct a category later with:

```text
/expense-category <transaction-id> <groceries|dining|shopping|entertainment|beer|other>
```

The importer resolves Wallet's relative date labels from the upload timestamp
in `America/Phoenix`. It does not change the default activity/plan/sleep image
routes.

Install it as `openclaw`:

```bash
cp deploy/systemd/user/groundhog-openclaw-media.service ~/.config/systemd/user/
cp deploy/systemd/user/groundhog-openclaw-media.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now groundhog-openclaw-media.timer
```

The first service run must initialize the checkpoint without importing older
Telegram images:

```bash
GROUNDHOG_DB_PATH=/home/openclaw/data/groundhog/groundhog.duckdb \
GROUNDHOG_OPENCLAW_MEDIA_INBOUND_DIR=/home/openclaw/media/inbound \
GROUNDHOG_OPENCLAW_MEDIA_STATE_PATH=/home/openclaw/data/groundhog/openclaw_activity_media_state.json \
venv/bin/python -m scripts.import_openclaw_activity_media --initialize
```

## OpenClaw Stock Schedule

OpenClaw is the production scheduler for weekday stock jobs. The deployed job
is `groundhog-daily-stocks`; it runs exactly at 5:00 PM in
`America/Phoenix` on Monday through Friday, with no stagger. It invokes the
same `groundhog_service.py run daily-stocks` command as the manual entrypoint.

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
