from pathlib import Path
import anthropic

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "db" / "groundhog.duckdb"
DROP_FOLDER = BASE_DIR / "data" / "drop"
OLLAMA_VISION_MODEL = "qwen3-vl:latest"
OLLAMA_SQL_MODEL = "qwen2.5-coder:32b"

WATCHLIST_FILE = BASE_DIR / "config" / "watchlist.txt"

def load_watchlist() -> list[tuple[str, str]]:
    result = []
    for line in WATCHLIST_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        ticker = parts[0]
        period = parts[1] if len(parts) > 1 else "1d"
        result.append((ticker, period))
    return result