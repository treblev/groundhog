import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion import stocks


class FrozenDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 7, 21)


def _price_frame(index: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": 100.0,
            "High": 102.0,
            "Low": 99.0,
            "Close": 101.0,
            "Volume": 1_000,
        },
        index=index,
    )


def _stock_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE stock_watchlist (
            date DATE,
            ticker VARCHAR,
            open DECIMAL(10, 2),
            high DECIMAL(10, 2),
            low DECIMAL(10, 2),
            closing_price DECIMAL(10, 2),
            volume BIGINT,
            PRIMARY KEY (date, ticker)
        )
        """
    )


class StockIngestionTests(unittest.TestCase):
    def test_incremental_fetch_rejects_history_outside_requested_window(self):
        start_date = date(2026, 7, 20)
        end_date = FrozenDate.today() + timedelta(days=1)
        old_dates = pd.date_range(end=start_date - timedelta(days=1), periods=498)
        yahoo_dates = old_dates.append(pd.DatetimeIndex([start_date, end_date]))

        with (
            patch.object(stocks, "date", FrozenDate),
            patch.object(stocks.yf, "Ticker") as ticker_class,
        ):
            ticker_class.return_value.history.return_value = _price_frame(yahoo_dates)
            rows = stocks._fetch_history("TEST", "2y", start_date)

        ticker_class.return_value.history.assert_called_once_with(
            period=None,
            start="2026-07-20",
            end="2026-07-22",
        )
        self.assertEqual([row[0] for row in rows], [start_date])

    def test_new_ticker_uses_configured_backfill_period(self):
        yahoo_dates = pd.DatetimeIndex(["2026-07-20"])

        with patch.object(stocks.yf, "Ticker") as ticker_class:
            ticker_class.return_value.history.return_value = _price_frame(yahoo_dates)
            rows = stocks._fetch_history("TEST", "7y")

        ticker_class.return_value.history.assert_called_once_with(period="7y")
        self.assertEqual(len(rows), 1)

    def test_latest_date_and_bulk_insert_are_idempotent(self):
        con = duckdb.connect(":memory:")
        try:
            _stock_table(con)
            rows = [
                (date(2026, 7, 20), "TEST", 100.0, 102.0, 99.0, 101.0, 1_000),
                (date(2026, 7, 21), "TEST", 101.0, 103.0, 100.0, 102.0, 1_100),
            ]

            self.assertIsNone(stocks._latest_date(con, "TEST"))
            self.assertEqual(stocks._bulk_insert(con, rows), 2)
            self.assertEqual(stocks._bulk_insert(con, rows), 0)
            self.assertEqual(stocks._latest_date(con, "TEST"), date(2026, 7, 21))
        finally:
            con.close()

    def test_run_fetches_starting_day_after_latest_stored_date(self):
        con = duckdb.connect(":memory:")
        _stock_table(con)
        con.execute(
            """
            INSERT INTO stock_watchlist
                (date, ticker, open, high, low, closing_price, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [date(2026, 7, 19), "TEST", 100.0, 102.0, 99.0, 101.0, 1_000],
        )

        with (
            patch.object(stocks, "date", FrozenDate),
            patch.object(stocks, "load_watchlist", return_value=[("TEST", "2y")]),
            patch.object(stocks.duckdb, "connect", return_value=con),
            patch.object(stocks, "_fetch_history", return_value=[]) as fetch,
            patch("builtins.print"),
        ):
            stocks.run()

        fetch.assert_called_once_with("TEST", "2y", date(2026, 7, 20))

    def test_empty_yahoo_response_returns_no_rows(self):
        with patch.object(stocks.yf, "Ticker") as ticker_class:
            ticker_class.return_value.history.return_value = pd.DataFrame()
            rows = stocks._fetch_history("TEST", "2y", date(2026, 7, 20))

        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
