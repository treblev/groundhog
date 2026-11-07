"""Bounded iCloud CalDAV operations. No database, LLM, or credential logging."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit
from uuid import UUID, NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import caldav
import recurring_ical_events
from caldav.lib.error import NotFoundError
from icalendar import Calendar, Event


class CalendarError(ValueError):
    """An actionable error safe to return to the caller."""


def icloud_url(value: str) -> str:
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or not (host == "icloud.com" or host.endswith(".icloud.com"))
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment):
        raise CalendarError("Calendar URLs must be HTTPS iCloud URLs without credentials or query strings.")
    return value.rstrip("/") + "/"


def temporal(value: str, *, all_day: bool):
    try:
        if all_day:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError
            return date.fromisoformat(value)
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        # Fixed offsets are not portable TZIDs. UTC serializes as RFC 5545's Z form
        # and cannot silently become a floating wall-clock time after a round trip.
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise CalendarError("Use YYYY-MM-DD for all-day dates and offset-aware ISO timestamps for timed events.") from None


def event_fields(args: dict) -> dict:
    if type(args.get("all_day")) is not bool:
        raise CalendarError("all_day must be true or false.")
    start = temporal(args.get("start"), all_day=args["all_day"])
    end = temporal(args.get("end"), all_day=args["all_day"])
    if end <= start:
        raise CalendarError("Event end must follow start. All-day end dates are exclusive.")
    fields = {"DTSTART": start, "DTEND": end}
    for name in ("summary", "description", "location"):
        value = args.get(name, "")
        if not isinstance(value, str) or len(value) > 16000:
            raise CalendarError("Event text fields must be strings of at most 16000 characters.")
        fields[name.upper()] = value
    if not fields["SUMMARY"].strip():
        raise CalendarError("An event needs a nonempty summary.")
    return fields


def _etag(obj) -> str | None:
    value = obj.props.get("{DAV:}getetag")
    return str(value) if value else None


def _value(component, name, fallback=None):
    item = component.get(name)
    return item.dt if item is not None else fallback


def _display(value, zone):
    if isinstance(value, datetime):
        value = value.replace(tzinfo=zone) if value.tzinfo is None else value
        value = value.astimezone(zone)
    return value.isoformat() if value is not None else None


def _instant(value, zone):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(timezone.utc)


def read_window(start, end, limit):
    left, right = temporal(start, all_day=False), temporal(end, all_day=False)
    if not timedelta(0) < right - left <= timedelta(days=93):
        raise CalendarError("Agenda windows must be positive and no longer than 93 days.")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise CalendarError("Agenda limit must be between 1 and 500.")
    return left, right


def _end(component):
    start = _value(component, "DTSTART")
    duration = component.get("DURATION")
    default = start + (duration.dt if duration else (
        timedelta(0) if isinstance(start, datetime) else timedelta(days=1)))
    return _value(component, "DTEND", default)


def record(obj, display_zone: ZoneInfo) -> dict:
    component = obj.get_icalendar_component()
    start = _value(component, "DTSTART")
    end = _end(component)
    def display(value):
        if isinstance(value, datetime):
            # Floating events use the explicitly configured display timezone.
            value = value.replace(tzinfo=display_zone) if value.tzinfo is None else value
            value = value.astimezone(display_zone)
        return value.isoformat() if value is not None else None
    return {
        "uid": str(component.get("UID", "")), "event_url": str(obj.url), "etag": _etag(obj),
        "summary": str(component.get("SUMMARY", "")),
        "start": display(start), "end": display(end),
        "all_day": isinstance(start, date) and not isinstance(start, datetime),
        "location": str(component.get("LOCATION", "")),
        "description": str(component.get("DESCRIPTION", "")),
        "status": str(component.get("STATUS", "")),
        "transparency": str(component.get("TRANSP", "OPAQUE")),
        "recurrence_id": display(_value(component, "RECURRENCE-ID")),
        "recurring": any(key in component for key in ("RRULE", "RDATE", "RECURRENCE-ID")),
    }


def _editable(obj):
    instance = obj.get_icalendar_instance()
    events = instance.walk("VEVENT")
    if len(events) != 1 or any(key in events[0] for key in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID")):
        raise CalendarError("Recurring events are readable; editing a series or occurrence is not supported yet.")
    if any(key in events[0] for key in ("ATTENDEE", "ORGANIZER")):
        raise CalendarError("Editing invitations is not supported yet.")
    return instance, events[0]


def _matches(component, fields):
    return all((_value(component, name) if name in ("DTSTART", "DTEND") else
                str(component.get(name, ""))) == value for name, value in fields.items())


class ICloudCalendar:
    def __init__(self, client, *, calendar_urls=(), writable_calendar_urls=(), display_timezone, reminder_readers=None):
        self.client = client
        self.readable = {icloud_url(url) for url in calendar_urls}
        self.writable = {icloud_url(url) for url in writable_calendar_urls}
        if not self.writable <= self.readable:
            raise CalendarError("Writable calendars must also be in calendarUrls.")
        self.zone = ZoneInfo(display_timezone)
        self._component_cache = {}
        self.reminder_readers = reminder_readers or {}

    def _components(self, calendar):
        url = str(calendar.url)
        if url not in self._component_cache:
            self.client.url = self.client.url.relocate(icloud_url(url))
            self._component_cache[url] = sorted(str(value).upper()
                                                for value in calendar.get_supported_components())
        return self._component_cache[url]

    def supported_components(self, *, calendar_url):
        return self._components(self._calendar(calendar_url))

    def _calendar(self, url, *, write=False):
        url = icloud_url(url)
        if url not in (self.writable if write else self.readable):
            raise CalendarError("This calendar is not enabled for the requested operation.")
        # Fresh connections start at caldav.icloud.com; saved calendars live on
        # an account-specific host. Bind this operation to the validated URL,
        # just as CalDAV discovery relocates its client to the calendar home.
        self.client.url = self.client.url.relocate(url)
        return caldav.Calendar(client=self.client, url=url)

    def _load(self, calendar, event_url):
        if not isinstance(event_url, str):
            raise CalendarError("Supply the exact event_url returned by the calendar tools.")
        icloud_url(event_url)  # Validate host, TLS, and absence of user info/query.
        base = urlsplit(str(calendar.url))
        target = urlsplit(event_url)
        base_path = unquote(base.path).rstrip("/") + "/"
        target_path = unquote(target.path)
        member = target_path.removeprefix(base_path)
        if (target.hostname != base.hostname or (target.port or 443) != (base.port or 443)
                or not target_path.startswith(base_path) or member in ("", ".", "..")
                or "/" in member or "\\" in member):
            raise CalendarError("The event URL is outside the selected calendar.")
        # iCloud does not support UID filters reliably. Use the discovered resource URL.
        obj = calendar.event_by_url(event_url)
        if not str(obj.url).startswith(str(calendar.url)):
            raise CalendarError("The event URL is outside the selected calendar.")
        return obj

    def calendars(self):
        return {"calendars": [{"name": calendar.name, "calendar_url": str(calendar.url),
                               "supported_components": self._components(calendar),
                               "read_enabled": icloud_url(str(calendar.url)) in self.readable,
                               "write_enabled": icloud_url(str(calendar.url)) in self.writable}
                              for calendar in self.client.principal().get_calendars()]}

    def agenda(self, *, calendar_url, start, end, limit=100):
        calendar = self._calendar(calendar_url)
        start_dt, end_dt = read_window(start, end, limit)
        if "VEVENT" not in self._components(calendar):
            raise CalendarError("This collection does not support calendar events. Use calendar_reminders for tasks or calendar_agenda for a combined agenda.")
        objects = calendar.search(event=True, start=start_dt, end=end_dt, expand=True)
        rows = [record(obj, self.zone) for obj in objects
                if str(obj.get_icalendar_component().get("STATUS", "")).upper() != "CANCELLED"]
        # CalDAV can return all-day DATEs whose UTC interpretation overlaps
        # the query even though their local calendar day does not. Enforce the
        # requested window here, with DATE/floating values in the display zone.
        def instant(value):
            return _instant(value, self.zone)

        def overlaps(row):
            left, right = instant(row["start"]), instant(row["end"])
            return left < end_dt and (right > start_dt if right > left else left >= start_dt)

        rows = [row for row in rows if overlaps(row)]
        # Sort by actual instant, including mixed all-day and timed events.
        def sort_key(row):
            value = datetime.fromisoformat(row["start"])
            if value.tzinfo is None:
                value = value.replace(tzinfo=self.zone)
            return value.timestamp(), row["uid"]
        rows.sort(key=sort_key)
        return {"calendar_url": calendar_url, "timezone": str(self.zone), "events": rows[:limit],
                "total_count": len(rows), "truncated": len(rows) > limit,
                "window": {"start": start, "end": end, "end_exclusive": True}}

    def reminders(self, *, calendar_url, start, end, limit=100):
        """Read unfinished VTODOs scheduled in a closed, bounded query window."""
        calendar = self._calendar(calendar_url)
        start_dt, end_dt = read_window(start, end, limit)
        if "VTODO" not in self._components(calendar):
            raise CalendarError("This collection does not support reminder tasks. Use calendar_events for events or calendar_agenda for a combined agenda.")
        if calendar_url in self.reminder_readers:
            return self.reminder_readers[calendar_url](start=start, end=end, limit=limit)
        # iCloud's CalDAV profile marks VTODO time-range search unsupported.
        # Read the selected task collection, then apply dates/status locally.
        # Library-wide expansion can drop due-only and non-recurring tasks, so
        # expand only actual recurrence sets and preserve ordinary VTODOs.
        objects = calendar.search(todo=True, include_completed=True, post_filter=False)
        rows = []
        for obj in objects:
            instance = obj.get_icalendar_instance()
            components = instance.walk("VTODO")
            if not components:
                raise CalendarError("The reminder query returned an unexpected item type.")
            for part in components:
                notice = str(part.get("SUMMARY", "")) + " " + str(part.get("DESCRIPTION", ""))
                if ("DTSTART" not in part and "DUE" not in part
                        and ("support.apple.com/HT210220" in notice
                             or "the creator of this list has upgraded these reminders" in notice.lower())):
                    raise CalendarError("iCloud Reminders were upgraded and are unavailable through this CalDAV collection. Current reminders need the configured Mac MCP source; do not report them as empty.")
            if any(any(key in part for key in ("RRULE", "RDATE", "EXDATE")) for part in components):
                components = recurring_ical_events.of(instance, components=["VTODO"]).between(start_dt, end_dt)
            for component in components:
                row = self._reminder_record(component, obj, start_dt, end_dt)
                if row is not None:
                    rows.append(row)
        rows.sort(key=lambda row: (_instant(row["scheduled_at"], self.zone), row["uid"]))
        return {"calendar_url": calendar_url, "timezone": str(self.zone), "reminders": rows[:limit],
                "total_count": len(rows), "truncated": len(rows) > limit,
                "window": {"start": start, "end": end, "end_exclusive": True},
                "scope": "Unfinished tasks with DUE (or DTSTART when no due date exists) inside the window. Undated tasks and tasks scheduled outside the window are not included."}

    def _reminder_record(self, component, obj, start_dt, end_dt):
        if component.name != "VTODO":
            raise CalendarError("The reminder query returned an unexpected item type.")
        status = str(component.get("STATUS", "NEEDS-ACTION")).upper()
        if (status in {"COMPLETED", "CANCELLED"} or "COMPLETED" in component
                or int(component.get("PERCENT-COMPLETE", 0)) >= 100):
            return None
        scheduled = _value(component, "DTSTART")
        due = _value(component, "DUE")
        if due is None and scheduled is not None and "DURATION" in component:
            due = scheduled + _value(component, "DURATION")
        target = due if due is not None else scheduled
        # Undated tasks are not evidence of an empty reminder list. The
        # returned scope explicitly limits this result to scheduled tasks.
        if target is None or not start_dt <= _instant(_display(target, self.zone), self.zone) < end_dt:
            return None
        return {
            "uid": str(component.get("UID", "")), "event_url": str(obj.url),
            "summary": str(component.get("SUMMARY", "")),
            "start": _display(scheduled, self.zone), "due": _display(due, self.zone),
            "scheduled_at": _display(target, self.zone),
            "schedule_basis": "due" if due is not None else "start",
            "all_day": not isinstance(target, datetime), "status": status,
            "priority": int(component.get("PRIORITY", 0)),
            "recurrence_id": _display(_value(component, "RECURRENCE-ID"), self.zone),
            "recurring": any(key in component for key in ("RRULE", "RDATE", "RECURRENCE-ID")),
        }

    def get_event(self, *, calendar_url, event_url):
        obj = self._load(self._calendar(calendar_url), event_url)
        result = record(obj, self.zone)
        # Inspect the whole resource, including recurrence overrides.
        events = obj.get_icalendar_instance().walk("VEVENT")
        result["editable"] = len(events) == 1 and not any(
            key in events[0] for key in ("RRULE", "RDATE", "EXDATE", "RECURRENCE-ID", "ATTENDEE", "ORGANIZER"))
        return {"calendar_url": calendar_url, "event": result}

    def create_event(self, *, calendar_url, request_id, **args):
        calendar = self._calendar(calendar_url, write=True)
        try:
            request_id = str(UUID(request_id))
        except (ValueError, TypeError, AttributeError):
            raise CalendarError("request_id must be a UUID, reused for retries of the same create request.") from None
        fields = event_fields(args)
        uid = str(uuid5(NAMESPACE_URL, f"{str(calendar.url)}groundhog/{request_id}"))
        url = str(calendar.url).rstrip("/") + "/" + uid + ".ics"
        # Store a fingerprint so a timed-out request can be retried after the event was edited.
        fingerprint = hashlib.sha256(json.dumps(
            {name: value.isoformat() if isinstance(value, date) else value for name, value in fields.items()},
            sort_keys=True).encode()).hexdigest()
        marker = "X-GROUNDHOG-CREATE-HASH"
        def existing_result():
            existing = self._load(calendar, url)
            if str(existing.get_icalendar_component().get(marker, "")) != fingerprint:
                raise CalendarError("This request_id was already used for different event details.")
            return {"created": False, "event": record(existing, self.zone)}
        try:
            return existing_result()
        except NotFoundError:
            pass
        component = Event()
        component.add("UID", uid)
        component.add("DTSTAMP", datetime.now(timezone.utc))
        component.add("SEQUENCE", 0)
        component.add(marker, fingerprint)
        for name, value in fields.items():
            component.add(name, value)
        instance = Calendar()
        instance.add("VERSION", "2.0")
        instance.add("PRODID", "-//Groundhog//iCloud Calendar//EN")
        instance.add_component(component)
        # A stable resource URL and conditional PUT protect even concurrent retries.
        response = self.client.put(url, instance.to_ical().decode(),
                                   headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"})
        if response.status == 412:
            return existing_result()
        if response.status not in (201, 204):
            raise CalendarError("iCloud did not confirm event creation. Retry with the same request_id.")
        return {"created": True, "event": record(self._load(calendar, url), self.zone)}

    def update_event(self, *, calendar_url, event_url, etag, **args):
        calendar = self._calendar(calendar_url, write=True)
        obj = self._load(calendar, event_url)
        instance, component = _editable(obj)
        fields = event_fields(args)
        # A repeat of a successful update is a no-op, even after its ETag changed.
        if _matches(component, fields):
            return {"updated": False, "event": record(obj, self.zone)}
        current_etag = _etag(obj)
        if (not isinstance(etag, str) or not re.fullmatch(r'"[^"\r\n]+"', etag)
                or current_etag != etag):
            raise CalendarError("The event changed or its ETag is missing. Fetch it again before updating.")
        for name, value in fields.items():
            component.pop(name, None)
            component.add(name, value)
        component.pop("DURATION", None)
        component["SEQUENCE"] = int(component.get("SEQUENCE", 0)) + 1
        for name in ("DTSTAMP", "LAST-MODIFIED"):
            component.pop(name, None)
            component.add(name, datetime.now(timezone.utc))
        response = self.client.put(str(obj.url), instance.to_ical().decode(),
                                   headers={"Content-Type": "text/calendar; charset=utf-8", "If-Match": etag})
        if response.status == 412:
            raise CalendarError("The event changed during the update. Fetch it again before updating.")
        if response.status not in (201, 204):
            raise CalendarError("iCloud did not confirm the update. Fetch the event before retrying.")
        return {"updated": True, "event": record(self._load(calendar, event_url), self.zone)}


    def delete_event(self, *, calendar_url, event_url, etag):
        calendar = self._calendar(calendar_url, write=True)
        try:
            obj = self._load(calendar, event_url)
        except NotFoundError:
            return {"deleted": False, "status": "not_found"}
        _editable(obj)
        if (not isinstance(etag, str) or not re.fullmatch(r'"[^"\r\n]+"', etag)
                or _etag(obj) != etag):
            raise CalendarError("The event changed or its ETag is missing. Fetch it again before deleting.")
        response = self.client.request(str(obj.url), "DELETE", headers={"If-Match": etag})
        if response.status == 412:
            raise CalendarError("The event changed during deletion. Fetch it again before deleting.")
        if response.status not in (200, 204):
            raise CalendarError("iCloud did not confirm deletion. Fetch the event before retrying.")
        return {"deleted": True, "uid": str(obj.get_icalendar_component().get("UID", ""))}
