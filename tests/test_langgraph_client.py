import json
import sys
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain.agents.middleware import TodoListMiddleware, ToolCallLimitMiddleware, ToolRetryMiddleware
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph_client.client import (
    AnswerReviewMiddleware,
    _make_reviewer_model,
    _review_date_context,
    _ask_routed,
    _canonical_note_tickers,
    _fallback_system_prompt,
    _fallback_tool_names,
    _make_middleware,
    _make_tools,
    _model_answer,
    _named_note_tickers,
    _prefetch_stock_notes,
    _apply_routed_guards,
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
        self.assertEqual(sum(isinstance(item, AnswerReviewMiddleware) for item in middleware), 1)
        self.assertEqual(len(middleware), 4)

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
            (getattr(tool, "name", None) or tool.__name__)
            for tool in _make_tools(
                Session(),
                _fallback_tool_names(RequestFeatures(domain="stock_alert", operation="list")),
            )
        }

        self.assertTrue({"query_stock_alerts", "summarize_stock_alerts", "query_stock_signals", "query_stock_prices", "compare_stock_performance", "get_stock_overview", "get_stock_alert_outcomes", "get_stock_data_coverage"}.issubset(names))
        self.assertNotIn("get_sleep_summary", names)
        self.assertNotIn("get_activity_summary", names)

    async def test_stock_question_leaves_tool_choice_to_llm_without_discovery_call(self):
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

        self.assertEqual(features.domain, "stocks")
        self.assertIsNone(decision)
        self.assertEqual(reason, "stock_tools_selected_by_model")
        self.assertEqual(session.calls, [])

    async def test_stock_structured_tool_binding_forwards_relative_period(self):
        class Session:
            call_tool = AsyncMock(return_value=SimpleNamespace(content=[SimpleNamespace(text='{"rows":[]}')]))
        session = Session()
        tools = _make_tools(session, {"query_stock_alerts", "query_stock_prices"})
        by_name = {tool.name: tool for tool in tools}
        await by_name["query_stock_alerts"].ainvoke({"alert_type": "death_cross", "period": "2w"})
        await by_name["query_stock_prices"].ainvoke({"tickers": ["TEST"]})
        self.assertEqual(session.call_tool.call_args_list[0].args, ("query_stock_alerts", {"alert_type": "death_cross", "period": "2w"}))
        self.assertEqual(session.call_tool.call_args_list[1].args, ("query_stock_prices", {"tickers": ["TEST"]}))

    async def test_stock_questions_do_not_choose_a_python_operation_route(self):
        session = SimpleNamespace(call_tool=AsyncMock())
        for question in (
            "List all stocks that have gotten into death cross last 2 weeks?",
            "Which weekly Supertrend buy alerts are recorded?",
            "What stock notes do I have for MSFT?",
            "Give me the weekly market summary",
        ):
            with self.subTest(question=question):
                features, decision, reason = await _route_question(session, question)
                self.assertEqual(features.domain, "stocks")
                self.assertIsNone(decision)
                self.assertEqual(reason, "stock_tools_selected_by_model")
        session.call_tool.assert_not_awaited()

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
        guard = AnswerReviewMiddleware(prefetched_evidence="BKNG: bought 4 shares")
        self.assertEqual(guard._tool_evidence([]), "BKNG: bought 4 shares")

    async def test_routed_guards_use_one_combined_reviewer_on_success(self):
        class Reviewer:
            def __init__(self):
                self.calls = 0

            async def ainvoke(self, messages, config=None):
                self.calls += 1
                prompt = str(messages[-1].content)
                assert "grounded" in prompt
                assert "safe" in prompt
                return AIMessage(content=(
                    '{"grounded": true, "safe": true, '
                    '"grounding_reason": "Supported.", "safety_reason": "User-facing."}'
                ))

        reviewer = Reviewer()
        answer = AIMessage(content="No matching record was found.")
        with patch("langgraph_client.client.ChatOllama", return_value=reviewer):
            result = await _apply_routed_guards(
                model=None,
                callback=None,
                question="What notes do I have for MSFT?",
                evidence='{"rows": [], "count": 0}',
                answer=answer,
            )

        self.assertEqual(result.content, answer.content)
        self.assertEqual(reviewer.calls, 1)

    def test_all_reviewers_disable_thinking_and_bound_json_output(self):
        with patch("langgraph_client.client.ChatOllama") as factory:
            reviewer = _make_reviewer_model()
            guard = AnswerReviewMiddleware()
        self.assertIs(reviewer, factory.return_value)
        self.assertIs(guard.verifier_model, reviewer)
        self.assertEqual(factory.call_count, 2)
        for call in factory.call_args_list:
            self.assertIs(call.kwargs["reasoning"], False)
            schema = call.kwargs["format"]
            self.assertEqual(set(schema["required"]), {"grounded", "safe", "grounding_reason", "safety_reason"})
            self.assertEqual(schema["properties"]["safe"], {"type": "boolean"})
            self.assertEqual(schema["properties"]["grounding_reason"]["maxLength"], 96)
            self.assertIs(schema["additionalProperties"], False)
            self.assertEqual(call.kwargs["num_predict"], 128)

    def test_review_date_checks_handle_rolling_window_and_boundaries_in_python(self):
        context = _review_date_context(
            "List records from the last 2 weeks", "2026-10-10 2026-10-11 2026-10-12 2026-10-25 2026-10-26",
            date(2026, 10, 25),
        )
        self.assertIn("2026-10-11 through 2026-10-25", context)
        for day in ("2026-10-11", "2026-10-12", "2026-10-25"):
            self.assertIn(f"{day} = IN RANGE", context)
        for day in ("2026-10-10", "2026-10-26"):
            self.assertIn(f"{day} = OUT OF RANGE", context)

    def test_review_date_context_supports_days_and_does_not_guess_other_windows(self):
        context = _review_date_context("past 3 days", "2026-10-23 2026-13-23", date(2026, 10, 25))
        self.assertIn("2026-10-22 through 2026-10-25", context)
        self.assertIn("2026-13-23 = INVALID DATE", context)
        context = _review_date_context("last month", "2026-10-23", date(2026, 10, 25))
        self.assertNotIn("Requested date bounds", context)

    async def test_fallback_checks_both_properties_once_and_preserves_trace_callback(self):
        reviewer = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content=(
            '{"grounded": true, "safe": true, "grounding_reason": "Matches.", "safety_reason": "User-facing."}'
        ))))
        callback = object()
        guard = AnswerReviewMiddleware(verifier_model=reviewer, trace_callback=callback)
        update = await guard.aafter_model({"messages": [
            HumanMessage(content="List recorded alerts"),
            ToolMessage(content='{"rows": [], "count": 0}', tool_call_id="alerts"),
            AIMessage(content="No recorded alerts."),
        ]}, runtime=None)
        self.assertIsNone(update)
        reviewer.ainvoke.assert_awaited_once()
        messages = reviewer.ainvoke.call_args.args[0]
        self.assertIn('"grounded"', messages[-1].content)
        self.assertIn('"safe"', messages[-1].content)
        self.assertIn('"rows": []', messages[-1].content)
        self.assertIn("Reference date (America/Phoenix):", messages[-1].content)
        self.assertEqual(reviewer.ainvoke.call_args.kwargs["config"]["callbacks"], [callback])

    async def test_agent_graph_runs_one_review_then_one_recheck_after_repair(self):
        reviewer = SimpleNamespace(ainvoke=AsyncMock(side_effect=[
            AIMessage(content='{"grounded": false, "safe": true, "grounding_reason": "No such recorded alert."}'),
            AIMessage(content='{"grounded": true, "safe": true}'),
        ]))
        model = FakeMessagesListChatModel(responses=[
            AIMessage(content="There was an alert."), AIMessage(content="No alert was recorded."),
        ])
        with patch("langgraph_client.client._make_reviewer_model", return_value=reviewer):
            agent = create_agent(model=model, middleware=_make_middleware(
                prefetched_evidence='{"rows": [], "count": 0}', include_todos=False,
            ))
            result = await agent.ainvoke({"messages": [HumanMessage(content="Were any alerts recorded?")]})
        self.assertEqual(result["messages"][-1].content, "No alert was recorded.")
        self.assertEqual(reviewer.ainvoke.await_count, 2)
        self.assertEqual(sum(isinstance(m, AIMessage) for m in result["messages"]), 2)

    async def test_fallback_skips_review_during_tool_selection(self):
        reviewer = SimpleNamespace(ainvoke=AsyncMock())
        guard = AnswerReviewMiddleware(verifier_model=reviewer)
        result = await guard.aafter_model({"messages": [
            HumanMessage(content="List alerts"),
            AIMessage(content="", tool_calls=[{"name": "query_stock_alerts", "args": {}, "id": "alerts"}]),
        ]}, runtime=None)
        self.assertIsNone(result)
        reviewer.ainvoke.assert_not_awaited()

    async def test_fallback_malformed_review_or_model_error_requests_repair_then_declines(self):
        for response in ("not json", '{"grounded": true}', '{"grounded": "true", "safe": true}', RuntimeError("offline")):
            with self.subTest(response=response):
                invoke = AsyncMock(side_effect=response) if isinstance(response, Exception) else AsyncMock(return_value=AIMessage(content=response))
                guard = AnswerReviewMiddleware(verifier_model=SimpleNamespace(ainvoke=invoke))
                messages = [HumanMessage(content="List alerts"), ToolMessage(content='{"rows": []}', tool_call_id="alerts"), AIMessage(content="No alerts.")]
                first = await guard.aafter_model({"messages": messages}, runtime=None)
                self.assertIsInstance(first, Command)
                second = await guard.aafter_model({"messages": messages[:-1] + first.update["messages"] + [AIMessage(content="Still no alerts.")]}, runtime=None)
                self.assertEqual(second["messages"][0].content, guard._GROUNDING_DECLINE_MESSAGE)
                self.assertEqual(invoke.await_count, 2)

    async def test_fallback_has_one_repair_budget_for_both_checks(self):
        reviewer = SimpleNamespace(ainvoke=AsyncMock(side_effect=[
            AIMessage(content='{"grounded": false, "safe": true, "grounding_reason": "Unsupported."}'),
            AIMessage(content='{"grounded": true, "safe": false, "safety_reason": "Internal details."}'),
        ]))
        guard = AnswerReviewMiddleware(verifier_model=reviewer)
        messages = [HumanMessage(content="List alerts"), ToolMessage(content='{"rows": []}', tool_call_id="alerts"), AIMessage(content="Unsupported answer.")]
        first = await guard.aafter_model({"messages": messages}, runtime=None)
        second = await guard.aafter_model({"messages": messages[:-1] + first.update["messages"] + [AIMessage(content="Corrected but internal.")]}, runtime=None)
        self.assertEqual(second["messages"][0].content, guard._DISCLOSURE_DECLINE_MESSAGE)
        self.assertEqual(reviewer.ainvoke.await_count, 2)

    async def test_fallback_accepts_repaired_answer_after_one_shared_recheck(self):
        reviewer = SimpleNamespace(ainvoke=AsyncMock(side_effect=[
            AIMessage(content='{"grounded": false, "safe": false, "grounding_reason": "Unsupported.", "safety_reason": "Internal."}'),
            AIMessage(content='{"grounded": true, "safe": true}'),
        ]))
        guard = AnswerReviewMiddleware(verifier_model=reviewer)
        messages = [HumanMessage(content="List alerts"), ToolMessage(content='{"rows": []}', tool_call_id="alerts"), AIMessage(content="Unsupported internal answer.")]
        first = await guard.aafter_model({"messages": messages}, runtime=None)
        self.assertIn("grounding: Unsupported.", first.update["messages"][0].content)
        self.assertIn("disclosure: Internal.", first.update["messages"][0].content)
        second = await guard.aafter_model({"messages": messages[:-1] + first.update["messages"] + [AIMessage(content="No alerts.")]}, runtime=None)
        self.assertIsNone(second)
        self.assertEqual(reviewer.ainvoke.await_count, 2)

    async def test_routed_guards_repair_then_recheck_with_combined_reviewer(self):
        class Reviewer:
            def __init__(self):
                self.calls = 0

            async def ainvoke(self, messages, config=None):
                self.calls += 1
                verdict = (
                    '{"grounded": false, "safe": true, '
                    '"grounding_reason": "Unsupported claim.", "safety_reason": "User-facing."}'
                    if self.calls == 1 else
                    '{"grounded": true, "safe": true, '
                    '"grounding_reason": "Supported.", "safety_reason": "User-facing."}'
                )
                return AIMessage(content=verdict)

        class AnswerModel:
            def __init__(self):
                self.calls = 0

            async def ainvoke(self, messages, config=None):
                self.calls += 1
                return AIMessage(content="Corrected answer.")

        reviewer = Reviewer()
        answer_model = AnswerModel()
        answer = AIMessage(content="Unsupported answer.")
        with patch("langgraph_client.client.ChatOllama", return_value=reviewer):
            result = await _apply_routed_guards(
                model=answer_model,
                callback=None,
                question="What notes do I have for MSFT?",
                evidence='{"rows": [], "count": 0}',
                answer=answer,
            )

        self.assertEqual(result.content, "Corrected answer.")
        self.assertEqual(reviewer.calls, 2)
        self.assertEqual(answer_model.calls, 1)

    async def test_grounding_guard_requests_a_corrective_retry_without_tool_result(self):
        guard = AnswerReviewMiddleware()
        update = await guard.aafter_model(
            {"messages": [HumanMessage(content="What was my latest resting heart rate?"), AIMessage(content="54 bpm")]},
            runtime=None,
        )

        self.assertIsInstance(update, Command)
        self.assertEqual(update.goto, "model")
        self.assertIn(guard._CORRECTION_MARKER, update.update["messages"][0].content)

    async def test_grounding_guard_declines_after_one_failed_retry(self):
        guard = AnswerReviewMiddleware()
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

        self.assertEqual(update["messages"][0].content, guard._GROUNDING_DECLINE_MESSAGE)

    async def test_grounding_guard_allows_verifier_approved_answer(self):
        class ApprovedVerifier:
            async def ainvoke(self, messages):
                return AIMessage(content='{"grounded": true, "safe": true, "grounding_reason": "The result states 54.", "safety_reason": "User-facing."}')

        guard = AnswerReviewMiddleware(verifier_model=ApprovedVerifier())
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
                return AIMessage(content='{"grounded": true, "safe": false, "safety_reason": "It exposes an internal path."}')

        guard = AnswerReviewMiddleware(verifier_model=UnsafeVerifier())
        update = await guard.aafter_model(
            {
                "messages": [
                    HumanMessage(content="Where is my latest activity?"),
                    ToolMessage(content="Activity was imported.", tool_call_id="activity-1"),
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
                return AIMessage(content='{"grounded": true, "safe": false, "safety_reason": "Still internal."}')

        guard = AnswerReviewMiddleware(verifier_model=UnsafeVerifier())
        update = await guard.aafter_model(
            {
                "messages": [
                    HumanMessage(content="Where is my latest activity?"),
                    HumanMessage(content=f"{guard._CORRECTION_MARKER} rewrite safely"),
                    ToolMessage(content="Activity was imported.", tool_call_id="activity-1"),
                    AIMessage(content="It is stored in an internal database path."),
                ]
            },
            runtime=None,
        )

        self.assertEqual(update["messages"][0].content, guard._DISCLOSURE_DECLINE_MESSAGE)


if __name__ == "__main__":
    unittest.main()
