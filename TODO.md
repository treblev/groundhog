# Groundhog — TODO

- `langgraph_client/client.py`: add `ToolRetryMiddleware` (from `langchain.agents.middleware`) to catch malformed tool calls — `qwen3:32b` occasionally emits a tool call as literal text content (e.g. `{"name": "get_health_summary", "arguments": {}}`) instead of a structured `tool_calls` entry, and nothing currently catches it, so the agent silently returns the raw text as its final answer instead of retrying.
