"""Calendar service, protocol, and credential tests with synthetic data only."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

try:
    from calendar_mcp.audit import AuditLog
    from calendar_mcp.credentials import SecretStore, _os_keyring
    from calendar_mcp.onboarding import select_calendars
    from calendar_mcp.server import approval_request, create_server
    from calendar_mcp.service import CalendarService, event_id
    from calendar_mcp.settings import CalendarConfig, Settings
    from integrations.icloud_calendar import CalendarError
    from mcp import ClientSession, types
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.shared.memory import create_connected_server_and_client_session
    from langgraph_client.calendar import connect_calendar, terminal_calendar_approval
except ImportError:
    create_server = None


URL = "https://p01-caldav.icloud.com/123/calendars/personal/"
OTHER_URL = "https://p01-caldav.icloud.com/123/calendars/work/"


def settings_for(root):
    return Settings("icloud", "https://caldav.icloud.com/", "America/Phoenix", Path(root) / "credentials.enc",
                    "groundhog-calendar-test", "synthetic-account", Path(root) / "audit.jsonl", "test-owner",
                    (CalendarConfig("personal", URL, True), CalendarConfig("work", OTHER_URL)))


def event(**changes):
    return {"event_url": URL + "event.ics", "uid": "synthetic-uid", "etag": '"v1"',
            "summary": "Original", "start": "2026-11-02T09:00:00-07:00", "end": "2026-11-02T10:00:00-07:00",
            "all_day": False, "description": "Private notes", "location": "Gym", "editable": True,
            "recurring": False, **changes}


@unittest.skipIf(create_server is None, "Install requirements-calendar.txt")
class CalendarServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = settings_for(self.temporary.name)
        self.provider = Mock()
        self.provider.supported_components.return_value = ["VEVENT"]
        self.provider.get_event.return_value = {"event": event()}
        @contextmanager
        def factory(_settings):
            yield self.provider
        self.service = CalendarService(self.settings, provider_factory=factory)

    def test_encrypted_credentials_roundtrip_and_disconnect_without_os_access(self):
        stored = {}
        backend = Mock()
        backend.get_password.side_effect = lambda service, account: stored.get((service, account))
        backend.set_password.side_effect = lambda service, account, value: stored.__setitem__((service, account), value)
        backend.delete_password.side_effect = lambda service, account: stored.pop((service, account))
        store = SecretStore(self.settings, backend=backend)
        store.set("test@example.invalid", "synthetic-secret")
        encrypted = self.settings.credential_file.read_bytes()
        self.assertNotIn(b"synthetic-secret", encrypted)
        self.assertNotIn(b"test@example.invalid", encrypted)
        self.assertEqual(self.settings.credential_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(store.get()["password"], "synthetic-secret")
        self.settings.credential_file.write_bytes(encrypted[:-10] + b"tampered")
        with self.assertRaises(CalendarError):
            store.get()
        store.disconnect()
        self.assertFalse(self.settings.credential_file.exists())
        self.assertEqual(stored, {})

    def test_plaintext_keyring_fallback_is_rejected(self):
        with patch("calendar_mcp.credentials.keyring.get_keyring", return_value=object()):
            with self.assertRaises(CalendarError):
                _os_keyring()

    def test_approval_text_preserves_multiline_notes_without_literal_controls(self):
        preview = {"operation": "calendar_create_event", "calendar_id": "personal",
                   "changes": {"description": "Bring shoes\nWarm up first", "summary": "Training"}}
        message, schema = approval_request(preview)
        self.assertNotIn("\n", message)
        self.assertIn("Bring shoes\\nWarm up first", message)
        self.assertEqual(schema.model_json_schema()["properties"], {})
        with self.assertRaises(CalendarError):
            approval_request({"changes": {"summary": "hidden\u200btext"}})

    def test_confirmation_limit_counts_utf16_and_never_truncates_event_content(self):
        with self.assertRaises(CalendarError):
            approval_request({"changes": {"summary": "\U0001f6b4" * 500}})

    def test_interactive_calendar_selection_preserves_ids_and_unrelated_settings(self):
        path = Path(self.temporary.name) / "config.json"
        path.write_text(json.dumps(asdict(self.settings), default=str))
        self.provider.calendars.return_value = {"calendars": [
            {"name": "Personal", "calendar_url": URL}, {"name": "Work", "calendar_url": OTHER_URL}]}
        answers = iter(["2", ""])
        result = select_calendars(self.settings, path, read_input=lambda _: next(answers), emit=lambda _: None,
                                  provider_factory=self.service.provider_factory)
        saved = Settings.load(path)
        self.assertEqual(result, {"readable_calendars": 1, "writable_calendars": 0})
        self.assertEqual(saved.calendars, (CalendarConfig("work", OTHER_URL, False),))
        self.assertEqual(saved.actor, self.settings.actor)
        self.assertEqual(saved.credential_file, self.settings.credential_file)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_interactive_selection_cannot_enable_writes_to_an_unselected_calendar(self):
        path = Path(self.temporary.name) / "config.json"
        original = json.dumps(asdict(self.settings), default=str)
        path.write_text(original)
        self.provider.calendars.return_value = {"calendars": [
            {"name": "Personal", "calendar_url": URL}, {"name": "Work", "calendar_url": OTHER_URL}]}
        answers = iter(["1", "2"])
        with self.assertRaises(CalendarError):
            select_calendars(self.settings, path, read_input=lambda _: next(answers), emit=lambda _: None,
                             provider_factory=self.service.provider_factory)
        self.assertEqual(path.read_text(), original)

    def test_partial_update_preserves_notes_and_location_and_needs_current_etag(self):
        args = {"calendar_id": "personal", "event_id": event_id(URL + "event.ics"), "etag": '"v1"', "summary": "Renamed"}
        preview, backend = self.service.preview("calendar_update_event", args)
        self.assertEqual(backend["description"], "Private notes")
        self.assertEqual(backend["location"], "Gym")
        self.assertEqual(preview["changes"], {"summary": "Renamed"})
        self.assertNotIn("Private notes", json.dumps(preview))
        with self.assertRaises(CalendarError):
            self.service.write("calendar_update_event", backend, approved=False)
        self.provider.update_event.assert_not_called()
        with self.assertRaises(CalendarError):
            self.service.preview("calendar_update_event", {**args, "etag": '"old"'})

    def test_unselected_or_readonly_calendar_cannot_write(self):
        for calendar_id in ("work", "unknown"):
            with self.subTest(calendar_id=calendar_id), self.assertRaises(CalendarError):
                self.service.preview("calendar_delete_event", {"calendar_id": calendar_id,
                                    "event_id": event_id(URL + "event.ics"), "etag": '"v1"'})
        self.provider.get_event.assert_not_called()

    def test_description_is_omitted_unless_requested(self):
        args = {"calendar_id": "personal", "event_id": event_id(URL + "event.ics")}
        result = self.service.read("calendar_get_event", args)
        self.assertNotIn("description", result["event"])
        self.assertNotIn("event_url", result["event"])
        self.assertEqual(self.service.read("calendar_get_event", {**args, "include_description": True})["event"]["description"], "Private notes")

    def test_availability_unions_calendars_and_ignores_transparent_events(self):
        def agenda(calendar_url, **kwargs):
            return {"truncated": False, "events": (
                [event(start="2026-11-02T09:00:00-07:00", end="2026-11-02T10:30:00-07:00"),
                 event(start="2026-11-02T12:00:00-07:00", end="2026-11-02T13:00:00-07:00", transparency="TRANSPARENT")]
                if calendar_url == URL else
                [event(start="2026-11-02T10:00:00-07:00", end="2026-11-02T11:00:00-07:00")])}
        self.provider.agenda.side_effect = agenda
        result = self.service.read("calendar_find_free_slots", {
            "start": "2026-11-02T08:00:00-07:00", "end": "2026-11-02T13:00:00-07:00", "duration_minutes": 60})
        self.assertEqual([row["start"] for row in result["slots"]], ["2026-11-02T08:00:00-07:00", "2026-11-02T11:00:00-07:00"])
        self.assertEqual(result["slots"][-1]["available_until"], "2026-11-02T13:00:00-07:00")
        self.assertEqual(self.provider.agenda.call_count, 2)

    def test_all_day_busy_or_truncated_results_never_create_false_availability(self):
        self.provider.agenda.return_value = {"truncated": False, "events": [event(start="2026-11-02", end="2026-11-03", all_day=True)]}
        args = {"start": "2026-11-02T08:00:00-07:00", "end": "2026-11-02T13:00:00-07:00", "duration_minutes": 60}
        self.assertEqual(self.service.read("calendar_find_free_slots", args)["slots"], [])
        self.provider.agenda.return_value = {"truncated": True, "events": []}
        with self.assertRaises(CalendarError):
            self.service.read("calendar_find_free_slots", args)

    def test_audit_records_actor_operation_target_outcome_without_event_content(self):
        self.service.audit.append("calendar_update_event", "succeeded", {"calendar_id": "personal",
            "event_id": "opaque-id", "summary": "Private title", "description": "Private notes"}, "test-request")
        text = self.settings.audit_path.read_text()
        row = json.loads(text)
        self.assertEqual(row["actor"], "test-owner")
        self.assertEqual(row["operation"], "calendar_update_event")
        self.assertEqual(row["outcome"], "succeeded")
        self.assertNotIn("Private", text)
        self.assertNotIn("opaque-id", text)


@unittest.skipIf(create_server is None, "Install requirements-calendar.txt")
class CalendarProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = settings_for(self.temporary.name)
        self.service = Mock(settings=self.settings, audit=AuditLog(self.settings))
        self.service.preview.return_value = ({"operation": "create", "summary": "Synthetic event"}, {"frozen": "payload"})
        self.service.write.return_value = {"created": True}
        self.server = create_server(service=self.service)
        self.arguments = {"calendar_id": "personal", "request_id": "a5d74bac-39e8-4543-b335-257a148d3803",
                          "summary": "Synthetic event", "start": "2026-11-02T09:00:00-07:00", "end": "2026-11-02T10:00:00-07:00", "all_day": False}

    async def test_catalog_is_provider_neutral_and_approval_is_not_a_model_argument(self):
        async with create_connected_server_and_client_session(self.server) as session:
            catalog = await session.list_tools()
        self.assertEqual(len(catalog.tools), 9)
        for name in ("calendar_agenda", "calendar_reminders"):
            tool = next(tool for tool in catalog.tools if tool.name == name)
            self.assertTrue(tool.annotations.readOnlyHint)
        self.assertTrue(all(tool.name.startswith("calendar_") for tool in catalog.tools))
        for tool in catalog.tools:
            self.assertNotIn("approved", tool.inputSchema.get("properties", {}))
            self.assertNotIn("ctx", tool.inputSchema.get("properties", {}))
        self.assertTrue(next(t for t in catalog.tools if t.name == "calendar_delete_event").annotations.destructiveHint)

    async def test_human_approval_reaches_write_and_is_audited(self):
        requests = []
        async def approve(_context, params):
            requests.append(params.message)
            self.assertEqual(params.requestedSchema["properties"], {})
            self.assertNotIn("\n", params.message)
            return types.ElicitResult(action="accept", content={})
        async with create_connected_server_and_client_session(self.server, elicitation_callback=approve) as session:
            result = await session.call_tool("calendar_create_event", self.arguments)
        self.assertEqual(json.loads(result.content[0].text)["status"], "ok")
        self.assertIn("Synthetic event", requests[0])
        self.service.write.assert_called_once_with("calendar_create_event", {"frozen": "payload"}, approved=True)
        outcomes = [json.loads(line)["outcome"] for line in self.settings.audit_path.read_text().splitlines()]
        self.assertEqual(outcomes, ["requested", "approved", "succeeded"])

    async def test_agenda_and_reminder_reads_reach_service_without_approval(self):
        self.service.read.return_value = {"reminders_checked": True, "reminder_count": 1}
        window = {"start": "2026-11-03T00:00:00-07:00", "end": "2026-11-04T00:00:00-07:00"}
        async with create_connected_server_and_client_session(self.server) as session:
            agenda = await session.call_tool("calendar_agenda", window)
            self.assertEqual(json.loads(agenda.content[0].text)["reminder_count"], 1)
            self.service.read.assert_called_with("calendar_agenda", {**window, "calendar_ids": None, "limit": 100})
            result = await session.call_tool("calendar_reminders", {**window, "calendar_id": "personal"})
            self.assertEqual(json.loads(result.content[0].text)["status"], "ok")
            self.service.read.assert_called_with("calendar_reminders", {**window, "calendar_id": "personal", "limit": 100})
        self.service.write.assert_not_called()

    async def test_failed_reminder_read_is_an_error_not_an_empty_agenda(self):
        self.service.read.side_effect = CalendarError("Reminder access unavailable")
        async with create_connected_server_and_client_session(self.server) as session:
            result = await session.call_tool("calendar_agenda", {
                "start": "2026-11-03T00:00:00-07:00", "end": "2026-11-04T00:00:00-07:00"})
        data = json.loads(result.content[0].text)
        self.assertEqual(data["status"], "error")
        self.assertNotIn("reminders", data)

    async def test_unrenderable_confirmation_never_reaches_approval_or_write(self):
        self.service.preview.return_value = ({"description": "x" * 4000}, {"frozen": "payload"})
        approval = AsyncMock()
        async with create_connected_server_and_client_session(self.server, elicitation_callback=approval) as session:
            result = await session.call_tool("calendar_create_event", self.arguments)
        self.assertEqual(json.loads(result.content[0].text)["status"], "error")
        approval.assert_not_awaited()
        self.service.write.assert_not_called()

    async def test_decline_cancel_false_and_missing_confirmation_never_write(self):
        for action, content in (("decline", None), ("cancel", None)):
            async def reject(_context, _params):
                return types.ElicitResult(action=action, content=content)
            async with create_connected_server_and_client_session(self.server, elicitation_callback=reject) as session:
                result = await session.call_tool("calendar_create_event", self.arguments)
                self.assertEqual(json.loads(result.content[0].text)["status"], "declined")
        async with create_connected_server_and_client_session(self.server) as session:
            result = await session.call_tool("calendar_create_event", self.arguments)
            self.assertEqual(json.loads(result.content[0].text)["status"], "approval_required")
        self.service.write.assert_not_called()

    async def test_extra_approval_fields_are_not_accepted_as_empty_form_confirmation(self):
        async def malformed(_context, _params):
            return types.ElicitResult(action="accept", content={"approved": False})
        async with create_connected_server_and_client_session(self.server, elicitation_callback=malformed) as session:
            result = await session.call_tool("calendar_create_event", self.arguments)
        self.assertEqual(json.loads(result.content[0].text)["status"], "approval_required")
        self.service.write.assert_not_called()

    async def test_unavailable_audit_storage_prevents_write(self):
        self.service.audit = Mock()
        self.service.audit.append.side_effect = OSError("private audit unavailable")
        async with create_connected_server_and_client_session(self.server) as session:
            result = await session.call_tool("calendar_create_event", self.arguments)
        self.assertEqual(json.loads(result.content[0].text)["status"], "error")
        self.service.preview.assert_not_called()
        self.service.write.assert_not_called()

    async def test_uncertain_network_outcome_is_never_reported_as_success(self):
        async def approve(_context, _params):
            return types.ElicitResult(action="accept", content={})
        self.service.write.side_effect = TimeoutError("must-not-leak-private-host")
        async with create_connected_server_and_client_session(self.server, elicitation_callback=approve) as session:
            result = await session.call_tool("calendar_create_event", self.arguments)
        text = result.content[0].text
        self.assertEqual(json.loads(text)["status"], "uncertain")
        self.assertNotIn("must-not-leak", text)

    async def test_noninteractive_terminal_cannot_approve(self):
        with patch("langgraph_client.calendar.sys.stdin", io.StringIO("yes\n")):
            result = await terminal_calendar_approval(None, SimpleNamespace(mode="form", requestedSchema={"properties": {"approved": {}}}))
        self.assertEqual(result.action, "decline")

    async def test_terminal_approval_requires_an_actual_affirmative_human_reply(self):
        parameters = SimpleNamespace(mode="form", requestedSchema={"properties": {}},
                                     message="Approve synthetic event at 9 AM?")
        for reply, action in (("yes\n", "accept"), ("\n", "decline"), ("no\n", "decline")):
            terminal = SimpleNamespace(isatty=lambda: True, readline=lambda: reply)
            output = io.StringIO()
            with patch("langgraph_client.calendar.sys.stdin", terminal), patch("langgraph_client.calendar.sys.stderr", output):
                result = await terminal_calendar_approval(None, parameters)
            self.assertEqual(result.action, action)
            self.assertIn("9 AM", output.getvalue())

    async def test_groundhog_calendar_request_bypasses_workout_history_routing(self):
        from langgraph_client import client
        session = SimpleNamespace(initialize=AsyncMock())
        @asynccontextmanager
        async def transport(_parameters):
            yield (None, None)
        @asynccontextmanager
        async def session_context(*_args, **_kwargs):
            yield session
        approval = AsyncMock()
        tools = [object()]
        with (
            patch.object(client, "stdio_client", transport),
            patch.object(client, "ClientSession", session_context),
            patch.object(client, "RequestTrace"),
            patch.object(client, "connect_calendar", new=AsyncMock(return_value=(tools, "calendar instructions"))) as connect,
            patch.object(client, "_route_question", new=AsyncMock()) as route,
            patch.object(client, "_ask_fallback", new=AsyncMock(return_value="Your calendar is free tomorrow.")) as fallback,
        ):
            answer = await client.ask_question("Schedule a workout tomorrow", routing_enabled=True,
                                              calendar_config="/private/synthetic-calendar.json",
                                              calendar_elicitation_callback=approval)
        self.assertEqual(answer, "Your calendar is free tomorrow.")
        route.assert_not_awaited()
        self.assertEqual(fallback.call_args.args[2].domain, "calendar")
        self.assertEqual(fallback.call_args.kwargs["calendar_tools"], tools)
        self.assertIs(connect.call_args.kwargs["elicitation_callback"], approval)

    async def test_stdio_server_and_groundhog_discovery_work_without_database_or_credentials(self):
        config = Path(self.temporary.name) / "calendar.json"
        config.write_text(json.dumps({
            "provider": "icloud", "provider_url": self.settings.provider_url, "timezone": self.settings.timezone,
            "credential_file": str(self.settings.credential_file), "audit_path": str(self.settings.audit_path),
            "keyring_service": self.settings.keyring_service, "keyring_account": self.settings.keyring_account,
            "actor": self.settings.actor, "calendars": [],
        }))
        # Discovery must not unlock a keyring or contact Apple.
        async with AsyncExitStack() as stack:
            tools, instructions = await connect_calendar(stack, config)
            self.assertEqual(len(tools), 9)
            self.assertIn("America/Phoenix", instructions)
            self.assertTrue(all(tool.name.startswith("calendar_") for tool in tools))
