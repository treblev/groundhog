"""Local semantic indexing and retrieval for Groundhog documents."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hashlib
import json
import re
from collections.abc import Callable
from datetime import date

import duckdb

from agent.embeddings import embed_texts
from config.settings import DB_PATH, OLLAMA_EMBEDDING_MODEL

DOMAIN_WORKOUT = "workout"
DOMAIN_STOCK_ALERT = "stock_alert"
DOMAIN_STOCK_NOTE = "stock_note"
SUPPORTED_DOMAINS = {DOMAIN_WORKOUT, DOMAIN_STOCK_ALERT, DOMAIN_STOCK_NOTE}
BATCH_SIZE = 32
MAX_RESULTS = 10
_WEEKLY_SUPERTREND_ALERT_TYPES = {
    "supertrend_weekly_bullish",
    "supertrend_weekly_bearish",
}

_SECTION_HEADING_RE = re.compile(
    r"^(?:fitness(?:\s*\+\s*performance)?|performance|hyrox\b.*|"
    r"tread\s+block\b.*|row\s+block\b.*|floor\s+block\b.*)$",
    re.IGNORECASE,
)
_ABBREVIATIONS = {
    "AMRAP": "as many rounds or repetitions as possible",
    "BB": "barbell",
    "DB": "dumbbell",
    "DL": "deadlift",
    "EMOM": "every minute on the minute",
    "FR": "front rack",
    "KB": "kettlebell",
    "KBS": "kettlebell swing",
    "RDL": "Romanian deadlift",
    "RKBS": "Russian kettlebell swing",
    "TRX": "suspension trainer",
    "WB": "wall ball",
}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _section_label(heading: str) -> str:
    normalized = heading.strip().lower()
    if normalized.startswith("fitness + performance"):
        return "Fitness + Performance"
    if normalized == "fitness":
        return "Fitness"
    if normalized == "performance":
        return "Performance"
    if normalized.startswith("hyrox"):
        return "HYROX"
    if normalized.startswith("tread block"):
        return "Tread"
    if normalized.startswith("row block"):
        return "Row"
    if normalized.startswith("floor block"):
        return "Floor"
    return heading.strip()


def split_workout_sections(description: str) -> list[dict]:
    """Split known SugarWOD and OrangeTheory tracks without losing block text."""
    lines = description.splitlines()
    starts = [index for index, line in enumerate(lines) if _SECTION_HEADING_RE.match(line.strip())]
    sections = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        content = "\n".join(lines[start:end]).strip()
        if content:
            sections.append(
                {
                    "section_label": _section_label(lines[start]),
                    "heading": lines[start].strip(),
                    "content": content,
                }
            )
    return sections


def _expanded_terms(text: str) -> list[str]:
    return [
        expansion
        for abbreviation, expansion in _ABBREVIATIONS.items()
        if re.search(rf"\b{re.escape(abbreviation)}\b", text, re.IGNORECASE)
    ]


def _embedding_input(
    *,
    title: str | None,
    category: str | None,
    structure_type: str | None,
    section_label: str | None,
    content: str,
) -> str:
    parts = ["Document type: workout plan"]
    if title:
        parts.append(f"Title: {title}")
    if category:
        parts.append(f"Category: {category}")
    if structure_type:
        parts.append(f"Structure: {structure_type}")
    if section_label:
        parts.append(f"Section: {section_label}")
    parts.append(f"Plan:\n{content.strip()}")
    aliases = _expanded_terms("\n".join(parts))
    if aliases:
        parts.append("Expanded terms: " + ", ".join(aliases))
    return "\n".join(parts)


def workout_chunks(workout: dict) -> list[dict]:
    """Create one whole-plan chunk and additional recognized section chunks."""
    description = (workout.get("description") or "").strip()
    if not description:
        return []
    metadata = {
        "category": workout.get("category"),
        "day_of_week": workout.get("day_of_week"),
        "structure_type": workout.get("structure_type"),
    }
    chunks = []
    candidates = [
        {
            "chunk_kind": "day",
            "chunk_index": 0,
            "section_label": None,
            "content": description,
        }
    ]
    candidates.extend(
        {
            "chunk_kind": "section",
            "chunk_index": index,
            "section_label": section["section_label"],
            "content": section["content"],
        }
        for index, section in enumerate(split_workout_sections(description))
    )

    for candidate in candidates:
        chunk_key = (
            f"{DOMAIN_WORKOUT}|{workout['id']}|{candidate['chunk_kind']}|"
            f"{candidate['chunk_index']}"
        )
        embedding_input = _embedding_input(
            title=workout.get("name") if candidate["chunk_kind"] == "day" else None,
            category=workout.get("category"),
            structure_type=workout.get("structure_type"),
            section_label=candidate["section_label"],
            content=candidate["content"],
        )
        hash_input = json.dumps(
            {"embedding_input": embedding_input, "metadata": metadata},
            sort_keys=True,
            default=str,
        )
        chunks.append(
            {
                "id": _hash(chunk_key)[:32],
                "domain": DOMAIN_WORKOUT,
                "source_id": workout["id"],
                "source_date": workout.get("date"),
                "chunk_kind": candidate["chunk_kind"],
                "chunk_index": candidate["chunk_index"],
                "section_label": candidate["section_label"],
                "title": workout.get("name"),
                "content": candidate["content"],
                "metadata": json.dumps(metadata, sort_keys=True, default=str),
                "content_hash": _hash(hash_input),
                "embedding_input": embedding_input,
            }
        )
    return chunks


def _workouts(con: duckdb.DuckDBPyConnection) -> list[dict]:
    cursor = con.execute(
        """
        SELECT id, date, day_of_week, name, category, structure_type, description
        FROM workouts
        """
    )
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _weekly_supertrend_alerts(con: duckdb.DuckDBPyConnection) -> list[dict]:
    cursor = con.execute(
        """
        SELECT id, date, ticker, alert_type, message
        FROM stock_alerts
        WHERE alert_type IN (?, ?)
        ORDER BY date, id
        """,
        sorted(_WEEKLY_SUPERTREND_ALERT_TYPES),
    )
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def stock_alert_chunks(alert: dict) -> list[dict]:
    """Create one derived chunk for a weekly Supertrend alert."""
    alert_type = alert["alert_type"]
    if alert_type not in _WEEKLY_SUPERTREND_ALERT_TYPES:
        return []
    direction = alert_type.rsplit("_", 1)[1]
    ticker = alert["ticker"]
    message = (alert.get("message") or "").strip()
    if not message:
        return []
    metadata = {
        "ticker": ticker,
        "direction": direction,
        "alert_type": alert_type,
        "timeframe": "weekly",
    }
    embedding_input = "\n".join(
        [
            "Document type: stock alert",
            f"Ticker: {ticker}",
            "Signal: Supertrend",
            "Timeframe: weekly",
            f"Direction: {direction}",
            f"Alert: {message}",
        ]
    )
    chunk_key = f"{DOMAIN_STOCK_ALERT}|{alert['id']}|alert|0"
    hash_input = json.dumps(
        {"embedding_input": embedding_input, "metadata": metadata},
        sort_keys=True,
        default=str,
    )
    return [
        {
            "id": _hash(chunk_key)[:32],
            "domain": DOMAIN_STOCK_ALERT,
            "source_id": alert["id"],
            "source_date": alert.get("date"),
            "chunk_kind": "alert",
            "chunk_index": 0,
            "section_label": None,
            "title": f"{ticker} weekly Supertrend {direction}",
            "content": message,
            "metadata": json.dumps(metadata, sort_keys=True),
            "content_hash": _hash(hash_input),
            "embedding_input": embedding_input,
        }
    ]


def _stock_notes(con: duckdb.DuckDBPyConnection) -> list[dict]:
    cursor = con.execute(
        """
        SELECT id, ticker, note, created_at, updated_at
        FROM stock_notes
        WHERE NOT is_deleted
        ORDER BY created_at, id
        """
    )
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def stock_note_chunks(note: dict) -> list[dict]:
    """Create one derived chunk for a user-authored ticker note."""
    content = (note.get("note") or "").strip()
    ticker = (note.get("ticker") or "").upper()
    if not content or not ticker:
        return []
    metadata = {"ticker": ticker, "source": "user_note"}
    embedding_input = "\n".join(
        [
            "Document type: user stock note",
            f"Ticker: {ticker}",
            f"Note: {content}",
        ]
    )
    chunk_key = f"{DOMAIN_STOCK_NOTE}|{note['id']}|note|0"
    hash_input = json.dumps(
        {"embedding_input": embedding_input, "metadata": metadata},
        sort_keys=True,
    )
    return [
        {
            "id": _hash(chunk_key)[:32],
            "domain": DOMAIN_STOCK_NOTE,
            "source_id": note["id"],
            "source_date": note.get("updated_at"),
            "chunk_kind": "note",
            "chunk_index": 0,
            "section_label": None,
            "title": f"{ticker} note",
            "content": content,
            "metadata": json.dumps(metadata, sort_keys=True),
            "content_hash": _hash(hash_input),
            "embedding_input": embedding_input,
        }
    ]


def sync_workout_embeddings(
    db_path: Path | str = DB_PATH,
    embedder: Callable[[list[str]], list[list[float]]] = embed_texts,
    batch_size: int = BATCH_SIZE,
    dry_run: bool = False,
) -> dict:
    """Idempotently index every workout, with model calls outside DB locks."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        desired = [chunk for workout in _workouts(con) for chunk in workout_chunks(workout)]
    finally:
        con.close()
    return _sync_chunks(
        DOMAIN_WORKOUT, desired, db_path, embedder, batch_size, dry_run
    )


