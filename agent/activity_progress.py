"""Deterministic Garmin activity progress facts and locally generated notes."""
from __future__ import annotations

import hashlib
import json
import re
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Callable

import duckdb

from agent.summaries import _ask_local_model
from config.settings import OLLAMA_SQL_MODEL


PERIOD_KINDS = {"weekly", "monthly"}
STAIRS_PER_MINUTE = 32
ROLLUP_KEY = "all"
_NUMBER_RE = re.compile(r"(?<![A-Za-z])\d+(?:[.,:]\d+)*(?![A-Za-z])")


def _month_end(value: date) -> date:
    return value.replace(day=monthrange(value.year, value.month)[1])


def _shift_month(value: date, months: int) -> date:
    index = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(index, 12)
    month = month_index + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def period_bounds(
    period_kind: str,
    period_end: str | date | None = None,
    *,
    current: bool = False,
    today: date | None = None,
) -> tuple[date, date]:
    """Resolve one completed period, or the current partial period on request."""
    if period_kind not in PERIOD_KINDS:
        raise ValueError("period_kind must be 'weekly' or 'monthly'")
    if current and period_end is not None:
        raise ValueError("period_end cannot be supplied with current=true")
    reference = today or date.today()
    if isinstance(period_end, str):
        try:
            period_end = date.fromisoformat(period_end)
        except ValueError as error:
            raise ValueError("period_end must be a valid YYYY-MM-DD date") from error

    if current:
        if period_kind == "weekly":
            start = reference - timedelta(days=(reference.weekday() + 1) % 7)
        else:
            start = reference.replace(day=1)
        return start, reference

    if period_end is None:
        if period_kind == "weekly":
            days_since_saturday = (reference.weekday() - 5) % 7 or 7
            period_end = reference - timedelta(days=days_since_saturday)
        else:
            period_end = reference.replace(day=1) - timedelta(days=1)

    if period_kind == "weekly":
        if period_end.weekday() != 5:
            raise ValueError("weekly period_end must be a Saturday")
        return period_end - timedelta(days=6), period_end
    if period_end != _month_end(period_end):
        raise ValueError("monthly period_end must be the last day of a month")
    return period_end.replace(day=1), period_end


def comparison_bounds(period_kind: str, start: date) -> dict[str, tuple[date, date, int]]:
    """Return the immediately previous period and rolling baseline bounds."""
    previous_end = start - timedelta(days=1)
    if period_kind == "weekly":
        return {
            "previous": (start - timedelta(days=7), previous_end, 1),
            "baseline": (start - timedelta(days=28), previous_end, 4),
        }
    previous_start = _shift_month(start, -1)
    baseline_start = _shift_month(start, -3)
    return {
        "previous": (previous_start, previous_end, 1),
        "baseline": (baseline_start, previous_end, 3),
    }


def _rows(con: duckdb.DuckDBPyConnection, start: date, end: date) -> list[dict[str, Any]]:
    cursor = con.execute(
        """
        SELECT id, date, activity_type_key, duration_seconds,
               NULLIF(moving_duration_seconds, 0) AS moving_duration_seconds,
               distance_meters, avg_hr, max_hr, calories,
               aerobic_training_effect, anaerobic_training_effect,
               activity_training_load, hr_time_in_zone_1, hr_time_in_zone_2,
               hr_time_in_zone_3, hr_time_in_zone_4, hr_time_in_zone_5,
               pool_distance_unit
        FROM activities
        WHERE activity_source = 'garmin'
          AND activity_type_key IS NOT NULL
          AND date BETWEEN ? AND ?
        ORDER BY date, id
        """,
        [start, end],
    )
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _metric_values(
    con: duckdb.DuckDBPyConnection, activity_ids: list[str]
) -> dict[str, dict[str, float]]:
    if not activity_ids:
        return {}
    result: dict[str, dict[str, float]] = defaultdict(dict)
    for activity_id, name, value in con.execute(
        """
        SELECT activity_id, metric_name, value
        FROM garmin_activity_metrics
        WHERE activity_id IN (SELECT UNNEST(?)) AND value IS NOT NULL
        """,
        [activity_ids],
    ).fetchall():
        result[activity_id][name] = float(value)
    return result


