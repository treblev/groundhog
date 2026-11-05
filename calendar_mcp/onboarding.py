"""Interactive calendar selection for the trusted local account owner."""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from calendar_mcp.provider import open_provider
from calendar_mcp.settings import Settings
from integrations.icloud_calendar import CalendarError, icloud_url


def _selection(text, count):
    if not text.strip():
        return set()
    try:
        values = {int(value) for value in re.split(r"[,\s]+", text.strip())}
        if not values <= set(range(1, count + 1)):
            raise ValueError
        return values
    except ValueError:
        raise CalendarError("Choose calendar numbers from the displayed list, separated by commas, or leave the selection empty.") from None


def select_calendars(settings, config_path, *, read_input=input, emit=print, provider_factory=open_provider):
    path = Path(config_path).resolve()
    original = path.read_bytes()
    if Settings.load(path) != settings:
        raise CalendarError("Calendar configuration changed. Run selection again.")
    with provider_factory(settings) as provider:
        calendars = provider.calendars()["calendars"]
    for index, row in enumerate(calendars, 1):
        emit(f"{index}. {json.dumps(row['name'], ensure_ascii=True)}")
    reads = _selection(read_input("Read which calendars? Numbers separated by commas; blank selects none: "), len(calendars))
    writes = _selection(read_input("Allow writes to which selected calendars? Blank keeps all read-only: "), len(calendars))
    if not writes <= reads:
        raise CalendarError("Writable calendars must also be selected for reading.")
    existing = {calendar.url: calendar.id for calendar in settings.calendars}
    selected = []
    for index in sorted(reads):
        url = icloud_url(calendars[index - 1]["calendar_url"])
        selected.append({"id": existing.get(url, "calendar-" + hashlib.sha256(url.encode()).hexdigest()[:12]),
                         "url": url, "writable": index in writes})
    raw = json.loads(original)
    raw["calendars"] = selected
    if path.read_bytes() != original:
        raise CalendarError("Calendar configuration changed during selection. Run it again.")
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(raw, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"readable_calendars": len(reads), "writable_calendars": len(writes)}
