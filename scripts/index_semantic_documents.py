"""Build or refresh Groundhog's local semantic document index."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json

from agent.semantic_search import DOMAIN_WORKOUT, sync_workout_embeddings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", choices=[DOMAIN_WORKOUT], default=DOMAIN_WORKOUT)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and validate embeddings without writing semantic chunks.",
    )
    args = parser.parse_args()

    if args.domain == DOMAIN_WORKOUT:
        print(json.dumps(sync_workout_embeddings(dry_run=args.dry_run), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
