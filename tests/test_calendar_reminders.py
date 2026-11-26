"""Task and combined-agenda regressions. All fixtures are synthetic."""

import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape

try:
    import caldav
    from icalendar import Calendar, Todo
    from calendar_mcp.service import CalendarService
    from integrations.icloud_calendar import CalendarError, ICloudCalendar
    from tests.test_calendar_mcp import settings_for, event
except ImportError:
    caldav = None

URL = "https://p01-caldav.icloud.com/123/calendars/personal/"
OTHER_URL = "https://p01-caldav.icloud.com/123/calendars/work/"
WINDOW = {"start": "2026-11-03T00:00:00-07:00", "end": "2026-11-04T00:00:00-07:00"}


def task(uid="task", **fields):
    component = Todo()
    component.add("UID", uid)
    component.add("DTSTAMP", datetime(2026, 11, 1, tzinfo=timezone.utc))
    component.add("SUMMARY", "Synthetic task")
    for key, value in fields.items():
        component.add(key.replace("_", "-"), value)
    instance = Calendar()
    instance.add("VERSION", "2.0")
    instance.add("PRODID", "-//Test//EN")
    instance.add_component(component)
    return caldav.Todo(data=instance.to_ical(), url=OTHER_URL + uid + ".ics")


@unittest.skipIf(caldav is None, "Install requirements-calendar.txt")
class ReminderAdapterTests(unittest.TestCase):
    def setUp(self):
        self.calendar = Mock(url=OTHER_URL)
        self.calendar.get_supported_components.return_value = ["VTODO"]
        self.adapter = ICloudCalendar(Mock(), calendar_urls=[OTHER_URL], display_timezone="America/Phoenix")
        self.factory = patch("integrations.icloud_calendar.caldav.Calendar", return_value=self.calendar)
        self.factory.start()
        self.addCleanup(self.factory.stop)

    def test_tasks_use_due_dates_local_time_and_exclude_completed_and_adjacent_days(self):
        due = datetime(2026, 11, 3, 17, tzinfo=timezone.utc)
        self.calendar.search.return_value = [
            task("timed", DUE=due, DTSTART=datetime(2026, 11, 1, tzinfo=timezone.utc)),
            task("date", DUE=date(2026, 11, 3)),
            task("floating", DUE=datetime(2026, 11, 3, 9)),
            task("old", DUE=date(2026, 11, 2)),
            task("next", DUE=date(2026, 11, 4)),
            task("boundary", DUE=datetime(2026, 11, 4, 7, tzinfo=timezone.utc)),
            task("completed-status", DUE=due, STATUS="COMPLETED"),
            task("completed-date", DUE=due, COMPLETED=due),
            task("completed-percent", DUE=due, PERCENT_COMPLETE=100),
            task("cancelled", DUE=due, STATUS="CANCELLED"),
            task("undated"),
        ]
        result = self.adapter.reminders(calendar_url=OTHER_URL, **WINDOW)
        self.assertEqual([row["uid"] for row in result["reminders"]], ["date", "floating", "timed"])
        self.assertEqual(result["reminders"][-1]["due"], "2026-11-03T10:00:00-07:00")
        self.assertEqual(result["total_count"], 3)
        self.assertFalse(result["truncated"])
        self.assertTrue(self.calendar.search.call_args.kwargs["todo"])
        self.assertNotIn("event", self.calendar.search.call_args.kwargs)

    def test_start_fallback_duration_and_recurring_occurrence_are_retained(self):
        from datetime import timedelta
        start = datetime(2026, 11, 3, 16, tzinfo=timezone.utc)
        self.calendar.search.return_value = [
            task("start", DTSTART=start),
            task("duration", DTSTART=start, DURATION=timedelta(hours=1)),
            task("repeat", DUE=start, RECURRENCE_ID=start),
        ]
        result = self.adapter.reminders(calendar_url=OTHER_URL, **WINDOW, limit=2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["total_count"], 3)
        rows = self.adapter.reminders(calendar_url=OTHER_URL, **WINDOW)["reminders"]
        by_id = {row["uid"]: row for row in rows}
        self.assertEqual(by_id["start"]["schedule_basis"], "start")
        self.assertIsNone(by_id["start"]["due"])
        self.assertEqual(by_id["duration"]["due"], "2026-11-03T10:00:00-07:00")
        self.assertTrue(by_id["repeat"]["recurring"])

    def test_wrong_component_and_invalid_window_fail_instead_of_appearing_empty(self):
        with self.assertRaisesRegex(CalendarError, "calendar_reminders"):
            self.adapter.agenda(calendar_url=OTHER_URL, **WINDOW)
        self.calendar.search.assert_not_called()
        self.calendar.get_supported_components.return_value = ["VEVENT"]
        self.adapter._component_cache.clear()
        with self.assertRaisesRegex(CalendarError, "does not support reminder"):
            self.adapter.reminders(calendar_url=OTHER_URL, **WINDOW)
        self.calendar.search.assert_not_called()
        with self.assertRaises(CalendarError):
            self.adapter.reminders(calendar_url=OTHER_URL, start=WINDOW["end"], end=WINDOW["start"])
        with self.assertRaises(CalendarError):
            self.adapter.reminders(calendar_url=URL, **WINDOW)

    def test_upgraded_icloud_notice_is_unavailable_not_an_empty_task_list(self):
        self.calendar.search.return_value = [task("notice", SUMMARY="Upgraded reminders",
            DESCRIPTION="See https://support.apple.com/HT210220")]
        with self.assertRaisesRegex(CalendarError, "upgraded.*unavailable"):
            self.adapter.reminders(calendar_url=OTHER_URL, **WINDOW)

    def test_configured_reader_replaces_legacy_caldav_for_only_its_selected_collection(self):
        reader = Mock(return_value={"reminders": [{"summary": "Current task"}]})
        self.adapter.reminder_readers = {OTHER_URL: reader}
        result = self.adapter.reminders(calendar_url=OTHER_URL, **WINDOW)
        self.assertEqual(result["reminders"][0]["summary"], "Current task")
        self.calendar.search.assert_not_called()

    def test_real_caldav_transport_expands_recurring_tasks_and_keeps_due_only_tasks(self):
        self.factory.stop()
        fixtures = [
            task("repeat", DTSTART=datetime(2026, 11, 2, 16, tzinfo=timezone.utc),
                 DUE=datetime(2026, 11, 2, 17, tzinfo=timezone.utc), RRULE={"FREQ": "DAILY", "COUNT": 3}),
            task("due-only", DUE=date(2026, 11, 3)),
            task("started-earlier", DTSTART=date(2026, 11, 1), DUE=date(2026, 11, 3)),
        ]
        body = ('<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">' + ''.join(
            '<D:response><D:href>' + escape(str(obj.url)) + '</D:href><D:propstat><D:prop>'
            '<D:getetag>"v1"</D:getetag><C:calendar-data>' + escape(obj.data) +
            '</C:calendar-data></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>'
            for obj in fixtures) + '</D:multistatus>')
        with caldav.DAVClient(url="https://caldav.icloud.com/", username="test", password="fake",
                              features="icloud", require_tls=True, enable_rfc6764=False) as client:
            def request(method, url, *, data, **kwargs):
                self.assertEqual(method, "REPORT")
                self.assertIn(b'VTODO', data)
                self.assertNotIn(b'VEVENT', data)
                self.assertNotIn(b'time-range', data, "iCloud VTODO time-range search is unsupported")
                return SimpleNamespace(status_code=207, content=body.encode(), text=body, reason="test",
                                       headers={"Content-Type": "application/xml"})
            client.session.request = Mock(side_effect=request)
            adapter = ICloudCalendar(client, calendar_urls=[OTHER_URL], display_timezone="America/Phoenix")
            with patch.object(caldav.Calendar, "get_supported_components", return_value=["VTODO"]):
                result = adapter.reminders(calendar_url=OTHER_URL, **WINDOW)
            self.assertEqual({row["uid"] for row in result["reminders"]}, {"repeat", "due-only", "started-earlier"})
            repeat = next(row for row in result["reminders"] if row["uid"] == "repeat")
            self.assertEqual(repeat["due"], "2026-11-03T10:00:00-07:00")