def _swim_details(
    con: duckdb.DuckDBPyConnection, activity_ids: list[str]
) -> tuple[dict[str, dict[int, dict[str, Any]]], dict[str, dict[str, Any]]]:
    laps: dict[str, dict[int, dict[str, Any]]] = defaultdict(lambda: defaultdict(dict))
    if activity_ids:
        for activity_id, lap_index, name, value, units in con.execute(
            """
            SELECT activity_id, lap_index, metric_name, value, units
            FROM garmin_activity_lap_metrics
            WHERE activity_id IN (SELECT UNNEST(?))
            """,
            [activity_ids],
        ).fetchall():
            laps[activity_id][int(lap_index)][name] = value
            if name == "total_distance":
                laps[activity_id][int(lap_index)]["distance_units"] = units

    aggregates: dict[str, dict[str, Any]] = {}
    if activity_ids:
        cursor = con.execute(
            """
            SELECT activity_id, pool_length_cm, active_lengths, strokes,
                   avg_stroke_distance_m, avg_strokes, avg_swim_cadence, avg_swolf
            FROM garmin_swimming_aggregates
            WHERE activity_id IN (SELECT UNNEST(?))
            """,
            [activity_ids],
        )
        columns = [column[0] for column in cursor.description]
        aggregates = {row[0]: dict(zip(columns, row)) for row in cursor.fetchall()}
    return laps, aggregates


def _is_forgotten_swim_length(
    distance: float | None,
    seconds: float | None,
    units: str | None,
    pool_length_cm: float | None,
) -> bool:
    if distance is None or seconds is None or seconds >= 5:
        return False
    normalized_units = (units or "").lower()
    native_yards = normalized_units in {"yd", "yard", "yards"} and 24.5 <= distance <= 25.5
    normalized_meters = (
        22.4 <= distance <= 23.3
        and pool_length_cm is not None
        and 2240 <= pool_length_cm <= 2330
    )
    return native_yards or normalized_meters


def _round(value: float | None, digits: int = 1) -> float | None:
    return round(value, digits) if value is not None else None


def _empty_metrics(activity_key: str) -> dict[str, Any]:
    return {
        "activity_key": activity_key,
        "sessions": 0,
        "duration_minutes": 0.0,
        "moving_minutes": 0.0,
        "distance_miles": 0.0,
        "distance_meters": 0.0,
        "average_hr": None,
        "max_hr": None,
        "calories": 0,
        "training_load": 0.0,
        "aerobic_training_effect": None,
        "anaerobic_training_effect": None,
        "hr_zone_minutes": {f"zone_{zone}": 0.0 for zone in range(1, 6)},
    }


