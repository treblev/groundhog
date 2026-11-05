"""Configuration for the standalone service. All deployment paths are explicit."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from integrations.icloud_calendar import CalendarError, icloud_url


@dataclass(frozen=True)
class CalendarConfig:
    id: str
    url: str
    writable: bool = False


@dataclass(frozen=True)
class Settings:
    provider: str
    provider_url: str
    timezone: str
    credential_file: Path
    keyring_service: str
    keyring_account: str
    audit_path: Path
    actor: str
    calendars: tuple[CalendarConfig, ...]

    @classmethod
    def load(cls, path: str | Path) -> "Settings":
        try:
            raw = json.loads(Path(path).read_text())
            settings = cls(
                provider=raw["provider"], provider_url=icloud_url(raw["provider_url"]),
                timezone=raw["timezone"], credential_file=Path(raw["credential_file"]),
                keyring_service=raw["keyring_service"], keyring_account=raw["keyring_account"],
                audit_path=Path(raw["audit_path"]), actor=raw["actor"],
                calendars=tuple(CalendarConfig(item["id"], icloud_url(item["url"]), item.get("writable", False))
                                for item in raw.get("calendars", [])),
            )
            if settings.provider != "icloud":
                raise ValueError
            ZoneInfo(settings.timezone)
            if any(not isinstance(value, str) or not value.strip() for value in (
                settings.keyring_service, settings.keyring_account, settings.actor)):
                raise ValueError
            if (not settings.credential_file.is_absolute() or not settings.audit_path.is_absolute()
                    or settings.credential_file == settings.audit_path):
                raise ValueError
            if (len({c.id for c in settings.calendars}) != len(settings.calendars)
                    or len({c.url for c in settings.calendars}) != len(settings.calendars)
                    or any(not isinstance(c.id, str) or not c.id.strip() or type(c.writable) is not bool
                           for c in settings.calendars)):
                raise ValueError
            return settings
        except Exception:
            raise CalendarError("Invalid calendar configuration. Check provider, timezone, unique calendar IDs, and absolute private storage paths.") from None

    def calendar(self, calendar_id: str, *, write: bool = False) -> CalendarConfig:
        match = next((c for c in self.calendars if c.id == calendar_id), None)
        if match is None or (write and not match.writable):
            raise CalendarError("The selected calendar is not enabled for this operation.")
        return match
