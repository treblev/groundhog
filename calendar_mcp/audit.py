"""Private, cross-process append-only operation records without event content."""

import fcntl
import hashlib
import json
import os
import stat
from datetime import datetime, timezone


class AuditLog:
    def __init__(self, settings):
        self.path = settings.audit_path
        self.actor = settings.actor  # Bound by the local server deployment, never model arguments.

    def append(self, operation, outcome, arguments, request_id):
        target = arguments.get("event_id") or arguments.get("request_id")
        row = {
            "at": datetime.now(timezone.utc).isoformat(), "actor": self.actor,
            "operation": operation, "outcome": outcome, "request_id": request_id,
            "calendar_id": arguments.get("calendar_id"),
            "event_id": hashlib.sha256(target.encode()).hexdigest() if isinstance(target, str) else None,
        }
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("Audit file must be private")
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.write(json.dumps(row) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
