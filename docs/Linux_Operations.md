# Groundhog Linux Operations

Current Linux deployment assumptions:

- Host: `192.168.1.38`
- Service user: `openclaw`
- Repo: `/home/openclaw/apps/groundhog`
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
outbox item itself; OpenClaw will read and update this table through MCP in a
later phase.

Inspect pending delivery items with their source facts:

```bash
venv/bin/python -c "import duckdb; from config.settings import DB_PATH; con = duckdb.connect(str(DB_PATH)); print(con.execute(\"SELECT o.id, e.event_type, e.payload, o.created_at FROM outbox o JOIN events e ON e.id = o.event_id WHERE o.status = 'pending' ORDER BY o.created_at\").fetchall())"
```

## systemd User Timer

Install the user service and timer as `openclaw`:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/user/groundhog-stocks.service ~/.config/systemd/user/
cp deploy/systemd/user/groundhog-stocks.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now groundhog-stocks.timer
```

The timer is pinned to `America/Phoenix` in the unit file, independent of the
host's system timezone.

Useful checks:

```bash
systemctl --user status groundhog-stocks.timer
systemctl --user list-timers groundhog-stocks.timer
journalctl --user -u groundhog-stocks.service -n 100 --no-pager
systemctl --user start groundhog-stocks.service
```

Linger is already enabled for `openclaw`, so the timer can run without an
active login session.

## Optional Daemon Mode

The existing timer is the default deployment. Daemon mode is for a continuous
Groundhog process that polls for due tasks and runs `daily-stocks` once per
Phoenix business day after 5pm. Do not enable both modes: near 5pm they can
race to start the same job.

To switch from the timer to daemon mode as `openclaw`:

```bash
systemctl --user disable --now groundhog-stocks.timer
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
