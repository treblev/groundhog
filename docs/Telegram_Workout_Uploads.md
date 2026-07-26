# Telegram Workout Uploads

OpenClaw's Telegram channel receives inbound images locally. Workout-result
screenshots are activity data and belong in Groundhog's existing `activities`
table. They are not SugarWOD workout-plan screenshots.

```bash
cd /home/openclaw/apps/groundhog
GROUNDHOG_DB_PATH=/home/openclaw/data/groundhog/groundhog.duckdb \
  venv/bin/python -m ingestion.health \
  --image "$MEDIA_PATH"
```

`$MEDIA_PATH` is the local file path provided by OpenClaw for the Telegram
attachment. Groundhog always reads the activity date shown in the screenshot,
archives a private copy, calls the local `qwen3-vl` model, and upserts the
extracted data into `activities`. The upload timestamp is never used as the
activity date.

Suggested OpenClaw behavior for direct Telegram messages:

1. On a PNG, JPG, or JPEG workout-result attachment, invoke the command above.
2. Reply with the imported activity type and visible metrics.
3. Put a date in the image caption when the screenshot date is unclear, for
   example `Easy run — 7/18`. Groundhog accepts `M/D`, `MM-DD`, or
   `YYYY-MM-DD`; short dates are resolved to the most recent occurrence based
   on the upload date. The caption fills only a missing or unreadable screenshot
   date. A visible screenshot date always wins.
4. If extraction fails, leave the attachment untouched and report the error.

The command does not move or delete OpenClaw's media-cache file.
