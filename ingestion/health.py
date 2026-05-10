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

from config.settings import DB_PATH, DROP_FOLDER, OLLAMA_VISION_MODEL

OLLAMA_URL = "http://localhost:11434/api/chat"
PROCESSED_DIR = DROP_FOLDER / "processed"
IMAGE_EXTS = {".jpg", ".jpeg", ".png"}

PROMPT = """Extract health metrics from this Garmin fitness tracker screenshot.

Return ONLY a JSON object with these exact fields:
{
  "date": "YYYY-MM-DD",
  "steps": <integer or null>,
  "avg_hr": <integer or null>,
  "active_minutes": <integer or null>
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


def _insert(con: duckdb.DuckDBPyConnection, metrics: dict) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO health_metrics (date, steps, avg_hr, active_minutes)
        VALUES (?, ?, ?, ?)
        """,
        [
            metrics["date"],
            metrics.get("steps"),
            metrics.get("avg_hr"),
            metrics.get("active_minutes"),
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
                _insert(con, metrics)
                shutil.move(str(image_path), PROCESSED_DIR / image_path.name)
                print(f"  Inserted: {metrics}")
            except Exception as e:
                print(f"  Error: {e}")
    finally:
        con.close()


if __name__ == "__main__":
    run()
