"""Read a configured Mac MCP tool through the existing OpenClaw node relay."""

import json
import subprocess

from integrations.icloud_calendar import CalendarError


class NodeReminderReader:
    def __init__(self, config, timezone):
        self.config = config
        self.timezone = timezone

    def __call__(self, *, start, end, limit):
        params = {"server": self.config["server"], "tool": "apple_reminders_agenda",
                  "arguments": {"start": start, "end": end, "timezone": self.timezone, "limit": limit}}
        command = [self.config["openclaw_command"], "nodes", "invoke", "--node", self.config["node_id"],
                   "--command", "mcp.tools.call.v1", "--params", json.dumps(params),
                   "--invoke-timeout", "45000", "--timeout", "50000", "--json"]
        try:
            response = subprocess.run(command, capture_output=True, text=True, timeout=55)
            if response.returncode:
                raise ValueError
            envelope = json.loads(response.stdout)
            if not envelope.get("ok"):
                raise ValueError
            payload = envelope["payload"]
            if payload.get("isError"):
                raise ValueError
            result = payload.get("structuredContent")
            if result is None:
                result = json.loads(next(block["text"] for block in payload["content"] if block["type"] == "text"))
            if result.get("status") != "ok" or not isinstance(result.get("reminders"), list):
                raise ValueError
            if result.get("timezone") != self.timezone or result.get("window") != {
                    "start": start, "end": end, "end_exclusive": True}:
                raise ValueError
            return result
        except Exception:
            raise CalendarError("Current Apple Reminders could not be read from the configured Mac MCP source. Check that the Mac node is online, its reminder MCP server is available, and macOS Reminders access is allowed. No reminder result was verified.") from None
