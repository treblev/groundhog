"""Offline calendar contract tests; never contact Apple or load real credentials."""

import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

try:
    import caldav
    from caldav.lib.error import NotFoundError
    from icalendar import Calendar, Event
    from integrations.icloud_calendar import CalendarError, ICloudCalendar, icloud_url
except ImportError:
    caldav = None


URL = "https://p01-caldav.icloud.com/123/calendars/personal/"
OTHER_URL = "https://p01-caldav.icloud.com/123/calendars/other/"


@unittest.skipIf(caldav is None, "Install requirements-calendar.txt for calendar tests")
class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.calendar = Mock(url=URL)
        self.calendar.get_supported_components.return_value = ["VEVENT", "VTODO"]
        self.adapter = ICloudCalendar(self.client, calendar_urls=[URL, OTHER_URL],
                                      writable_calendar_urls=[URL], display_timezone="America/Phoenix")
        self.calendar_patch = patch("integrations.icloud_calendar.caldav.Calendar", return_value=self.calendar)
        self.calendar_patch.start()
        self.addCleanup(self.calendar_patch.stop)
        self.args = {"summary": "Training", "start": "2026-11-02T09:00:00-07:00",
                     "end": "2026-11-02T10:00:00-07:00", "all_day": False,
                     "location": "Gym", "description": "Strength"}

    def resource(self, *, uid="test-uid", start=None, end=None, **extras):
        component = Event()
        component.add("UID", uid)
        component.add("DTSTAMP", datetime(2026, 11, 1, tzinfo=timezone.utc))
        component.add("DTSTART", start or datetime(2026, 11, 2, 16, tzinfo=timezone.utc))
        component.add("DTEND", end or datetime(2026, 11, 2, 17, tzinfo=timezone.utc))
        component.add("SUMMARY", "Original")
        component.add("SEQUENCE", 2)
        for key, value in extras.items():
            component.add(key.replace("_", "-"), value)
        instance = Calendar()
        instance.add("VERSION", "2.0")
        instance.add("PRODID", "-//Test//EN")
        instance.add_component(component)
        obj = caldav.Event(data=instance.to_ical(), url=URL + uid + ".ics")
        obj.props["{DAV:}getetag"] = '"v1"'
        obj.load = Mock(return_value=obj)
        return obj

    def test_url_and_calendar_permissions_fail_before_io(self):
        for url in ("http://caldav.icloud.com/", "https://icloud.com.evil.test/", "https://u:p@caldav.icloud.com/", "https://caldav.icloud.com/?secret=x"):
            with self.subTest(url=url), self.assertRaises(CalendarError):
                icloud_url(url)
        with self.assertRaises(CalendarError):
            self.adapter.create_event(calendar_url=OTHER_URL, request_id="ignored", **self.args)
        self.client.put.assert_not_called()
        with self.assertRaises(CalendarError):
            ICloudCalendar(self.client, calendar_urls=[], writable_calendar_urls=[URL], display_timezone="UTC")

    def test_agenda_expands_recurrences_preserves_all_day_and_converts_timezone(self):
        timed = self.resource(RECURRENCE_ID=datetime(2026, 11, 2, 16, tzinfo=timezone.utc))
        all_day = self.resource(uid="all-day", start=date(2026, 11, 2), end=date(2026, 11, 3))
        cancelled = self.resource(uid="cancelled", STATUS="CANCELLED")
        self.calendar.search.return_value = [timed, cancelled, all_day]
        result = self.adapter.agenda(calendar_url=URL, start="2026-11-02T00:00:00-07:00",
                                     end="2026-11-03T00:00:00-07:00", limit=1)
        self.assertTrue(self.calendar.search.call_args.kwargs["expand"])
        self.assertTrue(self.calendar.search.call_args.kwargs["event"])
        self.assertEqual(result["total_count"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["events"][0]["start"], "2026-11-02")
        self.assertEqual(result["events"][0]["end"], "2026-11-03")
        result = self.adapter.agenda(calendar_url=URL, start="2026-11-02T00:00:00-07:00",
                                     end="2026-11-03T00:00:00-07:00")
        self.assertEqual(result["events"][1]["start"], "2026-11-02T09:00:00-07:00")
        self.assertTrue(result["events"][1]["recurring"])

    def test_unbounded_or_naive_agenda_is_rejected(self):
        for start, end in (("2026-11-02T09:00", self.args["end"]),
                           (self.args["end"], self.args["start"]),
                           ("2026-01-01T00:00Z", "2026-12-31T00:00Z")):
            with self.subTest(start=start), self.assertRaises(CalendarError):
                self.adapter.agenda(calendar_url=URL, start=start, end=end)
        self.calendar.search.assert_not_called()

    def test_all_day_dates_stay_on_their_local_day_even_if_server_overreturns(self):
        self.calendar.search.return_value = [self.resource(
            start=date(2026, 11, 6), end=date(2026, 11, 7))]
        for day, expected in ((5, 0), (6, 1), (7, 0)):
            with self.subTest(day=day):
                result = self.adapter.agenda(calendar_url=URL,
                    start=f"2026-11-{day:02}T00:00:00-07:00",
                    end=f"2026-11-{day + 1:02}T00:00:00-07:00")
                self.assertEqual(result["total_count"], expected)
                if expected:
                    self.assertEqual(result["events"][0]["start"], "2026-11-06")

    def test_window_filter_keeps_overlaps_and_point_events_with_exclusive_end(self):
        # Phoenix midnight is 07:00 UTC. Keep a crossing event and an instant
        # at the start; exclude events ending at the start or starting at end.
        at = lambda day, hour: datetime(2026, 11, day, hour, tzinfo=timezone.utc)
        self.calendar.search.return_value = [
            self.resource(uid="crossing", start=at(5, 6), end=at(5, 8)),
            self.resource(uid="point-start", start=at(5, 7), end=at(5, 7)),
            self.resource(uid="ends-at-start", start=at(5, 6), end=at(5, 7)),
            self.resource(uid="starts-at-end", start=at(6, 7), end=at(6, 8)),
            self.resource(uid="point-end", start=at(6, 7), end=at(6, 7)),
        ]
        result = self.adapter.agenda(calendar_url=URL,
            start="2026-11-05T00:00:00-07:00", end="2026-11-06T00:00:00-07:00")
        self.assertEqual([row["uid"] for row in result["events"]], ["crossing", "point-start"])

    def test_create_uses_conditional_put_and_retry_returns_same_event(self):
        state = {}
        def get_event(url):
            if url not in state:
                raise NotFoundError()
            return state[url]
        def put(url, body, headers):
            obj = caldav.Event(data=body, url=url)
            obj.load = Mock(return_value=obj)
            obj.props["{DAV:}getetag"] = '"created"'
            state[url] = obj
            return SimpleNamespace(status=201)
        self.calendar.event_by_url.side_effect = get_event
        self.client.put.side_effect = put
        request = {"calendar_url": URL, "request_id": "a5d74bac-39e8-4543-b335-257a148d3803", **self.args}
        first = self.adapter.create_event(**request)
        second = self.adapter.create_event(**request)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["event"]["uid"], second["event"]["uid"])
        self.assertEqual(self.client.put.call_count, 1)
        self.assertEqual(self.client.put.call_args.kwargs["headers"]["If-None-Match"], "*")
        with self.assertRaises(CalendarError):
            self.adapter.create_event(**{**request, "summary": "Different request"})

    def test_concurrent_create_conflict_reconciles_by_resource_url(self):
        # Capture the first request, then simulate a racing create before its PUT.
        self.test_create_uses_conditional_put_and_retry_returns_same_event()
        existing = self.calendar.event_by_url.side_effect
        attempts = 0
        def get_event(url):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise NotFoundError()
            return existing(url)
        self.calendar.event_by_url.side_effect = get_event
        self.client.put.side_effect = None
        self.client.put.return_value = SimpleNamespace(status=412)
        result = self.adapter.create_event(calendar_url=URL, request_id="a5d74bac-39e8-4543-b335-257a148d3803", **self.args)
        self.assertFalse(result["created"])

    def test_update_preserves_properties_and_requires_server_etag(self):
        obj = self.resource(CATEGORIES="training", X_APPLE_TEST="preserve")
        self.calendar.event_by_url.return_value = obj
        self.client.put.return_value = SimpleNamespace(status=204)
        self.adapter.update_event(calendar_url=URL, event_url=URL + "test-uid.ics", etag='"v1"', **self.args)
        call = self.client.put.call_args
        self.assertEqual(call.kwargs["headers"]["If-Match"], '"v1"')
        saved = Calendar.from_ical(call.args[1]).walk("VEVENT")[0]
        self.assertEqual(str(saved["X-APPLE-TEST"]), "preserve")
        self.assertEqual(str(saved["CATEGORIES"].cats[0]), "training")
        self.assertEqual(int(saved["SEQUENCE"]), 3)
        with self.assertRaises(CalendarError):
            self.adapter.update_event(calendar_url=URL, event_url=URL + "test-uid.ics", etag='"stale"', **self.args)
        self.assertEqual(self.client.put.call_count, 1)

    def test_update_rejects_race_series_invitations_and_other_calendar(self):
        for extra in ({"RRULE": {"FREQ": "WEEKLY"}}, {"ATTENDEE": "mailto:test@example.invalid"},
                      {"RECURRENCE_ID": datetime(2026, 11, 2, 16, tzinfo=timezone.utc)}):
            self.calendar.event_by_url.return_value = self.resource(**extra)
            with self.subTest(extra=extra), self.assertRaises(CalendarError):
                self.adapter.update_event(calendar_url=URL, event_url=URL + "test-uid.ics", etag='"v1"', **self.args)
        self.client.put.assert_not_called()
        obj = self.resource()
        self.calendar.event_by_url.return_value = obj
        self.client.put.return_value = SimpleNamespace(status=412)
        with self.assertRaisesRegex(CalendarError, "changed during"):
            self.adapter.update_event(calendar_url=URL, event_url=URL + "test-uid.ics", etag='"v1"', **self.args)
        obj.url = OTHER_URL + "test-uid.ics"
        with self.assertRaisesRegex(CalendarError, "outside"):
            self.adapter.get_event(calendar_url=URL, event_url=URL + "test-uid.ics")

    def test_event_urls_cannot_escape_selected_calendar(self):
        for url in (OTHER_URL + "private.ics", URL + "../other/private.ics", URL + "%2e%2e%2fother/private.ics",
                    URL + "event.ics?account=private", URL + "event.ics#fragment"):
            with self.subTest(url=url), self.assertRaises(CalendarError):
                self.adapter.get_event(calendar_url=URL, event_url=url)
        self.calendar.event_by_url.assert_not_called()

    def test_all_day_end_is_exclusive_and_repeated_update_is_noop(self):
        obj = self.resource(start=date(2026, 11, 2), end=date(2026, 11, 3))
        self.calendar.event_by_url.return_value = obj
        args = {**self.args, "summary": "Original", "start": "2026-11-02", "end": "2026-11-03",
                "all_day": True, "location": "", "description": ""}
        result = self.adapter.update_event(calendar_url=URL, event_url=URL + "test-uid.ics", etag='"old"', **args)
        self.assertFalse(result["updated"])
        self.client.put.assert_not_called()
        with self.assertRaises(CalendarError):
            self.adapter.create_event(calendar_url=URL, request_id="a5d74bac-39e8-4543-b335-257a148d3803",
                                      **{**args, "end": "2026-11-02"})

    def test_delete_requires_exact_etag_and_preserves_concurrent_change(self):
        self.calendar.event_by_url.return_value = self.resource()
        with self.assertRaises(CalendarError):
            self.adapter.delete_event(calendar_url=URL, event_url=URL + "test-uid.ics", etag='"stale"')
        self.client.request.assert_not_called()
        self.client.request.return_value = SimpleNamespace(status=412)
        with self.assertRaisesRegex(CalendarError, "changed during"):
            self.adapter.delete_event(calendar_url=URL, event_url=URL + "test-uid.ics", etag='"v1"')
        self.assertEqual(self.client.request.call_args.kwargs["headers"]["If-Match"], '"v1"')
        self.client.request.return_value = SimpleNamespace(status=204)
        self.assertTrue(self.adapter.delete_event(calendar_url=URL, event_url=URL + "test-uid.ics", etag='"v1"')["deleted"])


