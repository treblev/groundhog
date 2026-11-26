"""Serialize file-backed DuckDB connections across processes and threads.

Every participating reader and writer holds the same exclusive advisory lock
until its connection closes. Never delete the sidecar lock file: replacing its
inode would let two processes acquire different locks for the same database.
"""
import fcntl
import math
import threading
import time
from pathlib import Path

import duckdb

from config.settings import DB_LOCK_TIMEOUT_SECONDS, database_lock_path

_owners_guard = threading.Lock()
_owners: set[tuple[str, int]] = set()


class DatabaseLockTimeout(TimeoutError):
    """Another database user did not finish within the configured wait."""


class LockedConnection:
    def __init__(self, connection, lock_file, owner):
        self._connection = connection
        self._lock_file = lock_file
        self._owner = owner

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def execute(self, *args, **kwargs):
        self._connection.execute(*args, **kwargs)
        return self

    def executemany(self, *args, **kwargs):
        self._connection.executemany(*args, **kwargs)
        return self

    def cursor(self):
        raise RuntimeError("Open a separate managed connection in each thread; cursors must not outlive the database lock.")

    def close(self):
        if self._lock_file is None:
            return
        self._connection.close()
        self._lock_file.close()
        self._lock_file = None
        with _owners_guard:
            _owners.discard(self._owner)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        if self.__dict__.get("_lock_file") is not None:
            self.close()


def connect(db_path: Path | str, *, read_only: bool = False,
            timeout: float | None = None):
    """Wait for exclusive file access, preserving DuckDB transaction semantics.

    Memory databases need no file lock. Nested connections in one thread are
    rejected immediately instead of waiting for a lock the caller itself owns.
    """
    if str(db_path) == ":memory:":
        return duckdb.connect(":memory:", read_only=read_only)
    timeout = DB_LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError("Database lock timeout must be finite and non-negative.")
    path = Path(db_path).resolve()
    owner = (str(path), threading.get_ident())
    with _owners_guard:
        if owner in _owners:
            raise RuntimeError("Close the current database connection before opening another in the same thread.")
        _owners.add(owner)
    lock_file = None
    deadline = time.monotonic() + timeout
    try:
        lock_file = database_lock_path(path).open("a+b")
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DatabaseLockTimeout(f"Timed out after {timeout:g}s waiting for database access: {path}") from None
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        while True:
            try:
                connection = duckdb.connect(str(path), read_only=read_only)
                return LockedConnection(connection, lock_file, owner)
            except duckdb.IOException as error:
                # Older tools may still open DuckDB without our sidecar lock.
                if "could not set lock" not in str(error).lower():
                    raise
                if time.monotonic() >= deadline:
                    raise DatabaseLockTimeout(f"Timed out after {timeout:g}s waiting for an unmanaged DuckDB connection: {path}") from error
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    except BaseException:
        if lock_file is not None:
            lock_file.close()
        with _owners_guard:
            _owners.discard(owner)
        raise
