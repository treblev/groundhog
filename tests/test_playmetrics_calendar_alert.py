import unittest
from datetime import date

from scripts.playmetrics_calendar_alert import (
    ScheduleEvent,
    _extract_events_from_texts,
    _find_page_payload,
    _find_target_id,
    format_message,
    request_for_day,
)


class PlayMetricsCalendarAlertTests(unittest.TestCase):
    def test_tuesday_requests_same_day_practice(self):
        request = request_for_day(date(2026, 10, 20))
        self.assertIsNotNone(request)
        self.assertEqual(request.kind, "practice")
        self.assertEqual(request.target_date, date(2026, 10, 20))
        self.assertEqual(request.relative_label, "today")

    def test_friday_requests_saturday_game(self):
        request = request_for_day(date(2026, 10, 23))
        self.assertIsNotNone(request)
        self.assertEqual(request.kind, "game")
        self.assertEqual(request.target_date, date(2026, 10, 24))
        self.assertEqual(request.relative_label, "tomorrow")

    def test_inactive_days_do_nothing(self):
        self.assertIsNone(request_for_day(date(2026, 10, 19)))
        self.assertIsNone(request_for_day(date(2026, 10, 24)))
        self.assertIsNone(request_for_day(date(2026, 10, 25)))

    def test_extracts_target_date_practice(self):
        request = request_for_day(date(2026, 10, 20))
        events = _extract_events_from_texts(
            [
                "Monday, October 19 Practice 5:00 PM Field 1",
                "Tuesday, October 20 Practice 6:15 PM - 7:30 PM Field #4",
            ],
            request,
        )
        self.assertEqual(
            events,
            [ScheduleEvent(time="6:15 PM - 7:30 PM", field="Field #4")],
        )

    def test_team_filter_excludes_other_team(self):
        request = request_for_day(date(2026, 10, 20))
        events = _extract_events_from_texts(
            [
                "Tuesday, October 20 Blue Team Practice 6:00 PM Field 2",
                "Tuesday, October 20 Red Team Practice 7:00 PM Field 5",
            ],
            request,
            team_filter="Red Team",
        )
        self.assertEqual(events, [ScheduleEvent(time="7:00 PM", field="Field 5")])

    def test_incomplete_event_is_not_sent(self):
        request = request_for_day(date(2026, 10, 23))
        events = _extract_events_from_texts(
            ["Saturday, October 24 Game 9:00 AM"],
            request,
        )
        self.assertEqual(events, [])

    def test_message_format(self):
        request = request_for_day(date(2026, 10, 23))
        message = format_message(
            request,
            [ScheduleEvent(time="9:00 AM", field="Field 8")],
        )
        self.assertEqual(message, "Game tomorrow (Sat Oct 24): 9:00 AM — Field 8")

    def test_finds_nested_openclaw_target_id(self):
        payload = {"result": {"target": {"targetId": "abc123"}}}
        self.assertEqual(_find_target_id(payload), "abc123")

    def test_finds_nested_browser_evaluation_payload(self):
        page = {"bodyText": "Practice", "candidates": ["Practice 6 PM"]}
        payload = {"result": {"value": page}}
        self.assertEqual(_find_page_payload(payload), page)


if __name__ == "__main__":
    unittest.main()