@unittest.skipIf(caldav is None, "Install requirements-calendar.txt for calendar tests")
class CalendarTransportTests(unittest.TestCase):
    def test_fresh_client_uses_selected_calendar_hosts_without_discovery(self):
        second = "https://p02-caldav.icloud.com/123/calendars/other/"
        with caldav.DAVClient(url="https://caldav.icloud.com/", username="test", password="fake",
                              features="icloud", require_tls=True, enable_rfc6764=False) as client:
            client.session.request = Mock(side_effect=AssertionError("No discovery request expected"))
            adapter = ICloudCalendar(client, calendar_urls=[URL, second], display_timezone="UTC")
            for selected in (URL, second, URL):
                self.assertEqual(str(adapter._calendar(selected).url), selected)
                self.assertEqual(str(client.url), selected)
            with self.assertRaises(CalendarError):
                adapter._calendar("https://p03-caldav.icloud.com/123/calendars/unselected/")
            self.assertEqual(str(client.url), URL)
            client.session.request.assert_not_called()

    def test_real_caldav_client_create_read_update_without_uid_search(self):
        # Exercise the pinned library too. Only its HTTP session is replaced.
        state = {}
        def response(status, body="", tag='"v1"'):
            return SimpleNamespace(status_code=status, content=body.encode(), text=body, reason="test",
                                   headers={"Content-Type": "text/calendar", "Etag": tag})
        def request(method, url, *, data, headers, **kwargs):
            if method == "GET":
                return response(200, state[url][0], state[url][1]) if url in state else response(404)
            self.assertEqual(method, "PUT", "No UID REPORT or whole-calendar scan is allowed")
            if "If-None-Match" in headers:
                self.assertEqual(headers["If-None-Match"], "*")
                if url in state:
                    return response(412)
                tag = '"v1"'
            else:
                self.assertEqual(headers["If-Match"], state[url][1])
                tag = '"v2"'
            state[url] = (data.decode(), tag)
            self.assertIn("DTSTART:20261102T160000Z", state[url][0])
            self.assertIn("DTEND:20261102T170000Z", state[url][0])
            return response(201 if tag == '"v1"' else 204)
        with caldav.DAVClient(url="https://caldav.icloud.com/", username="test", password="fake", features="icloud",
                              require_tls=True, enable_rfc6764=False) as client:
            client.session.request = Mock(side_effect=request)
            adapter = ICloudCalendar(client, calendar_urls=[URL], writable_calendar_urls=[URL],
                                      display_timezone="America/Phoenix")
            args = {"calendar_url": URL, "summary": "Training", "start": "2026-11-02T09:00:00-07:00",
                    "end": "2026-11-02T10:00:00-07:00", "all_day": False, "description": "", "location": "Gym"}
            created = adapter.create_event(request_id="a5d74bac-39e8-4543-b335-257a148d3803", **args)
            self.assertTrue(created["created"])
            event = created["event"]
            self.assertEqual(event["etag"], '"v1"')
            self.assertEqual(event["start"], args["start"])
            updated = adapter.update_event(event_url=event["event_url"], etag=event["etag"],
                                            **{**args, "summary": "Updated training"})
            self.assertTrue(updated["updated"])
            self.assertEqual(updated["event"]["summary"], "Updated training")
            self.assertEqual(updated["event"]["etag"], '"v2"')
            repeated = adapter.update_event(event_url=event["event_url"], etag=event["etag"],
                                             **{**args, "summary": "Updated training"})
            self.assertFalse(repeated["updated"])
            self.assertFalse(adapter.create_event(request_id="a5d74bac-39e8-4543-b335-257a148d3803", **args)["created"])
