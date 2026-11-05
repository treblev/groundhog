"""Provider contract and iCloud connection lifecycle. Tools use neutral IDs."""

from contextlib import contextmanager
from typing import Protocol

import caldav

from calendar_mcp.credentials import SecretStore
from integrations.icloud_calendar import ICloudCalendar


class CalendarProvider(Protocol):
    def calendars(self) -> dict: ...
    def agenda(self, **arguments) -> dict: ...
    def get_event(self, **arguments) -> dict: ...
    def create_event(self, **arguments) -> dict: ...
    def update_event(self, **arguments) -> dict: ...
    def delete_event(self, **arguments) -> dict: ...


@contextmanager
def open_provider(settings):
    credentials = SecretStore(settings).get()
    with caldav.DAVClient(url=settings.provider_url, **credentials, timeout=30,
                          ssl_verify_cert=True, require_tls=True, enable_rfc6764=False,
                          rate_limit_handle=False, features="icloud") as client:
        yield ICloudCalendar(client, calendar_urls=[c.url for c in settings.calendars],
                             writable_calendar_urls=[c.url for c in settings.calendars if c.writable],
                             display_timezone=settings.timezone)
