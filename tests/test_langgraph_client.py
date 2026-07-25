import sys
import unittest
from pathlib import Path

from langchain.agents.middleware import TodoListMiddleware, ToolCallLimitMiddleware, ToolRetryMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph_client.client import DatabaseGroundingMiddleware, _make_middleware


class LangGraphClientTests(unittest.IsolatedAsyncioTestCase):
    def test_middleware_includes_retry_and_mutable_todos(self):
        middleware = _make_middleware()

        self.assertTrue(any(isinstance(item, ToolRetryMiddleware) for item in middleware))
        limits = [item for item in middleware if isinstance(item, ToolCallLimitMiddleware)]
        self.assertEqual(len(limits), 1)
        self.assertEqual(limits[0].run_limit, 12)
        self.assertEqual(limits[0].exit_behavior, "continue")
        self.assertTrue(any(isinstance(item, TodoListMiddleware) for item in middleware))
        self.assertTrue(any(isinstance(item, DatabaseGroundingMiddleware) for item in middleware))

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


if __name__ == "__main__":
    unittest.main()
