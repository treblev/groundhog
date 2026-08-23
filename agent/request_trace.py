"""Local JSONL request tracing shared by agents and asynchronous workers."""
import contextlib
import contextvars
import fcntl
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from config.settings import OPENCLAW_MEDIA_STATE_PATH

TRACE_SCHEMA_VERSION = 1
TRACE_RETENTION_DAYS = 15
TRACE_DIR = OPENCLAW_MEDIA_STATE_PATH.parent / "logs" / "request-traces"

_CURRENT_TRACE: contextvars.ContextVar["RequestTrace | None"] = contextvars.ContextVar(
    "groundhog_request_trace", default=None
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return _timestamp(value)
    if isinstance(value, bytes):
        return {"byte_count": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(item) for item in value]
    if hasattr(value, "model_dump"):
        return _safe(value.model_dump())
    if hasattr(value, "dict"):
        return _safe(value.dict())
    return str(value)


def _append_record(record: dict, trace_dir: Path) -> None:
    trace_dir.mkdir(parents=True, exist_ok=True)
    day = str(record["started_at"])[:10]
    path = trace_dir / f"{day}.jsonl"
    encoded = json.dumps(_safe(record), sort_keys=True, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as output:
        fcntl.flock(output.fileno(), fcntl.LOCK_EX)
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())
        fcntl.flock(output.fileno(), fcntl.LOCK_UN)


def cleanup_trace_logs(
    trace_dir: Path | None = None,
    retention_days: int = TRACE_RETENTION_DAYS,
    now: datetime | None = None,
) -> list[str]:
    """Delete whole daily trace files older than the local retention window."""
    trace_dir = trace_dir or TRACE_DIR
    if retention_days < 1:
        raise ValueError("Trace retention must be at least one day.")
    if not trace_dir.is_dir():
        return []
    cutoff = (now or _utcnow()).date() - timedelta(days=retention_days)
    removed = []
    for path in trace_dir.glob("????-??-??.jsonl"):
        try:
            day = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            path.unlink(missing_ok=True)
            removed.append(str(path))
    return removed


class RequestTrace:
    """Append one ordered request lifecycle to the shared local JSONL log."""

    def __init__(
        self,
        operation: str,
        source: str,
        request_id: str | None = None,
        started_at: datetime | None = None,
        trace_dir: Path | None = None,
    ):
        self.request_id = request_id or uuid.uuid4().hex
        self.operation = operation
        self.source = source
        self.started_at = started_at or _utcnow()
        if self.started_at.tzinfo is None:
            self.started_at = self.started_at.replace(tzinfo=timezone.utc)
        self.trace_dir = trace_dir or TRACE_DIR
        self._monotonic_started = time.monotonic()
        self._ended = False
        self._call_counts = {"llm_call": 0, "tool_call": 0}
        self._tool_counts: dict[str, int] = {}
        self._prompt_chars = 0
        self._prompt_eval_count = 0
        self._prompt_eval_duration_ns = 0

    def _write(self, event_type: str, started_at: datetime, **fields) -> None:
        _append_record(
            {
                "schema_version": TRACE_SCHEMA_VERSION,
                "request_id": self.request_id,
                "type": event_type,
                "started_at": _timestamp(started_at),
                **fields,
            },
            self.trace_dir,
        )

    def start(self, **metadata) -> "RequestTrace":
        cleanup_trace_logs(self.trace_dir)
        self._write(
            "request_start",
            self.started_at,
            source=self.source,
            operation=self.operation,
            metadata=metadata,
        )
        return self

    def record_call(
        self,
        call_type: str,
        started_at: datetime,
        duration_ms: int,
        status: str,
        **fields,
    ) -> None:
        if call_type not in {"llm_call", "tool_call"}:
            raise ValueError(f"Unsupported call type: {call_type}")
        if status not in {"passed", "failed"}:
            raise ValueError(f"Unsupported call status: {status}")
        self._call_counts[call_type] += 1
        if call_type == "tool_call":
            tool = str(fields.get("tool") or "unknown")
            self._tool_counts[tool] = self._tool_counts.get(tool, 0) + 1
        else:
            self._prompt_chars += int(fields.get("prompt_chars") or 0)
            self._prompt_eval_count += int(fields.get("prompt_eval_count") or 0)
            self._prompt_eval_duration_ns += int(fields.get("prompt_eval_duration_ns") or 0)
        self._write(
            call_type,
            started_at,
            duration_ms=max(0, int(duration_ms)),
            status=status,
            **fields,
        )

    def summary(self) -> dict:
        """Return sanitized aggregate metrics for the current request."""
        return {
            "llm_calls": self._call_counts["llm_call"],
            "tool_calls": self._call_counts["tool_call"],
            "tools": dict(sorted(self._tool_counts.items())),
            "prompt_chars": self._prompt_chars,
            "prompt_eval_count": self._prompt_eval_count,
            "prompt_eval_duration_ms": round(self._prompt_eval_duration_ns / 1_000_000, 3),
        }

    def end(self, status: str, error: str | None = None, **metadata) -> None:
        if self._ended:
            return
        if status not in {"passed", "failed"}:
            raise ValueError(f"Unsupported request status: {status}")
        self._ended = True
        metadata.setdefault("trace_summary", self.summary())
        self._write(
            "request_end",
            _utcnow(),
            duration_ms=max(0, int((_utcnow() - self.started_at).total_seconds() * 1000)),
            status=status,
            error=error,
            metadata=metadata,
        )


def current_trace() -> RequestTrace | None:
    return _CURRENT_TRACE.get()


@contextlib.contextmanager
def use_trace(trace: RequestTrace) -> Iterator[RequestTrace]:
    token = _CURRENT_TRACE.set(trace)
    try:
        yield trace
    finally:
        _CURRENT_TRACE.reset(token)


def record_llm_call(
    *,
    started_at: datetime,
    monotonic_started: float,
    model: str,
    prompt: Any,
    response: Any = None,
    error: Exception | None = None,
    metadata: dict | None = None,
) -> None:
    trace = current_trace()
    owned_trace = trace is None
    if trace is None:
        operation = str((metadata or {}).get("operation") or "standalone_llm_call")
        trace = RequestTrace(
            operation=operation,
            source="groundhog_internal",
            started_at=started_at,
        ).start(model=model)
    trace.record_call(
        "llm_call",
        started_at,
        int((time.monotonic() - monotonic_started) * 1000),
        "failed" if error else "passed",
        model=model,
        prompt=_safe(prompt),
        response=_safe(response),
        error=str(error) if error else None,
        metadata=_safe(metadata or {}),
    )
    if owned_trace:
        trace.end("failed" if error else "passed", str(error) if error else None)


def record_tool_call(
    *,
    started_at: datetime,
    monotonic_started: float,
    tool: str,
    arguments: Any,
    result: Any = None,
    error: Exception | None = None,
) -> None:
    trace = current_trace()
    owned_trace = trace is None
    if trace is None:
        trace = RequestTrace(
            operation="standalone_tool_call",
            source="groundhog_internal",
            started_at=started_at,
        ).start(tool=tool)
    trace.record_call(
        "tool_call",
        started_at,
        int((time.monotonic() - monotonic_started) * 1000),
        "failed" if error else "passed",
        tool=tool,
        arguments=_safe(arguments),
        result=_safe(result),
        error=str(error) if error else None,
    )
    if owned_trace:
        trace.end("failed" if error else "passed", str(error) if error else None)
