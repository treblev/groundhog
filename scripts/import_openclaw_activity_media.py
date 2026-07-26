"""Import newly received OpenClaw image attachments into Groundhog activities."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import hashlib
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import duckdb

from agent.events import event_id_for, record_event
from agent.outbox import enqueue_event
from config.settings import DB_PATH, OPENCLAW_MEDIA_INBOUND_DIR, OPENCLAW_MEDIA_STATE_PATH
from ingestion.health import IMAGE_EXTS, process_image
from ingestion.sleep import process_image as process_sleep
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
    if kind not in {"activity", "plan", "sleep"}:
        raise ValueError(f"Unsupported upload type: {kind}")
    state = _load_state(state_path)
    state[NEXT_KIND_KEY] = {"kind": kind}
    _write_state(state_path, state)


def _next_kind(state: dict[str, dict]) -> str:
    next_kind = state.pop(NEXT_KIND_KEY, {}).get("kind", "activity")
    return next_kind if next_kind in {"activity", "plan", "sleep"} else "activity"


def _date_hint_from_caption(caption: str | None) -> str | None:
    """Return a valid date token from an attachment caption, if one is present."""
    if not caption:
        return None
    import re

    tokens = re.findall(r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2})(?!\d)", caption)
    for token in tokens:
        try:
            date.fromisoformat(token)
            return token
        except ValueError:
            try:
                month, day = (int(part) for part in re.split(r"[/-]", token))
                date(2000, month, day)
                return token
            except ValueError:
                continue
    return None


def _format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "not detected"
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def _format_pace(seconds: int | None, unit: str) -> str:
    if seconds is None:
        return "not detected"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}:{seconds:02d}/{unit}"


def _confirmation_message(kind: str, records, screenshot_date: date | None = None) -> str:
    """Build a concise, reviewable summary for one successfully imported image."""
    if kind == "plan":
        date_text = screenshot_date.isoformat() if screenshot_date else "unknown date"
        return f"Imported workout plan: {records} plan(s) for {date_text}."

    if kind == "sleep":
        return (
            f"Imported sleep data for {records.get('date', 'unknown date')}: "
            f"resting HR {records.get('resting_hr', 'not detected')} bpm; "
            f"HRV {records.get('hrv', 'not detected')}."
        )

    activities = records if isinstance(records, list) else []
    summaries = []
    for activity in activities:
        activity_type = activity.get("activity_type", "other")
        date_text = activity.get("date", "unknown date")
        if activity_type == "pool swim":
            distance = activity.get("pool_distance")
            distance_unit = activity.get("pool_distance_unit") or "units"
            pace = _format_pace(activity.get("swim_pace_seconds_per_100"), f"100 {activity.get('swim_pace_unit') or 'units'}")
            distance_text = f"{distance} {distance_unit}" if distance is not None else "not detected"
        else:
            distance = activity.get("distance_miles")
            distance_text = f"{distance} mi" if distance is not None else "not detected"
            pace = _format_pace(activity.get("avg_pace_seconds_per_mile"), "mi")
        summaries.append(
            f"Imported activity: {activity_type} ({date_text}). Distance: {distance_text}; "
            f"duration: {_format_duration(activity.get('duration_seconds'))}; "
            f"avg pace: {pace}; avg HR: {activity.get('avg_hr', 'not detected')} bpm."
        )
    return "\n".join(summaries) or "Imported activity screenshot, but no activity details were returned."


def _enqueue_confirmation(kind: str, upload_id: str, records, screenshot_date: date | None = None) -> None:
    """Queue exactly one Telegram confirmation per successfully imported image."""
    dedupe_key = f"upload_confirmation:{upload_id}"
    message = _confirmation_message(kind, records, screenshot_date)
    con = duckdb.connect(str(DB_PATH))
    try:
        record_event(
            con,
            event_type="upload_imported",
            source="scripts.import_openclaw_activity_media",
            subject_type="media_upload",
            subject_id=upload_id,
            payload={"kind": kind, "message": message},
            dedupe_key=dedupe_key,
        )
        enqueue_event(con, event_id_for(dedupe_key))
    finally:
        con.close()


def import_captioned_activity(image_path: Path, caption: str | None, state_path: Path) -> list[dict]:
    """Import one Telegram activity attachment using its caption as optional metadata."""
    upload_id = _file_id(image_path)
    state = _load_state(state_path)
    if upload_id in state:
        return []

    reference_date = datetime.fromtimestamp(image_path.stat().st_mtime, PHOENIX).date()
    date_hint = _date_hint_from_caption(caption)
    records = process_image(image_path, reference_date, date_hint)
    _enqueue_confirmation("activity", upload_id, records)
    state[upload_id] = {
        "status": "imported",
        "kind": "activity",
        "path": str(image_path),
        "records": len(records),
        **({"caption_date_hint": date_hint} if date_hint else {}),
    }
    _write_state(state_path, state)
    return records


def run(
    inbound_dir: Path | None,
    state_path: Path,
    initialize: bool = False,
    force_kind: str | None = None,
) -> int:
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
        kind = force_kind or _next_kind(state)
        try:
            screenshot_date = None
            if kind == "plan":
                screenshot_date = datetime.fromtimestamp(image_path.stat().st_mtime, PHOENIX).date()
                records = process_workout_plan(image_path, screenshot_date)
                record_count = records
            elif kind == "sleep":
                screenshot_date = datetime.fromtimestamp(image_path.stat().st_mtime, PHOENIX).date()
                records = process_sleep(image_path, screenshot_date)
                record_count = 1
            else:
                records = process_image(image_path)
                record_count = len(records)
            _enqueue_confirmation(kind, image_id, records, screenshot_date)
        except Exception as error:
            state[image_id] = {
                "status": "failed", "kind": kind, "path": str(image_path), "error": str(error)
            }
            print(f"Failed {image_path.name}: {error}")
        else:
            state[image_id] = {
                "status": "imported", "kind": kind, "path": str(image_path), "records": record_count,
            }
            imported += record_count
            print(f"Imported {record_count} {kind} record(s) from {image_path.name}.")
        _write_state(state_path, state)
    return imported


def main() -> None:
    parser = argparse.ArgumentParser(description="Import new OpenClaw activity screenshots.")
    parser.add_argument("--initialize", action="store_true", help="Record current images without importing them.")
    parser.add_argument("--next-kind", choices=["activity", "plan", "sleep"], help="Use this type for exactly the next image.")
    parser.add_argument("--image", type=Path, help="A direct Telegram activity attachment path.")
    parser.add_argument("--caption", help="Caption text supplied with --image.")
    parser.add_argument(
        "--all-new-kind",
        choices=["activity", "plan", "sleep"],
        help="Use this type for every image currently waiting to be imported.",
    )
    parser.add_argument("--inbound-dir", type=Path, default=OPENCLAW_MEDIA_INBOUND_DIR)
    parser.add_argument("--state-path", type=Path, default=OPENCLAW_MEDIA_STATE_PATH)
    args = parser.parse_args()
    if args.image:
        records = import_captioned_activity(args.image, args.caption, args.state_path)
        print(json.dumps(records, sort_keys=True))
        return
    if args.next_kind:
        set_next_kind(args.state_path, args.next_kind)
        print(f"Next OpenClaw image will be imported as a {args.next_kind}.")
        return
    run(args.inbound_dir, args.state_path, args.initialize, args.all_new_kind)


if __name__ == "__main__":
    main()
