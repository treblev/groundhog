"""Best-effort, local-only JSONL request traces for the Groundhog agent."""
from __future__ import annotations

import json
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Groundhog runs on macOS/Linux
    fcntl = None


def json_safe(value: Any) -> Any:
    """Convert LangChain/MCP values to JSON-safe, inspectable local data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if hasattr(value, "model_dump"):
        return json_safe(value.model_dump())
    if hasattr(value, "dict"):
        return json_safe(value.dict())
    return str(value)


class RequestTrace:
    """Append ordered events for one request without ever breaking that request."""

    def __init__(self, directory: Path, retention_days: int = 30):
        self.directory = directory
        self.retention_days = retention_days
        self.trace_id = uuid.uuid4().hex
        self.sequence = 0
        self.started_at = perf_counter()

    def record(self, event_type: str, component: str, *, outcome: str | None = None,
               duration_ms: float | None = None, payload: Any | None = None) -> None:
        try:
            self.sequence += 1
            now = datetime.now(timezone.utc)
            event = {
                "trace_id": self.trace_id,
                "sequence": self.sequence,
                "timestamp": now.isoformat(),
                "event_type": event_type,
                "component": component,
            }
            if outcome is not None:
                event["outcome"] = outcome
            if duration_ms is not None:
                event["duration_ms"] = round(duration_ms, 3)
            if payload is not None:
                event["payload"] = json_safe(payload)
            self._append(now, event)
        except Exception:
            # Observability is intentionally unable to change request behavior.
            pass

    def elapsed_ms(self) -> float:
        return (perf_counter() - self.started_at) * 1000

    def exception(self, event_type: str, component: str, error: BaseException) -> None:
        self.record(event_type, component, outcome="failed", duration_ms=self.elapsed_ms(), payload={
            "error": str(error), "traceback": traceback.format_exc(),
        })

    def _append(self, now: datetime, event: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._prune(now)
        path = self.directory / f"{now.date().isoformat()}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _prune(self, now: datetime) -> None:
        cutoff = now.date() - timedelta(days=self.retention_days)
        for path in self.directory.glob("*.jsonl"):
            try:
                if datetime.strptime(path.stem, "%Y-%m-%d").date() < cutoff:
                    path.unlink()
            except (ValueError, OSError):
                continue


def read_events(directory: Path) -> list[dict[str, Any]]:
    """Read valid trace events from the local JSONL files, oldest first."""
    events = []
    for path in sorted(directory.glob("*.jsonl")) if directory.exists() else []:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
        except OSError:
            continue
    return events
