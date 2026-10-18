"""Read PlayMetrics schedule details and send a concise Telegram reminder.

The script uses OpenClaw's managed browser so the PlayMetrics login stays in a
browser profile instead of this repository. It performs work only on Tuesday,
Wednesday, Thursday, and Friday in the configured timezone:

* Tuesday-Thursday: report today's practice time and field.
* Friday: report tomorrow's game time and field, when a game exists.

Required configuration is supplied by arguments or environment variables. The
script never stores a PlayMetrics password or Telegram token.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


DEFAULT_TIMEZONE = "America/Phoenix"
ACTIVE_WEEKDAYS = {1, 2, 3, 4}  # Tuesday-Friday
PRACTICE_WEEKDAYS = {1, 2, 3}
TIME_RE = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s*(?:AM|PM)\b"
    r"(?:\s*(?:-|–|—|to)\s*\d{1,2}(?::\d{2})?\s*(?:AM|PM)\b)?",
    re.IGNORECASE,
)
FIELD_RE = re.compile(
    r"\b(?:field|fld)\s*(?:#|no\.?|number)?\s*[:\-]?\s*[A-Za-z0-9-]+",
    re.IGNORECASE,
)
LOGIN_MARKERS = ("log in", "login", "sign in", "forgot password")


@dataclass(frozen=True)
class ScheduleRequest:
    kind: str
    target_date: date
    relative_label: str


@dataclass(frozen=True)
class ScheduleEvent:
    time: str
    field: str


def request_for_day(run_date: date) -> ScheduleRequest | None:
    """Return the schedule query for a run date, or None on inactive days."""
    if run_date.weekday() not in ACTIVE_WEEKDAYS:
        return None
    if run_date.weekday() in PRACTICE_WEEKDAYS:
        return ScheduleRequest("practice", run_date, "today")
    return ScheduleRequest("game", run_date + timedelta(days=1), "tomorrow")


def date_labels(value: date) -> list[str]:
    """Produce labels commonly rendered by weekly/monthly web calendars."""
    weekday = value.strftime("%A")
    short_weekday = value.strftime("%a")
    month = value.strftime("%B")
    short_month = value.strftime("%b")
    labels = [
        value.isoformat(),
        f"{value.month}/{value.day}/{value.year}",
        f"{value.month}/{value.day}",
        f"{weekday}, {month} {value.day}, {value.year}",
        f"{weekday}, {month} {value.day}",
        f"{short_weekday}, {short_month} {value.day}",
        f"{month} {value.day}",
        f"{short_month} {value.day}",
    ]
    return list(dict.fromkeys(labels))


def _normalise_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _contains_any(text: str, values: Iterable[str]) -> bool:
    lowered = text.casefold()
    return any(value.casefold() in lowered for value in values)


def _extract_events_from_texts(
    texts: Iterable[str],
    request: ScheduleRequest,
    team_filter: str | None = None,
) -> list[ScheduleEvent]:
    """Extract unique, fully specified events from browser candidate text."""
    labels = date_labels(request.target_date)
    keyword = request.kind.casefold()
    matching: list[str] = []

    for raw_text in texts:
        text = _normalise_text(raw_text)
        lowered = text.casefold()
        if not text or keyword not in lowered:
            continue
        if team_filter and team_filter.casefold() not in lowered:
            continue
        if _contains_any(text, labels):
            matching.append(text)

    # Some event cards omit the date because the date is rendered in a sibling
    # calendar header. The browser extractor walks ancestors to include it, but
    # retain a conservative fallback for a calendar that has only one matching
    # event visible. Multiple undated matches are intentionally treated as
    # ambiguous instead of guessing.
    if not matching:
        undated = []
        for raw_text in texts:
            text = _normalise_text(raw_text)
            lowered = text.casefold()
            if keyword not in lowered:
                continue
            if team_filter and team_filter.casefold() not in lowered:
                continue
            undated.append(text)
        unique_undated = list(dict.fromkeys(undated))
        if len(unique_undated) == 1:
            matching = unique_undated

    events: list[ScheduleEvent] = []
    seen: set[tuple[str, str]] = set()
    for text in matching:
        time_match = TIME_RE.search(text)
        field_match = FIELD_RE.search(text)
        if not time_match or not field_match:
            continue
        event = ScheduleEvent(
            time=_normalise_text(time_match.group(0)).upper(),
            field=_normalise_text(field_match.group(0)),
        )
        key = (event.time.casefold(), event.field.casefold())
        if key not in seen:
            seen.add(key)
            events.append(event)
    return events


def format_message(request: ScheduleRequest, events: list[ScheduleEvent]) -> str:
    heading = f"{request.kind.title()} {request.relative_label}"
    rendered_date = (
        f"{request.target_date.strftime('%a %b')} {request.target_date.day}"
    )
    details = "; ".join(f"{event.time} — {event.field}" for event in events)
    return f"{heading} ({rendered_date}): {details}"


def _parse_json_output(stdout: str) -> Any:
    stripped = stdout.strip()
    if not stripped:
        raise RuntimeError("OpenClaw returned no JSON output")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        for line in reversed(stripped.splitlines()):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    raise RuntimeError(f"OpenClaw returned invalid JSON: {stripped[:500]}")


def _run(command: list[str], timeout: int = 60) -> Any:
    proc = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise RuntimeError(f"Command failed ({proc.returncode}): {detail}")
    return _parse_json_output(proc.stdout)


def _find_target_id(value: Any) -> str | None:
    """Find a browser target ID in OpenClaw's version-dependent JSON shape."""
    if isinstance(value, dict):
        for key in ("targetId", "target_id"):
            target_id = value.get(key)
            if isinstance(target_id, str) and target_id:
                return target_id
        for child in value.values():
            target_id = _find_target_id(child)
            if target_id:
                return target_id
    elif isinstance(value, list):
        for child in value:
            target_id = _find_target_id(child)
            if target_id:
                return target_id
    return None


