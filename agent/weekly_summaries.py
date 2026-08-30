"""Structured Sunday-through-Saturday health and market summaries."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import duckdb


def week_bounds(week_end: str | None = None, *, today: date | None = None) -> tuple[date, date]:
    """Return the Sunday/Saturday bounds for a requested or latest week."""
    if week_end:
        try:
            end = date.fromisoformat(week_end)
        except ValueError as error:
            raise ValueError("week_end must be a valid YYYY-MM-DD date") from error
        if end.weekday() != 5:
            raise ValueError("week_end must be a Saturday")
    else:
        current = today or date.today()
        end = current - timedelta(days=(current.weekday() - 5) % 7)
    return end - timedelta(days=6), end


def _row_dict(cursor: duckdb.DuckDBPyConnection) -> dict:
    columns = [column[0] for column in cursor.description]
    row = cursor.fetchone()
    return dict(zip(columns, row)) if row else {}


def health_summary(
    con: duckdb.DuckDBPyConnection, week_end: str | None = None
) -> dict:
    start, end = week_bounds(week_end)
    health = _row_dict(con.execute(
        """
        SELECT COUNT(*) AS days_recorded,
               SUM(active_minutes) AS total_active_minutes,
               SUM(steps) AS total_steps,
               AVG(steps) AS average_daily_steps,
               AVG(avg_hr) AS average_daily_hr
        FROM health_metrics
        WHERE date BETWEEN ? AND ?
        """,
        [start, end],
    ))
    activities = _row_dict(con.execute(
        """
        SELECT COUNT(*) AS activity_count,
               ROUND(SUM(duration_seconds) / 60.0, 1) AS recorded_activity_minutes,
               SUM(distance_miles) AS total_distance_miles,
               SUM(calories) AS total_activity_calories,
               MAX(max_hr) AS highest_activity_hr,
               ROUND(
                   SUM(avg_hr * duration_seconds) FILTER (WHERE avg_hr IS NOT NULL AND duration_seconds > 0)
                   / NULLIF(SUM(duration_seconds) FILTER (WHERE avg_hr IS NOT NULL AND duration_seconds > 0), 0),
                   1
               ) AS duration_weighted_activity_hr
        FROM activities
        WHERE date BETWEEN ? AND ?
        """,
        [start, end],
    ))
    activity_types = con.execute(
        """
        SELECT activity_type, COUNT(*) AS count,
               ROUND(SUM(duration_seconds) / 60.0, 1) AS minutes
        FROM activities
        WHERE date BETWEEN ? AND ?
        GROUP BY activity_type
        ORDER BY minutes DESC NULLS LAST, activity_type
        """,
        [start, end],
    ).fetchall()
    sleep = _row_dict(con.execute(
        """
        SELECT COUNT(*) AS nights_recorded,
               AVG(resting_hr) AS average_resting_hr,
               AVG(hrv) AS average_hrv,
               AVG(breath_rate) AS average_breath_rate,
               AVG(deep_sleep_minutes) AS average_deep_sleep_minutes,
               AVG(time_to_fall_asleep_minutes) AS average_time_to_fall_asleep_minutes
        FROM sleep_metrics
        WHERE date BETWEEN ? AND ?
        """,
        [start, end],
    ))
    return {
        "week_start": start,
        "week_end": end,
        "health": health,
        "activities": activities,
        "activity_types": [
            {"activity_type": row[0], "count": row[1], "minutes": row[2]}
            for row in activity_types
        ],
        "sleep": sleep,
        "notes": [
            "total_active_minutes comes from daily health summaries",
            "recorded_activity_minutes comes from individual activities and may overlap active minutes",
        ],
    }


def market_summary(
    con: duckdb.DuckDBPyConnection, week_end: str | None = None
) -> dict:
    start, end = week_bounds(week_end)
    btc_rows = con.execute(
        """
        SELECT date, closing_price, high, low, volume
        FROM stock_watchlist
        WHERE ticker = 'BTC-USD' AND date BETWEEN ? AND ?
        ORDER BY date
        """,
        [start, end],
    ).fetchall()
    if btc_rows:
        first, last = btc_rows[0], btc_rows[-1]
        first_close, last_close = first[1], last[1]
        change_amount = last_close - first_close if first_close is not None and last_close is not None else None
        change_percent = (
            Decimal(100) * change_amount / first_close
            if change_amount is not None and first_close
            else None
        )
        highs = [row[2] for row in btc_rows if row[2] is not None]
        lows = [row[3] for row in btc_rows if row[3] is not None]
        bitcoin = {
            "first_date": first[0],
            "last_date": last[0],
            "trading_days": len(btc_rows),
            "first_close": first_close,
            "last_close": last_close,
            "change_amount": change_amount,
            "change_percent": change_percent,
            "weekly_high": max(highs) if highs else None,
            "weekly_low": min(lows) if lows else None,
            "total_volume": sum(row[4] for row in btc_rows if row[4] is not None) or None,
            "trend": "up" if change_amount and change_amount > 0 else (
                "down" if change_amount and change_amount < 0 else "flat"
            ),
        }
    else:
        bitcoin = None

    signals_cursor = con.execute(
        """
        SELECT timeframe, direction, value, date
        FROM stock_signals
        WHERE ticker = 'BTC-USD' AND signal_type = 'supertrend' AND date <= ?
        QUALIFY ROW_NUMBER() OVER (PARTITION BY timeframe ORDER BY date DESC) = 1
        ORDER BY timeframe
        """,
        [end],
    )
    signal_columns = [column[0] for column in signals_cursor.description]
    bitcoin_signals = [dict(zip(signal_columns, row)) for row in signals_cursor.fetchall()]

    alert_cursor = con.execute(
        """
        SELECT date, ticker, alert_type, message,
               CASE
                   WHEN alert_type LIKE '%_bullish' OR alert_type = 'golden_cross' THEN 'bullish'
                   WHEN alert_type LIKE '%_bearish' OR alert_type = 'death_cross' THEN 'bearish'
                   ELSE 'other'
               END AS direction,
               CASE
                   WHEN alert_type LIKE 'supertrend_weekly_%' THEN 'weekly'
                   WHEN alert_type LIKE 'supertrend_daily_%' THEN 'daily'
                   ELSE 'cross'
               END AS timeframe
        FROM stock_alerts
        WHERE date BETWEEN ? AND ?
        ORDER BY date DESC, ticker
        """,
        [start, end],
    )
    alert_columns = [column[0] for column in alert_cursor.description]
    alerts = [dict(zip(alert_columns, row)) for row in alert_cursor.fetchall()]
    bullish = sum(row["direction"] == "bullish" for row in alerts)
    bearish = sum(row["direction"] == "bearish" for row in alerts)
    weekly = sum(row["timeframe"] == "weekly" for row in alerts)
    daily = sum(row["timeframe"] == "daily" for row in alerts)
    crosses = sum(row["timeframe"] == "cross" for row in alerts)

    return {
        "week_start": start,
        "week_end": end,
        "bitcoin": bitcoin,
        "bitcoin_supertrend": bitcoin_signals,
        "alert_summary": {
            "total": len(alerts),
            "bullish": bullish,
            "bearish": bearish,
            "other": len(alerts) - bullish - bearish,
            "weekly_supertrend": weekly,
            "daily_supertrend": daily,
            "moving_average_crosses": crosses,
            "net_bullish": bullish - bearish,
        },
        "alerts": alerts,
    }
