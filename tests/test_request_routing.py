import unittest
from datetime import date

from langgraph_client.routing import RouteId, extract_features, select_route


SYMBOLS = ["BTC-USD", "MSFT", "BKNG", "INTC", "GOOG", "GOOGL", "NFLX", "BRK.B"]
ALIASES = {
    "bitcoin": "BTC-USD",
    "microsoft": "MSFT",
}
REFERENCE_DATE = date(2026, 8, 23)


def route(question: str):
    features = extract_features(question, SYMBOLS, ALIASES, REFERENCE_DATE)
    return select_route(features, question)


class RequestRoutingTests(unittest.TestCase):
    def test_exact_stock_note_routes_without_semantic_search(self):
        match = route("List every currently saved, non-deleted stock note for BTC-USD, including the note dates.")

        self.assertEqual(match.decision.route_id, RouteId.STOCK_NOTE_EXACT)
        self.assertEqual(match.decision.tool, "query_stock_notes")
        self.assertEqual(match.decision.arguments["tickers"], ["BTC-USD"])

    def test_company_alias_resolves_against_runtime_symbols(self):
        match = route("What stock notes do I have for Microsoft (MSFT)?")

        self.assertEqual(match.decision.route_id, RouteId.STOCK_NOTE_EXACT)
        self.assertEqual(match.decision.arguments["tickers"], ["MSFT"])

    def test_meaning_based_note_query_uses_embeddings(self):
        match = route("Which currently saved, non-deleted note says INTC issued new shares?")

        self.assertEqual(match.decision.route_id, RouteId.STOCK_NOTE_SEMANTIC)
        self.assertEqual(match.decision.tool, "search_documents")
        self.assertEqual(match.decision.arguments["domain"], "stock_note")
        self.assertEqual(match.decision.arguments["ticker"], "INTC")

    def test_share_total_uses_all_exact_ticker_notes(self):
        match = route("How many shares of BKNG did I buy so far?")

        self.assertEqual(match.decision.route_id, RouteId.STOCK_NOTE_AGGREGATE)
        self.assertEqual(match.decision.tool, "query_stock_notes")

    def test_multi_ticker_weekly_alert_uses_exact_tool(self):
        match = route("Which weekly Supertrend alerts are recorded for GOOG and GOOGL?")

        self.assertEqual(match.decision.route_id, RouteId.STOCK_ALERT_EXACT)
        self.assertEqual(match.decision.arguments["tickers"], ["GOOGL", "GOOG"])
        self.assertEqual(match.decision.arguments["timeframe"], "weekly")

    def test_exact_alert_date_and_direction_are_filters(self):
        match = route("List every bullish weekly Supertrend alert recorded on 2026-08-07.")

        self.assertEqual(match.decision.arguments["start_date"], "2026-08-07")
        self.assertEqual(match.decision.arguments["end_date"], "2026-08-07")
        self.assertEqual(match.decision.arguments["direction"], "bullish")

    def test_direction_choice_question_does_not_pre_filter_answer(self):
        match = route("Was the weekly Supertrend alert for NFLX bullish or bearish, and when was it recorded?")

        self.assertNotIn("direction", match.decision.arguments)

    def test_latest_alert_limit_and_direction(self):
        match = route("What are the 3 most recent tickers for whom super trend buy was triggered?")

        self.assertEqual(match.decision.arguments["limit"], 3)
        self.assertTrue(match.decision.arguments["latest_per_ticker"])
        self.assertEqual(match.decision.arguments["direction"], "bullish")

    def test_workout_meaning_query_uses_semantic_route(self):
        match = route("Find a stored workout with an AMRAP structure.")

        self.assertEqual(match.decision.route_id, RouteId.WORKOUT_SEMANTIC)
        self.assertEqual(match.decision.arguments["domain"], "workout")

    def test_latest_price_requires_exactly_one_ticker(self):
        match = route("What is the latest closing price for MSFT?")

        self.assertEqual(match.decision.route_id, RouteId.LATEST_PRICE)
        self.assertEqual(match.decision.arguments, {"ticker": "MSFT"})

    def test_undefined_recent_window_falls_back(self):
        match = route("What have I written about MSFT recently?")

        self.assertIsNone(match.decision)
        self.assertEqual(match.fallback_reason, "undefined_relative_date")

    def test_unresolved_ticker_falls_back(self):
        match = route("What stock notes do I have for NOTAREALTICKER?")

        self.assertIsNone(match.decision)
        self.assertEqual(match.fallback_reason, "stock_note_requires_resolved_ticker")


if __name__ == "__main__":
    unittest.main()
