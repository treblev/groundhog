import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server import server
from mcp_server.tool_docs import ToolDocumentationError, load_tool_documentation


class ToolDocumentationTests(unittest.TestCase):
    def _write_document(self, directory: Path, name: str, content: str) -> None:
        (directory / f"{name}.md").write_text(content, encoding="utf-8")

    def test_loads_tool_and_argument_descriptions(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            self._write_document(
                directory,
                "sample_tool",
                "# `sample_tool`\n\nTool description.\n\n"
                "## Arguments\n\n### `query`\n\nArgument description.\n",
            )

            documentation = load_tool_documentation(directory, {"sample_tool"})

        self.assertEqual(documentation["sample_tool"].description, "Tool description.")
        self.assertEqual(
            documentation["sample_tool"].arguments["query"],
            "Argument description.",
        )

    def test_default_path_does_not_depend_on_working_directory(self):
        previous_directory = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary_directory:
            try:
                os.chdir(temporary_directory)
                documentation = load_tool_documentation(
                    expected_tool_names=server._TOOL_NAMES
                )
            finally:
                os.chdir(previous_directory)

        self.assertEqual(set(documentation), server._TOOL_NAMES)

    def test_rejects_missing_and_unexpected_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            self._write_document(directory, "extra", "# `extra`\n\nDescription.\n")

            with self.assertRaisesRegex(
                ToolDocumentationError,
                "missing: required; unexpected: extra",
            ):
                load_tool_documentation(directory, {"required"})

    def test_rejects_malformed_documentation(self):
        documents = {
            "wrong_heading": "# `another_tool`\n\nDescription.\n",
            "blank_tool": "# `blank_tool`\n",
            "duplicate_argument": (
                "# `duplicate_argument`\n\nDescription.\n\n## Arguments\n\n"
                "### `query`\n\nFirst.\n\n### `query`\n\nSecond.\n"
            ),
            "blank_argument": (
                "# `blank_argument`\n\nDescription.\n\n## Arguments\n\n"
                "### `query`\n"
            ),
            "empty_arguments": (
                "# `empty_arguments`\n\nDescription.\n\n## Arguments\n"
            ),
            "unexpected_heading": (
                "# `unexpected_heading`\n\nDescription.\n\n## Notes\n\nMore.\n"
            ),
        }

        for name, content in documents.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary_directory:
                directory = Path(temporary_directory)
                self._write_document(directory, name, content)
                with self.assertRaises(ToolDocumentationError):
                    load_tool_documentation(directory, {name})

    def test_registered_tools_use_markdown_documentation(self):
        tools = asyncio.run(server.list_tools())

        self.assertEqual({tool.name for tool in tools}, server._TOOL_NAMES)
        for tool in tools:
            documentation = server._TOOL_DOCUMENTATION[tool.name]
            self.assertEqual(tool.description, documentation.description)

            documented_schema_arguments = {
                name: schema["description"]
                for name, schema in tool.inputSchema["properties"].items()
                if "description" in schema
            }
            self.assertEqual(
                documented_schema_arguments,
                dict(documentation.arguments),
            )


if __name__ == "__main__":
    unittest.main()
