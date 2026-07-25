"""Import newly received OpenClaw image attachments into Groundhog activities."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import hashlib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from config.settings import OPENCLAW_MEDIA_INBOUND_DIR, OPENCLAW_MEDIA_STATE_PATH
from ingestion.health import IMAGE_EXTS, process_image
from ingestion.workouts import process_image as process_workout_plan

PHOENIX = ZoneInfo("America/Phoenix")
NEXT_KIND_KEY = "_next_kind"


def _file_id(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_state(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return state if isinstance(state, dict) else {}


def _write_state(path: Path, state: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def _images(inbound_dir: Path) -> list[Path]:
    if not inbound_dir.is_dir():
        return []
    return sorted(
        path for path in inbound_dir.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_EXTS
    )


def set_next_kind(state_path: Path, kind: str) -> None:
    if kind not in {"activity", "plan"}:
        raise ValueError(f"Unsupported upload type: {kind}")
    state = _load_state(state_path)
    state[NEXT_KIND_KEY] = {"kind": kind}
    _write_state(state_path, state)


def _next_kind(state: dict[str, dict]) -> str:
    next_kind = state.pop(NEXT_KIND_KEY, {}).get("kind", "activity")
    return next_kind if next_kind in {"activity", "plan"} else "activity"


def run(inbound_dir: Path | None, state_path: Path, initialize: bool = False) -> int:
    """Process each attachment once; initialization records current files without importing them."""
    if not inbound_dir:
        raise ValueError("GROUNDHOG_OPENCLAW_MEDIA_INBOUND_DIR must be configured.")

    state = _load_state(state_path)
    images = _images(inbound_dir)
    if initialize and not state_path.exists():
        for image_path in images:
            state[_file_id(image_path)] = {"status": "ignored_existing", "path": str(image_path)}
        _write_state(state_path, state)
        print(f"Initialized watcher; ignored {len(images)} existing image(s).")
        return 0

    imported = 0
    for image_path in images:
        image_id = _file_id(image_path)
        if image_id in state:
            continue
        kind = _next_kind(state)
        try:
            if kind == "plan":
                screenshot_date = datetime.fromtimestamp(image_path.stat().st_mtime, PHOENIX).date()
                records = process_workout_plan(image_path, screenshot_date)
                record_count = records
            else:
                records = process_image(image_path)
                record_count = len(records)
        except Exception as error:
            state[image_id] = {
                "status": "failed", "kind": kind, "path": str(image_path), "error": str(error)
            }
            print(f"Failed {image_path.name}: {error}")
        else:
            state[image_id] = {
                "status": "imported", "kind": kind, "path": str(image_path), "records": record_count
            }
            imported += record_count
            print(f"Imported {record_count} {kind} record(s) from {image_path.name}.")
        _write_state(state_path, state)
    return imported


def main() -> None:
    parser = argparse.ArgumentParser(description="Import new OpenClaw activity screenshots.")
    parser.add_argument("--initialize", action="store_true", help="Record current images without importing them.")
    parser.add_argument("--next-kind", choices=["activity", "plan"], help="Use this type for exactly the next image.")
    parser.add_argument("--inbound-dir", type=Path, default=OPENCLAW_MEDIA_INBOUND_DIR)
    parser.add_argument("--state-path", type=Path, default=OPENCLAW_MEDIA_STATE_PATH)
    args = parser.parse_args()
    if args.next_kind:
        set_next_kind(args.state_path, args.next_kind)
        print(f"Next OpenClaw image will be imported as a {args.next_kind}.")
        return
    run(args.inbound_dir, args.state_path, args.initialize)


if __name__ == "__main__":
    main()