def sync_stock_alert_embeddings(
    db_path: Path | str = DB_PATH,
    embedder: Callable[[list[str]], list[list[float]]] = embed_texts,
    batch_size: int = BATCH_SIZE,
    dry_run: bool = False,
) -> dict:
    """Idempotently index weekly Supertrend alerts outside DB write locks."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        desired = [
            chunk
            for alert in _weekly_supertrend_alerts(con)
            for chunk in stock_alert_chunks(alert)
        ]
    finally:
        con.close()
    return _sync_chunks(
        DOMAIN_STOCK_ALERT, desired, db_path, embedder, batch_size, dry_run
    )


def sync_stock_note_embeddings(
    db_path: Path | str = DB_PATH,
    embedder: Callable[[list[str]], list[list[float]]] = embed_texts,
    batch_size: int = BATCH_SIZE,
    dry_run: bool = False,
) -> dict:
    """Idempotently index active user-authored ticker notes."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        desired = [chunk for note in _stock_notes(con) for chunk in stock_note_chunks(note)]
    finally:
        con.close()
    return _sync_chunks(DOMAIN_STOCK_NOTE, desired, db_path, embedder, batch_size, dry_run)


def _sync_chunks(
    domain: str,
    desired: list[dict],
    db_path: Path | str,
    embedder: Callable[[list[str]], list[list[float]]],
    batch_size: int,
    dry_run: bool,
) -> dict:
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    existing_rows = []
    if not dry_run:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            existing_rows = con.execute(
                """
                SELECT id, content_hash, embedding_model
                FROM semantic_chunks WHERE domain = ?
                """,
                [domain],
            ).fetchall()
        finally:
            con.close()

    existing = {row[0]: (row[1], row[2]) for row in existing_rows}
    pending = [
        chunk
        for chunk in desired
        if existing.get(chunk["id"]) != (chunk["content_hash"], OLLAMA_EMBEDDING_MODEL)
    ]
    pending_vectors: dict[str, list[float]] = {}
    dimensions: set[int] = set()
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        vectors = embedder([chunk["embedding_input"] for chunk in batch])
        if len(vectors) != len(batch):
            raise ValueError("Embedding provider returned an unexpected vector count.")
        dimensions.update(len(vector) for vector in vectors)
        if not dry_run:
            pending_vectors.update(zip((chunk["id"] for chunk in batch), vectors))

    if dry_run:
        if len(dimensions) > 1:
            raise ValueError("Embedding provider returned inconsistent vector dimensions.")
        return {
            "dry_run": True,
            "sources": len({chunk["source_id"] for chunk in desired}),
            "chunks": len(desired),
            "would_embed": len(pending),
            "batches": (len(pending) + batch_size - 1) // batch_size,
            "dimensions": next(iter(dimensions), None),
            "writes": 0,
        }

    desired_ids = {chunk["id"] for chunk in desired}
    stale_ids = set(existing) - desired_ids
    if pending or stale_ids:
        con = duckdb.connect(str(db_path))
        try:
            con.execute("BEGIN")
            for chunk in pending:
                con.execute(
                    """
                    INSERT INTO semantic_chunks
                        (id, domain, source_id, source_date, chunk_kind, chunk_index,
                         section_label, title, content, metadata, content_hash,
                         embedding_model, embedding)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (id) DO UPDATE SET
                        domain = excluded.domain,
                        source_id = excluded.source_id,
                        source_date = excluded.source_date,
                        chunk_kind = excluded.chunk_kind,
                        chunk_index = excluded.chunk_index,
                        section_label = excluded.section_label,
                        title = excluded.title,
                        content = excluded.content,
                        metadata = excluded.metadata,
                        content_hash = excluded.content_hash,
                        embedding_model = excluded.embedding_model,
                        embedding = excluded.embedding,
                        updated_at = excluded.updated_at
                    """,
                    [
                        chunk["id"],
                        chunk["domain"],
                        chunk["source_id"],
                        chunk["source_date"],
                        chunk["chunk_kind"],
                        chunk["chunk_index"],
                        chunk["section_label"],
                        chunk["title"],
                        chunk["content"],
                        chunk["metadata"],
                        chunk["content_hash"],
                        OLLAMA_EMBEDDING_MODEL,
                        pending_vectors[chunk["id"]],
                    ],
                )
            for stale_id in stale_ids:
                con.execute("DELETE FROM semantic_chunks WHERE id = ?", [stale_id])
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    return {
        "dry_run": False,
        "sources": len({chunk["source_id"] for chunk in desired}),
        "chunks": len(desired),
        "embedded": len(pending),
        "unchanged": len(desired) - len(pending),
        "deleted": len(stale_ids),
    }


