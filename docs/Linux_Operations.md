# Groundhog Linux Operations

Current Linux deployment assumptions:

- Host: `192.168.1.38`
- Service user: `openclaw`
- Repo: `/home/openclaw/apps/groundhog`
- DuckDB: `/home/openclaw/data/groundhog/groundhog.duckdb`
- OpenClaw gateway: user systemd service on `127.0.0.1:18789`
- Ollama: `http://192.168.1.13:11434`
- Privacy: local Ollama only; no OpenAI or Anthropic fallback

## Stock Jobs

The stock pipeline should run as the `openclaw` user:

```bash
cd /home/openclaw/apps/groundhog
GROUNDHOG_DB_PATH=/home/openclaw/data/groundhog/groundhog.duckdb \
  GROUNDHOG_ALERT_BACKEND=none \
  scripts/daily_stocks.sh
```

`scripts/daily_stocks.sh` runs:

1. `python ingestion/stocks.py`
2. `python analytics/signals.py`
3. `python analytics/alerts.py`

`analytics/alerts.py` records deduped rows in `stock_alerts`. On Linux, set
`GROUNDHOG_ALERT_BACKEND=none` when OpenClaw is responsible for chat,
scheduler, and delivery. For interactive desktop notifications, use
`GROUNDHOG_ALERT_BACKEND=notify-send`.

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

## Agent Direction

Keep OpenClaw as the chat, scheduling, and delivery layer. Groundhog should stay
focused on local data capture, analytics, and query tools.

A future long-running Groundhog agent should use an append-only `events` table
as its boundary with OpenClaw:

- Groundhog writes detected facts and signal events.
- OpenClaw reads or is triggered by those events.
- Delivery state remains outside Groundhog, except for local dedupe tables such
  as `stock_alerts`.

Do not modify `mcp_server/server.py` for this migration unless the OpenClaw MCP
contract changes.
