import asyncio
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph_client import client as langgraph_client
from langgraph_client.client import ask_question
from scripts import ask_groundhog


class AskGroundhogTests(unittest.TestCase):
    def test_empty_question_is_rejected_without_starting_agent(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(langgraph_client, "REQUEST_TRACE_DIR", Path(directory)):
            with self.assertRaises(ValueError):
                asyncio.run(ask_question("  "))

    def test_cli_prints_only_agent_answer(self):
        stdout = io.StringIO()
        with (
            patch.object(ask_groundhog, "ask_question", new=AsyncMock(return_value="Your latest run was 2.27 miles.")),
            patch.object(sys, "argv", ["ask_groundhog.py", "latest", "run"]),
            redirect_stdout(stdout),
        ):
            exit_code = asyncio.run(ask_groundhog.main())

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "Your latest run was 2.27 miles.\n")


if __name__ == "__main__":
    unittest.main()
