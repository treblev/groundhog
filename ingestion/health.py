import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import base64
import hashlib
import json
import re
import shutil
from typing import Optional

import duckdb
import httpx

from config.settings import DB_PATH, DROP_FOLDER, OLLAMA_VISION_MODEL

OLLAMA_URL = "http://localhost:11434/api/chat"
PROCESSED_DIR = DROP_FOLDER / "processed"
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}

PROMPT = """Identify the type of this Garmin fitness tracker screenshot and extract the relevant metrics.

If it shows a daily activity summary (steps, heart rate for the day), return:
{
  "type": "daily_summary",
  "date": "YYYY-MM-DD",
  "steps": <integer or null>,
  "avg_hr": <integer or null>,
  "active_minutes": <integer or null>
}

If it shows a specific workout or activity (run, walk, bike ride, etc.), return:
{
  "type": "activity",
  "date": "YYYY-MM-DD",
  "activity_type": <string e.g. "running", "walking", "cycling">,
  "distance_miles": <float or null>,
  "duration_seconds": <integer or null>,
  "avg_pace_seconds_per_mile": <integer or null>,
  "avg_hr": <integer or null>,
  "calories": <integer or null>
}

Use the date shown in the screenshot. Return null for any metric not visible. No explanation, just JSON."""


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
    response = httpx.post(OLLAMA_URL, json=payload, timeout=120.0)
    response.raise_for_status()
    return response.json()["message"]["content"]


def _parse_metrics(raw: str) -> Optional[dict]:
    match = re.search(r"\{.*?\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return None


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
            avg_pace_seconds_per_mile, avg_hr, calories
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO NOTHING
        """,
        [
            activity_id,
            metrics["date"],
            metrics.get("activity_type"),
            metrics.get("distance_miles"),
            metrics.get("duration_seconds"),
            metrics.get("avg_pace_seconds_per_mile"),
            metrics.get("avg_hr"),
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
                metrics = _parse_metrics(raw)
                if not metrics:
                    print(f"  Could not parse response: {raw[:200]}")
                    continue
                record_type = metrics.get("type")
                if record_type == "daily_summary":
                    _insert_daily_summary(con, metrics)
                elif record_type == "activity":
                    _insert_activity(con, metrics)
                else:
                    print(f"  Unknown type '{record_type}', skipping.")
                    continue
                shutil.move(str(image_path), PROCESSED_DIR / image_path.name)
                print(f"  Inserted: {metrics}")
            except Exception as e:
                print(f"  Error: {e}")
    finally:
        con.close()


if __name__ == "__main__":
    run()
