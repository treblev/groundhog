"""Export imported Telegram workout-plan screenshots for local evaluation review."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo

from scripts.import_openclaw_activity_media import _load_state

PHOENIX = ZoneInfo("America/Phoenix")


def export(state_path: Path, output_dir: Path) -> int:
    """Copy imported plan images once and register them as review-pending examples."""
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    existing = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"examples": []}
    examples = existing.setdefault("examples", [])
    known = {example["upload_id"] for example in examples}

    added = 0
    for upload_id, record in _load_state(state_path).items():
        if record.get("status") != "imported" or record.get("kind") != "plan" or upload_id in known:
            continue
        source = Path(record["path"])
        if not source.is_file():
            continue
        destination = output_dir / f"{upload_id}{source.suffix.lower()}"
        if not destination.exists():
            shutil.copy2(source, destination)
        examples.append(
            {
                "upload_id": upload_id,
                "image": destination.name,
                "screenshot_date": datetime.fromtimestamp(source.stat().st_mtime, PHOENIX).date().isoformat(),
                "label_status": "pending_review",
                "expected_workout": None,
            }
        )
        known.add(upload_id)
        added += 1

    manifest_path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n")
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-path", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(f"Exported {export(args.state_path, args.output_dir)} workout-plan evaluation candidate(s).")


if __name__ == "__main__":
    main()
