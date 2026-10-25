"""Offline contracts for read-only stock research, date windows, and state semantics."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import date, timedelta
import json
import tempfile
import unittest
from unittest.mock import patch

import duckdb
from jsonschema import ValidationError

from agent import stock_tools as st
from ingestion.schema import init_db
from mcp_server import server

TODAY = date(2026, 10, 25)


class WindowTests(unittest.TestCase):
    def test_calendar_and_rolling_windows(self):
        cases = {
            "2w": (date(2026, 10, 12), TODAY),
            "14d": (date(2026, 10, 12), TODAY),
            "today": (TODAY, TODAY),
            "yesterday": (date(2026, 10, 24), date(2026, 10, 24)),
            "this_week": (TODAY, TODAY),
            "last_week": (date(2026, 10, 18), date(2026, 10, 24)),
            "this_month": (date(2026, 10, 1), TODAY),
            "last_month": (date(2026, 9, 1), date(2026, 9, 30)),
            "ytd": (date(2026, 1, 1), TODAY),
            "all": (None, TODAY),
        }
        for period, expected in cases.items():
            with self.subTest(period=period):
                w = st.resolve_window({"period": period}, today=TODAY)
                self.assertEqual((w["start_date"], w["end_date"]), expected)
                self.assertEqual(w["timezone"], "America/Phoenix")

    def test_month_end_leap_year_and_historical_reference(self):
        w = st.resolve_window({"period": "1mo"}, today=date(2024, 3, 31))
        self.assertEqual(w["start_date"], date(2024, 3, 1))
        w = st.resolve_window({"period": "1y"}, today=date(2024, 2, 29))
        self.assertEqual(w["start_date"], date(2023, 3, 1))
        w = st.resolve_window({"period": "2w", "as_of": "2026-10-24"}, today=TODAY)
        self.assertEqual(w["start_date"], date(2026, 10, 11))

    def test_rejects_conflicting_invalid_and_future_inputs(self):
        for args in (
            {"period": "2w", "start_date": "2026-10-01"}, {"period": "0w"},
            {"period": "last two weeks"}, {"start_date": "2026-10-26"},
            {"as_of": "2026-10-26"}, {"start_date": "2026-10-25", "end_date": "2026-10-24"},
            {"end_date": "2026-13-01"}, {"period": "9999y"},
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                st.resolve_window(args, today=TODAY)


class StockToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.duckdb"
        init_db(self.path)
        self.con = duckdb.connect(str(self.path))
        self.addCleanup(lambda: self.con.close())
        patcher = patch.object(st, "business_today", return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)
        for id, ticker, day, kind in (
            ("old", "TEST", "2026-10-11", "death_cross"),
            ("boundary", "BOUND", "2026-10-12", "death_cross"),
            ("death1", "TEST", "2026-10-23", "death_cross"),
            ("death2", "TEST", "2026-10-24", "death_cross"),
            ("gold", "TEST", "2026-10-25", "golden_cross"),
            ("st-bear", "OTHER", "2026-10-24", "supertrend_weekly_bearish"),
            ("st-bull", "UP", "2026-10-25", "supertrend_daily_bullish"),
            ("future", "FUTURE", "2026-10-26", "death_cross"),
        ):
            self.con.execute("INSERT INTO stock_alerts (id,ticker,date,alert_type,message,notified_at) VALUES (?,?,?,?,?,CAST(? AS DATE))", [id, ticker, day, kind, ticker + " " + kind, day])
        for id, ticker, day, kind, timeframe, direction in (
            ("s-old", "TEST", "2026-10-10", "sma_cross", "daily", "bearish"),
            ("s-new", "TEST", "2026-10-23", "sma_cross", "daily", "bullish"),
            ("s-d", "TEST", "2026-10-23", "supertrend", "daily", "bullish"),
            ("s-w", "TEST", "2026-10-24", "supertrend", "weekly", "bearish"),
            ("g-d", "GOOD", "2026-10-23", "supertrend", "daily", "bullish"),
            ("g-w", "GOOD", "2026-10-24", "supertrend", "weekly", "bullish"),
            ("s-future", "TEST", "2026-10-26", "sma_cross", "daily", "bearish"),
        ):
            self.con.execute("INSERT INTO stock_signals (id,ticker,date,signal_type,timeframe,direction,value) VALUES (?,?,?,?,?,?,1)", [id, ticker, day, kind, timeframe, direction])
        for ticker, prices in (("TEST", [100, 110, 120, 130]), ("DOWN", [100, 90, 80, 70])):
            for offset, price in enumerate(prices):
                self.con.execute("INSERT INTO stock_watchlist (ticker,date,closing_price,open,high,low,volume) VALUES (?,?,?,?,?,?,10)", [ticker, date(2026,10,20)+timedelta(days=offset), price, price, price+1, price-1])
        self.con.execute("INSERT INTO stock_watchlist (ticker,date,closing_price) VALUES ('TEST','2026-10-26',999), ('SINGLE','2026-10-23',50), ('ZERO','2026-10-20',0), ('ZERO','2026-10-23',10)")
        self.con.execute("INSERT INTO stock_notes (id,ticker,note,created_at,is_deleted) VALUES ('note','TEST','Example thesis','2026-10-20',false), ('deleted','TEST','Old thesis','2026-10-19',true)")

    def call(self, name, **args):
        return st.dispatch_stock_tool(self.con, name, args)

    def test_death_cross_relative_window_unique_stocks_and_asof(self):
        r = self.call("query_stock_alerts", alert_type="death_cross", period="2w", latest_per_ticker=True)
        self.assertEqual({row["ticker"] for row in r["rows"]}, {"TEST", "BOUND"})
        self.assertEqual(r["rows"][0]["id"], "death2")
        self.assertEqual(r["window"]["start_date"], date(2026,10,12))
        self.assertEqual(r["total_count"], 2)
        self.assertEqual(r["latest_available_date"], TODAY)

    def test_direction_and_daily_filters_include_sma_crosses(self):
        r = self.call("query_stock_alerts", direction="bearish", timeframe="daily", period="2w")
        self.assertEqual({row["alert_type"] for row in r["rows"]}, {"death_cross"})
        r = self.call("query_stock_alerts", direction="bullish", period="2w")
        self.assertEqual({row["alert_type"] for row in r["rows"]}, {"golden_cross", "supertrend_daily_bullish"})
        r = self.call("query_stock_alerts", direction="bearish", timeframe="weekly")
        self.assertEqual([row["id"] for row in r["rows"]], ["st-bear"])

    def test_pagination_is_complete_stable_and_counts_all_matches(self):
        first = self.call("query_stock_alerts", period="2w", limit=2)
        second = self.call("query_stock_alerts", period="2w", limit=2, cursor=first["next_cursor"])
        self.assertEqual(first["total_count"], 6)
        self.assertEqual(first["count"], 2)
        self.assertTrue(first["truncated"])
        self.assertFalse({r["id"] for r in first["rows"]} & {r["id"] for r in second["rows"]})

    def test_large_result_set_is_explicitly_paginated_without_silent_loss(self):
        self.con.execute("INSERT INTO stock_alerts (id,ticker,date,alert_type,message) SELECT 'bulk-' || i, 'BULK', DATE '2026-10-25', 'death_cross', 'example' FROM range(501) x(i)")
        first = self.call("query_stock_alerts", tickers=["BULK"], limit=500)
        second = self.call("query_stock_alerts", tickers=["BULK"], limit=500, cursor=first["next_cursor"])
        self.assertEqual((first["count"], first["total_count"], second["count"]), (500,501,1))
        self.assertFalse(second["truncated"])
        self.assertEqual(len({r["id"] for r in first["rows"] + second["rows"]}), 501)

    def test_counts_are_not_page_counts_and_grouping_is_server_side(self):
        r = self.call("summarize_stock_alerts", period="2w", limit=1)
        self.assertEqual(r["totals"], {"alert_count": 6, "ticker_count": 4})
        self.assertEqual(r["rows"][0]["alert_type"], "death_cross")
        self.assertEqual(r["rows"][0]["alert_count"], 3)
        self.assertTrue(r["truncated"])
        r = self.call("summarize_stock_alerts", group_by="week")
        self.assertEqual(sum(row["alert_count"] for row in r["rows"]), 7)
        self.assertTrue(all(row["week"].weekday() == 6 for row in r["rows"]))

    def test_latest_state_is_selected_before_direction_and_never_uses_future(self):
        r = self.call("query_stock_signals", signal_type="sma_cross", direction="bearish")
        self.assertEqual(r["rows"], [])
        r = self.call("query_stock_signals", signal_type="sma_cross", direction="bearish", mode="history")
        self.assertEqual([row["id"] for row in r["rows"]], ["s-old"])
        r = self.call("query_stock_signals", signal_type="sma_cross", direction="bearish", as_of="2026-10-15")
        self.assertEqual([row["id"] for row in r["rows"]], ["s-old"])

    def test_alignment_requires_each_selected_state_and_rejects_missing_states(self):
        r = self.call("query_stock_signals", signal_type="supertrend", direction="bullish", require_all=True)
        self.assertEqual({row["ticker"] for row in r["rows"]}, {"GOOD"})
        self.assertEqual(len(r["rows"]), 2)
        r = self.call("query_stock_signals", direction="bullish", require_all=True)
        self.assertEqual(r["rows"], [])
        with self.assertRaises(ValueError):
            self.call("query_stock_signals", require_all=True)

    def test_latest_prices_history_missing_symbols_and_cutoff(self):
        r = self.call("query_stock_prices", tickers=[" test ", "MISSING"])
        self.assertEqual(r["rows"][0]["closing_price"], 130)
        self.assertEqual(r["rows"][0]["date"], date(2026,10,23))
        self.assertEqual(r["missing_tickers"], ["MISSING"])
        r = self.call("query_stock_prices", tickers=["TEST"], mode="history", as_of="2026-10-21")
        self.assertEqual(len(r["rows"]), 2)

    def test_signal_stock_count_is_distinct_and_independent_of_page_size(self):
        r = self.call("query_stock_signals", signal_type="supertrend", direction="bullish", require_all=True, limit=1)
        self.assertEqual(r["ticker_count"], 1)
        self.assertEqual(r["total_count"], 2)
        self.assertEqual(r["count"], 1)
        self.assertTrue(r["truncated"])
        self.assertNotIn("created_at", r["rows"][0])

    def test_performance_returns_rank_missing_data_and_zero_baseline(self):
        r = self.call("compare_stock_performance", tickers=["TEST", "DOWN", "SINGLE", "ZERO", "MISSING"], period="2w")
        by_ticker = {row["ticker"]: row for row in r["rows"]}
        self.assertAlmostEqual(by_ticker["TEST"]["price_change_percent"], 30)
        self.assertAlmostEqual(by_ticker["DOWN"]["price_change_percent"], -30)
        self.assertIsNone(by_ticker["SINGLE"]["price_change_percent"])
        self.assertIsNone(by_ticker["ZERO"]["price_change_percent"])
        self.assertEqual(by_ticker["TEST"]["total_volume"], 40)
        self.assertEqual(r["missing_tickers"], ["MISSING"])
        self.assertEqual(r["rows"][0]["ticker"], "TEST")

    def test_outcomes_sessions_and_incomplete_horizons_are_explicit(self):
        self.con.execute("INSERT INTO stock_alerts (id,ticker,date,alert_type,message) VALUES ('outcome','TEST','2026-10-20','golden_cross','example')")
        r = self.call("get_stock_alert_outcomes", tickers=["TEST"], start_date="2026-10-20", end_date="2026-10-20", horizon_sessions=2)
        row = r["rows"][0]
        self.assertEqual(row["status"], "complete")
        self.assertAlmostEqual(row["horizon_price_change_percent"], 20)
        r = self.call("get_stock_alert_outcomes", tickers=["TEST"], start_date="2026-10-20", end_date="2026-10-20", horizon_sessions=5)
        row = r["rows"][0]
        self.assertEqual(row["status"], "insufficient_future_prices")
        self.assertIsNone(row["horizon_price_change_percent"])
        self.assertEqual(row["observed_end_date"], date(2026,10,23))
        r = self.call("get_stock_alert_outcomes", tickers=["TEST"], start_date="2026-10-20", end_date="2026-10-20", as_of="2026-10-21", horizon_sessions=2)
        self.assertEqual(r["rows"][0]["observed_sessions_after_baseline"], 1)

    def test_overview_combines_sources_without_deleted_notes_or_fake_smas(self):
        r = self.call("get_stock_overview", tickers=["TEST", "MISSING"])
        by_ticker = {row["ticker"]: row for row in r["rows"]}
        self.assertEqual(by_ticker["TEST"]["price"]["closing_price"], 130)
        self.assertEqual(by_ticker["TEST"]["note_count"], 1)
        self.assertEqual(by_ticker["TEST"]["notes"][0]["note"], "Example thesis")
        self.assertIsNone(by_ticker["TEST"]["moving_averages"]["sma50"])
        self.assertFalse(by_ticker["MISSING"]["available"])

    def test_overview_computes_moving_averages_with_enough_stored_closes(self):
        for i in range(200):
            self.con.execute("INSERT INTO stock_watchlist (ticker,date,closing_price) VALUES ('AVG',?,?)", [TODAY-timedelta(days=199-i), i+1])
        row = self.call("get_stock_overview", tickers=["AVG"])["rows"][0]
        self.assertEqual(row["moving_averages"]["sma50"], 175.5)
        self.assertEqual(row["moving_averages"]["sma200"], 100.5)

    def test_coverage_and_tools_work_on_readonly_connection_without_writes(self):
        before = self.con.execute("SELECT COUNT(*) FROM stock_alerts").fetchone()[0]
        self.con.close()
        self.con = duckdb.connect(str(self.path), read_only=True)
        for tool in st.STOCK_TOOL_SCHEMAS:
            args = {"tickers": ["TEST"]} if tool == "get_stock_overview" else {}
            with self.subTest(tool=tool):
                r = self.call(tool, **args)
                self.assertIn("rows", r)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM stock_alerts").fetchone()[0], before)

    def test_mcp_dispatch_exposes_same_filters_and_schema_validates_inputs(self):
        r = json.loads(server._dispatch("query_stock_alerts", {"period": "2w", "alert_type": "death_cross"}, self.con))
        self.assertEqual(r["total_count"], 3)
        for name, args in (
            ("query_stock_alerts", {"alert_type": "invented"}),
            ("query_stock_prices", {"tickers": "TEST"}),
            ("query_stock_alerts", {"latest_per_ticker": "true"}),
            ("query_stock_prices", {"unexpected": True}),
            ("query_stock_alerts", {"limit": 0}),
        ):
            with self.subTest(name=name,args=args), self.assertRaises(ValidationError):
                self.call(name, **args)

    def test_ticker_input_is_parameterized_and_empty_results_are_structured(self):
        r = self.call("query_stock_alerts", tickers=["' OR 1=1 --"])
        self.assertEqual((r["rows"],r["total_count"],r["truncated"]), ([],0,False))
        r = self.call("summarize_stock_alerts", tickers=["MISSING"])
        self.assertEqual(r["totals"], {"alert_count":0,"ticker_count":0})
