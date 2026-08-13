import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from langchain.agents.middleware import TodoListMiddleware, ToolCallLimitMiddleware, ToolRetryMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph_client.client import (
    DatabaseGroundingMiddleware,
    InternalDetailsMiddleware,
    _make_middleware,
    _named_note_tickers,
    _prefetch_stock_notes,
    _system_prompt,
)


class LangGraphClientTests(unittest.IsolatedAsyncioTestCase):
    def test_named_note_tickers_match_without_classifying_note_content(self):
        self.assertEqual(
            _named_note_tickers(
                "How much BKNG stock did I buy recently?",
                ["AAPL", "BKNG", "SHOP"],
            ),
            ["BKNG"],
        )

    async def test_named_ticker_prefetches_all_stock_note_content(self):
        class Session:
            def __init__(self):
                self.calls = []

            async def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                if name == "run_sql":
                    text = "ticker\n  AAPL\n  BKNG"
                else:
                    text = '[{"ticker":"BKNG","note":"bought 4 shares in Roth IRA for $214"}]'
                return SimpleNamespace(content=[SimpleNamespace(text=text)])

        session = Session()
        evidence = await _prefetch_stock_notes(
            session,
            "How much BKNG stock did I buy recently?",
        )

        self.assertIn("bought 4 shares", evidence)
        self.assertEqual(session.calls[1][0], "search_documents")
        self.assertEqual(session.calls[1][1]["domain"], "stock_note")
        self.assertEqual(session.calls[1][1]["ticker"], "BKNG")

    async def test_broad_stock_question_prefetches_notes_without_content_classification(self):
        class Session:
            def __init__(self):
                self.calls = []

            async def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                text = "ticker\n  BKNG" if name == "run_sql" else '[{"ticker":"BKNG"}]'
                return SimpleNamespace(content=[SimpleNamespace(text=text)])

        session = Session()
        await _prefetch_stock_notes(session, "What stocks did I mention recently?")

        self.assertEqual(session.calls[1][0], "search_documents")
        self.assertEqual(session.calls[1][1]["domain"], "stock_note")
        self.assertNotIn("ticker", session.calls[1][1])

    def test_stock_note_context_is_authoritative_prompt_evidence(self):
        prompt = _system_prompt(
            "stock_notes(id, ticker, note)",
            'BKNG: [{"note":"bought 4 shares"}]',
        )

        self.assertIn("Never classify them by content", prompt)
        self.assertIn("bought 4 shares", prompt)

    def test_middleware_includes_retry_and_mutable_todos(self):
        middleware = _make_middleware()

        self.assertTrue(any(isinstance(item, ToolRetryMiddleware) for item in middleware))
        limits = [item for item in middleware if isinstance(item, ToolCallLimitMiddleware)]
        self.assertEqual(len(limits), 1)
        self.assertEqual(limits[0].run_limit, 12)
        self.assertEqual(limits[0].exit_behavior, "continue")
        self.assertTrue(any(isinstance(item, TodoListMiddleware) for item in middleware))
        self.assertTrue(any(isinstance(item, DatabaseGroundingMiddleware) for item in middleware))
        self.assertTrue(any(isinstance(item, InternalDetailsMiddleware) for item in middleware))

    def test_grounding_guard_includes_prefetched_note_evidence(self):
        guard = DatabaseGroundingMiddleware(prefetched_evidence="BKNG: bought 4 shares")
        self.assertEqual(guard._tool_evidence([]), "BKNG: bought 4 shares")

    async def test_grounding_guard_requests_a_corrective_retry_without_tool_result(self):
        guard = DatabaseGroundingMiddleware()
        update = await guard.aafter_model(
            {"messages": [HumanMessage(content="What was my latest resting heart rate?"), AIMessage(content="54 bpm")]},
            runtime=None,
        )

        self.assertIsInstance(update, Command)
        self.assertEqual(update.goto, "model")
        self.assertIn(guard._CORRECTION_MARKER, update.update["messages"][0].content)

    async def test_grounding_guard_declines_after_one_failed_retry(self):
        guard = DatabaseGroundingMiddleware()
        update = await guard.aafter_model(
            {
                "messages": [
                    HumanMessage(content="What was my latest resting heart rate?"),
                    HumanMessage(content=f"{guard._CORRECTION_MARKER} retrieve evidence"),
                    AIMessage(content="54 bpm"),
                ]
            },
            runtime=None,
        )

        self.assertEqual(update["messages"][0].content, guard._DECLINE_MESSAGE)

    async def test_grounding_guard_allows_verifier_approved_answer(self):
        class ApprovedVerifier:
            async def ainvoke(self, messages):
                return AIMessage(content='{"grounded": true, "reason": "The result states 54."}')

        guard = DatabaseGroundingMiddleware(verifier_model=ApprovedVerifier())
        update = await guard.aafter_model(
            {
                "messages": [
                    HumanMessage(content="What was my latest resting heart rate?"),
                    ToolMessage(content="resting_hr\n54", tool_call_id="call-1"),
                    AIMessage(content="Your latest resting heart rate was 54 bpm."),
                ]
            },
            runtime=None,
        )

        self.assertIsNone(update)

    async def test_internal_details_guard_requests_a_safe_rewrite(self):
        class UnsafeVerifier:
            async def ainvoke(self, messages):
                return AIMessage(content='{"safe": false, "reason": "It exposes an internal path."}')

        guard = InternalDetailsMiddleware(verifier_model=UnsafeVerifier())
        update = await guard.aafter_model(
            {
                "messages": [
                    HumanMessage(content="Where is my latest activity?"),
                    AIMessage(content="It is stored in an internal database path."),
                ]
            },
            runtime=None,
        )

        self.assertIsInstance(update, Command)
        self.assertEqual(update.goto, "model")
        self.assertIn(guard._CORRECTION_MARKER, update.update["messages"][0].content)

    async def test_internal_details_guard_declines_after_failed_rewrite(self):
        class UnsafeVerifier:
            async def ainvoke(self, messages):
                return AIMessage(content='{"safe": false, "reason": "Still internal."}')

        guard = InternalDetailsMiddleware(verifier_model=UnsafeVerifier())
        update = await guard.aafter_model(
            {
                "messages": [
                    HumanMessage(content="Where is my latest activity?"),
                    HumanMessage(content=f"{guard._CORRECTION_MARKER} rewrite safely"),
                    AIMessage(content="It is stored in an internal database path."),
                ]
            },
            runtime=None,
        )

        self.assertEqual(update["messages"][0].content, guard._DECLINE_MESSAGE)


if __name__ == "__main__":
    unittest.main()
