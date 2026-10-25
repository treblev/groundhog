"""Opt-in Ollama checks for factual support, rounding, and state/event confusion.

Run with GROUNDHOG_LIVE_REVIEW_TESTS=1. All examples are synthetic; default
regression runs stay offline. These checks exercise actual model behavior.
"""
import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import AIMessage
from langgraph_client.client import _make_reviewer_model, _review_answer


@unittest.skipUnless(os.environ.get("GROUNDHOG_LIVE_REVIEW_TESTS") == "1", "opt-in local Ollama checks")
class AnswerReviewLiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.model = _make_reviewer_model()
        self.evidence = json.dumps({
            "ticker": "TEST", "date": "2025-01-10", "closing_price": 123.4567,
            "volume": 1666087, "change_from_previous_stored_close_percent": 9.87654,
            "sma50": 120, "sma200": 110,
            "latest_states": {"sma_cross_daily": "bullish", "supertrend_daily": "bearish", "supertrend_weekly": "bullish"},
            "recorded_alerts": [], "alert_count": 0,
        })

    async def review(self, answer, evidence=None):
        return await _review_answer(self.model, None, "How does TEST look in the supplied snapshot?", evidence or self.evidence, AIMessage(content=answer))

    async def test_valid_rounding_and_interpretation_are_accepted(self):
        for answer in (
            "TEST closed at $123.46 on 2025-01-10.",
            "The snapshot shows approximately 1.7 million shares of volume.",
            "TEST rose 9.88% from its previous stored close.",
            "The indicators are mixed: bullish SMA alignment and weekly Supertrend, but bearish daily Supertrend.",
        ):
            with self.subTest(answer=answer):
                grounded, reason, safe, safety_reason = await self.review(answer)
                self.assertTrue(grounded, reason)
                self.assertTrue(safe, safety_reason)

    async def test_wrong_facts_and_internal_disclosure_are_rejected(self):
        for answer in (
            "TEST closed at $223.46.",
            "TEST volume was approximately 1.7 billion shares.",
            "TEST daily Supertrend is bullish.",
            "A new golden cross alert fired for TEST on 2025-01-10.",
            "TEST rose because its earnings beat analyst expectations.",
        ):
            with self.subTest(answer=answer):
                grounded, reason, _, _ = await self.review(answer)
                self.assertFalse(grounded, reason)
        evidence = json.dumps({"ticker":"TEST", "price":100, "internal_debug":{"database_path":"/srv/example/private.duckdb"}})
        _, _, safe, reason = await self.review("TEST is $100. Its database path is /srv/example/private.duckdb.", evidence)
        self.assertFalse(safe, reason)
