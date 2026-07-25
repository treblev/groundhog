import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import base64
import hashlib
import json
import re
import shutil
from datetime import date
from typing import Optional

import duckdb
import httpx

from config.settings import DB_PATH, OLLAMA_CHAT_URL, WORKOUTS_DROP_FOLDER, OLLAMA_VISION_MODEL
from agent.events import record_event

OLLAMA_URL = OLLAMA_CHAT_URL
PROCESSED_DIR = WORKOUTS_DROP_FOLDER / "processed"
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}

PROMPT = """You are extracting one complete daily workout plan from a SugarWOD screenshot.

Always return a JSON array containing exactly one object. A screenshot may show
multiple cards or sections (for example, a HYROX section followed by strength
work), but they are components of one workout plan—not separate workouts.

Return:
{
  "day_of_week": <"MON"|"TUE"|"WED"|"THU"|"FRI"|"SAT"|"SUN" — from the column header, or null if unavailable>,
  "date_day": <integer day-of-month from the column header, or null>,
  "date": <"YYYY-MM-DD" if fully visible, otherwise null>,
  "name": <the main workout title, or the first card header if no overall title exists>,
  "category": <the primary visible label, or null>,
  "structure_type": <the primary format, or null if the plan has multiple formats>,
  "description": <all sections and cards in reading order, including each card heading and its full text, preserving newlines as \\n>
}

structure_type rules:
- "amrap"     → text contains "AMRAP"
- "emom"      → text contains "EMOM"
- "rotating"  → text contains "rotating"
- "for_time"  → text contains "for time" or a time cap like "(15 cap)"
- "strength"  → a lift with a rep scheme like "8-8-6-6-4-4" or "5-5-5-3-3-3"
- "intervals" → timed work/rest blocks like "30 second work / 2 minute rest"
- null        → if unclear

Do not infer dates from the screen; the importer assigns the date from the
screenshot filename.

Return null for any field not visible. No explanation, just the JSON array."""


def _encode_image(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("utf-8")


def _query_ollama(image_path: Path) -> str:
    payload = {
        "model": OLLAMA_VISION_MODEL,
        "messages": [{"role": "user", "content": PROMPT, "images": [_encode_image(image_path)]}],
        "stream": False,
    }
    response = httpx.post(OLLAMA_URL, json=payload, timeout=600.0)
    response.raise_for_status()
    return response.json()["message"]["content"]


def _parse_workouts(raw: str) -> list[dict]:
    """Extract the one-plan JSON array or object, with optional Markdown fencing."""
    match = re.search(r"```(?:json)?\s*(\[.*?\]|\{.*?\})\s*```", raw, re.DOTALL | re.IGNORECASE)
    if match:
        candidate = match.group(1)
    else:
        array_start, object_start = raw.find("["), raw.find("{")
        if array_start >= 0 and (object_start < 0 or array_start < object_start):
            candidate = raw[array_start : raw.rfind("]") + 1]
        else:
            candidate = raw[object_start : raw.rfind("}") + 1]
    if not candidate:
        return []
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict):
        return [parsed]
    return [workout for workout in parsed if isinstance(workout, dict)] if isinstance(parsed, list) else []


def _combine_workout_cards(workouts: list[dict]) -> list[dict]:
    """Defensively merge multi-card model output into the one plan per screenshot."""
    if len(workouts) <= 1:
        return workouts

    primary = workouts[0].copy()
    sections = []
    for workout in workouts:
        heading = workout.get("name") or "Workout section"
        description = workout.get("description") or ""
        sections.append(f"{heading}\n{description}".strip())
    primary["description"] = "\n\n".join(sections)
    primary["structure_type"] = (
        primary.get("structure_type")
        if len({workout.get("structure_type") for workout in workouts}) == 1
        else None
    )
    return [primary]


