from pathlib import Path
import anthropic

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "db" / "groundhog.duckdb"
DROP_FOLDER = BASE_DIR / "data" / "drop"
OLLAMA_MODEL = "qwen2.5-coder:32b"