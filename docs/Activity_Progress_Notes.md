# Garmin Activity Progress Notes

Activity-specific progress notes use only Garmin activity rows. Legacy activity
uploads are excluded. The combined rollup also includes Garmin daily step
totals, aggregated from 15-minute intervals by Phoenix calendar day. Each
completed Sunday-through-Saturday week or calendar month produces one note per
relevant Garmin activity type and one rollup.

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

Run generation after the daily 5 PM Phoenix Garmin sync. The weekly job
delivers its compact summary to Telegram; the monthly job stores and indexes
notes without sending a message:

```text
name: groundhog-activity-progress-weekly
schedule: cron 0 18 * * 0
timezone: America/Phoenix
command: venv/bin/python groundhog_service.py summarize activity --period weekly --notify && venv/bin/python scripts/index_semantic_documents.py --domain activity_progress
delivery.mode: announce
delivery.channel: telegram

name: groundhog-activity-progress-monthly
schedule: cron 10 18 1 * *
timezone: America/Phoenix
command: venv/bin/python groundhog_service.py summarize activity --period monthly && venv/bin/python scripts/index_semantic_documents.py --domain activity_progress
delivery.mode: none
```

Stair counts are estimates based on elapsed workout time at 32 stairs per
minute. They do not use Garmin's generic activity-level `steps` and
`total_cycles` fields. Daily steps are a separate whole-day metric in the
combined rollup and are not assigned to walking, treadmill, or stair sessions.

For lap swimming, a 25-yard length recorded in under five seconds contributes
distance only. Its time and derived efficiency metrics are excluded.
