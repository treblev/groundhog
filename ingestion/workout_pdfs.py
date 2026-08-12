import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import hashlib
import re
import subprocess
from datetime import date, datetime, timedelta

import duckdb

from agent.events import record_event
from config.settings import DB_PATH
from ingestion.workouts import _insert


DAY_HEADER_RE = re.compile(
    r"(?m)^[ \t]*(MON|TUE|WED|THU|FRI|SAT|SUN)[ \t]+(\d{2})[ \t]*$"
)
WEEK_RE = re.compile(r"[?&]week=(\d{8})(?:&|\s|$)")
TRACK_RE = re.compile(r"[?&]track=([^&\s]+)")
PRINT_TIMESTAMP_RE = re.compile(
    r"^\d{1,2}/\d{1,2}/\d{2,4},\s+\d{1,2}:\d{2}\s+[AP]M\b"
)
RESULT_COUNT_RE = re.compile(r"^\d+\s+results?$", re.IGNORECASE)
PAGE_COUNT_RE = re.compile(r"^\d+/\d+$")
PRIVATE_USE_RE = re.compile(r"[\ue000-\uf8ff]")
WEEKDAY_OFFSETS = {
    "MON": 0,
    "TUE": 1,
    "WED": 2,
    "THU": 3,
    "FRI": 4,
    "SAT": 5,
    "SUN": 6,
}


def _extract_pdf_text(path: Path) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError("pdftotext is required; install poppler-utils.") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or "unknown pdftotext error"
        raise ValueError(f"Could not extract {path.name}: {detail}") from error
    return result.stdout


def _week_start(text: str) -> date:
    week_tokens = set(WEEK_RE.findall(text))
    if len(week_tokens) != 1:
        raise ValueError(f"Expected one SugarWOD calendar week, found {sorted(week_tokens)}")
    return datetime.strptime(week_tokens.pop(), "%Y%m%d").date()


def _track_name(text: str) -> str:
    match = TRACK_RE.search(text)
    if not match:
        return "Workout of the Day"
    value = match.group(1).replace("-", " ").strip()
    return value.title().replace(" Of ", " of ").replace(" The ", " the ")


def _clean_description(raw: str) -> str:
    lines: list[str] = []
    for raw_line in raw.replace("\f", "\n").splitlines():
        line = PRIVATE_USE_RE.sub("", raw_line).strip()
        if PRINT_TIMESTAMP_RE.match(line):
            continue
        if line == "Whiteboard Calendar : SugarWOD":
            continue
        if "app.sugarwod.com/workouts/calendar?" in line:
            continue
        if RESULT_COUNT_RE.match(line) or PAGE_COUNT_RE.match(line):
            continue
        lines.append(line)

    while lines and not lines[0]:
        lines.pop(0)
    if lines and lines[0].isdigit():
        lines.pop(0)
    while lines and not lines[0]:
        lines.pop(0)

    collapsed: list[str] = []
    for line in lines:
        if not line and (not collapsed or not collapsed[-1]):
            continue
        collapsed.append(line)
    while collapsed and not collapsed[-1]:
        collapsed.pop()
    return "\n".join(collapsed)


def _structure_type(description: str) -> str | None:
    text = description.lower()
    matches: set[str] = set()
    if "amrap" in text:
        matches.add("amrap")
    if "emom" in text:
        matches.add("emom")
    if "rotating" in text:
        matches.add("rotating")
    if "for time" in text or re.search(r"\b\d+\s*(?:min(?:ute)?s?)?\s+cap\b", text):
        matches.add("for_time")
    if re.search(r"\b\d+\s*(?:second|minute)s?\s+work\s*/\s*\d+\s*(?:second|minute)s?\s+rest\b", text):
        matches.add("intervals")
    return matches.pop() if len(matches) == 1 else None


