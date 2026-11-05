"""Optional calendar MCP connection and generic tool bindings for the local agent."""

import json
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from langchain_core.tools import StructuredTool
from mcp import ClientSession
from mcp.types import ElicitResult
from mcp.client.stdio import StdioServerParameters, stdio_client


def configured_calendar_path(explicit=None):
    return explicit or os.environ.get("GROUNDHOG_CALENDAR_CONFIG")


def looks_like_calendar_request(question):
    return bool(re.search(r"\b(?:calendar|agenda|appointment|appointments|meeting|meetings|reschedule|free\s+(?:slot|time)|availability)\b|\bschedule\s+(?:a|an|my|the)\b", question, re.I))


async def terminal_calendar_approval(_context, parameters):
    """Human-only terminal interaction; noninteractive callers cannot approve writes."""
    if (not sys.stdin.isatty() or getattr(parameters, "mode", "form") != "form"
            or parameters.requestedSchema.get("properties") != {}):
        return ElicitResult(action="decline")
    print(parameters.message, file=sys.stderr)
    print("Approve this calendar change? Type yes to approve: ", end="", file=sys.stderr, flush=True)
    response = await asyncio.to_thread(sys.stdin.readline)
    if response.strip().lower() != "yes":
        return ElicitResult(action="decline")
    return ElicitResult(action="accept", content={})


async def connect_calendar(stack, config_path, *, elicitation_callback=None):
    # Forward only what a local OS keyring and Python runtime need.
    allowed_env = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "DBUS_SESSION_BUS_ADDRESS",
                   "XDG_RUNTIME_DIR", "PYTHON_KEYRING_BACKEND"}
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "calendar_mcp", "serve", "--config", str(Path(config_path).resolve())],
        cwd=str(Path(__file__).resolve().parent.parent),
        env={k: v for k, v in os.environ.items() if k in allowed_env},
    )
    read, write = await stack.enter_async_context(stdio_client(parameters))
    session = await stack.enter_async_context(ClientSession(
        read, write, elicitation_callback=elicitation_callback,
        read_timeout_seconds=timedelta(seconds=240)))
    initialized = await session.initialize()
    catalog = await session.list_tools()

    def bind(specification):
        async def invoke(**arguments):
            result = await session.call_tool(specification.name, arguments)
            text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
            return text or json.dumps({"status": "error", "message": "Calendar service returned no result."})
        return StructuredTool.from_function(
            coroutine=invoke, name=specification.name, description=specification.description or specification.name,
            args_schema=specification.inputSchema)

    instructions = (
        f"Current instant: {datetime.now(timezone.utc).isoformat()}. "
        "Calendar tools read live calendar state, not database tables. Empty calendar results are authoritative. "
        "Only a human-facing elicitation callback can approve writes. Never answer one yourself. "
        "If approval is unavailable or declined, say no change was made and stop that write. "
        "Do not claim success for error, uncertain, or approval_required results. "
        + (initialized.instructions or "")
    )
    return [bind(tool) for tool in catalog.tools], instructions
