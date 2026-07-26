---
name: groundhog-activity-upload
description: Import a Telegram activity screenshot directly, using its caption to fill a missing date.
---

# Groundhog activity screenshot uploads

For a direct Telegram PNG, JPG, or JPEG activity-result attachment, invoke the
Groundhog importer immediately. The runtime provides the local attachment path
and the message caption/text. Pass both as separate arguments; never interpolate
caption text into a shell string:

```bash
venv/bin/python -m scripts.import_openclaw_activity_media --image <media-path> --caption <caption-text>
```

Working directory: `/home/openclaw/apps/groundhog`.
Environment: `GROUNDHOG_DB_PATH=/home/openclaw/data/groundhog/groundhog.duckdb`
and `GROUNDHOG_OPENCLAW_MEDIA_STATE_PATH=/home/openclaw/data/groundhog/openclaw_activity_media_state.json`.

The importer reads `M/D`, `MM-DD`, or `YYYY-MM-DD` in the caption only when the
screenshot date is missing or unclear. A visible screenshot date always wins.
It records the attachment in the media-watch state, so the periodic watcher will
not import it again. Reply with the importer confirmation only.
