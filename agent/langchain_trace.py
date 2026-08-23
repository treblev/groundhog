"""LangChain callbacks that collapse start/end hooks into Groundhog call spans."""
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler

from agent.request_trace import current_trace


def _name(serialized: dict, metadata: dict | None = None) -> str:
    metadata = metadata or {}
    return str(
        metadata.get("ls_model_name")
        or serialized.get("name")
        or serialized.get("id", ["unknown"])[-1]
    )


def _response_metrics(response: Any) -> dict[str, int]:
    """Promote Ollama prompt metrics from nested LangChain response metadata."""
    payload = response.model_dump() if hasattr(response, "model_dump") else response
    wanted = {
        "prompt_eval_count": "prompt_eval_count",
        "prompt_eval_duration": "prompt_eval_duration_ns",
    }
    found: dict[str, int] = {}

    def visit(value: Any) -> None:
        if len(found) == len(wanted):
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if key in wanted and wanted[key] not in found:
                    try:
                        found[wanted[key]] = int(item)
                    except (TypeError, ValueError):
                        pass
                else:
                    visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(payload)
    return found


class GroundhogTraceCallback(AsyncCallbackHandler):
    """Record one completed JSONL span for each LangChain LLM or tool run."""

    def __init__(self):
        self._llm: dict[UUID, dict[str, Any]] = {}
        self._tools: dict[UUID, dict[str, Any]] = {}

    async def on_llm_start(
        self, serialized, prompts, *, run_id, parent_run_id=None, metadata=None, **kwargs
    ):
        self._llm.setdefault(run_id, {
            "started_at": datetime.now(timezone.utc),
            "monotonic_started": time.monotonic(),
            "model": _name(serialized, metadata),
            "prompt": prompts,
        })

    async def on_chat_model_start(
        self, serialized, messages, *, run_id, parent_run_id=None, metadata=None, **kwargs
    ):
        self._llm.setdefault(run_id, {
            "started_at": datetime.now(timezone.utc),
            "monotonic_started": time.monotonic(),
            "model": _name(serialized, metadata),
            "prompt": messages,
        })

    async def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs):
        call = self._llm.pop(run_id, None)
        trace = current_trace()
        if call is not None and trace is not None:
            metrics = _response_metrics(response)
            trace.record_call(
                "llm_call",
                call["started_at"],
                int((time.monotonic() - call["monotonic_started"]) * 1000),
                "passed",
                model=call["model"],
                prompt=call["prompt"],
                response=response,
                error=None,
                prompt_chars=len(str(call["prompt"])),
                **metrics,
            )

    async def on_llm_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        call = self._llm.pop(run_id, None)
        trace = current_trace()
        if call is not None and trace is not None:
            trace.record_call(
                "llm_call",
                call["started_at"],
                int((time.monotonic() - call["monotonic_started"]) * 1000),
                "failed",
                model=call["model"],
                prompt=call["prompt"],
                response=None,
                error=str(error),
                prompt_chars=len(str(call["prompt"])),
            )

    async def on_tool_start(
        self,
        serialized,
        input_str,
        *,
        run_id,
        parent_run_id=None,
        metadata=None,
        inputs=None,
        **kwargs,
    ):
        self._tools[run_id] = {
            "started_at": datetime.now(timezone.utc),
            "monotonic_started": time.monotonic(),
            "tool": _name(serialized, metadata),
            "arguments": inputs if inputs is not None else input_str,
        }

    async def on_tool_end(self, output, *, run_id, parent_run_id=None, **kwargs):
        call = self._tools.pop(run_id, None)
        trace = current_trace()
        if call is not None and trace is not None:
            trace.record_call(
                "tool_call",
                call["started_at"],
                int((time.monotonic() - call["monotonic_started"]) * 1000),
                "passed",
                tool=call["tool"],
                arguments=call["arguments"],
                result=output,
                error=None,
            )

    async def on_tool_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        call = self._tools.pop(run_id, None)
        trace = current_trace()
        if call is not None and trace is not None:
            trace.record_call(
                "tool_call",
                call["started_at"],
                int((time.monotonic() - call["monotonic_started"]) * 1000),
                "failed",
                tool=call["tool"],
                arguments=call["arguments"],
                result=None,
                error=str(error),
            )