def search_documents(
    query: str,
    *,
    domain: str = DOMAIN_WORKOUT,
    top_k: int = 5,
    start_date: str | date | None = None,
    end_date: str | date | None = None,
    section: str | None = None,
    structure_type: str | None = None,
    ticker: str | None = None,
    direction: str | None = None,
    db_path: Path | str = DB_PATH,
    embedder: Callable[[list[str]], list[list[float]]] = embed_texts,
    sync: bool = True,
) -> list[dict]:
    """Search semantic chunks and return grounded evidence for one domain."""
    if domain not in SUPPORTED_DOMAINS:
        raise ValueError(f"Unsupported semantic-search domain: {domain}")
    if not query.strip():
        raise ValueError("query must not be empty")
    limit = max(1, min(int(top_k), MAX_RESULTS))
    if sync:
        if domain == DOMAIN_WORKOUT:
            sync_workout_embeddings(db_path, embedder=embedder)
        elif domain == DOMAIN_STOCK_ALERT:
            sync_stock_alert_embeddings(db_path, embedder=embedder)
        else:
            sync_stock_note_embeddings(db_path, embedder=embedder)
    query_vector = embedder([query])[0]

    clauses = ["c.domain = ?", "c.embedding_model = ?"]
    parameters: list = [query_vector, domain, OLLAMA_EMBEDDING_MODEL]
    source_date = "CAST(c.source_date AS DATE)" if domain == DOMAIN_STOCK_NOTE else "c.source_date"
    if start_date:
        clauses.append(f"{source_date} >= ?")
        parameters.append(start_date)
    if end_date:
        clauses.append(f"{source_date} <= ?")
        parameters.append(end_date)
    if section:
        clauses.append("lower(c.section_label) = lower(?)")
        parameters.append(section)
    if structure_type:
        clauses.append("lower(json_extract_string(c.metadata, '$.structure_type')) = lower(?)")
        parameters.append(structure_type)
    if ticker:
        clauses.append("lower(json_extract_string(c.metadata, '$.ticker')) = lower(?)")
        parameters.append(ticker)
    if direction:
        clauses.append("lower(json_extract_string(c.metadata, '$.direction')) = lower(?)")
        parameters.append(direction)
    parameters.append(limit)

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        if domain == DOMAIN_WORKOUT:
            cursor = con.execute(
                f"""
            WITH scored AS (
                SELECT c.*,
                       list_cosine_similarity(c.embedding, ?) AS score
                FROM semantic_chunks c
                WHERE {' AND '.join(clauses)}
            ), ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY source_id
                    ORDER BY score DESC, CASE WHEN chunk_kind = 'section' THEN 0 ELSE 1 END
                ) AS source_rank
                FROM scored
            )
            SELECT r.source_id, r.source_date AS date, w.day_of_week, w.name,
                   w.category, w.structure_type, r.chunk_kind AS match_kind,
                   r.section_label AS section, r.content AS matched_text,
                   w.description AS full_description, r.score
            FROM ranked r
            JOIN workouts w ON w.id = r.source_id
            WHERE r.source_rank = 1
            ORDER BY r.score DESC
            LIMIT ?
            """,
                parameters,
            )
        elif domain == DOMAIN_STOCK_ALERT:
            cursor = con.execute(
                f"""
                WITH scored AS (
                    SELECT c.*, list_cosine_similarity(c.embedding, ?) AS score
                    FROM semantic_chunks c
                    WHERE {' AND '.join(clauses)}
                )
                SELECT c.source_id, c.source_date AS date,
                       json_extract_string(c.metadata, '$.ticker') AS ticker,
                       json_extract_string(c.metadata, '$.direction') AS direction,
                       json_extract_string(c.metadata, '$.alert_type') AS alert_type,
                       a.message, c.score
                FROM scored c
                JOIN stock_alerts a ON a.id = c.source_id
                ORDER BY c.score DESC
                LIMIT ?
                """,
                parameters,
            )
        else:
            cursor = con.execute(
                f"""
                WITH scored AS (
                    SELECT c.*, list_cosine_similarity(c.embedding, ?) AS score
                    FROM semantic_chunks c
                    WHERE {' AND '.join(clauses)}
                )
                SELECT c.source_id, c.source_date AS date,
                       n.ticker, n.note, n.created_at, n.updated_at, c.score
                FROM scored c
                JOIN stock_notes n ON n.id = c.source_id
                WHERE NOT n.is_deleted
                ORDER BY c.score DESC
                LIMIT ?
                """,
                parameters,
            )
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        con.close()
