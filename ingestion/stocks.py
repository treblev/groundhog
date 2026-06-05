import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import math

import duckdb
import yfinance as yf

from config.settings import DB_PATH, load_watchlist


def _fetch_history(ticker: str, period: str):
    data = yf.Ticker(ticker).history(period=period)
    if data.empty:
        return []
    data.index = data.index.tz_localize(None)

    def _safe(val):
        f = float(val)
        return None if math.isnan(f) else f

    rows = []
    for _, row in data.iterrows():
        close = _safe(row["Close"])
        if close is None:
            continue  # skip rows with no closing price
        rows.append((
            row.name.date(),
            ticker,
            _safe(row["Open"]),
            _safe(row["High"]),
            _safe(row["Low"]),
            close,
            int(row["Volume"]) if not math.isnan(float(row["Volume"])) else None,
        ))
    return rows


def _bulk_insert(con: duckdb.DuckDBPyConnection, rows: list) -> int:
    if not rows:
        return 0
    ticker = rows[0][1]
    before = con.execute("SELECT COUNT(*) FROM stock_watchlist WHERE ticker = ?", [ticker]).fetchone()[0]
    for date, ticker, open_, high, low, close, volume in rows:
        con.execute(
            """
            INSERT INTO stock_watchlist (date, ticker, open, high, low, closing_price, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (date, ticker) DO NOTHING
            """,
            [date, ticker, open_, high, low, close, volume],
        )
    after = con.execute("SELECT COUNT(*) FROM stock_watchlist WHERE ticker = ?", [ticker]).fetchone()[0]
    return after - before


def run() -> None:
    watchlist = load_watchlist()
    if not watchlist:
        print("Watchlist is empty.")
        return

    con = duckdb.connect(str(DB_PATH))
    try:
        for ticker, period in watchlist:
            print(f"Fetching {ticker} ({period})...")
            try:
                rows = _fetch_history(ticker, period)
                if not rows:
                    print(f"  No data returned, skipping.")
                    continue
                inserted = _bulk_insert(con, rows)
                print(f"  {len(rows)} rows fetched, {inserted} inserted.")
            except Exception as e:
                print(f"  Error: {e}")
    finally:
        con.close()


if __name__ == "__main__":
    run()
