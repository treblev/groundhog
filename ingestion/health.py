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

from config.settings import DB_PATH, DROP_FOLDER, OLLAMA_CHAT_URL, OLLAMA_VISION_MODEL

OLLAMA_URL = OLLAMA_CHAT_URL
PROCESSED_DIR = DROP_FOLDER / "processed"
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}

PROMPT = """You are extracting data from a Garmin fitness tracker screenshot.

First, decide which type of screen this is:
- ACTIVITY screen: shows a specific workout (e.g. a run, walk, or ride) with metrics like distance, duration, pace, or workout heart rate. There may be multiple activities listed.
- DAILY SUMMARY screen: shows aggregate stats for the whole day — total steps, active minutes, resting heart rate. No distance or pace is shown.

Only extract a value if that specific activity's own row shows a labeled field for it. Never copy or infer a value from a different metric, or from a different activity's row — if a field isn't present for this activity, return null for it.

Screenshots rarely show a year — do not guess one. Only extract the month and day shown.

If this is an ACTIVITY screen, return a JSON array with one object per activity — even if there is only one:
[
  {
    "type": "activity",
    "month_day": "MM-DD",
    "activity_type": <"running", "walking", "cycling", "strength training", "cardio" or "other">,
    "distance_miles": <float or null>,
    "duration_seconds": <integer or null>,
    "avg_pace_seconds_per_mile": <integer or null>,
    "avg_hr": <integer or null>,
    "max_hr": <integer or null>,
    "calories": <integer or null>
  }
]

If this is a DAILY SUMMARY screen, return a single JSON object:
{
  "type": "daily_summary",
  "month_day": "MM-DD",
  "steps": <integer or null>,
  "avg_hr": <integer or null>,
  "active_minutes": <integer or null>
}

Return null for any metric not visible. No explanation, just JSON."""

def _encode_image(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("utf-8")


def _query_ollama(image_path: Path) -> str:
    payload = {
        "model": OLLAMA_VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": PROMPT,
                "images": [_encode_image(image_path)],
            }
        ],
        "stream": False,
    }
    response = httpx.post(OLLAMA_URL, json=payload, timeout=600.0)
    response.raise_for_status()
    return response.json()["message"]["content"]


def _parse_metrics(raw: str) -> Optional[list[dict]]:
    match = re.search(r"(\[.*?\]|\{.*?\})", raw, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group())
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        return None


def _resolve_date(month_day: str, today: date) -> Optional[str]:
    """The model is never asked for a year (screenshots rarely show one reliably).
    Pin the year to today's, rolling back one year if that would land in the future."""
    try:
        month, day_num = (int(p) for p in month_day.split("-"))
    except (TypeError, ValueError, AttributeError):
        return None
    try:
        candidate = date(today.year, month, day_num)
    except ValueError:
        return None
    if candidate > today:
        candidate = candidate.replace(year=candidate.year - 1)
    return candidate.isoformat()


def _activity_id(date: str, activity_type: str, duration_seconds: Optional[int]) -> str:
    key = f"{date}|{activity_type}|{duration_seconds}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _insert_daily_summary(con: duckdb.DuckDBPyConnection, metrics: dict) -> None:
    con.execute(
        """
        INSERT INTO health_metrics (date, steps, avg_hr, active_minutes)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (date) DO NOTHING
        """,
        [
            metrics["date"],
            metrics.get("steps"),
            metrics.get("avg_hr"),
            metrics.get("active_minutes"),
        ],
    )


def _insert_activity(con: duckdb.DuckDBPyConnection, metrics: dict) -> None:
    activity_id = _activity_id(
        metrics["date"],
        metrics.get("activity_type", ""),
        metrics.get("duration_seconds"),
    )
    con.execute(
        """
        INSERT INTO activities (
            id, date, activity_type, distance_miles, duration_seconds,
            avg_pace_seconds_per_mile, avg_hr, max_hr, calories
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            distance_miles = COALESCE(excluded.distance_miles, activities.distance_miles),
            duration_seconds = COALESCE(excluded.duration_seconds, activities.duration_seconds),
            avg_pace_seconds_per_mile = COALESCE(excluded.avg_pace_seconds_per_mile, activities.avg_pace_seconds_per_mile),
            avg_hr = COALESCE(excluded.avg_hr, activities.avg_hr),
            max_hr = COALESCE(excluded.max_hr, activities.max_hr),
            calories = COALESCE(excluded.calories, activities.calories)
        """,
        [
            activity_id,
            metrics["date"],
            metrics.get("activity_type"),
            metrics.get("distance_miles"),
            metrics.get("duration_seconds"),
            metrics.get("avg_pace_seconds_per_mile"),
            metrics.get("avg_hr"),
            metrics.get("max_hr"),
            metrics.get("calories"),
        ],
    )


def run() -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    images = [p for p in DROP_FOLDER.iterdir() if p.suffix.lower() in IMAGE_EXTS]

    if not images:
        print("No images found in drop folder.")
        return

    con = duckdb.connect(str(DB_PATH))
    try:
        for image_path in images:
            print(f"Processing {image_path.name}...")
            try:
                raw = _query_ollama(image_path)
                records = _parse_metrics(raw)
                if not records:
                    print(f"  Could not parse response: {raw[:200]}")
                    continue
                for metrics in records:
                    metrics["date"] = _resolve_date(metrics.get("month_day"), date.today())
                    if not metrics["date"]:
                        print(f"  Could not resolve date from month_day={metrics.get('month_day')!r}, skipping.")
                        continue
                    record_type = metrics.get("type")
                    if record_type == "daily_summary":
                        _insert_daily_summary(con, metrics)
                    elif record_type == "activity":
                        _insert_activity(con, metrics)
                    else:
                        print(f"  Unknown type '{record_type}', skipping.")
                        continue
                    print(f"  Inserted: {metrics}")
                shutil.move(str(image_path), PROCESSED_DIR / image_path.name)
            except Exception as e:
                print(f"  Error: {e}")
    finally:
        con.close()


if __name__ == "__main__":
    run()
