import sys
import unittest
from pathlib import Path

from langchain.agents.middleware import TodoListMiddleware, ToolCallLimitMiddleware, ToolRetryMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph_client.client import DatabaseGroundingMiddleware, _make_middleware


class LangGraphClientTests(unittest.TestCase):
    def test_middleware_includes_retry_and_mutable_todos(self):
        middleware = _make_middleware()

        self.assertTrue(any(isinstance(item, ToolRetryMiddleware) for item in middleware))
        limits = [item for item in middleware if isinstance(item, ToolCallLimitMiddleware)]
        self.assertEqual(len(limits), 1)
        self.assertEqual(limits[0].run_limit, 12)
        self.assertEqual(limits[0].exit_behavior, "continue")
        self.assertTrue(any(isinstance(item, TodoListMiddleware) for item in middleware))
        self.assertTrue(any(isinstance(item, DatabaseGroundingMiddleware) for item in middleware))

    def test_grounding_guard_declines_final_answer_without_tool_result(self):
        guard = DatabaseGroundingMiddleware()
        update = guard.after_model(
            {"messages": [HumanMessage(content="What was my latest resting heart rate?"), AIMessage(content="54 bpm")]},
            runtime=None,
        )

        self.assertEqual(update["messages"][0].content, guard._DECLINE_MESSAGE)

    def test_grounding_guard_allows_answer_backed_by_tool_result(self):
        guard = DatabaseGroundingMiddleware()
        update = guard.after_model(
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
