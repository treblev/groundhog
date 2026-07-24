"""Compatibility entrypoint for the Groundhog service runner."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from groundhog_service import run_daily_stocks


def run() -> None:
    run_daily_stocks()


if __name__ == "__main__":
    run()
