"""Import Garmin Connect's local SQLite export into Groundhog DuckDB.

The Garmin extractor is deliberately kept separate from Groundhog.  This
module is the only bridge between the two databases and is safe to re-run:
the Garmin activity id is the stable identity and all child rows are replaced
inside the same transaction for that activity.
"""

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import DB_PATH
from ingestion.schema import init_db


ACTIVITY_COLUMNS = (
    "id, date, activity_type, distance_miles, duration_seconds, "
    "avg_pace_seconds_per_mile, avg_hr, max_hr, calories, activity_source, "
    "garmin_activity_id, garmin_user_id, activity_name, activity_type_key, "
    "event_type_key, start_at, end_at, timezone_offset_hours, "
    "elapsed_duration_seconds, moving_duration_seconds, distance_meters, "
    "lap_count, average_speed_mps, max_speed_mps, start_latitude, "
    "start_longitude, end_latitude, end_longitude, location_name, "
    "aerobic_training_effect, aerobic_training_effect_message, "
    "anaerobic_training_effect, anaerobic_training_effect_message, "
    "training_effect_label, activity_training_load, difference_body_battery, "
    "moderate_intensity_minutes, vigorous_intensity_minutes, bmr_calories, "
    "water_estimated, hr_time_in_zone_1, hr_time_in_zone_2, hr_time_in_zone_3, "
    "hr_time_in_zone_4, hr_time_in_zone_5, device_id, device_manufacturer, "
    "garmin_time_zone_id, has_polyline, has_images, has_video, has_splits, "
    "has_heat_map, is_parent_activity, purposeful, favorite, elevation_corrected, "
    "atp_activity, manual_activity, personal_record, auto_calc_calories, "
    "ts_data_available, parent_garmin_activity_id"
)

_ACTIVITY_UPDATE_COLUMNS = [
    column.strip()
    for column in ACTIVITY_COLUMNS.split(",")
    if column.strip() not in {"id", "date", "activity_source", "garmin_activity_id"}
]


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [table]
    ).fetchone() is not None


