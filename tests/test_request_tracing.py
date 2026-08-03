import asyncio
import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stderr

from langgraph_client import client
from langgraph_client.request_tracing import RequestTrace, read_events
import groundhog_service


class RequestTraceTests(unittest.TestCase):
    def test_events_are_ordered_and_payloads_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = RequestTrace(Path(directory))
            trace.record("request.started", "ask_question", payload={"question": "latest run?"})
            trace.record("tool.completed", "get_recent_activities", outcome="succeeded", payload={"result": "2.3 miles"})
            events = read_events(Path(directory))
        self.assertEqual([event["sequence"] for event in events], [1, 2])
        self.assertEqual(events[0]["payload"]["question"], "latest run?")
        self.assertEqual(events[1]["payload"]["result"], "2.3 miles")

    def test_old_daily_files_are_pruned(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            old_date = datetime.now(timezone.utc).date() - timedelta(days=31)
            old_path = path / f"{old_date.isoformat()}.jsonl"
            path.mkdir(exist_ok=True)
            old_path.write_text('{"old":true}\n', encoding="utf-8")
            RequestTrace(path, retention_days=30).record("request.started", "ask_question")
            self.assertFalse(old_path.exists())

    def test_blank_question_records_rejection(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(client, "REQUEST_TRACE_DIR", Path(directory)):
            with self.assertRaises(ValueError):
                asyncio.run(client.ask_question("  "))
            events = read_events(Path(directory))
        self.assertEqual([event["event_type"] for event in events], ["request.started", "request.rejected"])
        self.assertEqual(events[-1]["outcome"], "rejected")

    def test_trace_write_failure_never_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = RequestTrace(Path(directory))
            with patch.object(trace, "_append", side_effect=OSError("disk unavailable")):
                trace.record("request.started", "ask_question")
        self.assertEqual(trace.sequence, 1)

    def test_cli_lists_and_reconstructs_traces(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(groundhog_service, "REQUEST_TRACE_DIR", Path(directory)):
            trace = RequestTrace(Path(directory))
            trace.record("request.started", "ask_question", payload={"question": "latest sleep?"})
            trace.record("request.completed", "ask_question", outcome="succeeded", duration_ms=12, payload={"answer": "8 hours"})
            listed = groundhog_service.list_request_traces(status="succeeded")
            shown = groundhog_service.show_request_trace(trace.trace_id)
        self.assertEqual(listed[0]["trace_id"], trace.trace_id)
        self.assertEqual(listed[0]["question"], "latest sleep?")
        self.assertEqual(len(shown), 2)

    def test_cli_unknown_trace_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(groundhog_service, "REQUEST_TRACE_DIR", Path(directory)):
            with redirect_stderr(io.StringIO()):
                self.assertEqual(groundhog_service.main(["traces", "show", "missing"]), 2)


if __name__ == "__main__":
    unittest.main()