def _find_page_payload(value: Any) -> dict[str, Any] | None:
    """Find the object returned by the in-page evaluation."""
    if isinstance(value, dict):
        if "candidates" in value and "bodyText" in value:
            return value
        for key in ("result", "value", "data", "payload"):
            if key in value:
                payload = _find_page_payload(value[key])
                if payload:
                    return payload
        for child in value.values():
            payload = _find_page_payload(child)
            if payload:
                return payload
    elif isinstance(value, list):
        for child in value:
            payload = _find_page_payload(child)
            if payload:
                return payload
    return None


def _browser_base(profile: str | None) -> list[str]:
    command = ["openclaw", "browser", "--json"]
    if profile:
        command.extend(["--browser-profile", profile])
    return command


def _extract_page(
    calendar_url: str,
    request: ScheduleRequest,
    browser_profile: str | None,
) -> dict[str, Any]:
    """Open PlayMetrics and return visible event candidates from the DOM."""
    label = f"groundhog-playmetrics-{int(time.time())}"
    base = _browser_base(browser_profile)
    _run([*base, "start"], timeout=60)
    opened = _run([*base, "open", calendar_url, "--label", label], timeout=60)
    target_id = _find_target_id(opened)
    if not target_id:
        raise RuntimeError("OpenClaw did not return a target ID for the PlayMetrics tab")

    labels_json = json.dumps(date_labels(request.target_date))
    keyword_json = json.dumps(request.kind)
    script = f"""() => {{
      const dateLabels = {labels_json}.map((value) => value.toLowerCase());
      const keyword = {keyword_json}.toLowerCase();
      const visible = (el) => {{
        const style = window.getComputedStyle(el);
        return style.visibility !== 'hidden' && style.display !== 'none';
      }};
      const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
      const nodes = Array.from(document.querySelectorAll(
        'article, li, [role="listitem"], [role="gridcell"], [role="button"], button, a, [data-testid], [aria-label], [class*="event"], [class*="Event"]'
      ));
      const candidates = [];
      for (const node of nodes) {{
        if (!visible(node)) continue;
        const ownText = clean(node.innerText || node.textContent);
        if (!ownText.toLowerCase().includes(keyword)) continue;
        let selected = node;
        let cursor = node;
        for (let depth = 0; depth < 6 && cursor; depth += 1) {{
          const text = clean(cursor.innerText || cursor.textContent);
          if (dateLabels.some((label) => text.toLowerCase().includes(label))) {{
            selected = cursor;
            break;
          }}
          cursor = cursor.parentElement;
        }}
        const text = clean(selected.innerText || selected.textContent);
        if (text && text.length <= 5000) candidates.push(text);
      }}
      return {{
        url: window.location.href,
        title: document.title,
        bodyText: clean(document.body?.innerText || ''),
        hasPasswordInput: Boolean(document.querySelector('input[type="password"]')),
        candidates: Array.from(new Set(candidates)).slice(0, 100),
      }};
    }}"""

    try:
        _run(
            [
                *base,
                "wait",
                "--target-id",
                target_id,
                "--load",
                "domcontentloaded",
            ],
            timeout=60,
        )
        _run(
            [*base, "wait", "--target-id", target_id, "--time", "1500"],
            timeout=30,
        )
        result = _run(
            [*base, "evaluate", "--target-id", target_id, "--fn", script],
            timeout=60,
        )
    finally:
        try:
            _run([*base, "close", target_id], timeout=30)
        except RuntimeError as error:
            print(f"Warning: could not close PlayMetrics tab: {error}", file=sys.stderr)

    page = _find_page_payload(result)
    if page is None:
        raise RuntimeError("OpenClaw browser returned an unexpected page payload")
    body_text = str(page.get("bodyText", ""))
    if page.get("hasPasswordInput") or (
        _contains_any(body_text, LOGIN_MARKERS)
        and not page.get("candidates")
    ):
        raise RuntimeError(
            "PlayMetrics is not authenticated in the selected OpenClaw browser profile"
        )
    return page


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"sent": {}}
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read state file {path}: {error}") from error
    if not isinstance(state, dict) or not isinstance(state.get("sent", {}), dict):
        raise RuntimeError(f"Invalid PlayMetrics state file: {path}")
    state.setdefault("sent", {})
    return state


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def _dedupe_key(request: ScheduleRequest, message: str) -> str:
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()[:16]
    return f"{request.kind}:{request.target_date.isoformat()}:{digest}"


