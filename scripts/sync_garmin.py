"""Run the read-only Garmin extraction followed by the Groundhog importer."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion.garmin import import_garmin
from config.settings import database_lock_path


@contextmanager
def _sync_lock(source_db: Path):
    """Protect SQLite extraction and import against overlapping sync runs."""
    lock_path = database_lock_path(source_db)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another Garmin sync is already running; wait for it to finish.") from None
        yield


@contextmanager
def _progress(stage: str, interval: float = 30):
    """Keep the scheduler informed during slow extraction and database waits."""
    stopped = threading.Event()
    started = time.monotonic()
    print(f"Garmin sync: {stage} started", flush=True)

    def report():
        while not stopped.wait(interval):
            print(f"Garmin sync: {stage} still running ({time.monotonic() - started:.0f}s)", flush=True)

    worker = threading.Thread(target=report, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join()


def _run_extractor(command: list[str], timeout: float):
    """Bound extraction and reap its process group on timeout or cancellation."""
    child = subprocess.Popen(command, start_new_session=True,
                             env={**os.environ, "PYTHONUNBUFFERED": "1"})
    try:
        returncode = child.wait(timeout=timeout)
    except BaseException:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        raise
    if returncode:
        raise subprocess.CalledProcessError(returncode, command)


@contextmanager
def _handle_termination():
    # Raising unwinds the heartbeat and file-lock contexts. The extractor
    # handler also terminates its separate process group before unwinding.
    def terminate(signum, frame):
        raise SystemExit(128 + signum)

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract Garmin activities and import them into Groundhog.")
    parser.add_argument("--extractor", required=True, type=Path, help="Path to the garmin CLI")
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--groundhog-db", type=Path, default=None)
    parser.add_argument("--lookback-days", type=int, default=7, help="Trailing days to re-import (default: 7)")
    parser.add_argument("--extractor-timeout-seconds", type=int, default=900,
                        help="Maximum extractor runtime (default: 900 seconds)")
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Re-import activities already present in Groundhog (slow; manual repair only)",
    )
    args = parser.parse_args(argv)
    if args.lookback_days < 0:
        parser.error("--lookback-days must be non-negative")
    if args.extractor_timeout_seconds <= 0:
        parser.error("--extractor-timeout-seconds must be positive")

    end_date = date.today() + timedelta(days=1)
    start_date = date.today() - timedelta(days=args.lookback_days)
    command = [
        str(args.extractor),
        "extract",
        "--data-types",
        "ACTIVITIES_LIST",
        "--data-types",
        "ACTIVITY",
        "--data-types",
        "STEPS",
        "--db-path",
        str(args.source_db),
        "--start-date",
        start_date.isoformat(),
        "--end-date",
        end_date.isoformat(),
    ]
    with _handle_termination(), _sync_lock(args.source_db):
        with _progress("extraction"):
            _run_extractor(command, args.extractor_timeout_seconds)
        with _progress("database import (including lock waits)"):
            summary = import_garmin(
                args.source_db,
                args.groundhog_db,
                start_date=start_date,
                end_date=end_date,
                only_missing=not args.refresh_existing,
            )
        print(json.dumps(summary, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