def _rows(con: sqlite3.Connection, table: str, where: str = "", params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    if not _table_exists(con, table):
        return []
    query = f"SELECT * FROM {table}{where}"
    cursor = con.execute(query, list(params))
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _iter_rows(
    con: sqlite3.Connection,
    table: str,
    where: str = "",
    params: Iterable[Any] = (),
    batch_size: int = 5000,
):
    """Stream a source table so FIT time-series data never fills memory."""
    if not _table_exists(con, table):
        return
    cursor = con.execute(f"SELECT * FROM {table}{where}", list(params))
    columns = [description[0] for description in cursor.description]
    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            return
        for row in batch:
            yield dict(zip(columns, row))


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _activity_date(start_at: datetime | None, offset_hours: Any) -> date | None:
    if start_at is None:
        return None
    try:
        offset = float(offset_hours) if offset_hours is not None else 0.0
    except (TypeError, ValueError):
        offset = 0.0
    # Garmin stores start_ts as UTC plus a separate local timezone offset.
    return (start_at + timedelta(hours=offset)).date()


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _float(value)
    return int(round(number)) if number is not None else None


def _bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _metric_value(value: Any) -> tuple[float | None, str | None]:
    number = _float(value)
    return (number, None) if number is not None else (None, None if value is None else str(value))


def _activity_type(row: dict[str, Any]) -> str:
    key = str(row.get("activity_type_key") or "other").lower()
    mapping = {
        "running": "running",
        "trail_running": "running",
        "treadmill_running": "running",
        "walking": "walking",
        "hiking": "walking",
        "cycling": "cycling",
        "road_biking": "cycling",
        "indoor_cycling": "cycling",
        "lap_swimming": "pool swim",
        "pool_swimming": "pool swim",
        "open_water_swimming": "open water swim",
        "strength_training": "strength training",
        "cardio": "cardio",
    }
    return mapping.get(key, key.replace("_", " "))


def _scalar_activity_values(row: dict[str, Any]) -> list[Any]:
    start_at = _parse_datetime(row.get("start_ts"))
    end_at = _parse_datetime(row.get("end_ts"))
    offset = _float(row.get("timezone_offset_hours"))
    distance_meters = _float(row.get("distance"))
    duration = _float(row.get("duration"))
    moving_duration = _float(row.get("moving_duration"))
    activity_type = _activity_type(row)
    pace = None
    if distance_meters and distance_meters > 0 and activity_type in {"running", "walking"}:
        pace = int(round((moving_duration or duration or 0.0) / (distance_meters / 1609.344)))

    source_id = _int(row.get("activity_id"))
    if source_id is None or start_at is None:
        raise ValueError("Garmin activity requires activity_id and start_ts")
    return [
        f"garmin:{source_id}",
        _activity_date(start_at, offset),
        activity_type,
        distance_meters / 1609.344 if distance_meters is not None else None,
        _int(duration),
        pace,
        _int(row.get("average_hr")),
        _int(row.get("max_hr")),
        _int(row.get("calories")),
        "garmin",
        source_id,
        _int(row.get("user_id")),
        row.get("activity_name"),
        row.get("activity_type_key"),
        row.get("event_type_key"),
        start_at,
        end_at,
        offset,
        duration,
        moving_duration,
        distance_meters,
        _int(row.get("lap_count")),
        _float(row.get("average_speed")),
        _float(row.get("max_speed")),
        _float(row.get("start_latitude")),
        _float(row.get("start_longitude")),
        _float(row.get("end_latitude")),
        _float(row.get("end_longitude")),
        row.get("location_name"),
        _float(row.get("aerobic_training_effect")),
        row.get("aerobic_training_effect_message"),
        _float(row.get("anaerobic_training_effect")),
        row.get("anaerobic_training_effect_message"),
        row.get("training_effect_label"),
        _float(row.get("activity_training_load")),
        _int(row.get("difference_body_battery")),
        _int(row.get("moderate_intensity_minutes")),
        _int(row.get("vigorous_intensity_minutes")),
        _float(row.get("bmr_calories")),
        _float(row.get("water_estimated")),
        _float(row.get("hr_time_in_zone_1")),
        _float(row.get("hr_time_in_zone_2")),
        _float(row.get("hr_time_in_zone_3")),
        _float(row.get("hr_time_in_zone_4")),
        _float(row.get("hr_time_in_zone_5")),
        _int(row.get("device_id")),
        row.get("manufacturer"),
        _int(row.get("time_zone_id")),
        _bool(row.get("has_polyline")),
        _bool(row.get("has_images")),
        _bool(row.get("has_video")),
        _bool(row.get("has_splits")),
        _bool(row.get("has_heat_map")),
        _bool(row.get("parent")),
        _bool(row.get("purposeful")),
        _bool(row.get("favorite")),
        _bool(row.get("elevation_corrected")),
        _bool(row.get("atp_activity")),
        _bool(row.get("manual_activity")),
        _bool(row.get("pr")),
        _bool(row.get("auto_calc_calories")),
        _bool(row.get("ts_data_available")),
        _int(row.get("parent_activity_id")),
    ]


def _delete_children(con: duckdb.DuckDBPyConnection, activity_id: str) -> None:
    for table in (
        "garmin_activity_metrics",
        "garmin_activity_lap_metrics",
        "garmin_activity_split_metrics",
        "garmin_activity_samples",
        "garmin_activity_paths",
        "garmin_swimming_aggregates",
        "garmin_strength_exercises",
        "garmin_strength_sets",
    ):
        con.execute(f"DELETE FROM {table} WHERE activity_id = ?", [activity_id])


def _insert_generic_metrics(
    con: duckdb.DuckDBPyConnection,
    source: sqlite3.Connection,
    source_activity_id: int,
    target_activity_id: str,
) -> int:
    count = 0
    pending: list[list[Any]] = []
    rows = _iter_rows(source, "supplemental_activity_metric", " WHERE activity_id = ?", [source_activity_id])
    for row in rows:
        value, text_value = _metric_value(row.get("value"))
        pending.append([target_activity_id, "supplemental", row.get("metric") or "unknown", value, text_value, None])
        count += 1

    for table, group in (("running_agg_metrics", "running"), ("cycling_agg_metrics", "cycling")):
        for row in _iter_rows(source, table, " WHERE activity_id = ?", [source_activity_id]):
            for name, raw_value in row.items():
                if name in {"activity_id", "create_ts", "update_ts"} or raw_value is None:
                    continue
                value, text_value = _metric_value(raw_value)
                pending.append([target_activity_id, group, name, value, text_value, None])
                count += 1
        if len(pending) >= 5000:
            con.executemany(
                "INSERT INTO garmin_activity_metrics VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET value=excluded.value, text_value=excluded.text_value",
                pending,
            )
            pending.clear()
    if pending:
        con.executemany(
            "INSERT INTO garmin_activity_metrics VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET value=excluded.value, text_value=excluded.text_value",
            pending,
        )
    return count


def _import_activity_children(
    con: duckdb.DuckDBPyConnection,
    source: sqlite3.Connection,
    source_row: dict[str, Any],
) -> dict[str, int]:
    source_id = _int(source_row.get("activity_id"))
    target_id = f"garmin:{source_id}"
    _delete_children(con, target_id)
    counts = {"metrics": 0, "laps": 0, "splits": 0, "samples": 0, "paths": 0, "strength_sets": 0}
    counts["metrics"] = _insert_generic_metrics(con, source, source_id, target_id)

    pending_laps: list[list[Any]] = []
    for row in _iter_rows(source, "activity_lap_metric", " WHERE activity_id = ?", [source_id]):
        pending_laps.append([target_id, _int(row.get("lap_idx")) or 0, row.get("name") or "unknown", _float(row.get("value")), row.get("units")])
        counts["laps"] += 1
    if pending_laps:
        con.executemany(
            "INSERT INTO garmin_activity_lap_metrics VALUES (?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET value=excluded.value, units=excluded.units",
            pending_laps,
        )

    pending_splits: list[list[Any]] = []
    for row in _iter_rows(source, "activity_split_metric", " WHERE activity_id = ?", [source_id]):
        pending_splits.append([target_id, _int(row.get("split_idx")) or 0, row.get("split_type"), row.get("name") or "unknown", _float(row.get("value")), row.get("units")])
        counts["splits"] += 1
    if pending_splits:
        con.executemany(
            "INSERT INTO garmin_activity_split_metrics VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET split_type=excluded.split_type, value=excluded.value, units=excluded.units",
            pending_splits,
        )

    pending_samples: list[list[Any]] = []
    for row in _iter_rows(source, "activity_ts_metric", " WHERE activity_id = ?", [source_id]):
        timestamp = _parse_datetime(row.get("timestamp"))
        if timestamp is None:
            continue
        pending_samples.append([target_id, timestamp, row.get("name") or "unknown", _float(row.get("value")), row.get("units")])
        counts["samples"] += 1
        if len(pending_samples) >= 5000:
            con.executemany(
                "INSERT INTO garmin_activity_samples VALUES (?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET value=excluded.value, units=excluded.units",
                pending_samples,
            )
            pending_samples.clear()
    if pending_samples:
        con.executemany(
            "INSERT INTO garmin_activity_samples VALUES (?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET value=excluded.value, units=excluded.units",
            pending_samples,
        )

    for row in _rows(source, "activity_path", " WHERE activity_id = ?", [source_id]):
        raw_path = row.get("path_json")
        try:
            path_json = json.dumps(json.loads(raw_path) if isinstance(raw_path, str) else raw_path)
        except (TypeError, json.JSONDecodeError):
            continue
        con.execute(
            "INSERT INTO garmin_activity_paths VALUES (?, ?, ?) ON CONFLICT DO UPDATE SET path_json=excluded.path_json, point_count=excluded.point_count",
            [target_id, path_json, _int(row.get("point_count")) or 0],
        )
        counts["paths"] += 1

    for row in _rows(source, "swimming_agg_metrics", " WHERE activity_id = ?", [source_id]):
        con.execute(
            "INSERT INTO garmin_swimming_aggregates VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET pool_length_cm=excluded.pool_length_cm, active_lengths=excluded.active_lengths, strokes=excluded.strokes, avg_stroke_distance_m=excluded.avg_stroke_distance_m, avg_strokes=excluded.avg_strokes, avg_swim_cadence=excluded.avg_swim_cadence, avg_swolf=excluded.avg_swolf",
            [target_id, _float(row.get("pool_length")), _int(row.get("active_lengths")), _float(row.get("strokes")), _float(row.get("avg_stroke_distance")), _float(row.get("avg_strokes")), _float(row.get("avg_swim_cadence")), _float(row.get("avg_swolf"))],
        )
        if source_row.get("distance") is not None:
            con.execute(
                "UPDATE activities SET pool_distance = COALESCE(pool_distance, distance_meters), pool_distance_unit = COALESCE(pool_distance_unit, 'meters'), laps = COALESCE(laps, ?), swim_pace_seconds_per_100 = COALESCE(swim_pace_seconds_per_100, ?), swim_pace_unit = COALESCE(swim_pace_unit, 'meters') WHERE id = ?",
                [_int(row.get("active_lengths")), _int((_float(source_row.get("duration")) or 0) / ((_float(source_row.get("distance")) or 1) / 100)), target_id],
            )

    pending_exercises: list[list[Any]] = []
    for row in _rows(source, "strength_exercise", " WHERE activity_id = ?", [source_id]):
        pending_exercises.append([target_id, row.get("exercise_category") or "", row.get("exercise_name") or "", _int(row.get("sets")), _int(row.get("reps")), _float(row.get("volume")), _float(row.get("duration_ms")), _float(row.get("max_weight"))])
    if pending_exercises:
        con.executemany(
            "INSERT INTO garmin_strength_exercises VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET sets=excluded.sets, reps=excluded.reps, volume=excluded.volume, duration_ms=excluded.duration_ms, max_weight=excluded.max_weight",
            pending_exercises,
        )
    pending_sets: list[list[Any]] = []
    for row in _rows(source, "strength_set", " WHERE activity_id = ?", [source_id]):
        pending_sets.append([target_id, _int(row.get("set_idx")) or 0, row.get("set_type"), _parse_datetime(row.get("start_time")), _float(row.get("duration")), _int(row.get("wkt_step_index")), _int(row.get("repetition_count")), _float(row.get("weight")), row.get("exercise_category"), row.get("exercise_name"), _float(row.get("exercise_probability"))])
        counts["strength_sets"] += 1
    if pending_sets:
        con.executemany(
            "INSERT INTO garmin_strength_sets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO UPDATE SET set_type=excluded.set_type, start_time=excluded.start_time, duration_seconds=excluded.duration_seconds, wkt_step_index=excluded.wkt_step_index, repetition_count=excluded.repetition_count, weight=excluded.weight, exercise_category=excluded.exercise_category, exercise_name=excluded.exercise_name, exercise_probability=excluded.exercise_probability",
            pending_sets,
        )
    return counts


def import_garmin(source_db: Path | str, groundhog_db: Path | str | None = None, start_date: date | None = None, end_date: date | None = None, only_missing: bool = False) -> dict[str, int]:
    """Import activities in an optional local-date range and return row counts."""
    source_path = Path(source_db)
    if not source_path.exists():
        raise FileNotFoundError(f"Garmin source database not found: {source_path}")
    target_path = Path(groundhog_db or DB_PATH)
    init_db(target_path)
    source = sqlite3.connect(str(source_path))
    source.row_factory = sqlite3.Row
    target = duckdb.connect(str(target_path))
    totals = {"activities_seen": 0, "activities_imported": 0, "metrics": 0, "laps": 0, "splits": 0, "samples": 0, "paths": 0, "strength_sets": 0}
    try:
        activity_rows = _rows(source, "activity")
        for row in activity_rows:
            totals["activities_seen"] += 1
            values = _scalar_activity_values(row)
            row_date = values[1]
            if start_date and (row_date is None or row_date < start_date):
                continue
            if end_date and (row_date is None or row_date >= end_date):
                continue
            target_id = values[0]
            if only_missing and target.execute("SELECT 1 FROM activities WHERE id = ?", [target_id]).fetchone():
                continue
            placeholders = ", ".join("?" for _ in values)
            # Keep the parent upsert and each child-table batch in separate
            # transactions.  A single activity can contain hundreds of
            # thousands of samples; one giant transaction causes DuckDB to
            # retain all index state in memory until the final commit.
            target.execute(
                f"INSERT INTO activities ({ACTIVITY_COLUMNS}) VALUES ({placeholders}) ON CONFLICT (id) DO UPDATE SET "
                + ", ".join(f"{column}=COALESCE(excluded.{column}, activities.{column})" for column in _ACTIVITY_UPDATE_COLUMNS),
                values,
            )
            child_counts = _import_activity_children(target, source, row)
            for key, value in child_counts.items():
                totals[key] += value
            totals["activities_imported"] += 1
    finally:
        target.close()
        source.close()
    return totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import Garmin SQLite data into Groundhog DuckDB.")
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--groundhog-db", type=Path, default=None)
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat, help="Exclusive end date")
    parser.add_argument("--only-missing", action="store_true", help="Skip Garmin activities already present in Groundhog")
    args = parser.parse_args(argv)
    print(json.dumps(import_garmin(args.source_db, args.groundhog_db, args.start_date, args.end_date, args.only_missing), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
