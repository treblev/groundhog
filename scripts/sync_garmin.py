"""Run the read-only Garmin extraction followed by the Groundhog importer."""

import argparse
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.garmin import import_garmin


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract Garmin activities and import them into Groundhog.")
    parser.add_argument("--extractor", required=True, type=Path, help="Path to the garmin CLI")
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--groundhog-db", type=Path, default=None)
    parser.add_argument("--lookback-days", type=int, default=7, help="Trailing days to re-import (default: 7)")
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Re-import activities already present in Groundhog (slow; manual repair only)",
    )
    args = parser.parse_args(argv)
    if args.lookback_days < 0:
        parser.error("--lookback-days must be non-negative")

    end_date = date.today() + timedelta(days=1)
    start_date = date.today() - timedelta(days=args.lookback_days)
    subprocess.run(
        [
            str(args.extractor),
            "extract",
            "--data-types",
            "ACTIVITIES_LIST",
            "--data-types",
            "ACTIVITY",
            "--db-path",
            str(args.source_db),
            "--start-date",
            start_date.isoformat(),
            "--end-date",
            end_date.isoformat(),
        ],
        check=True,
    )
    summary = import_garmin(
        args.source_db,
        args.groundhog_db,
        start_date=start_date,
        end_date=end_date,
        only_missing=not args.refresh_existing,
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