def _aggregate_kind(
    con: duckdb.DuckDBPyConnection,
    activity_key: str,
    rows: list[dict[str, Any]],
    generic_metrics: dict[str, dict[str, float]],
) -> dict[str, Any]:
    result = _empty_metrics(activity_key)
    if not rows:
        return result

    result["sessions"] = len(rows)
    result["duration_minutes"] = _round(sum(float(row["duration_seconds"] or 0) for row in rows) / 60)
    result["moving_minutes"] = _round(sum(float(row["moving_duration_seconds"] or 0) for row in rows) / 60)
    meters = sum(float(row["distance_meters"] or 0) for row in rows)
    result["distance_meters"] = _round(meters, 0)
    result["distance_miles"] = _round(meters / 1609.344, 2)
    weighted_seconds = sum(float(row["duration_seconds"] or 0) for row in rows if row["avg_hr"] is not None)
    result["average_hr"] = _round(
        sum(float(row["avg_hr"]) * float(row["duration_seconds"] or 0) for row in rows if row["avg_hr"] is not None)
        / weighted_seconds
        if weighted_seconds
        else None
    )
    max_values = [int(row["max_hr"]) for row in rows if row["max_hr"] is not None]
    result["max_hr"] = max(max_values) if max_values else None
    result["calories"] = int(sum(int(row["calories"] or 0) for row in rows))
    result["training_load"] = _round(sum(float(row["activity_training_load"] or 0) for row in rows))
    for field in ("aerobic_training_effect", "anaerobic_training_effect"):
        values = [float(row[field]) for row in rows if row[field] is not None]
        result[field] = _round(sum(values) / len(values) if values else None, 2)
    result["hr_zone_minutes"] = {
        f"zone_{zone}": _round(sum(float(row[f"hr_time_in_zone_{zone}"] or 0) for row in rows) / 60)
        for zone in range(1, 6)
    }

    if activity_key in {"treadmill_running", "walking"} and meters > 0:
        moving_seconds = sum(float(row["moving_duration_seconds"] or 0) for row in rows)
        result["pace_seconds_per_mile"] = _round(moving_seconds / (meters / 1609.344), 0) if moving_seconds else None
    if activity_key == "treadmill_running":
        for output, source in (
            ("average_cadence_spm", "average_running_cadence_in_steps_per_minute"),
            ("average_power_watts", "avg_power"),
        ):
            values = [generic_metrics.get(row["id"], {}).get(source) for row in rows]
            values = [value for value in values if value is not None]
            result[output] = _round(sum(values) / len(values) if values else None)
    elif activity_key == "walking":
        gains = [generic_metrics.get(row["id"], {}).get("elevation_gain") for row in rows]
        gains = [value for value in gains if value is not None]
        result["elevation_gain_meters"] = _round(sum(gains), 0) if gains else None
    elif activity_key == "stair_climbing":
        result["stairs_climbed"] = sum(
            round(float(row["duration_seconds"] or 0) * STAIRS_PER_MINUTE / 60) for row in rows
        )
        result["stairs_estimated"] = True
        result["stairs_per_minute"] = STAIRS_PER_MINUTE
        result["distance_meters"] = 0.0
        result["distance_miles"] = 0.0
        result["moving_minutes"] = 0.0
    elif activity_key == "lap_swimming":
        result.update(_aggregate_swimming(con, rows))
    elif activity_key == "strength_training":
        result.update(_aggregate_strength(con, rows))
    return result


