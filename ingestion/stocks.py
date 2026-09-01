import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import math
from datetime import date, timedelta

import duckdb
import yfinance as yf

from config.settings import DB_PATH, load_watchlist


BTC_TICKER = "BTC-USD"


def _fetch_history(ticker: str, period: str, start_date: date | None = None):
    if start_date is None:
        data = yf.Ticker(ticker).history(period=period)
    else:
        end_date = date.today() + timedelta(days=1)
        data = yf.Ticker(ticker).history(
            period=None,
            start=start_date.isoformat(),
            end=end_date.isoformat(),
        )
    if data.empty:
        return []
    data.index = data.index.tz_localize(None)

    if start_date is not None:
        data = data[
            (data.index.date >= start_date)
            & (data.index.date < end_date)
        ]

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


def _latest_date(con: duckdb.DuckDBPyConnection, ticker: str) -> date | None:
    row = con.execute(
        "SELECT MAX(date) FROM stock_watchlist WHERE ticker = ?",
        [ticker],
    ).fetchone()
    return row[0] if row and row[0] is not None else None


def fetch_latest_intraday_price(ticker: str) -> tuple | None:
    """Return a current-day OHLCV snapshot from the latest intraday bar."""
    data = yf.Ticker(ticker).history(period="1d", interval="1m")
    if data.empty:
        return None
    data = data.dropna(subset=["Close"])
    if data.empty:
        return None
    latest = data.iloc[-1]
    volume = data["Volume"].sum()

    def _safe(val):
        f = float(val)
        return None if math.isnan(f) else f

    close = _safe(latest["Close"])
    if close is None:
        return None
    return (
        date.today(),
        ticker,
        _safe(data["Open"].iloc[0]),
        _safe(data["High"].max()),
        _safe(data["Low"].min()),
        close,
        int(volume) if not math.isnan(float(volume)) else None,
    )


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


def _bulk_upsert(con: duckdb.DuckDBPyConnection, rows: list) -> int:
    if not rows:
        return 0
    ticker = rows[0][1]
    before = con.execute("SELECT COUNT(*) FROM stock_watchlist WHERE ticker = ?", [ticker]).fetchone()[0]
    for row in rows:
        upsert_current_price(con, row)
    after = con.execute("SELECT COUNT(*) FROM stock_watchlist WHERE ticker = ?", [ticker]).fetchone()[0]
    return after - before


def upsert_current_price(con: duckdb.DuckDBPyConnection, row: tuple) -> None:
    """Store one current-day market snapshot, refreshing it on repeated runs."""
    con.execute(
        """
        INSERT INTO stock_watchlist (date, ticker, open, high, low, closing_price, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (date, ticker) DO UPDATE SET
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            closing_price = excluded.closing_price,
            volume = excluded.volume
        """,
        row,
    )


def run(tickers: set[str] | None = None) -> dict:
    watchlist = load_watchlist()
    stats = {
        "watchlist_count": 0,
        "fetched_tickers": [],
        "fallback_tickers": [],
        "skipped_current": [],
        "no_data": [],
        "errors": [],
        "rows_inserted": 0,
    }
    if tickers is not None:
        watchlist = [(ticker, period) for ticker, period in watchlist if ticker in tickers]
    stats["watchlist_count"] = len(watchlist)
    if not watchlist:
        print("No matching watchlist tickers.")
        return stats

    con = duckdb.connect(str(DB_PATH))
    try:
        for ticker, period in watchlist:
            try:
                latest_date = _latest_date(con, ticker)
                if latest_date and ticker == BTC_TICKER:
                    start_date = latest_date
                else:
                    start_date = latest_date + timedelta(days=1) if latest_date else None
                if start_date and start_date > date.today():
                    print(f"Skipping {ticker}: already current through {latest_date}.")
                    stats["skipped_current"].append(ticker)
                    continue

                if start_date:
                    print(f"Fetching {ticker} (since {start_date})...")
                else:
                    print(f"Fetching {ticker} ({period})...")

                rows = _fetch_history(ticker, period, start_date)
                has_current_bitcoin_candle = any(
                    row[0] == date.today() for row in rows
                )
                if ticker == BTC_TICKER and not has_current_bitcoin_candle:
                    print("  Current daily candle missing; trying an intraday snapshot.")
                    quote = fetch_latest_intraday_price(ticker)
                    if quote is not None and (
                        start_date is None or quote[0] >= start_date
                    ):
                        rows = [row for row in rows if row[0] != quote[0]]
                        rows.append(quote)
                        stats["fallback_tickers"].append(ticker)
                if not rows:
                    print(f"  No data returned, skipping.")
                    stats["no_data"].append(ticker)
                    continue
                if ticker == BTC_TICKER:
                    inserted = _bulk_upsert(con, rows)
                else:
                    inserted = _bulk_insert(con, rows)
                print(f"  {len(rows)} rows fetched, {inserted} inserted.")
                stats["fetched_tickers"].append(ticker)
                stats["rows_inserted"] += inserted
            except Exception as e:
                print(f"  Error: {e}")
                stats["errors"].append({"ticker": ticker, "error": str(e)})
    finally:
        con.close()
    return stats


if __name__ == "__main__":
    run()