def _send_telegram(target: str, message: str) -> None:
    result = _run(
        [
            "openclaw",
            "message",
            "send",
            "--channel",
            "telegram",
            "--target",
            target,
            "--message",
            message,
            "--json",
        ],
        timeout=60,
    )
    payload = result.get("payload") if isinstance(result, dict) else None
    ok = (
        bool((payload or {}).get("ok", result.get("ok", False)))
        if isinstance(result, dict)
        else False
    )
    if not ok:
        raise RuntimeError(f"Telegram delivery did not report success: {result!r}")


def run(args: argparse.Namespace) -> int:
    timezone = ZoneInfo(args.timezone)
    run_date = (
        date.fromisoformat(args.date) if args.date else datetime.now(timezone).date()
    )
    request = request_for_day(run_date)
    if request is None:
        print(f"No PlayMetrics action scheduled for {run_date.isoformat()}.")
        return 0

    page = _extract_page(args.calendar_url, request, args.browser_profile)
    candidates = page.get("candidates") or []
    if not isinstance(candidates, list):
        raise RuntimeError("PlayMetrics browser candidates were not a list")
    events = _extract_events_from_texts(
        [str(value) for value in candidates],
        request,
        args.team_filter,
    )
    if not events:
        if request.kind == "game":
            print(
                f"No game found for {request.target_date.isoformat()}; "
                "no Telegram message sent."
            )
            return 0
        raise RuntimeError(
            f"No complete practice time and field found for {request.target_date.isoformat()}"
        )

    message = format_message(request, events)
    state = _load_state(args.state_path)
    key = _dedupe_key(request, message)
    if key in state["sent"]:
        print(f"PlayMetrics reminder already sent: {message}")
        return 0
    if args.dry_run:
        print(f"DRY RUN Telegram message: {message}")
        return 0

    _send_telegram(args.telegram_target, message)
    state["sent"][key] = {
        "message": message,
        "sentAt": datetime.now(timezone).isoformat(),
    }
    _write_state(args.state_path, state)
    print(f"Sent PlayMetrics reminder: {message}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--calendar-url",
        default=os.environ.get("PLAYMETRICS_CALENDAR_URL"),
        help="PlayMetrics calendar URL (or PLAYMETRICS_CALENDAR_URL)",
    )
    parser.add_argument(
        "--telegram-target",
        default=os.environ.get("OPENCLAW_TELEGRAM_TARGET"),
        help="Telegram chat id/username (or OPENCLAW_TELEGRAM_TARGET)",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=(
            Path(value)
            if (value := os.environ.get("PLAYMETRICS_STATE_PATH"))
            else None
        ),
        help="dedupe state file (or PLAYMETRICS_STATE_PATH)",
    )
    parser.add_argument(
        "--browser-profile",
        default=os.environ.get("PLAYMETRICS_BROWSER_PROFILE"),
        help="authenticated OpenClaw browser profile; default uses OpenClaw's configured profile",
    )
    parser.add_argument(
        "--team-filter",
        default=os.environ.get("PLAYMETRICS_TEAM_FILTER"),
        help="optional team/player text required in matching events",
    )
    parser.add_argument(
        "--timezone",
        default=os.environ.get("PLAYMETRICS_TIMEZONE", DEFAULT_TIMEZONE),
    )
    parser.add_argument("--date", help="override run date as YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.calendar_url:
        parser.error("set --calendar-url or PLAYMETRICS_CALENDAR_URL")
    if not args.telegram_target:
        parser.error("set --telegram-target or OPENCLAW_TELEGRAM_TARGET")
    if not args.state_path:
        parser.error("set --state-path or PLAYMETRICS_STATE_PATH")
    try:
        return run(args)
    except (RuntimeError, subprocess.TimeoutExpired, ValueError) as error:
        print(f"PlayMetrics reminder failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
