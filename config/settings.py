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
OLLAMA_VISION_MODEL = "qwen3-vl:latest"
OLLAMA_SQL_MODEL = "qwen3.6:latest"
OLLAMA_BASE_URL = "http://192.168.1.13:11434"
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
