from pathlib import Path
import os

import anthropic
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DB_PATH = Path(os.environ["GROUNDHOG_DB_PATH"])
DROP_FOLDER = BASE_DIR / "data" / "drop"
SLEEP_DROP_FOLDER = BASE_DIR / "data" / "drop" / "sleep8"
WORKOUTS_DROP_FOLDER = BASE_DIR / "data" / "drop" / "workouts"
OPENCLAW_MEDIA_INBOUND_DIR = (
    Path(os.environ["GROUNDHOG_OPENCLAW_MEDIA_INBOUND_DIR"])
    if os.environ.get("GROUNDHOG_OPENCLAW_MEDIA_INBOUND_DIR")
    else None
)
OPENCLAW_MEDIA_STATE_PATH = Path(
    os.environ.get(
        "GROUNDHOG_OPENCLAW_MEDIA_STATE_PATH",
        BASE_DIR / "data" / "openclaw_activity_media_state.json",
    )
)
REQUEST_TRACE_DIR = Path(
    os.environ.get("GROUNDHOG_REQUEST_TRACE_DIR", BASE_DIR / "data" / "logs" / "request-traces")
)
REQUEST_TRACE_RETENTION_DAYS = 30
OLLAMA_VISION_MODEL = "qwen3-vl:latest"
OLLAMA_SQL_MODEL = "qwen3.6:latest"
OLLAMA_BASE_URL = os.environ.get("GROUNDHOG_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_CHAT_URL = f"{OLLAMA_BASE_URL}/api/chat"
OLLAMA_EMBEDDINGS_URL = f"{OLLAMA_BASE_URL}/api/embeddings"

WATCHLIST_FILE = BASE_DIR / "config" / "watchlist.txt"

def load_watchlist() -> list[tuple[str, str]]:
    result = []
    for line in WATCHLIST_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        ticker = parts[0]
        period = parts[1] if len(parts) > 1 else "2y"
        result.append((ticker, period))
    return result