def _aggregate_swimming(con: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [row["id"] for row in rows]
    laps, aggregates = _swim_details(con, ids)
    total_distance = 0.0
    active_seconds = 0.0
    elapsed_seconds = 0.0
    hr_numerator = 0.0
    hr_seconds = 0.0
    forgotten_lengths = 0
    excluded_activities = 0
    swolf_values = []
    cadence_values = []
    strokes = 0.0
    lengths = 0
    included_rows = []

    for row in rows:
        aggregate = aggregates.get(row["id"], {})
        pool_length = aggregate.get("pool_length_cm")
        activity_laps = laps.get(row["id"], {})
        whole_activity_forgotten = _is_forgotten_swim_length(
            float(row["distance_meters"] or 0),
            float(row["duration_seconds"] or 0),
            row.get("pool_distance_unit"),
            pool_length,
        )
        if whole_activity_forgotten:
            total_distance += float(row["distance_meters"] or 0)
            forgotten_lengths += 1
            excluded_activities += 1
            continue
        included_rows.append(row)
        if not activity_laps:
            distance = float(row["distance_meters"] or 0)
            duration = float(row["duration_seconds"] or 0)
            forgotten = _is_forgotten_swim_length(distance, duration, row.get("pool_distance_unit"), pool_length)
            total_distance += distance
            if not forgotten:
                elapsed_seconds += duration
                active_seconds += float(row["moving_duration_seconds"] or 0)
                if row["avg_hr"] is not None:
                    hr_numerator += float(row["avg_hr"]) * duration
                    hr_seconds += duration
            else:
                forgotten_lengths += 1
        else:
            activity_has_forgotten = False
            for lap in activity_laps.values():
                distance = float(lap.get("total_distance") or 0)
                timer = float(lap.get("total_timer_time") or lap.get("total_elapsed_time") or 0)
                forgotten = _is_forgotten_swim_length(
                    distance, timer, lap.get("distance_units"), pool_length
                )
                total_distance += distance
                if forgotten:
                    forgotten_lengths += 1
                    activity_has_forgotten = True
                    continue
                elapsed_seconds += timer
                if distance > 0:
                    active_seconds += timer
                if lap.get("avg_heart_rate") is not None and timer > 0:
                    hr_numerator += float(lap["avg_heart_rate"]) * timer
                    hr_seconds += timer
            if not activity_has_forgotten:
                if aggregate.get("avg_swolf") is not None:
                    swolf_values.append(float(aggregate["avg_swolf"]))
                if aggregate.get("avg_swim_cadence") is not None:
                    cadence_values.append(float(aggregate["avg_swim_cadence"]))
                strokes += float(aggregate.get("strokes") or 0)
            lengths += int(aggregate.get("active_lengths") or 0)

    max_values = [int(row["max_hr"]) for row in included_rows if row["max_hr"] is not None]
    aerobic = [float(row["aerobic_training_effect"]) for row in included_rows if row["aerobic_training_effect"] is not None]
    anaerobic = [float(row["anaerobic_training_effect"]) for row in included_rows if row["anaerobic_training_effect"] is not None]

    return {
        "distance_meters": _round(total_distance, 0),
        "distance_miles": _round(total_distance / 1609.344, 2),
        "duration_minutes": _round(elapsed_seconds / 60),
        "moving_minutes": _round(active_seconds / 60),
        "active_pace_seconds_per_100": _round(active_seconds / (total_distance / 100), 0) if total_distance else None,
        "average_hr": _round(hr_numerator / hr_seconds if hr_seconds else None),
        "max_hr": max(max_values) if max_values else None,
        "calories": int(sum(int(row["calories"] or 0) for row in included_rows)),
        "training_load": _round(sum(float(row["activity_training_load"] or 0) for row in included_rows)),
        "aerobic_training_effect": _round(sum(aerobic) / len(aerobic) if aerobic else None, 2),
        "anaerobic_training_effect": _round(sum(anaerobic) / len(anaerobic) if anaerobic else None, 2),
        "hr_zone_minutes": {
            f"zone_{zone}": _round(sum(float(row[f"hr_time_in_zone_{zone}"] or 0) for row in included_rows) / 60)
            for zone in range(1, 6)
        },
        "lengths": lengths,
        "strokes": _round(strokes, 0),
        "average_swolf": _round(sum(swolf_values) / len(swolf_values) if swolf_values else None),
        "average_swim_cadence": _round(sum(cadence_values) / len(cadence_values) if cadence_values else None),
        "forgotten_timer_lengths": forgotten_lengths,
        "forgotten_timer_activities": excluded_activities,
    }


def _aggregate_strength(con: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [row["id"] for row in rows]
    details = con.execute(
        """
        SELECT activity_id, set_index, repetition_count, weight,
               exercise_category, exercise_name
        FROM garmin_strength_sets
        WHERE activity_id IN (SELECT UNNEST(?))
        """,
        [ids],
    ).fetchall()
    complete = bool(details) and {row[0] for row in details} == set(ids) and all(
        row[2] is not None and row[3] is not None and row[4] and row[5] for row in details
    )
    if not complete:
        return {"strength_details_complete": False}
    return {
        "strength_details_complete": True,
        "sets": len(details),
        "repetitions": sum(int(row[2]) for row in details),
        "weight_total": _round(sum(float(row[3]) for row in details), 1),
    }


def aggregate_period(
    con: duckdb.DuckDBPyConnection, start: date, end: date
) -> dict[str, dict[str, Any]]:
    """Aggregate Garmin-only facts by detailed activity key."""
    rows = _rows(con, start, end)
    generic = _metric_values(con, [row["id"] for row in rows])
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["activity_type_key"]].append(row)
    return {
        key: _aggregate_kind(con, key, activity_rows, generic)
        for key, activity_rows in sorted(grouped.items())
    }


def _volume_average(metrics: dict[str, Any], periods: int) -> dict[str, Any]:
    result = dict(metrics)
    for key in (
        "sessions", "duration_minutes", "moving_minutes", "distance_miles",
        "distance_meters", "calories", "training_load", "stairs_climbed",
        "lengths", "strokes",
    ):
        if key in result and result[key] is not None:
            result[key] = _round(float(result[key]) / periods, 2)
    return result


def _percent_change(current: float | int | None, previous: float | int | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return _round((float(current) - float(previous)) * 100 / float(previous), 1)


def _comparison(target: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    return {
        "sessions_percent": _percent_change(target.get("sessions"), reference.get("sessions")),
        "duration_percent": _percent_change(target.get("duration_minutes"), reference.get("duration_minutes")),
        "distance_percent": _percent_change(target.get("distance_miles"), reference.get("distance_miles")),
        "training_load_percent": _percent_change(target.get("training_load"), reference.get("training_load")),
        "average_hr_delta": _round(target["average_hr"] - reference["average_hr"])
        if target.get("average_hr") is not None and reference.get("average_hr") is not None else None,
        "pace_seconds_per_mile_delta": _round(target["pace_seconds_per_mile"] - reference["pace_seconds_per_mile"], 0)
        if target.get("pace_seconds_per_mile") is not None and reference.get("pace_seconds_per_mile") is not None else None,
        "active_pace_seconds_per_100_delta": _round(target["active_pace_seconds_per_100"] - reference["active_pace_seconds_per_100"], 0)
        if target.get("active_pace_seconds_per_100") is not None and reference.get("active_pace_seconds_per_100") is not None else None,
        "swolf_delta": _round(target["average_swolf"] - reference["average_swolf"])
        if target.get("average_swolf") is not None and reference.get("average_swolf") is not None else None,
    }


def activity_progress_facts(
    con: duckdb.DuckDBPyConnection,
    period_kind: str,
    period_end: str | date | None = None,
    *,
    current: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    """Build deterministic target, prior, and rolling-baseline facts."""
    start, end = period_bounds(period_kind, period_end, current=current, today=today)
    bounds = comparison_bounds(period_kind, start)
    target = aggregate_period(con, start, end)
    previous = aggregate_period(con, bounds["previous"][0], bounds["previous"][1])
    baseline_raw = aggregate_period(con, bounds["baseline"][0], bounds["baseline"][1])
    keys = sorted(set(target) | set(previous) | set(baseline_raw))
    activities = []
    for key in keys:
        target_metrics = target.get(key, _empty_metrics(key))
        previous_metrics = previous.get(key, _empty_metrics(key))
        baseline_metrics = _volume_average(
            baseline_raw.get(key, _empty_metrics(key)), bounds["baseline"][2]
        )
        activities.append({
            "activity_key": key,
            "target": target_metrics,
            "previous": previous_metrics,
            "baseline_per_period": baseline_metrics,
            "change_from_previous": _comparison(target_metrics, previous_metrics),
            "change_from_baseline": _comparison(target_metrics, baseline_metrics),
        })
    return {
        "period_kind": period_kind,
        "period_start": start,
        "period_end": end,
        "current_partial": current,
        "previous_start": bounds["previous"][0],
        "previous_end": bounds["previous"][1],
        "baseline_start": bounds["baseline"][0],
        "baseline_end": bounds["baseline"][1],
        "baseline_periods": bounds["baseline"][2],
        "activities": activities,
    }


def _pace(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    rounded = round(seconds)
    return f"{rounded // 60}:{rounded % 60:02d}"


def render_activity_note(item: dict[str, Any], period_start: date, period_end: date) -> str:
    """Return a concise deterministic narrative suitable as a safe fallback."""
    key = item["activity_key"]
    target = item["target"]
    previous = item["previous"]
    label = key.replace("_", " ").title()
    if target["sessions"] == 0:
        return (
            f"{label}: no Garmin sessions were recorded from {period_start} through {period_end}. "
            f"The previous period had {previous['sessions']} session(s)."
        )
    parts = [f"{label}: {target['sessions']} session(s), {target['duration_minutes']:.1f} minutes"]
    if key == "stair_climbing":
        parts.append(f"an estimated {target['stairs_climbed']:,.0f} stairs at 32 per minute")
    elif key == "lap_swimming":
        parts.append(f"{target['distance_meters']:,.0f} meters")
        if target.get("active_pace_seconds_per_100") is not None:
            parts.append(f"{_pace(target['active_pace_seconds_per_100'])} active pace per 100 meters")
        if target.get("average_swolf") is not None:
            parts.append(f"{target['average_swolf']:.1f} average SWOLF")
    elif target.get("distance_miles"):
        parts.append(f"{target['distance_miles']:.2f} miles")
        if target.get("pace_seconds_per_mile") is not None:
            parts.append(f"{_pace(target['pace_seconds_per_mile'])} moving pace per mile")
    if target.get("average_hr") is not None:
        parts.append(f"{target['average_hr']:.0f} bpm average HR")
    sentence = ", ".join(parts) + "."
    change = item["change_from_previous"].get("duration_percent")
    if change is not None:
        sentence += f" Duration changed {change:+.1f}% from the previous period."
    return sentence


def _rollup_facts(facts: dict[str, Any]) -> dict[str, Any]:
    target = [item["target"] for item in facts["activities"]]
    return {
        "activity_key": ROLLUP_KEY,
        "activity_types": [item["activity_key"] for item in facts["activities"] if item["target"]["sessions"]],
        "sessions": int(sum(item["sessions"] for item in target)),
        "duration_minutes": _round(sum(item["duration_minutes"] for item in target)),
        "calories": int(sum(item["calories"] for item in target)),
        "training_load": _round(sum(item["training_load"] for item in target)),
    }


def render_rollup_note(rollup: dict[str, Any], start: date, end: date) -> str:
    types = ", ".join(key.replace("_", " ") for key in rollup["activity_types"]) or "none"
    return (
        f"Garmin activity rollup for {start} through {end}: {rollup['sessions']} session(s), "
        f"{rollup['duration_minutes']:.1f} minutes, and {rollup['training_load']:.1f} training-load points. "
        f"Active types: {types}."
    )


def _validated_model_note(prompt: str, fallback: str, model_call: Callable[[str], str]) -> str:
    try:
        candidate = model_call(prompt).strip()
    except Exception:
        return fallback
    if not candidate or len(candidate) > 900:
        return fallback
    allowed = set(_NUMBER_RE.findall(prompt))
    if any(number not in allowed for number in _NUMBER_RE.findall(candidate)):
        return fallback
    return candidate


def _note_id(period_kind: str, start: date, end: date, activity_key: str) -> str:
    raw = f"activity-progress:{period_kind}:{start}:{end}:{activity_key}"
    return hashlib.sha256(raw.encode()).hexdigest()


def generate_activity_progress_notes(
    con: duckdb.DuckDBPyConnection,
    period_kind: str,
    period_end: str | date | None = None,
    *,
    today: date | None = None,
    model_call: Callable[[str], str] = _ask_local_model,
) -> dict[str, Any]:
    """Generate and idempotently store one completed activity-progress period."""
    facts = activity_progress_facts(con, period_kind, period_end, today=today)
    start, end = facts["period_start"], facts["period_end"]
    existing = {
        row[0]: {"facts_hash": row[1], "narrative": row[2]}
        for row in con.execute(
            """
            SELECT activity_key, facts_hash, narrative
            FROM activity_progress_notes
            WHERE period_kind = ? AND period_start = ? AND period_end = ?
            """,
            [period_kind, start, end],
        ).fetchall()
    }
    pending = []
    for item in facts["activities"]:
        fallback = render_activity_note(item, start, end)
        serialized = json.dumps(item, default=str, sort_keys=True, separators=(",", ":"))
        facts_hash = hashlib.sha256(serialized.encode()).hexdigest()
        prior = existing.get(item["activity_key"])
        if prior and prior["facts_hash"] == facts_hash:
            narrative = prior["narrative"]
        else:
            prompt = (
                "Write one or two concise sentences about this Garmin activity period. "
                "Use only supplied facts. Describe changes; do not diagnose, coach, or invent numbers.\n"
                + json.dumps(item, default=str, sort_keys=True)
            )
            narrative = _validated_model_note(prompt, fallback, model_call)
        pending.append((item["activity_key"], "activity", serialized, facts_hash, narrative))
    rollup = _rollup_facts(facts)
    rollup_fallback = render_rollup_note(rollup, start, end)
    rollup_serialized = json.dumps(rollup, default=str, sort_keys=True, separators=(",", ":"))
    rollup_hash = hashlib.sha256(rollup_serialized.encode()).hexdigest()
    prior_rollup = existing.get(ROLLUP_KEY)
    if prior_rollup and prior_rollup["facts_hash"] == rollup_hash:
        rollup_narrative = prior_rollup["narrative"]
    else:
        rollup_prompt = (
            "Write a concise Garmin activity-period rollup using only these facts. "
            "Do not diagnose, coach, or invent numbers.\n"
            + json.dumps(rollup, default=str, sort_keys=True)
        )
        rollup_narrative = _validated_model_note(rollup_prompt, rollup_fallback, model_call)
    pending.append((ROLLUP_KEY, "rollup", rollup_serialized, rollup_hash, rollup_narrative))

    con.execute("BEGIN")
    try:
        for activity_key, note_kind, serialized, facts_hash, narrative in pending:
            con.execute(
                """
                INSERT INTO activity_progress_notes
                    (id, period_kind, period_start, period_end, activity_key,
                     note_kind, facts, narrative, model, facts_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    facts = excluded.facts,
                    narrative = excluded.narrative,
                    model = excluded.model,
                    facts_hash = excluded.facts_hash,
                    updated_at = excluded.updated_at
                """,
                [_note_id(period_kind, start, end, activity_key), period_kind, start, end,
                 activity_key, note_kind, serialized, narrative, OLLAMA_SQL_MODEL, facts_hash],
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return activity_progress(con, period_kind, end)


def activity_progress(
    con: duckdb.DuckDBPyConnection,
    period_kind: str,
    period_end: str | date | None = None,
    *,
    activity_type: str | None = None,
    current: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    """Return exact progress facts and any stored narratives."""
    facts = activity_progress_facts(con, period_kind, period_end, current=current, today=today)
    narratives = {}
    if not current:
        narratives = {
            row[0]: row[1]
            for row in con.execute(
                """
                SELECT activity_key, narrative FROM activity_progress_notes
                WHERE period_kind = ? AND period_start = ? AND period_end = ?
                """,
                [period_kind, facts["period_start"], facts["period_end"]],
            ).fetchall()
        }
    filtered = []
    for item in facts["activities"]:
        if activity_type and item["activity_key"].lower() != activity_type.lower():
            continue
        item = dict(item)
        item["narrative"] = narratives.get(item["activity_key"]) or render_activity_note(
            item, facts["period_start"], facts["period_end"]
        )
        filtered.append(item)
    facts["activities"] = filtered
    rollup = _rollup_facts(facts) if not activity_type else None
    if rollup is not None:
        rollup["narrative"] = narratives.get(ROLLUP_KEY) or render_rollup_note(
            rollup, facts["period_start"], facts["period_end"]
        )
    facts["rollup"] = rollup
    return facts
