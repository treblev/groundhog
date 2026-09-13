# Garmin Activity Progress Notes

Activity progress notes use only Garmin rows. Legacy activity uploads and daily
step totals are excluded. Each completed Sunday-through-Saturday week or
calendar month produces one note per relevant Garmin activity type and one
rollup.

Generate a completed period manually:

```bash
venv/bin/python groundhog_service.py summarize activity --period weekly
venv/bin/python groundhog_service.py summarize activity --period monthly
venv/bin/python groundhog_service.py summarize activity --period weekly --date 2026-09-12
```

The commands resolve the latest completed period when `--date` is omitted.
They store facts and local-Ollama narratives in `activity_progress_notes`.
Refresh the semantic index with:

```bash
venv/bin/python scripts/index_semantic_documents.py --domain activity_progress
```

## OpenClaw schedules

Run generation after the daily 5 PM Phoenix Garmin sync. These jobs store and
index notes locally and do not send Telegram messages:

```text
name: groundhog-activity-progress-weekly
schedule: cron 0 18 * * 0
timezone: America/Phoenix
command: venv/bin/python groundhog_service.py summarize activity --period weekly && venv/bin/python scripts/index_semantic_documents.py --domain activity_progress
delivery.mode: none

name: groundhog-activity-progress-monthly
schedule: cron 10 18 1 * *
timezone: America/Phoenix
command: venv/bin/python groundhog_service.py summarize activity --period monthly && venv/bin/python scripts/index_semantic_documents.py --domain activity_progress
delivery.mode: none
```

Stair counts are estimates based on elapsed workout time at 32 stairs per
minute. They do not use `health_metrics.steps` or Garmin's generic
activity-level `steps` and `total_cycles` fields.

For lap swimming, a 25-yard length recorded in under five seconds contributes
distance only. Its time and derived efficiency metrics are excluded.
