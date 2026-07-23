import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import base64
import json
import re
import shutil
from typing import Optional

import duckdb
import httpx

from config.settings import DB_PATH, OLLAMA_CHAT_URL, SLEEP_DROP_FOLDER, OLLAMA_VISION_MODEL

OLLAMA_URL = OLLAMA_CHAT_URL
PROCESSED_DIR = SLEEP_DROP_FOLDER / "processed"
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}

PROMPT = """You are extracting sleep data from a Sleep8 app screenshot.

Return a JSON object with:
{
  "resting_hr": <integer bpm or null>,
  "hrv": <integer ms or null>,
  "breath_rate": <float breaths/min or null>,
  "time_to_fall_asleep_minutes": <integer or null>,
  "deep_sleep_minutes": <integer or null>
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


def _insert(con: duckdb.DuckDBPyConnection, metrics: dict) -> None:
    con.execute(
        """
        INSERT INTO sleep_metrics (date, resting_hr, hrv, breath_rate, time_to_fall_asleep_minutes, deep_sleep_minutes)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (date) DO NOTHING
        """,
        [
            metrics["date"],
            metrics.get("resting_hr"),
            metrics.get("hrv"),
            metrics.get("breath_rate"),
            metrics.get("time_to_fall_asleep_minutes"),
            metrics.get("deep_sleep_minutes"),
        ],
    )


def _date_from_filename(path: Path) -> Optional[str]:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", path.stem)
    return match.group(1) if match else None


def run() -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    images = [p for p in SLEEP_DROP_FOLDER.iterdir() if p.suffix.lower() in IMAGE_EXTS]

    if not images:
        print("No images found in sleep8 drop folder.")
        return

    con = duckdb.connect(str(DB_PATH))
    try:
        for image_path in images:
            print(f"Processing {image_path.name}...")
            date = _date_from_filename(image_path)
            if not date:
                print(f"  Skipping: no YYYY-MM-DD date found in filename.")
                continue
            try:
                raw = _query_ollama(image_path)
                metrics = _parse_metrics(raw)
                if not metrics:
                    print(f"  Could not parse response: {raw[:200]}")
                    continue
                metrics["date"] = date
                _insert(con, metrics)
                print(f"  Inserted: {metrics}")
                shutil.move(str(image_path), PROCESSED_DIR / image_path.name)
            except Exception as e:
                print(f"  Error: {e}")
    finally:
        con.close()


if __name__ == "__main__":
    run()
