"""Manage user-authored semantic notes for stock tickers."""
import argparse
import json
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb

from agent.semantic_search import sync_stock_note_embeddings
from config.settings import DB_PATH


_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")


def normalize_ticker(value: str) -> str:
    ticker = value.strip().upper()
    if not _TICKER_RE.fullmatch(ticker):
        raise ValueError("ticker must contain only letters, numbers, dots, or hyphens")
    return ticker


def _active_note(con: duckdb.DuckDBPyConnection, note_id: str) -> tuple | None:
    note_id = note_id.strip()
    if len(note_id) < 8 or not re.fullmatch(r"[0-9a-f]+", note_id):
        raise ValueError("note ID must be at least the eight-character hexadecimal prefix")
    rows = con.execute(
        """
        SELECT id, ticker, note, created_at, updated_at
        FROM stock_notes WHERE id LIKE ? AND NOT is_deleted
        """,
        [f"{note_id}%"],
    ).fetchall()
    if len(rows) > 1:
        raise ValueError(f"note ID prefix is ambiguous: {note_id}")
    return rows[0] if rows else None


def add_note(
    ticker: str,
    note: str,
    *,
    db_path: Path | str = DB_PATH,
    embedder=None,
) -> dict:
    ticker = normalize_ticker(ticker)
    note = note.strip()
    if not note:
        raise ValueError("note must not be empty")
    note_id = uuid.uuid4().hex
    con = duckdb.connect(str(db_path))
    transaction_started = False
    try:
        con.execute("BEGIN")
        transaction_started = True
        con.execute(
            "INSERT INTO stock_notes (id, ticker, note) VALUES (?, ?, ?)",
            [note_id, ticker, note],
        )
        con.execute(
            """
            INSERT INTO stock_note_revisions (note_id, revision, note, action)
            VALUES (?, 1, ?, 'created')
            """,
            [note_id, note],
        )
        con.execute("COMMIT")
        transaction_started = False
    except Exception:
        if transaction_started:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    sync_result = _sync(db_path, embedder)
    return {"id": note_id, "ticker": ticker, "note": note, "revision": 1, "embedding": sync_result}


def edit_note(
    note_id: str,
    note: str,
    *,
    db_path: Path | str = DB_PATH,
    embedder=None,
) -> dict:
    note = note.strip()
    if not note:
        raise ValueError("note must not be empty")
    con = duckdb.connect(str(db_path))
    transaction_started = False
    try:
        row = _active_note(con, note_id)
        if row is None:
            raise ValueError(f"active stock note not found: {note_id}")
        canonical_id = row[0]
        next_revision = con.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM stock_note_revisions WHERE note_id = ?",
            [canonical_id],
        ).fetchone()[0]
        con.execute("BEGIN")
        transaction_started = True
        con.execute(
            "UPDATE stock_notes SET note = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            [note, canonical_id],
        )
        con.execute(
            """
            INSERT INTO stock_note_revisions (note_id, revision, note, action)
            VALUES (?, ?, ?, 'edited')
            """,
            [canonical_id, next_revision, note],
        )
        con.execute("COMMIT")
        transaction_started = False
        ticker = row[1]
    except Exception:
        if transaction_started:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    sync_result = _sync(db_path, embedder)
    return {"id": canonical_id, "ticker": ticker, "note": note, "revision": next_revision, "embedding": sync_result}


def delete_note(
    note_id: str,
    *,
    db_path: Path | str = DB_PATH,
    embedder=None,
) -> dict:
    con = duckdb.connect(str(db_path))
    transaction_started = False
    try:
        row = _active_note(con, note_id)
        if row is None:
            raise ValueError(f"active stock note not found: {note_id}")
        canonical_id = row[0]
        next_revision = con.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM stock_note_revisions WHERE note_id = ?",
            [canonical_id],
        ).fetchone()[0]
        con.execute("BEGIN")
        transaction_started = True
        con.execute(
            "UPDATE stock_notes SET is_deleted = TRUE, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            [canonical_id],
        )
        con.execute(
            """
            INSERT INTO stock_note_revisions (note_id, revision, note, action)
            VALUES (?, ?, ?, 'deleted')
            """,
            [canonical_id, next_revision, row[2]],
        )
        con.execute("COMMIT")
        transaction_started = False
        ticker = row[1]
    except Exception:
        if transaction_started:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    sync_result = _sync(db_path, embedder)
    return {"id": canonical_id, "ticker": ticker, "revision": next_revision, "deleted": True, "embedding": sync_result}


def list_notes(ticker: str, *, db_path: Path | str = DB_PATH) -> list[dict]:
    ticker = normalize_ticker(ticker)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        cursor = con.execute(
            """
            SELECT id, ticker, note, created_at, updated_at
            FROM stock_notes
            WHERE ticker = ? AND NOT is_deleted
            ORDER BY updated_at DESC, id
            """,
            [ticker],
        )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        con.close()


def _sync(db_path: Path | str, embedder):
    kwargs = {"db_path": db_path}
    if embedder is not None:
        kwargs["embedder"] = embedder
    return sync_stock_note_embeddings(**kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_parser = subparsers.add_parser("add")
    add_parser.add_argument("ticker")
    add_parser.add_argument("note")
    edit_parser = subparsers.add_parser("edit")
    edit_parser.add_argument("note_id")
    edit_parser.add_argument("note")
    delete_parser = subparsers.add_parser("delete")
    delete_parser.add_argument("note_id")
    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("ticker")
    args = parser.parse_args()
    try:
        if args.command == "add":
            result = add_note(args.ticker, args.note)
        elif args.command == "edit":
            result = edit_note(args.note_id, args.note)
        elif args.command == "delete":
            result = delete_note(args.note_id)
        else:
            result = list_notes(args.ticker)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
