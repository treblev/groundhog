"""Read-only Mac MCP and node relay contract tests with synthetic reminders."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    from calendar_mcp.mac_reminders import read_reminders, create_server
    from calendar_mcp.reminder_node import NodeReminderReader
    from integrations.icloud_calendar import CalendarError
    from mcp.shared.memory import create_connected_server_and_client_session
except ImportError:
    create_server = None

WINDOW = {"start": "2026-11-03T00:00:00-07:00", "end": "2026-11-04T00:00:00-07:00"}
CONFIG = {"openclaw_command": "/usr/local/bin/openclaw", "node_id": "synthetic-node", "server": "synthetic-reminders"}


@unittest.skipIf(create_server is None, "Install requirements-calendar.txt")
class MacReminderTests(unittest.TestCase):
    def test_read_command_strips_notes_and_filters_day_completion_and_selected_lists(self):
        def row(identity, **values):
            return {"id": identity, "title": "Synthetic task", "listID": "selected", "listName": "Synthetic list",
                    "isCompleted": False, "dueDate": "2026-11-03T17:00:00Z", "notes": "PRIVATE", **values}
        fixture = [row("timed"), row("date", dueDate="2026-11-03T00:00:00Z", dueDateIsAllDay=True,
            dueDateComponents={"year": 2026, "month": 11, "day": 3}), row("other", listID="other"),
            row("done", isCompleted=True), row("undated", dueDate=None), row("outside", dueDate="2026-11-04T07:00:00Z")]
        with patch("calendar_mcp.mac_reminders.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(fixture))) as run:
            result = read_reminders("/approved/remindctl", **WINDOW, timezone="America/Phoenix", list_ids=["selected"])
        self.assertEqual([r["uid"] for r in result["reminders"]], ["date", "timed"])
        self.assertEqual(result["reminders"][0]["due"], "2026-11-03")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(run.call_args.args[0], ["/approved/remindctl", "show", "open", "--json", "--no-input"])

    def test_permission_failure_and_malformed_output_fail_closed(self):
        for response in (SimpleNamespace(returncode=1, stdout=""), SimpleNamespace(returncode=0, stdout="{}")):
            with patch("calendar_mcp.mac_reminders.subprocess.run", return_value=response), self.assertRaises(CalendarError):
                read_reminders("/approved/remindctl", **WINDOW, timezone="America/Phoenix")

    def test_node_relay_uses_only_configured_mcp_tool_and_validates_window(self):
        data = {"status": "ok", "reminders": [], "timezone": "America/Phoenix", "window": {**WINDOW, "end_exclusive": True}}
        response = {"ok": True, "payload": {"structuredContent": data}}
        reader = NodeReminderReader(CONFIG, "America/Phoenix")
        with patch("calendar_mcp.reminder_node.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(response))) as run:
            self.assertEqual(reader(**WINDOW, limit=100), data)
        command = run.call_args.args[0]
        params = json.loads(command[command.index("--params")+1])
        self.assertEqual(params["tool"], "apple_reminders_agenda")
        self.assertEqual(params["server"], CONFIG["server"])
        self.assertNotIn("system.run", command)
        data["window"] = {"start": "wrong", "end": "wrong"}
        with patch("calendar_mcp.reminder_node.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(response))), self.assertRaises(CalendarError):
            reader(**WINDOW, limit=100)

    def test_node_disconnection_or_mcp_errors_are_never_empty_reminders(self):
        reader = NodeReminderReader(CONFIG, "America/Phoenix")
        for payload in ({"ok": False}, {"ok": True, "payload": {"isError": True}}):
            with patch("calendar_mcp.reminder_node.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(payload))), self.assertRaises(CalendarError):
                reader(**WINDOW, limit=100)


@unittest.skipIf(create_server is None, "Install requirements-calendar.txt")
class MacReminderProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_read_tool_is_exposed_and_permission_errors_remain_errors(self):
        async with create_connected_server_and_client_session(create_server("/approved/remindctl")) as session:
            catalog = await session.list_tools()
            self.assertEqual([t.name for t in catalog.tools], ["apple_reminders_agenda"])
            self.assertTrue(catalog.tools[0].annotations.readOnlyHint)
            with patch("calendar_mcp.mac_reminders.subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="")):
                result = await session.call_tool("apple_reminders_agenda", {**WINDOW, "timezone": "America/Phoenix"})
            self.assertTrue(result.isError)
