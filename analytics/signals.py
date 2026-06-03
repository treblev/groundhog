import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hashlib

import duckdb
import pandas as pd
from ta.trend import SMAIndicator

from config.settings import DB_PATH


def _signal_id(date, ticker, signal_type, timeframe) -> str:
    key = f"{date}|{ticker}|{signal_type}|{timeframe}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    hl2 = (df["high"] + df["low"]) / 2
    close = df["closing_price"]

    # True Range
    prev_close = close.shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)

    # Wilder's RMA — matches Pine Script's atr() default (alpha = 1/period)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()

    raw_lower = hl2 - multiplier * atr   # bullish line ("up" in Pine)
    raw_upper = hl2 + multiplier * atr   # bearish line ("dn" in Pine)

    upper = raw_upper.copy()
    lower = raw_lower.copy()
    direction = pd.Series(index=df.index, dtype=int)
    supertrend = pd.Series(index=df.index, dtype=float)

    for i in range(len(df)):
        if i == 0:
            direction.iloc[i] = 1
            supertrend.iloc[i] = lower.iloc[i]
            continue

        prev_close_val = close.iloc[i - 1]
        prev_lower = lower.iloc[i - 1]
        prev_upper = upper.iloc[i - 1]
        prev_dir = direction.iloc[i - 1]

        # Lower band ratchets up only when prev close was above it
        lower.iloc[i] = max(raw_lower.iloc[i], prev_lower) if prev_close_val > prev_lower else raw_lower.iloc[i]
        # Upper band ratchets down only when prev close was below it
        upper.iloc[i] = min(raw_upper.iloc[i], prev_upper) if prev_close_val < prev_upper else raw_upper.iloc[i]

        # Flip direction when close crosses the previous band
        if prev_dir == -1 and close.iloc[i] > prev_upper:
            direction.iloc[i] = 1
        elif prev_dir == 1 and close.iloc[i] < prev_lower:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = prev_dir

        supertrend.iloc[i] = lower.iloc[i] if direction.iloc[i] == 1 else upper.iloc[i]

    df = df.copy()
    df["supertrend"] = supertrend
    df["supertrend_direction"] = direction
    return df


def _upsert_signal(con, date, ticker, signal_type, timeframe, value, direction) -> None:
    con.execute(
        """
        INSERT INTO stock_signals (id, date, ticker, signal_type, timeframe, value, direction)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO NOTHING
        """,
        [_signal_id(date, ticker, signal_type, timeframe), date, ticker, signal_type, timeframe, value, direction],
    )


def compute_signals(con: duckdb.DuckDBPyConnection, ticker: str) -> None:
    df = con.execute(
        """
        SELECT date, open, high, low, closing_price, volume
        FROM stock_watchlist
        WHERE ticker = ?
        ORDER BY date
        """,
        [ticker],
    ).df()

    if len(df) < 200:
        print(f"  {ticker}: not enough data for SMA200 (have {len(df)} rows), skipping.")
        return

    # SMA 50 and 200
    df["sma50"] = SMAIndicator(df["closing_price"], window=50).sma_indicator()
    df["sma200"] = SMAIndicator(df["closing_price"], window=200).sma_indicator()

    # Supertrend daily
    df = _supertrend(df, period=10, multiplier=3.0)

    # Weekly resampling for weekly supertrend
    df_weekly = df.set_index("date").resample("W-FRI").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "closing_price": "last",
        "volume": "sum",
    }).dropna().reset_index()
    df_weekly = _supertrend(df_weekly, period=10, multiplier=3.0)

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    date = latest["date"]

    # SMA cross signals
    if pd.notna(latest["sma50"]) and pd.notna(latest["sma200"]):
        sma_dir = "bullish" if latest["sma50"] > latest["sma200"] else "bearish"
        _upsert_signal(con, date, ticker, "sma_cross", "daily", round(float(latest["sma50"] - latest["sma200"]), 4), sma_dir)

    # Supertrend daily
    st_dir = "bullish" if latest["supertrend_direction"] == 1 else "bearish"
    _upsert_signal(con, date, ticker, "supertrend", "daily", round(float(latest["supertrend"]), 4), st_dir)

    # Supertrend weekly
    if len(df_weekly) > 0:
        w_latest = df_weekly.iloc[-1]
        wst_dir = "bullish" if w_latest["supertrend_direction"] == 1 else "bearish"
        _upsert_signal(con, w_latest["date"], ticker, "supertrend", "weekly", round(float(w_latest["supertrend"]), 4), wst_dir)

    print(f"  {ticker}: SMA50={latest['sma50']:.2f} SMA200={latest['sma200']:.2f} ST_daily={st_dir} ST_weekly={wst_dir if len(df_weekly) > 0 else 'n/a'}")


def run() -> None:
    con = duckdb.connect(str(DB_PATH))
    try:
        tickers = [r[0] for r in con.execute("SELECT DISTINCT ticker FROM stock_watchlist").fetchall()]
        if not tickers:
            print("No tickers in stock_watchlist.")
            return
        for ticker in tickers:
            print(f"Computing signals for {ticker}...")
            try:
                compute_signals(con, ticker)
            except Exception as e:
                print(f"  Error: {e}")
    finally:
        con.close()


if __name__ == "__main__":
    run()
