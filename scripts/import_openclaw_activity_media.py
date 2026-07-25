"""Import newly received OpenClaw image attachments into Groundhog activities."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import hashlib
import json

from config.settings import OPENCLAW_MEDIA_INBOUND_DIR, OPENCLAW_MEDIA_STATE_PATH
from ingestion.health import IMAGE_EXTS, process_image


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
        try:
            records = process_image(image_path)
        except Exception as error:
            state[image_id] = {"status": "failed", "path": str(image_path), "error": str(error)}
            print(f"Failed {image_path.name}: {error}")
        else:
            state[image_id] = {"status": "imported", "path": str(image_path), "records": len(records)}
            imported += len(records)
            print(f"Imported {len(records)} activity record(s) from {image_path.name}.")
        _write_state(state_path, state)
    return imported


def main() -> None:
    parser = argparse.ArgumentParser(description="Import new OpenClaw activity screenshots.")
    parser.add_argument("--initialize", action="store_true", help="Record current images without importing them.")
    parser.add_argument("--inbound-dir", type=Path, default=OPENCLAW_MEDIA_INBOUND_DIR)
    parser.add_argument("--state-path", type=Path, default=OPENCLAW_MEDIA_STATE_PATH)
    args = parser.parse_args()
    run(args.inbound_dir, args.state_path, args.initialize)


if __name__ == "__main__":
    main()
