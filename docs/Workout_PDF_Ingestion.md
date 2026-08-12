# Workout PDF Ingestion

## Purpose

Import manually saved SugarWOD calendar PDFs into the canonical `workouts`
table. This is a manual local workflow; it has no Telegram command, scheduler,
or automatic folder watcher.

## Run

```bash
python ingestion/workout_pdfs.py --folder /path/to/calendar-pdfs --dry-run
python ingestion/workout_pdfs.py --folder /path/to/calendar-pdfs
```

The importer uses Poppler's `pdftotext` command. Install Poppler if that command
is unavailable.

Each PDF must contain exactly one SugarWOD calendar-week URL. The parser derives
each plan date from its `MON`–`SUN` calendar header and validates that it matches
the week encoded in the PDF URL. It removes common print headers, page counters,
and comment counts while preserving the workout text.

## Safety and idempotency

- The database transaction covers the full folder import: if one PDF fails, no
  PDF in that invocation is committed.
- Existing `workouts` dates are retained and reported as skipped. This avoids
  replacing imported screenshot plans.
- Each successful source PDF records a `workout_data_imported` event keyed by
  its file hash.
- `--dry-run` parses and reports intended changes without writing workouts or
  events.

Imported workouts become eligible for the existing local workout semantic index.
Refresh it after a manual import when you need immediate semantic retrieval:

```bash
python scripts/index_semantic_documents.py --domain workout
```

## Verify

```bash
python -m unittest tests.test_workout_pdf_ingestion
```