def parse_pdf_text(text: str) -> list[dict]:
    week_start = _week_start(text)
    track_name = _track_name(text)
    headers = list(DAY_HEADER_RE.finditer(text))
    if not headers:
        raise ValueError("No dated SugarWOD workout sections were found.")

    workouts: list[dict] = []
    seen_dates: set[date] = set()
    for index, header in enumerate(headers):
        weekday, day_text = header.groups()
        workout_date = week_start + timedelta(days=WEEKDAY_OFFSETS[weekday])
        if workout_date.day != int(day_text):
            raise ValueError(
                f"Calendar header {weekday} {day_text} does not match week {week_start.isoformat()}"
            )
        if workout_date in seen_dates:
            raise ValueError(f"Duplicate calendar section for {workout_date.isoformat()}")

        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        description = _clean_description(text[header.end() : end])
        if not description:
            continue
        first_line = next(line for line in description.splitlines() if line)
        workouts.append(
            {
                "date": workout_date.isoformat(),
                "day_of_week": weekday,
                "name": first_line,
                "category": track_name,
                "structure_type": _structure_type(description),
                "description": description,
            }
        )
        seen_dates.add(workout_date)
    return workouts


def parse_pdf(path: Path) -> list[dict]:
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Unsupported workout-plan file: {path.name}")
    if not path.is_file():
        raise FileNotFoundError(path)
    return parse_pdf_text(_extract_pdf_text(path))


def import_records(
    con: duckdb.DuckDBPyConnection,
    records: list[dict],
    source_path: Path,
    dry_run: bool = False,
) -> dict:
    inserted_dates: list[str] = []
    skipped_dates: list[str] = []
    for workout in records:
        existing = con.execute(
            "SELECT COUNT(*) FROM workouts WHERE date = ?", [workout["date"]]
        ).fetchone()[0]
        if existing:
            skipped_dates.append(workout["date"])
            continue
        if not dry_run:
            _insert(con, workout)
        inserted_dates.append(workout["date"])

    if not dry_run:
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        record_event(
            con,
            event_type="workout_data_imported",
            source="ingestion.workout_pdfs",
            subject_type="workout_pdf",
            subject_id=source_hash,
            payload={
                "source_file": source_path.name,
                "parsed_count": len(records),
                "inserted_dates": inserted_dates,
                "skipped_existing_dates": skipped_dates,
            },
            dedupe_key=f"workout_pdf:{source_hash}",
        )
    return {
        "parsed": len(records),
        "inserted": len(inserted_dates),
        "skipped_existing": len(skipped_dates),
        "inserted_dates": inserted_dates,
    }


def _natural_key(path: Path) -> list[int | str]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def run(folder: Path, dry_run: bool = False) -> dict:
    if not folder.is_dir():
        raise NotADirectoryError(folder)
    pdfs = sorted(folder.glob("*.pdf"), key=_natural_key)
    if not pdfs:
        raise ValueError(f"No PDF files found in {folder}")

    totals = {"files": 0, "parsed": 0, "inserted": 0, "skipped_existing": 0}
    con = duckdb.connect(str(DB_PATH), read_only=dry_run)
    try:
        if not dry_run:
            con.execute("BEGIN")
        for path in pdfs:
            records = parse_pdf(path)
            result = import_records(con, records, path, dry_run=dry_run)
            totals["files"] += 1
            for key in ("parsed", "inserted", "skipped_existing"):
                totals[key] += result[key]
            action = "would insert" if dry_run else "inserted"
            print(
                f"{path.name}: parsed {result['parsed']}, {action} {result['inserted']}, "
                f"skipped existing {result['skipped_existing']}"
            )
        if not dry_run:
            con.execute("COMMIT")
    except Exception:
        if not dry_run:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import manually saved SugarWOD calendar PDFs into Groundhog."
    )
    parser.add_argument("--folder", type=Path, required=True, help="Folder containing calendar PDFs.")
    parser.add_argument("--dry-run", action="store_true", help="Parse and report without writing.")
    args = parser.parse_args()
    totals = run(args.folder, dry_run=args.dry_run)
    prefix = "Dry run" if args.dry_run else "Import"
    print(
        f"{prefix} complete: {totals['files']} files, {totals['parsed']} plans, "
        f"{totals['inserted']} inserted, {totals['skipped_existing']} skipped existing."
    )


if __name__ == "__main__":
    main()
