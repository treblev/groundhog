"""Read-only Mac MCP server for current EventKit reminders via remindctl."""

import argparse
import json
from pathlib import Path
import subprocess
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from integrations.icloud_calendar import CalendarError, _instant, read_window


def read_reminders(command, *, start, end, timezone, limit=100, list_ids=()):
    left, right = read_window(start, end, limit)
    zone = ZoneInfo(timezone)
    try:
        # The executable and selected lists come from operator configuration.
        # Model arguments cannot choose a command or turn this into a write.
        process = subprocess.run([command, "show", "open", "--json", "--no-input"],
                                 capture_output=True, text=True, timeout=30)
        if process.returncode:
            raise ValueError
        raw = json.loads(process.stdout)
        if not isinstance(raw, list):
            raise ValueError
        rows = []
        for task in raw:
            if list_ids and task["listID"] not in list_ids:
                continue
            if task["isCompleted"] or not task.get("dueDate"):
                continue
            due = task["dueDate"]
            all_day = task.get("dueDateIsAllDay", False)
            if all_day:
                parts = task.get("dueDateComponents", {})
                if all(key in parts for key in ("year", "month", "day")):
                    due = f'{parts["year"]:04}-{parts["month"]:02}-{parts["day"]:02}'
                else:
                    due = _instant(due, zone).astimezone(zone).date().isoformat()
            elif _instant(due, zone).utcoffset() is not None:
                due = _instant(due, zone).astimezone(zone).isoformat()
            if not left <= _instant(due, zone) < right:
                continue
            rows.append({"uid": task["id"], "event_url": "urn:apple-reminder:" + task["id"],
                         "summary": task["title"], "list_name": task.get("listName", ""),
                         "start": None, "due": due, "scheduled_at": due, "schedule_basis": "due",
                         "all_day": all_day, "status": "NEEDS-ACTION", "recurring": bool(task.get("recurrenceRule"))})
        rows.sort(key=lambda row: (_instant(row["due"], zone), row["uid"]))
        return {"status": "ok", "reminders": rows[:limit], "total_count": len(rows),
                "truncated": len(rows) > limit, "timezone": timezone,
                "window": {"start": start, "end": end, "end_exclusive": True},
                "scope": "Current unfinished EventKit reminders due inside the window, in the configured lists. Undated and outside-window reminders are excluded; future recurrence instances are not projected.",
                "source": "macOS EventKit via node-hosted MCP"}
    except Exception:
        raise CalendarError("Apple Reminders access failed. Check macOS Reminders permission and the configured remindctl executable. No reminder result was verified.") from None


def create_server(command, list_ids=()):
    server = FastMCP("groundhog-apple-reminders", log_level="ERROR")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True))
    def apple_reminders_agenda(start: str, end: str, timezone: str, limit: int = 100) -> dict:
        """Read current unfinished Apple Reminders due within an offset-aware window. Dates use the supplied timezone; exclusive end; max 93 days. Undated/outside-window tasks excluded. Read-only. Errors mean unavailable, never empty."""
        return read_reminders(command, start=start, end=end, timezone=timezone, limit=limit, list_ids=list_ids)

    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remindctl", required=True, help="Absolute path to the approved remindctl executable")
    parser.add_argument("--list-id", action="append", default=[], help="Restrict reads to these EventKit list IDs")
    args = parser.parse_args()
    if not Path(args.remindctl).is_absolute():
        parser.error("--remindctl must be absolute")
    create_server(args.remindctl, args.list_id).run(transport="stdio")
