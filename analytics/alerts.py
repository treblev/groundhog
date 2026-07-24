import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hashlib
import os
import platform
import shutil
import subprocess
from datetime import date

import duckdb

from agent.events import event_id_for, record_event
from agent.outbox import enqueue_event
from config.settings import DB_PATH


def _alert_id(date, ticker, alert_type) -> str:
    key = f"{date}|{ticker}|{alert_type}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _already_notified(con, alert_id: str) -> bool:
    row = con.execute("SELECT 1 FROM stock_alerts WHERE id = ?", [alert_id]).fetchone()
    return row is not None


def _record_alert(con, alert_id, date, ticker, alert_type, message) -> None:
    con.execute(
        """
        INSERT INTO stock_alerts (id, date, ticker, alert_type, message)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (id) DO NOTHING
        """,
        [alert_id, date, ticker, alert_type, message],
    )


def _record_signal_flip_event(
    con: duckdb.DuckDBPyConnection,
    ticker: str,
    signal_type: str,
    timeframe: str,
    date: date,
    previous_direction: str,
    direction: str,
) -> None:
    record_event(
        con,
        event_type="stock_signal_flipped",
        source="analytics.alerts",
        subject_type="stock_signal",
        subject_id=f"{ticker}:{signal_type}:{timeframe}:{date}",
        payload={
            "ticker": ticker,
            "signal_type": signal_type,
            "timeframe": timeframe,
            "date": date.isoformat(),
            "previous_direction": previous_direction,
            "direction": direction,
        },
        dedupe_key=f"stock_signal_flip:{ticker}:{signal_type}:{timeframe}:{date}:{direction}",
    )


def _record_alert_event(
    con: duckdb.DuckDBPyConnection,
    alert_id: str,
    date: date,
    ticker: str,
    alert_type: str,
    message: str,
) -> None:
    dedupe_key = f"stock_alert:{alert_id}"
    record_event(
        con,
        event_type="stock_alert_created",
        source="analytics.alerts",
        subject_type="stock_alert",
        subject_id=alert_id,
        payload={
            "date": date.isoformat(),
            "ticker": ticker,
            "alert_type": alert_type,
            "message": message,
        },
        dedupe_key=dedupe_key,
    )
    enqueue_event(con, event_id_for(dedupe_key))


def _notify(title: str, message: str) -> None:
    backend = os.environ.get("GROUNDHOG_ALERT_BACKEND", "auto").lower()
    if backend in {"none", "off", "disabled"}:
        return

    system = platform.system()
    if backend == "auto":
        if system == "Darwin":
            backend = "osascript"
        elif system == "Linux" and shutil.which("notify-send"):
            backend = "notify-send"
        else:
            backend = "stdout"

    if backend == "osascript":
        if not shutil.which("osascript"):
            print(f"  Notification skipped: osascript not found. {message}")
            return
        escaped_title = title.replace("\\", "\\\\").replace('"', '\\"')
        escaped_message = message.replace("\\", "\\\\").replace('"', '\\"')
        script = f'display notification "{escaped_message}" with title "{escaped_title}"'
        subprocess.run(["osascript", "-e", script], check=False)
        return

    if backend == "notify-send":
        if not shutil.which("notify-send"):
            print(f"  Notification skipped: notify-send not found. {message}")
            return
        subprocess.run(["notify-send", title, message], check=False)
        return

    print(f"  {title}: {message}")


def _check_sma_cross(con, ticker: str) -> None:
    rows = con.execute(
        """
        SELECT date, direction
        FROM stock_signals
        WHERE ticker = ? AND signal_type = 'sma_cross' AND timeframe = 'daily'
        ORDER BY date DESC
        LIMIT 2
        """,
        [ticker],
    ).fetchall()

    if len(rows) < 2:
        return

    today_dir = rows[0][1]
    prev_dir = rows[1][1]
    today_date = rows[0][0]

    if today_dir == prev_dir:
        return

    _record_signal_flip_event(
        con, ticker, "sma_cross", "daily", today_date, prev_dir, today_dir
    )

    if today_dir == "bullish":
        alert_type = "golden_cross"
        message = f"{ticker}: Golden Cross — SMA50 crossed above SMA200 (BUY signal)"
    else:
        alert_type = "death_cross"
        message = f"{ticker}: Death Cross — SMA50 crossed below SMA200 (SELL signal)"

    alert_id = _alert_id(today_date, ticker, alert_type)
    if _already_notified(con, alert_id):
        return

    _notify("Groundhog Stock Alert", message)
    _record_alert(con, alert_id, today_date, ticker, alert_type, message)
    _record_alert_event(con, alert_id, today_date, ticker, alert_type, message)
    print(f"  Alert fired: {message}")


def _check_supertrend_flip(con, ticker: str, timeframe: str) -> None:
    rows = con.execute(
        """
        SELECT date, direction
        FROM stock_signals
        WHERE ticker = ? AND signal_type = 'supertrend' AND timeframe = ?
        ORDER BY date DESC
        LIMIT 2
        """,
        [ticker, timeframe],
    ).fetchall()

    if len(rows) < 2:
        return

    today_dir = rows[0][1]
    prev_dir = rows[1][1]
    today_date = rows[0][0]

    if today_dir == prev_dir:
        return

    _record_signal_flip_event(
        con, ticker, "supertrend", timeframe, today_date, prev_dir, today_dir
    )

    label = f"supertrend_{timeframe}"
    if today_dir == "bullish":
        alert_type = f"{label}_bullish"
        message = f"{ticker}: Supertrend ({timeframe}) flipped BULLISH (BUY signal)"
    else:
        alert_type = f"{label}_bearish"
        message = f"{ticker}: Supertrend ({timeframe}) flipped BEARISH (SELL signal)"

    alert_id = _alert_id(today_date, ticker, alert_type)
    if _already_notified(con, alert_id):
        return

    _notify("Groundhog Stock Alert", message)
    _record_alert(con, alert_id, today_date, ticker, alert_type, message)
    _record_alert_event(con, alert_id, today_date, ticker, alert_type, message)
    print(f"  Alert fired: {message}")


def run() -> None:
    con = duckdb.connect(str(DB_PATH))
    try:
        tickers = [r[0] for r in con.execute("SELECT DISTINCT ticker FROM stock_signals").fetchall()]
        if not tickers:
            print("No signals found. Run analytics/signals.py first.")
            return
        for ticker in tickers:
            print(f"Checking alerts for {ticker}...")
            _check_sma_cross(con, ticker)
            _check_supertrend_flip(con, ticker, "weekly")
    finally:
        con.close()


if __name__ == "__main__":
    run()
