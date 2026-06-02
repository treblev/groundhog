import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import base64
import hashlib
import json
import re
import shutil
from datetime import date
from typing import Optional

import duckdb
import httpx

from config.settings import DB_PATH, WORKOUTS_DROP_FOLDER, OLLAMA_VISION_MODEL

OLLAMA_URL = "http://localhost:11434/api/chat"
PROCESSED_DIR = WORKOUTS_DROP_FOLDER / "processed"
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}

PROMPT = """You are extracting workout data from a SugarWOD screenshot.

Always return a JSON array. Each element represents one workout card.

For each workout card, return:
{
  "day_of_week": <"MON"|"TUE"|"WED"|"THU"|"FRI"|"SAT"|"SUN" — from the column header, or null if single-day view>,
  "date_day": <integer day-of-month from the column header, or null if not shown>,
  "date": <"YYYY-MM-DD" if the full date is visible, otherwise null>,
  "name": <the workout card header text, e.g. "MURPH", "Deadlift 8-8-6-6-4-4-4", "HYROX (9 am and 4 pm)">,
  "category": <"Fitness"|"Performance"|"HYROX"|or other visible label, or null>,
  "structure_type": <"amrap"|"emom"|"rotating"|"for_time"|"strength"|"intervals"|null>,
  "description": <full workout text as a single string, preserving newlines as \\n>
}

structure_type rules:
- "amrap"     → text contains "AMRAP"
- "emom"      → text contains "EMOM"
- "rotating"  → text contains "rotating"
- "for_time"  → text contains "for time" or a time cap like "(15 cap)"
- "strength"  → a lift with a rep scheme like "8-8-6-6-4-4" or "5-5-5-3-3-3"
- "intervals" → timed work/rest blocks like "30 second work / 2 minute rest"
- null        → if unclear

If the screenshot shows a weekly calendar (MON–SUN columns), extract every visible workout card across all days.
If the screenshot shows a single day, extract each section (Fitness, Performance, etc.) as a separate element.

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
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        return []
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return []


def _fill_missing_date(workouts: list[dict]) -> None:
    today = date.today()
    for w in workouts:
        if not w.get("date"):
            w["date"] = today.isoformat()
        if not w.get("date_day"):
            w["date_day"] = today.day
        if not w.get("day_of_week"):
            w["day_of_week"] = today.strftime("%a").upper()


def _workout_id(workout: dict) -> str:
    key = f"{workout.get('date')}|{workout.get('name')}|{workout.get('description', '')[:50]}"
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


def run() -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    images = [p for p in WORKOUTS_DROP_FOLDER.iterdir() if p.suffix.lower() in IMAGE_EXTS]

    if not images:
        print("No images found in workouts drop folder.")
        return

    con = duckdb.connect(str(DB_PATH))
    try:
        for image_path in images:
            print(f"Processing {image_path.name}...")
            try:
                raw = _query_ollama(image_path)
                workouts = _parse_workouts(raw)
                if not workouts:
                    print(f"  Could not parse response: {raw[:200]}")
                    continue
                _fill_missing_date(workouts)
                for w in workouts:
                    _insert(con, w)
                    print(f"  Inserted: {w.get('day_of_week')} | {w.get('name')} | {w.get('structure_type')}")
                shutil.move(str(image_path), PROCESSED_DIR / image_path.name)
            except Exception as e:
                print(f"  Error: {e}")
    finally:
        con.close()


if __name__ == "__main__":
    run()