@unittest.skipIf(caldav is None, "Install requirements-calendar.txt")
class CombinedAgendaTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = settings_for(directory.name)
        self.provider = Mock()
        self.provider.supported_components.side_effect = lambda calendar_url: ["VEVENT"] if calendar_url == URL else ["VTODO"]
        self.provider.agenda.return_value = {"events": [event()], "total_count": 1, "truncated": False}
        self.provider.reminders.return_value = {
            "reminders": [{"uid": "task", "event_url": OTHER_URL + "task.ics", "summary": "Synthetic task",
                           "due": "2026-11-03", "scheduled_at": "2026-11-03", "schedule_basis": "due"}],
            "timezone": "America/Phoenix", "total_count": 1, "truncated": False,
            "window": WINDOW, "scope": "Unfinished scheduled tasks only",
        }
        @contextmanager
        def factory(_settings):
            yield self.provider
        self.service = CalendarService(self.settings, provider_factory=factory)

    def test_combined_agenda_reads_both_types_and_records_source_coverage(self):
        result = self.service.read("calendar_agenda", WINDOW)
        self.assertEqual((result["event_count"], result["reminder_count"]), (1, 1))
        self.assertTrue(result["events_checked"] and result["reminders_checked"])
        self.assertEqual(result["reminders"][0]["calendar_id"], "work")
        self.assertNotIn("event_url", json.dumps(result))
        self.assertNotIn("description", json.dumps(result))
        self.assertTrue(result["sources"][1]["reminders_checked"])
        self.assertFalse(result["sources"][1]["events_checked"])
        self.provider.agenda.assert_called_once_with(calendar_url=URL, **WINDOW, limit=500)
        self.provider.reminders.assert_called_once_with(calendar_url=OTHER_URL, **WINDOW, limit=500)

    def test_unchecked_and_failed_sources_cannot_be_reported_as_empty_reminders(self):
        result = self.service.read("calendar_agenda", {**WINDOW, "calendar_ids": ["personal"]})
        self.assertFalse(result["reminders_checked"])
        self.provider.reminders.side_effect = CalendarError("Reminder read failed")
        with self.assertRaisesRegex(CalendarError, "Reminder read failed"):
            self.service.read("calendar_agenda", WINDOW)
        with self.assertRaises(CalendarError):
            self.service.read("calendar_agenda", {**WINDOW, "calendar_ids": ["unknown"]})

    def test_truncation_and_task_only_permissions_are_explicit(self):
        self.provider.reminders.return_value["total_count"] = 501
        self.provider.reminders.return_value["truncated"] = True
        result = self.service.read("calendar_agenda", WINDOW)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["reminder_count"], 501)
        self.provider.calendars.return_value = {"calendars": [
            {"name": "Synthetic tasks", "calendar_url": URL, "supported_components": ["VTODO"]}]}
        catalog = self.service.read("calendar_list", {})["calendars"]
        self.assertFalse(catalog[0]["write_enabled"])
        self.assertFalse(catalog[0]["reminder_write_enabled"])
        self.provider.supported_components.side_effect = None
        self.provider.supported_components.return_value = ["VTODO"]
        with self.assertRaisesRegex(CalendarError, "Reminder writes are not supported"):
            self.service.preview("calendar_create_event", {"calendar_id": "personal",
                "request_id": "a5d74bac-39e8-4543-b335-257a148d3803", "summary": "Task-shaped event",
                "all_day": True, "start": "2026-11-03", "end": "2026-11-04"})

    def test_reminders_do_not_block_free_slots_or_hide_missing_event_sources(self):
        self.provider.agenda.return_value = {"events": [], "total_count": 0, "truncated": False}
        result = self.service.read("calendar_find_free_slots", {**WINDOW, "duration_minutes": 30})
        self.assertEqual(result["calendar_ids"], ["personal"])
        self.assertEqual(result["excluded_task_collection_ids"], ["work"])
        self.provider.reminders.assert_not_called()
        with self.assertRaisesRegex(CalendarError, "availability could not be checked"):
            self.service.read("calendar_find_free_slots", {**WINDOW, "duration_minutes": 30, "calendar_ids": ["work"]})
