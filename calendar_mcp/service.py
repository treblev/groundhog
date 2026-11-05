"""Provider-neutral calendar operations, approval previews, and availability."""

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from calendar_mcp.audit import AuditLog
from calendar_mcp.provider import open_provider
from integrations.icloud_calendar import CalendarError, event_fields, temporal


def event_id(url):
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


def resource_url(value):
    try:
        if not isinstance(value, str) or not value or len(value) > 8192:
            raise ValueError
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True).decode()
    except Exception:
        raise CalendarError("Use the exact event_id returned by the calendar tools.") from None


def public_event(row, *, include_description=False):
    result = {k: row[k] for k in ("uid", "etag", "summary", "start", "end", "all_day", "location",
                                  "status", "recurrence_id", "recurring", "editable") if k in row}
    result["event_id"] = event_id(row["event_url"])
    if include_description:
        result["description"] = row.get("description", "")
    return result


class CalendarService:
    def __init__(self, settings, provider_factory=open_provider, audit=None):
        self.settings = settings
        self.provider_factory = provider_factory
        self.audit = audit if audit is not None else AuditLog(settings)

    def _arguments(self, args, *, write=False):
        calendar = self.settings.calendar(args["calendar_id"], write=write)
        result = {k: v for k, v in args.items() if k not in ("calendar_id", "event_id")}
        result["calendar_url"] = calendar.url
        if "event_id" in args:
            result["event_url"] = resource_url(args["event_id"])
        return result

    def read(self, operation, args):
        with self.provider_factory(self.settings) as provider:
            if operation == "calendar_list":
                discovered = provider.calendars()["calendars"]
                configured = {c.url: c for c in self.settings.calendars}
                return {"calendars": [{
                    "calendar_id": configured[row["calendar_url"]].id if row["calendar_url"] in configured
                        else "unselected-" + hashlib.sha256(row["calendar_url"].encode()).hexdigest()[:16],
                    "name": row["name"], "read_enabled": row["calendar_url"] in configured,
                    "write_enabled": bool(configured.get(row["calendar_url"]) and configured[row["calendar_url"]].writable),
                } for row in discovered], "timezone": self.settings.timezone}
            if operation == "calendar_events":
                result = provider.agenda(**self._arguments(args))
                return {"calendar_id": args["calendar_id"], "timezone": result["timezone"],
                        "events": [public_event(row) for row in result["events"]],
                        **{k: result[k] for k in ("window", "total_count", "truncated")}}
            if operation == "calendar_get_event":
                actual = {k: v for k, v in args.items() if k != "include_description"}
                row = provider.get_event(**self._arguments(actual))["event"]
                return {"calendar_id": args["calendar_id"],
                        "event": public_event(row, include_description=args.get("include_description", False))}
            if operation == "calendar_find_free_slots":
                return self._availability(provider, **args)
        raise CalendarError("Unsupported calendar read operation.")

    def _availability(self, provider, *, start, end, duration_minutes, calendar_ids=None, limit=50):
        start_dt, end_dt = temporal(start, all_day=False), temporal(end, all_day=False)
        if not timedelta(0) < end_dt - start_dt <= timedelta(days=31):
            raise CalendarError("Availability windows must be positive and no longer than 31 days.")
        if type(duration_minutes) is not int or not 1 <= duration_minutes <= 1440:
            raise CalendarError("duration_minutes must be between 1 and 1440.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CalendarError("Availability limit must be between 1 and 100.")
        selected = list(dict.fromkeys(calendar_ids if calendar_ids is not None else [c.id for c in self.settings.calendars]))
        if not selected:
            raise CalendarError("Select at least one readable calendar before checking availability.")
        zone = ZoneInfo(self.settings.timezone)
        busy = []
        for selected_id in selected:
            calendar = self.settings.calendar(selected_id)
            agenda = provider.agenda(calendar_url=calendar.url, start=start, end=end, limit=500)
            if agenda["truncated"]:
                raise CalendarError("The busy-event window is truncated. Narrow the time window before finding free slots.")
            for event in agenda["events"]:
                if event.get("transparency", "OPAQUE").upper() == "TRANSPARENT":
                    continue
                def instant(value):
                    if event["all_day"]:
                        return datetime.fromisoformat(value).replace(tzinfo=zone).astimezone(timezone.utc)
                    return temporal(value, all_day=False)
                left, right = max(start_dt, instant(event["start"])), min(end_dt, instant(event["end"]))
                if right > left:
                    busy.append((left, right))
        cursor = start_dt
        free = []
        duration = timedelta(minutes=duration_minutes)
        for left, right in sorted(busy) + [(end_dt, end_dt)]:
            if left - cursor >= duration:
                free.append({"start": cursor.astimezone(zone).isoformat(),
                             "end": (cursor + duration).astimezone(zone).isoformat(),
                             "available_until": left.astimezone(zone).isoformat()})
            cursor = max(cursor, right)
        return {"slots": free[:limit], "total_count": len(free), "truncated": len(free) > limit,
                "calendar_ids": selected, "timezone": self.settings.timezone,
                "window": {"start": start, "end": end, "end_exclusive": True},
                "duration_minutes": duration_minutes,
                "interpretation": "Each slot is the earliest candidate in a maximal free interval. Availability is a live snapshot, not a reservation."}

    def preview(self, operation, args):
        """Validate and freeze the exact mutation before asking the human."""
        backend = self._arguments(args, write=True)
        if operation == "calendar_create_event":
            try:
                UUID(args["request_id"])
            except Exception:
                raise CalendarError("request_id must be a UUID reused for retries of this create.") from None
            event_fields(args)
            return {"operation": operation, "calendar_id": args["calendar_id"],
                    "changes": {k: v for k, v in args.items() if k not in ("calendar_id", "request_id")}}, backend
        with self.provider_factory(self.settings) as provider:
            current = provider.get_event(calendar_url=backend["calendar_url"], event_url=backend["event_url"])["event"]
        if not current.get("editable"):
            raise CalendarError("Editing or deleting recurring events and invitations is not supported yet.")
        if not args.get("etag") or current["etag"] != args["etag"]:
            raise CalendarError("The event changed. Fetch it again before requesting this write.")
        if operation == "calendar_delete_event":
            return {"operation": operation, "calendar_id": args["calendar_id"],
                    "event": public_event(current)}, backend
        if operation != "calendar_update_event":
            raise CalendarError("Unsupported calendar write operation.")
        values = {k: args.get(k, current[k]) for k in ("summary", "start", "end", "all_day", "location", "description")}
        event_fields(values)
        backend = {"calendar_url": backend["calendar_url"], "event_url": backend["event_url"], "etag": args["etag"], **values}
        return {"operation": operation, "calendar_id": args["calendar_id"],
                "before": public_event(current),
                "changes": {k: args[k] for k in values if k in args}}, backend

    def write(self, operation, backend, *, approved):
        if approved is not True:
            raise CalendarError("Human confirmation is required before a calendar write.")
        with self.provider_factory(self.settings) as provider:
            method = {"calendar_create_event": provider.create_event,
                      "calendar_update_event": provider.update_event,
                      "calendar_delete_event": provider.delete_event}[operation]
            result = method(**backend)
        if "event" in result:
            result["event"] = public_event(result["event"])
        return result
