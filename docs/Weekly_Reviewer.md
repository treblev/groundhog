# Weekly Reviewer

`/weekly-review` produces a stored-facts review for one completed
Sunday-through-Saturday week.
The OpenClaw plugin calls Groundhog directly. It does not send the request
through OpenClaw model routing. Any generation remains inside the configured
local Ollama workflow.

## Manual CLI

Run the service command from the Groundhog checkout with a Saturday date:

```bash
venv/bin/python groundhog_service.py summarize weekly --date 2026-08-29
```

`GROUNDHOG_DB_PATH` must be available in the environment as normal. The command
writes the derived review locally and prints it. It does not deliver a message.
Add `--notify` when a scheduled run should queue the review through Groundhog's
existing outbox and Telegram delivery bridge. Notification is deduplicated by
week.

## Telegram command

After the `groundhog-weekly-review-router` plugin is installed and enabled,
Telegram accepts:

```text
/weekly-review
/weekly-review 2026-08-29
```

Install it from the deployed Groundhog checkout, enable it, then restart the
Gateway:

```bash
openclaw plugins install /home/openclaw/apps/groundhog/deploy/openclaw/plugins/groundhog-weekly-review-router
openclaw plugins enable groundhog-weekly-review-router
systemctl --user restart openclaw-gateway.service
```

The optional argument must be a real `YYYY-MM-DD` Saturday. With no argument,
the router resolves the latest Saturday that was already complete in
`America/Phoenix`; when invoked on Saturday, that means the prior Saturday.
The command runs this local process with a twelve-minute timeout, enough for
five sequential local-model calls at the configured two-minute call limit:

```text
python groundhog_service.py summarize weekly --date <resolved-saturday>
```

It returns validation, timeout, empty-output, and process-failure messages as
factual command errors. It never routes the review request through a cloud or
OpenClaw conversational model.

## OpenClaw schedule

The deployed job runs every Saturday at 6:00 PM Phoenix time. Because Saturday
is still in progress when the job starts, the default date resolution selects
the prior fully completed Sunday-through-Saturday week:

```text
name: groundhog-weekly-review
schedule: cron 0 18 * * 6
timezone: America/Phoenix
command: venv/bin/python groundhog_service.py summarize weekly --notify
delivery.mode: none
timeout: 12m
```

The scheduled command resolves the latest completed Saturday automatically and
queues one deduplicated outbox message. OpenClaw cron delivery stays disabled;
the existing Groundhog outbox bridge owns Telegram delivery. A manual run may
still pass an explicit Saturday when a different completed week is needed.
