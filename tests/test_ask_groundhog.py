import asyncio
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph_client import client
from langgraph_client.client import ask_question
from scripts import ask_groundhog


class AskGroundhogTests(unittest.TestCase):
    def test_mcp_subprocess_receives_groundhog_database_environment(self):
        with (
            patch.dict(os.environ, {"GROUNDHOG_DB_PATH": "/tmp/groundhog-test.duckdb"}),
            patch.object(client, "stdio_client", side_effect=RuntimeError("stop after parameters")) as stdio,
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after parameters"):
                asyncio.run(ask_question("test environment forwarding"))

        parameters = stdio.call_args.args[0]
        self.assertEqual(
            parameters.env["GROUNDHOG_DB_PATH"],
            "/tmp/groundhog-test.duckdb",
        )

    def test_empty_question_is_rejected_without_starting_agent(self):
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
