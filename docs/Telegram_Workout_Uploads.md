# Telegram Workout Uploads

OpenClaw's Telegram channel receives inbound images locally. For a SugarWOD
screenshot, it should call Groundhog directly with the received media path:

```bash
cd /home/openclaw/apps/groundhog
GROUNDHOG_DB_PATH=/home/openclaw/data/groundhog/groundhog.duckdb \
  venv/bin/python -m ingestion.workouts \
  --image "$MEDIA_PATH" --date YYYY-MM-DD
```

`$MEDIA_PATH` is the local file path provided by OpenClaw for the Telegram
attachment. The date comes from the user's `YYYY-MM-DD` caption, not from the
vision model or screenshot pixels. Groundhog copies the source image into its
private processed archive, calls the local `qwen3-vl` model, upserts the parsed
workouts, and records one idempotent `workout_data_imported` event.

Suggested OpenClaw behavior for direct Telegram messages:

1. On a PNG, JPG, or JPEG attachment with a `YYYY-MM-DD` caption, invoke the
   command above with that date.
2. Reply with the number of imported workout cards and their names.
3. If the date is absent or invalid, ask for it; do not guess from the image or
   the Telegram message timestamp.
4. If extraction fails, leave the attachment untouched and report the error.

The command does not move or delete OpenClaw's media-cache file.
