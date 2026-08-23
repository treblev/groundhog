import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import request_trace
from agent.langchain_trace import GroundhogTraceCallback
from ingestion import health


class RequestTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.trace_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _records(self):
        paths = list(self.trace_dir.glob("*.jsonl"))
        self.assertEqual(len(paths), 1)
        return [json.loads(line) for line in paths[0].read_text().splitlines()]

    def test_request_encloses_combined_call_spans(self):
        trace = request_trace.RequestTrace(
            "ask", "telegram", request_id="request-1", trace_dir=self.trace_dir
        ).start(question="How far did I run?")
        with request_trace.use_trace(trace):
            started_at = datetime.now(timezone.utc)
            monotonic_started = time.monotonic()
            request_trace.record_llm_call(
                started_at=started_at,
                monotonic_started=monotonic_started,
                model="qwen",
                prompt="choose a tool",
                response="tool call",
            )
            request_trace.record_tool_call(
                started_at=started_at,
                monotonic_started=monotonic_started,
                tool="get_recent_activities",
                arguments={"limit": 5},
                result="3.1 miles",
            )
        trace.end("passed")

        records = self._records()
        self.assertEqual([record["type"] for record in records], [
            "request_start", "llm_call", "tool_call", "request_end"
        ])
        self.assertTrue(all(record["request_id"] == "request-1" for record in records))
        self.assertIn("started_at", records[1])
        self.assertIn("duration_ms", records[1])
        self.assertNotIn("completed_at", records[1])
        self.assertEqual(records[-1]["status"], "passed")

    def test_failed_request_and_call_record_errors(self):
        trace = request_trace.RequestTrace(
            "activity_import", "telegram", trace_dir=self.trace_dir
        ).start()
        with request_trace.use_trace(trace):
            error = TimeoutError("model timed out")
            request_trace.record_llm_call(
                started_at=datetime.now(timezone.utc),
                monotonic_started=time.monotonic(),
                model="qwen-vl",
                prompt="extract",
                error=error,
            )
        trace.end("failed", "model timed out")

        records = self._records()
        self.assertEqual(records[1]["status"], "failed")
        self.assertEqual(records[-1]["status"], "failed")
        self.assertEqual(records[-1]["error"], "model timed out")

    def test_bytes_are_hashed_instead_of_embedded(self):
        trace = request_trace.RequestTrace("activity", "test", trace_dir=self.trace_dir).start()
        with request_trace.use_trace(trace):
            request_trace.record_tool_call(
                started_at=datetime.now(timezone.utc),
                monotonic_started=time.monotonic(),
                tool="upload",
                arguments={"image": b"image bytes"},
                result="ok",
            )
        trace.end("passed")

        image = self._records()[1]["arguments"]["image"]
        self.assertEqual(image["byte_count"], 11)
        self.assertIn("sha256", image)

    def test_activity_vision_call_records_prompt_response_and_duration(self):
        image_path = self.trace_dir / "activity.jpg"
        image_path.write_bytes(b"image")
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"message": {"content": '[{"type":"activity"}]'}}
        trace = request_trace.RequestTrace(
            "activity_import", "telegram", trace_dir=self.trace_dir
        ).start()

        with request_trace.use_trace(trace), patch.object(health.httpx, "post", return_value=response):
            self.assertEqual(health._query_ollama(image_path), '[{"type":"activity"}]')
        trace.end("passed")

        call = self._records()[1]
        self.assertEqual(call["type"], "llm_call")
        self.assertEqual(call["model"], health.OLLAMA_VISION_MODEL)
        self.assertEqual(call["prompt"], health.PROMPT)
        self.assertEqual(call["response"], '[{"type":"activity"}]')
        self.assertIn("duration_ms", call)

    def test_langchain_start_end_hooks_become_single_spans(self):
        import asyncio

        trace = request_trace.RequestTrace("ask", "telegram", trace_dir=self.trace_dir).start()
        callback = GroundhogTraceCallback()
        llm_id = uuid4()
        tool_id = uuid4()

        async def exercise():
            await callback.on_chat_model_start(
                {"name": "ChatOllama"}, [["prompt"]], run_id=llm_id,
                metadata={"ls_model_name": "qwen3.6:latest"},
            )
            await callback.on_llm_end({
                "generations": [[{
                    "message": {
                        "response_metadata": {
                            "prompt_eval_count": 321,
                            "prompt_eval_duration": 456000000,
                        }
                    }
                }]],
            }, run_id=llm_id)
            await callback.on_tool_start(
                {"name": "run_sql"}, "query", run_id=tool_id,
                inputs={"query": "SELECT 1"},
            )
            await callback.on_tool_end("1", run_id=tool_id)

        with request_trace.use_trace(trace):
            asyncio.run(exercise())
        trace.end("passed")

        records = self._records()
        self.assertEqual([record["type"] for record in records], [
            "request_start", "llm_call", "tool_call", "request_end"
        ])
        self.assertEqual(records[1]["model"], "qwen3.6:latest")
        self.assertEqual(records[2]["tool"], "run_sql")
        self.assertEqual(records[2]["arguments"], {"query": "SELECT 1"})
        self.assertEqual(records[1]["prompt_eval_count"], 321)
        self.assertEqual(records[1]["prompt_eval_duration_ns"], 456000000)
        self.assertEqual(records[-1]["metadata"]["trace_summary"]["llm_calls"], 1)
        self.assertEqual(records[-1]["metadata"]["trace_summary"]["tool_calls"], 1)
        self.assertEqual(records[-1]["metadata"]["trace_summary"]["prompt_eval_count"], 321)
        self.assertEqual(records[-1]["metadata"]["trace_summary"]["prompt_eval_duration_ms"], 456.0)


if __name__ == "__main__":
    unittest.main()