def _date_from_filename(path: Path) -> Optional[date]:
    """Return the required screenshot date from a YYYY-MM-DD filename component."""
    match = re.search(r"(\d{4}-\d{2}-\d{2})", path.stem)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _apply_filename_date(workouts: list[dict], screenshot_date: date) -> None:
    """Use the filename as the authoritative date for every imported workout."""
    for w in workouts:
        w["date"] = screenshot_date.isoformat()
        w["date_day"] = screenshot_date.day
        w["day_of_week"] = screenshot_date.strftime("%a").upper()


def _workout_id(workout: dict) -> str:
    key = f"{workout.get('date')}|{workout.get('name')}|{(workout.get('description') or '')[:50]}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _insert(con: duckdb.DuckDBPyConnection, workout: dict) -> None:
    con.execute(
        """
        INSERT INTO workouts (id, date, day_of_week, name, category, structure_type, description)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO NOTHING
        """,
        [
            _workout_id(workout),
            workout.get("date"),
            workout.get("day_of_week"),
            workout.get("name"),
            workout.get("category"),
            workout.get("structure_type"),
            workout.get("description"),
        ],
    )


def _upload_id(image_path: Path) -> str:
    """Identify an uploaded screenshot by content, independent of its filename."""
    return hashlib.sha256(image_path.read_bytes()).hexdigest()


def _archive_image(image_path: Path, upload_id: str) -> Path:
    """Keep an immutable local copy without changing OpenClaw's media cache."""
    destination = PROCESSED_DIR / f"{upload_id}{image_path.suffix.lower()}"
    if not destination.exists():
        shutil.copy2(image_path, destination)
    return destination


def process_image(image_path: Path, screenshot_date: date | None = None) -> int:
    """Extract one screenshot supplied directly by an upload integration.

    The integration supplies the date as metadata; it is used to construct the
    same filename-derived date that manual drop-folder ingestion requires.
    """
    if image_path.suffix.lower() not in IMAGE_EXTS:
        raise ValueError(f"Unsupported image type: {image_path.suffix or '(none)'}")
    if not image_path.is_file():
        raise FileNotFoundError(image_path)

    WORKOUTS_DROP_FOLDER.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    upload_id = _upload_id(image_path)
    screenshot_date = screenshot_date or _date_from_filename(image_path)
    if screenshot_date is None:
        raise ValueError("A valid YYYY-MM-DD workout date is required.")

    raw = _query_ollama(image_path)
    workouts = _parse_workouts(raw)
    if not workouts:
        raise ValueError(f"Could not parse workout data: {raw[:200]}")
    workouts = _combine_workout_cards(workouts)
    _apply_filename_date(workouts, screenshot_date)

    con = duckdb.connect(str(DB_PATH))
    try:
        for workout in workouts:
            _insert(con, workout)
        record_event(
            con,
            event_type="workout_data_imported",
            source="ingestion.workouts",
            subject_type="workout_upload",
            subject_id=upload_id,
            payload={
                "date": screenshot_date.isoformat(),
                "source_file": image_path.name,
                "workout_count": len(workouts),
            },
            dedupe_key=f"workout_upload:{upload_id}",
        )
    finally:
        con.close()

    _archive_image(image_path, upload_id)
    return len(workouts)


def run() -> None:
    WORKOUTS_DROP_FOLDER.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    images = [p for p in WORKOUTS_DROP_FOLDER.iterdir() if p.suffix.lower() in IMAGE_EXTS]

    if not images:
        print("No images found in workouts drop folder.")
        return

    for image_path in images:
        print(f"Processing {image_path.name}...")
        try:
            count = process_image(image_path)
            shutil.move(str(image_path), PROCESSED_DIR / image_path.name)
            print(f"  Imported {count} workout(s).")
        except Exception as error:
            print(f"  Error: {error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract workouts from SugarWOD screenshots.")
    parser.add_argument("--image", type=Path, help="A screenshot provided by an upload integration.")
    parser.add_argument("--date", type=date.fromisoformat, help="Workout date, in YYYY-MM-DD format.")
    args = parser.parse_args()
    if args.image is None:
        run()
        return
    if args.date is None:
        parser.error("--date is required when --image is used")
    count = process_image(args.image, args.date)
    print(f"Imported {count} workout(s) from {args.image.name}.")


if __name__ == "__main__":
    main()
