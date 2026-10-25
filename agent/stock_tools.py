"""Read-only stock research tools over canonical stored facts."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import calendar
from copy import deepcopy
from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo
from jsonschema import Draft202012Validator

from config.settings import ASK_BUSINESS_TIMEZONE

ALERT_TYPES = (
    "golden_cross", "death_cross", "supertrend_daily_bullish",
    "supertrend_daily_bearish", "supertrend_weekly_bullish", "supertrend_weekly_bearish",
)
SIGNAL_COMBINATIONS = (("sma_cross", "daily"), ("supertrend", "daily"), ("supertrend", "weekly"))
_ALERT_RELATION = """SELECT *,
    CASE WHEN alert_type IN ('golden_cross', 'supertrend_daily_bullish', 'supertrend_weekly_bullish') THEN 'bullish'
         WHEN alert_type IN ('death_cross', 'supertrend_daily_bearish', 'supertrend_weekly_bearish') THEN 'bearish' END AS direction,
    CASE WHEN alert_type LIKE 'supertrend_weekly_%' THEN 'weekly'
         WHEN alert_type IN ('golden_cross', 'death_cross') OR alert_type LIKE 'supertrend_daily_%' THEN 'daily' END AS timeframe,
    CASE WHEN alert_type IN ('golden_cross', 'death_cross') THEN 'sma_cross'
         WHEN alert_type LIKE 'supertrend_%' THEN 'supertrend' END AS signal_type
    FROM stock_alerts"""


def _choice(value, options, name):
    if value not in options:
        raise ValueError(f"{name} must be one of: {', '.join(options)}")
    return value


def _integer(value, lower, upper, name):
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise ValueError(f"{name} must be an integer between {lower} and {upper}")
    return value


def _date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Dates must use YYYY-MM-DD")
    return date.fromisoformat(value)


def _months_before(value, months):
    year, month = divmod(value.year * 12 + value.month - 1 - months, 12)
    return date(year, month + 1, min(value.day, calendar.monthrange(year, month + 1)[1]))


def business_today():
    return datetime.now(ZoneInfo(ASK_BUSINESS_TIMEZONE)).date()


def resolve_window(args, *, default_period=None, today=None):
    """Resolve structured periods, never free-form question text, in business time."""
    today = today or business_today()
    reference = _date(args["as_of"]) if args.get("as_of") else today
    if reference > today:
        raise ValueError("as_of cannot be in the future")
    period = args.get("period")
    explicit = bool(args.get("start_date") or args.get("end_date"))
    if period is not None and explicit:
        raise ValueError("Use period or explicit start_date/end_date, not both")
    if not period and not explicit:
        period = default_period
    end, start = reference, None
    if explicit:
        start = _date(args["start_date"]) if args.get("start_date") else None
        end = _date(args["end_date"]) if args.get("end_date") else reference
    elif period and period != "all":
        week_start = reference - timedelta(days=(reference.weekday() + 1) % 7)
        if period == "today":
            start = reference
        elif period == "yesterday":
            start = end = reference - timedelta(days=1)
        elif period == "this_week":
            start = week_start
        elif period == "last_week":
            start, end = week_start - timedelta(days=7), week_start - timedelta(days=1)
        elif period == "this_month":
            start = reference.replace(day=1)
        elif period == "last_month":
            end = reference.replace(day=1) - timedelta(days=1)
            start = end.replace(day=1)
        elif period == "ytd":
            start = reference.replace(month=1, day=1)
        else:
            match = re.fullmatch(r"([1-9]\d{0,3})(d|w|mo|y)", str(period))
            if not match:
                raise ValueError("period must be Nd, Nw, Nmo, Ny, today, yesterday, this_week, last_week, this_month, last_month, ytd, or all")
            count, unit = int(match[1]), match[2]
            try:
                start = (
                    reference - timedelta(days=count * (7 if unit == "w" else 1))
                    if unit in {"d", "w"} else _months_before(reference, count * (12 if unit == "y" else 1))
                ) + timedelta(days=1)
            except (ValueError, OverflowError) as error:
                raise ValueError("period is outside the supported date range") from error
    if end > reference or (start is not None and start > end):
        raise ValueError("Dates must satisfy start_date <= end_date <= as_of")
    return {"reference_date": reference, "start_date": start, "end_date": end, "timezone": ASK_BUSINESS_TIMEZONE}


def _tickers(args):
    values = args.get("tickers") or []
    if not isinstance(values, list) or any(not isinstance(t, str) or not t.strip() for t in values):
        raise ValueError("tickers must be a list of non-empty ticker strings")
    return sorted({t.strip().upper() for t in values})


def _filters(args, window, *, dates=True):
    clauses, params = [], []
    tickers = _tickers(args)
    if tickers:
        clauses.append("upper(ticker) IN (" + ",".join("?" for _ in tickers) + ")")
        params.extend(tickers)
    if dates:
        clauses.append("date <= ?")
        params.append(window["end_date"])
        if window["start_date"]:
            clauses.append("date >= ?")
            params.append(window["start_date"])
    return clauses, params


def _rows(con, sql, params=None):
    cursor = con.execute(sql, params or [])
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _latest_date(con, table, args, window):
    clauses, params = _filters(args, {**window, "start_date": None, "end_date": window["reference_date"]})
    return con.execute(f"SELECT MAX(date) FROM {table} WHERE {' AND '.join(clauses)}", params).fetchone()[0]


def _envelope(con, sql, params, args, window, table):
    limit = _integer(args.get("limit", 100), 1, 500, "limit")
    offset = _integer(args.get("cursor", 0), 0, 2**31 - 1, "cursor")
    total = con.execute(f"SELECT COUNT(*) FROM ({sql}) matches", params).fetchone()[0]
    rows = _rows(con, f"SELECT * FROM ({sql}) matches LIMIT ? OFFSET ?", [*params, limit, offset])
    truncated = offset + len(rows) < total
    return {
        "rows": rows, "count": len(rows), "total_count": total,
        "truncated": truncated, "next_cursor": offset + len(rows) if truncated else None,
        "window": window, "latest_available_date": _latest_date(con, table, args, window),
        "source": table,
    }


def _order(args):
    return _choice(args.get("order", "desc"), ("asc", "desc"), "order").upper()


def _alert_query(args, window):
    clauses, params = _filters(args, window)
    if args.get("alert_type"):
        clauses.append("alert_type = ?")
        params.append(_choice(args["alert_type"], ALERT_TYPES, "alert_type"))
    for name, choices in (("direction", ("bullish", "bearish")), ("timeframe", ("daily", "weekly"))):
        if args.get(name):
            clauses.append(f"{name} = ?")
            params.append(_choice(args[name], choices, name))
    query = f"SELECT * FROM ({_ALERT_RELATION}) alerts WHERE {' AND '.join(clauses)}"
    return query, params


def query_stock_alerts(con, args):
    window = resolve_window(args)
    query, params = _alert_query(args, window)
    if args.get("latest_per_ticker"):
        query = f"SELECT * FROM ({query}) alerts QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC, notified_at DESC, id) = 1"
    order = _order(args)
    return _envelope(con, f"{query} ORDER BY date {order}, notified_at {order}, ticker, id", params, args, window, "stock_alerts")


def summarize_stock_alerts(con, args):
    window = resolve_window(args)
    query, params = _alert_query(args, window)
    group = _choice(args.get("group_by", "alert_type"), ("ticker", "alert_type", "direction", "timeframe", "date", "week", "month"), "group_by")
    expression = {
        "week": "CAST(date_trunc('week', date + INTERVAL '1 day') - INTERVAL '1 day' AS DATE)",
        "month": "CAST(date_trunc('month', date) AS DATE)",
    }.get(group, group)
    result = _envelope(con, f"""SELECT {expression} AS {group}, COUNT(*) AS alert_count,
        COUNT(DISTINCT ticker) AS ticker_count, MIN(date) AS first_date, MAX(date) AS last_date
        FROM ({query}) alerts GROUP BY 1 ORDER BY alert_count DESC, {group}""",
        params, args, window, "stock_alerts")
    result["totals"] = _rows(con, f"SELECT COUNT(*) AS alert_count, COUNT(DISTINCT ticker) AS ticker_count FROM ({query}) alerts", params)[0]
    return result


def query_stock_signals(con, args):
    window = resolve_window(args)
    mode = _choice(args.get("mode", "latest"), ("latest", "history"), "mode")
    # Choose the latest observation BEFORE filtering direction; never resurrect an old state.
    base_clauses, base_params = _filters(args, {**window, "start_date": None})
    query = f"SELECT * FROM stock_signals WHERE {' AND '.join(base_clauses)}"
    if mode == "latest":
        query += " QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker, signal_type, timeframe ORDER BY date DESC, id) = 1"
    clauses, params = ["true"], list(base_params)
    if window["start_date"]:
        clauses.append("date >= ?")
        params.append(window["start_date"])
    for name, choices in (("signal_type", ("sma_cross", "supertrend")), ("timeframe", ("daily", "weekly")), ("direction", ("bullish", "bearish"))):
        if args.get(name):
            clauses.append(f"{name} = ?")
            params.append(_choice(args[name], choices, name))
    query = f"SELECT * FROM ({query}) signals WHERE {' AND '.join(clauses)}"
    if args.get("require_all"):
        if mode != "latest" or not args.get("direction"):
            raise ValueError("require_all needs mode=latest and a direction")
        combinations = [pair for pair in SIGNAL_COMBINATIONS if (not args.get("signal_type") or pair[0] == args["signal_type"]) and (not args.get("timeframe") or pair[1] == args["timeframe"])]
        if not combinations:
            raise ValueError("No supported signal/timeframe combination requested")
        query = f"SELECT * FROM ({query}) signals QUALIFY COUNT(*) OVER (PARTITION BY ticker) = ?"
        params.append(len(combinations))
    result = _envelope(con, f"{query} ORDER BY date {_order(args)}, ticker, signal_type, timeframe, id", params, args, window, "stock_signals")
    result["mode"] = mode
    result["value_meaning"] = {"sma_cross": "SMA50 minus SMA200", "supertrend": "Supertrend line price"}
    result["interpretation"] = "Latest stored indicator states are not new crossover events; use alert history for transitions."
    return result


def query_stock_prices(con, args):
    window = resolve_window(args)
    mode = _choice(args.get("mode", "latest"), ("latest", "history"), "mode")
    clauses, params = _filters(args, window)
    query = f"SELECT ticker, date, open, high, low, closing_price, volume FROM stock_watchlist WHERE {' AND '.join(clauses)}"
    if mode == "latest":
        query += " QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY date DESC) = 1"
    result = _envelope(con, f"{query} ORDER BY date {_order(args)}, ticker", params, args, window, "stock_watchlist")
    result["mode"] = mode
    result["price_kind"] = "Stored historical bars, not a live quote"
    present = {r[0] for r in con.execute(f"SELECT DISTINCT ticker FROM ({query}) prices", params).fetchall()}
    result["missing_tickers"] = sorted(set(_tickers(args)) - present)
    return result


def compare_stock_performance(con, args):
    window = resolve_window(args, default_period="1mo")
    clauses, params = _filters(args, window)
    clauses.append("closing_price IS NOT NULL")
    sql = f"""SELECT ticker, MIN(date) AS first_date, MAX(date) AS last_date,
        COUNT(*) AS sessions, first(closing_price ORDER BY date) AS first_close,
        last(closing_price ORDER BY date) AS last_close,
        CASE WHEN COUNT(*) > 1 THEN last(closing_price ORDER BY date) - first(closing_price ORDER BY date) END AS price_change,
        CASE WHEN COUNT(*) > 1 THEN 100.0 * (last(closing_price ORDER BY date) / NULLIF(first(closing_price ORDER BY date), 0) - 1) END AS price_change_percent,
        MIN(COALESCE(low, closing_price)) AS period_low,
        MAX(COALESCE(high, closing_price)) AS period_high, SUM(volume) AS total_volume
        FROM stock_watchlist WHERE {' AND '.join(clauses)} GROUP BY ticker"""
    sort = _choice(args.get("sort_by", "price_change_percent"), ("price_change_percent", "ticker", "total_volume"), "sort_by")
    result = _envelope(con, f"{sql} ORDER BY {sort} {_order(args)} NULLS LAST, ticker", params, args, window, "stock_watchlist")
    # One bar does not establish a return over a period.
    for row in result["rows"]:
        row["status"] = "observed" if row["sessions"] > 1 else "insufficient_observations"
        if row["sessions"] > 1 and not row["first_close"]:
            row["status"] = "invalid_baseline"
        if row["sessions"] < 2:
            row["price_change"] = row["price_change_percent"] = None
    present = {r[0] for r in con.execute(f"SELECT ticker FROM ({sql}) performance", params).fetchall()}
    result["missing_tickers"] = sorted(set(_tickers(args)) - present)
    result["method"] = "First-to-last stored close within requested bounds; observed dates are shown. Not portfolio P&L or total return."
    return result


def get_stock_overview(con, args):
    tickers = _tickers(args)
    if not tickers or len(tickers) > 20:
        raise ValueError("Overview needs between 1 and 20 tickers")
    window = resolve_window(args)
    rows = []
    for ticker in tickers:
        scoped = {"tickers": [ticker], "as_of": window["reference_date"].isoformat(), "limit": 5}
        prices = query_stock_prices(con, scoped)["rows"]
        previous = _rows(con, "SELECT date, closing_price FROM stock_watchlist WHERE ticker = ? AND date <= ? AND closing_price IS NOT NULL ORDER BY date DESC LIMIT 2", [ticker, window["end_date"]])
        change = None
        if len(previous) == 2 and previous[1]["closing_price"]:
            change = 100 * (float(previous[0]["closing_price"]) / float(previous[1]["closing_price"]) - 1)
        signals = query_stock_signals(con, scoped)["rows"]
        bars = _rows(con, "SELECT date, closing_price FROM stock_watchlist WHERE ticker = ? AND date <= ? AND closing_price IS NOT NULL ORDER BY date DESC LIMIT 200", [ticker, window["reference_date"]])
        averages = {f"sma{n}": sum(float(b["closing_price"]) for b in bars[:n]) / n if len(bars) >= n else None for n in (50, 200)}
        alerts = query_stock_alerts(con, scoped)
        notes = _rows(con, "SELECT id, note, created_at, updated_at FROM stock_notes WHERE ticker = ? AND NOT is_deleted AND CAST(created_at AS DATE) <= ? ORDER BY created_at DESC, id", [ticker, window["reference_date"]])
        rows.append({"ticker": ticker, "price": prices[0] if prices else None,
            "change_from_previous_stored_close_percent": change,
            "signals": signals, "moving_averages": {"date": bars[0]["date"] if bars else None, **averages},
            "recent_alerts": alerts["rows"], "alert_count": alerts["total_count"],
            "notes": notes[:10], "note_count": len(notes), "notes_truncated": len(notes) > 10,
            "available": bool(prices or signals or alerts["rows"] or notes)})
    return {"rows": rows, "count": len(rows), "window": window,
        "note_history_boundary": "Notes show current active text created by as_of; historical note revisions are not reconstructed."}


def get_stock_alert_outcomes(con, args):
    horizon = _integer(args.get("horizon_sessions", 5), 1, 252, "horizon_sessions")
    result = query_stock_alerts(con, args)
    as_of = result["window"]["reference_date"]
    for row in result["rows"]:
        prices = _rows(con, """SELECT date, closing_price FROM stock_watchlist
            WHERE ticker = ? AND date >= ? AND date <= ? AND closing_price IS NOT NULL
            ORDER BY date LIMIT ?""", [row["ticker"], row["date"], as_of, horizon + 1])
        complete = len(prices) == horizon + 1
        start, end = (prices[0], prices[-1]) if prices else (None, None)
        change = 100 * (float(end["closing_price"]) / float(start["closing_price"]) - 1) if prices and start["closing_price"] else None
        row.update({"horizon_sessions": horizon, "observed_sessions_after_baseline": max(0, len(prices) - 1),
            "status": "complete" if complete else "insufficient_future_prices",
            "baseline_date": start["date"] if start else None, "baseline_close": start["closing_price"] if start else None,
            "observed_end_date": end["date"] if end else None,
            "observed_end_close": end["closing_price"] if end else None,
            "observed_price_change_percent": change,
            "horizon_price_change_percent": change if complete else None})
    result["method"] = "Close-to-close change after N stored price sessions, starting at the first bar on/after the alert; prices capped at as_of. Not a backtest, trade fill, or buy/sell P&L."
    return result


def get_stock_data_coverage(con, args):
    window = resolve_window(args)
    rows = []
    for table in ("stock_watchlist", "stock_signals", "stock_alerts", "stock_notes"):
        clauses, params = _filters(args, window, dates=table != "stock_notes")
        if table == "stock_notes":
            clauses.extend(["NOT is_deleted", "CAST(created_at AS DATE) <= ?"])
            params.append(window["end_date"])
            if window["start_date"]:
                clauses.append("CAST(created_at AS DATE) >= ?")
                params.append(window["start_date"])
        column = "CAST(created_at AS DATE)" if table == "stock_notes" else "date"
        stats = _rows(con, f"SELECT COUNT(*) AS row_count, COUNT(DISTINCT ticker) AS ticker_count, MIN({column}) AS first_date, MAX({column}) AS last_date FROM {table} WHERE {' AND '.join(clauses)}", params)[0]
        rows.append({"source": table, **stats})
    return {"rows": rows, "window": window,
        "available": ["historical OHLCV", "SMA and Supertrend states", "recorded alert events", "user-authored notes"],
        "not_available": ["live quotes", "company fundamentals", "news", "broker positions", "trade execution"],
        "note_history_boundary": "Active notes are current text; as_of does not reconstruct prior revisions."}


_DATE_PROPERTIES = {
    "period": {"type": "string"}, "start_date": {"type": "string"},
    "end_date": {"type": "string"}, "as_of": {"type": "string"},
}
_TICKERS = {"type": "array", "items": {"type": "string"}}
_PAGE_PROPERTIES = {
    "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
    "cursor": {"type": "integer", "minimum": 0, "default": 0},
}
_ORDER = {"type": "string", "enum": ["asc", "desc"], "default": "desc"}
_ALERT_PROPERTIES = {
    "tickers": _TICKERS, **_DATE_PROPERTIES,
    "timeframe": {"type": "string", "enum": ["daily", "weekly"]},
    "direction": {"type": "string", "enum": ["bullish", "bearish"]},
    "alert_type": {"type": "string", "enum": list(ALERT_TYPES)},
}


def _schema(properties, required=None):
    return {"type": "object", "properties": deepcopy(properties), "required": required or [], "additionalProperties": False}


STOCK_TOOL_SCHEMAS = {
    "query_stock_alerts": _schema({**_ALERT_PROPERTIES, "latest_per_ticker": {"type": "boolean", "default": False}, "order": _ORDER, **_PAGE_PROPERTIES}),
    "summarize_stock_alerts": _schema({**_ALERT_PROPERTIES, "group_by": {"type": "string", "enum": ["ticker", "alert_type", "direction", "timeframe", "date", "week", "month"], "default": "alert_type"}, **_PAGE_PROPERTIES}),
    "query_stock_signals": _schema({"tickers": _TICKERS, **_DATE_PROPERTIES,
        "signal_type": {"type": "string", "enum": ["sma_cross", "supertrend"]},
        "timeframe": {"type": "string", "enum": ["daily", "weekly"]},
        "direction": {"type": "string", "enum": ["bullish", "bearish"]},
        "mode": {"type": "string", "enum": ["latest", "history"], "default": "latest"},
        "require_all": {"type": "boolean", "default": False}, "order": _ORDER, **_PAGE_PROPERTIES}),
    "query_stock_prices": _schema({"tickers": _TICKERS, **_DATE_PROPERTIES, "mode": {"type": "string", "enum": ["latest", "history"], "default": "latest"}, "order": _ORDER, **_PAGE_PROPERTIES}),
    "compare_stock_performance": _schema({"tickers": _TICKERS, **_DATE_PROPERTIES, "sort_by": {"type": "string", "enum": ["price_change_percent", "ticker", "total_volume"], "default": "price_change_percent"}, "order": _ORDER, **_PAGE_PROPERTIES}),
    "get_stock_overview": _schema({"tickers": {**_TICKERS, "minItems": 1, "maxItems": 20}, "as_of": {"type": "string"}}, ["tickers"]),
    "get_stock_alert_outcomes": _schema({**_ALERT_PROPERTIES, "horizon_sessions": {"type": "integer", "minimum": 1, "maximum": 252, "default": 5}, "order": _ORDER, **_PAGE_PROPERTIES}),
    "get_stock_data_coverage": _schema({"tickers": _TICKERS, **_DATE_PROPERTIES}),
}


def documented_schema(name, descriptions):
    schema = deepcopy(STOCK_TOOL_SCHEMAS[name])
    for argument, description in descriptions.items():
        schema["properties"][argument]["description"] = description
    return schema


def dispatch_stock_tool(con, name, args):
    Draft202012Validator(STOCK_TOOL_SCHEMAS[name]).validate(args)
    return {
        "query_stock_alerts": query_stock_alerts, "summarize_stock_alerts": summarize_stock_alerts,
        "query_stock_signals": query_stock_signals, "query_stock_prices": query_stock_prices,
        "compare_stock_performance": compare_stock_performance, "get_stock_overview": get_stock_overview,
        "get_stock_alert_outcomes": get_stock_alert_outcomes, "get_stock_data_coverage": get_stock_data_coverage,
    }[name](con, args)
