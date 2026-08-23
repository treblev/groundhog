import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain.agents.middleware import TodoListMiddleware, ToolCallLimitMiddleware, ToolRetryMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph_client.client import (
    DatabaseGroundingMiddleware,
    InternalDetailsMiddleware,
    _ask_routed,
    _canonical_note_tickers,
    _fallback_system_prompt,
    _fallback_tool_names,
    _make_middleware,
    _make_tools,
    _model_answer,
    _named_note_tickers,
    _prefetch_stock_notes,
    _resolve_search_domain,
    _route_question,
    _system_prompt,
)
from langgraph_client.routing import RequestFeatures, RouteDecision, RouteId


class LangGraphClientTests(unittest.IsolatedAsyncioTestCase):
    def test_ticker_search_without_domain_resolves_to_stock_notes(self):
        self.assertEqual(_resolve_search_domain(None, "BKNG"), "stock_note")
        self.assertEqual(_resolve_search_domain(None, None), "workout")
        self.assertEqual(_resolve_search_domain("stock_alert", "BKNG"), "stock_alert")

    def test_named_note_tickers_match_without_classifying_note_content(self):
        self.assertEqual(
            _named_note_tickers(
                "How much BKNG stock did I buy recently?",
                ["AAPL", "BKNG", "SHOP"],
            ),
            ["BKNG"],
        )

    def test_company_aliases_resolve_to_canonical_note_tickers(self):
        self.assertEqual(
            _canonical_note_tickers("What have I written about Bitcoin lately?", ["BTC-USD"]),
            ["BTC-USD"],
        )
        self.assertEqual(
            _canonical_note_tickers("Do I have notes on Microsoft?", ["BKNG"]),
            ["MSFT"],
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

    async def test_alias_prefetch_uses_sql_fallback_when_semantic_search_is_empty(self):
        class Session:
            def __init__(self):
                self.calls = []

            async def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                if name == "run_sql":
                    if len([call for call in self.calls if call[0] == "run_sql"]) == 1:
                        text = "ticker\n  BTC-USD"
                    else:
                        text = "id ticker note\nnote-1 BTC-USD Weekly Supertrend flipped to buy"
                else:
                    text = "[]"
                return SimpleNamespace(content=[SimpleNamespace(text=text)])

        session = Session()
        evidence = await _prefetch_stock_notes(session, "What have I written about Bitcoin lately?")

        self.assertIn("BTC-USD (sql_fallback)", evidence)
        self.assertIn("Weekly Supertrend flipped to buy", evidence)
        self.assertEqual(session.calls[1][1]["ticker"], "BTC-USD")
        self.assertIn("upper(ticker) = upper('BTC-USD')", session.calls[2][1]["query"])

    async def test_alias_with_no_active_notes_returns_explicit_empty_fallback(self):
        class Session:
            def __init__(self):
                self.calls = []

            async def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                if name == "run_sql":
                    if len([call for call in self.calls if call[0] == "run_sql"]) == 1:
                        text = "ticker\n  BKNG"
                    else:
                        text = "No results."
                else:
                    text = "[]"
                return SimpleNamespace(content=[SimpleNamespace(text=text)])

        session = Session()
        evidence = await _prefetch_stock_notes(session, "What have I written about Microsoft lately?")

        self.assertIn("MSFT (sql_fallback): No results.", evidence)
        self.assertEqual(session.calls[1][1]["ticker"], "MSFT")
        self.assertNotIn("all active stock notes", evidence)

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

    def test_routed_middleware_excludes_todo_planner(self):
        middleware = _make_middleware(include_todos=False)

        self.assertFalse(any(isinstance(item, TodoListMiddleware) for item in middleware))

    def test_fallback_context_is_compact_and_has_no_full_schema(self):
        prompt = _fallback_system_prompt(RequestFeatures(domain="stock_note", operation="list"))

        self.assertIn("stock_notes", prompt)
        self.assertIn("DESCRIBE", prompt)
        self.assertNotIn("Database schema:", prompt)
        self.assertNotIn("stock_notes(id", prompt)

    def test_fallback_tools_exclude_unrelated_domains(self):
        class Session:
            async def call_tool(self, name, arguments):
                raise AssertionError("tool should not execute during construction")

        names = {
            tool.__name__
            for tool in _make_tools(
                Session(),
                _fallback_tool_names(RequestFeatures(domain="stock_alert", operation="list")),
            )
        }

        self.assertEqual(names, {"run_sql", "query_stock_alerts", "search_documents"})

    async def test_runtime_symbols_feed_deterministic_route(self):
        class Session:
            def __init__(self):
                self.calls = []

            async def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({
                    "rows": [{"ticker": "NEWIPO"}],
                    "count": 1,
                    "truncated": False,
                    "next_cursor": None,
                }))])

        session = Session()
        features, decision, reason = await _route_question(
            session,
            "What is the latest closing price for NEWIPO?",
        )

        self.assertEqual(features.tickers, ("NEWIPO",))
        self.assertEqual(decision.route_id, RouteId.LATEST_PRICE)
        self.assertIsNone(reason)
        self.assertEqual(session.calls, [("get_stock_symbols", {})])

    async def test_routed_model_prompt_contains_evidence_but_no_schema_or_tools(self):
        class Model:
            def __init__(self):
                self.messages = None

            async def ainvoke(self, messages, config):
                self.messages = messages
                return AIMessage(content="No matching notes were found.")

        model = Model()
        await _model_answer(
            model,
            callback=None,
            question="What notes do I have for MSFT?",
            evidence='{"rows":[],"count":0}',
            instruction="Treat empty rows as authoritative.",
        )
        prompt = "\n".join(str(message.content) for message in model.messages)

        self.assertIn('{"rows":[],"count":0}', prompt)
        self.assertNotIn("Database schema:", prompt)
        self.assertNotIn("run_sql", prompt)
        self.assertNotIn("search_documents", prompt)

    async def test_confident_route_executes_only_its_selected_mcp_tool(self):
        class Session:
            def __init__(self):
                self.calls = []

            async def call_tool(self, name, arguments):
                self.calls.append((name, arguments))
                return SimpleNamespace(content=[SimpleNamespace(text='{"rows":[],"count":0}')])

        class Model:
            async def ainvoke(self, messages, config):
                return AIMessage(content="No matching notes were found.")

        session = Session()
        decision = RouteDecision(
            route_id=RouteId.STOCK_NOTE_EXACT,
            tool="query_stock_notes",
            arguments={"tickers": ["MSFT"], "active_only": True},
            answer_instruction="Treat empty rows as authoritative.",
        )
        with (
            patch("langgraph_client.client.ChatOllama", return_value=Model()),
            patch(
                "langgraph_client.client._apply_routed_guards",
                new=AsyncMock(side_effect=lambda _model, _callback, _question, _evidence, answer: answer),
            ),
        ):
            answer = await _ask_routed(session, "What notes do I have for MSFT?", decision, None)

        self.assertEqual(answer, "No matching notes were found.")
        self.assertEqual(session.calls, [
            ("query_stock_notes", {"tickers": ["MSFT"], "active_only": True})
        ])

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
