"""Import transaction-list screenshots into Groundhog spending."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import base64
import hashlib
from io import BytesIO
import json
import os
import re
import shutil
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import duckdb
import httpx
from PIL import Image, ImageOps

from config.settings import DB_PATH, DROP_FOLDER, OLLAMA_CHAT_URL, OLLAMA_VISION_MODEL
from agent.request_trace import RequestTrace, record_llm_call, use_trace
from ingestion.schema import init_db

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
PROCESSED_DIR = DROP_FOLDER / "spending" / "processed"
CATEGORIES = {"groceries", "dining", "shopping", "entertainment", "beer", "other"}
MERCHANT_CATEGORY_OVERRIDES = {"circlek": "beer"}
MAX_IMAGE_DIMENSION = 1200
WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

PROMPT = """Extract every visible transaction from this personal-finance transaction-list screenshot.

The screenshot may come from Apple Wallet, Bank of America, or another bank or credit-card app.

Return only a JSON array. One object per visible transaction:
[
  {
    "merchant": "merchant name exactly as shown",
    "amount": 4.46,
    "visible_date_label": "9 hours ago|Yesterday|Sunday|7/28/26|Aug 8, 2026|...",
    "payment_method": "Apple Pay|Bank of America|Visa 0241|... or null",
    "category": "groceries|dining|shopping|entertainment|beer|other",
    "status": "posted|pending|unknown"
  }
]

Rules:
- Include only rows with both a merchant and a monetary amount.
- Preserve the visible date label exactly; do not infer a calendar date.
- The amount is the transaction charge, usually the primary or blue amount on bank screenshots.
- Ignore account balances and running balances, usually the smaller or gray amount below the charge.
- Mark a row pending only when the screenshot explicitly labels it Pending.
- Use only these categories. Use other if uncertain.
- Do not include headings, card balances, account balances, or running balances as transactions.
- No explanation or Markdown fences."""


def _encode_image(path: Path) -> str:
    """Return an in-memory image small enough for reliable local inference."""
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source)
        if max(image.size) <= MAX_IMAGE_DIMENSION:
            return base64.b64encode(path.read_bytes()).decode("utf-8")
        image.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))
        if image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=90, optimize=True)
        return base64.b64encode(buffer.getvalue()).decode("utf-8")


def _query_ollama(image_path: Path) -> str:
    started_at = datetime.now(timezone.utc)
    monotonic_started = time.monotonic()
    try:
        response = httpx.post(
            OLLAMA_CHAT_URL,
            json={
                "model": OLLAMA_VISION_MODEL,
                "messages": [{"role": "user", "content": PROMPT, "images": [_encode_image(image_path)]}],
                "stream": False,
            },
            timeout=600.0,
        )
        response.raise_for_status()
        content = response.json()["message"]["content"]
    except Exception as error:
        record_llm_call(
            started_at=started_at,
            monotonic_started=monotonic_started,
            model=OLLAMA_VISION_MODEL,
            prompt=PROMPT,
            error=error,
            metadata={"image_path": image_path},
        )
        raise
    record_llm_call(
        started_at=started_at,
        monotonic_started=monotonic_started,
        model=OLLAMA_VISION_MODEL,
        prompt=PROMPT,
        response=content,
        metadata={"image_path": image_path},
    )
    return content


def _parse_transactions(raw: str) -> list[dict]:
    match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.DOTALL | re.IGNORECASE)
    candidate = match.group(1) if match else raw[raw.find("[") : raw.rfind("]") + 1]
    if not candidate:
        return []
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return []
    return [row for row in parsed if isinstance(row, dict)] if isinstance(parsed, list) else []


def _resolve_date(label: str | None, reference_date: date) -> date | None:
    if not isinstance(label, str):
        return None
    value = label.strip()
    lowered = value.lower()
    if re.fullmatch(r"\d+\s+(?:minute|minutes|hour|hours)\s+ago", lowered) or lowered == "today":
        return reference_date
    if lowered == "yesterday":
        return reference_date - timedelta(days=1)
    if lowered in WEEKDAYS:
        delta = (reference_date.weekday() - WEEKDAYS[lowered]) % 7
        return reference_date - timedelta(days=delta)
    for fmt in (
        "%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y", "%m-%d-%y", "%m-%d-%Y",
        "%b %d, %Y", "%B %d, %Y", "%b %d, %y", "%B %d, %y",
    ):
        try:
            parsed = datetime.strptime(value, fmt).date()
            return parsed
        except ValueError:
            continue
    return None


def _amount(value) -> Decimal | None:
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        try:
            return Decimal(str(value)).quantize(Decimal("0.01"))
        except InvalidOperation:
            return None
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"[^0-9.-]", "", value)
    try:
        return Decimal(cleaned).copy_abs().quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _category_for(merchant: str, suggested_category) -> str:
    normalized_merchant = re.sub(r"[^a-z0-9]", "", merchant.lower())
    for merchant_prefix, category in MERCHANT_CATEGORY_OVERRIDES.items():
        if normalized_merchant.startswith(merchant_prefix):
            return category
    category = str(suggested_category or "other").strip().lower()
    return category if category in CATEGORIES else "other"


def _merchant_key(merchant: str) -> str:
    return re.sub(r"[^a-z0-9]", "", merchant.lower())


def _same_merchant(left: str, right: str) -> bool:
    left_key = _merchant_key(left)
    right_key = _merchant_key(right)
    if left_key == right_key:
        return True
    shorter, longer = sorted((left_key, right_key), key=len)
    return len(shorter) >= 5 and longer.startswith(shorter)


def _new_import_result() -> dict:
    return {
        "transactions": [],
        "skipped_pending": 0,
        "skipped_duplicates": 0,
        "skipped_invalid": 0,
    }


def _normalize(rows: list[dict], reference_date: date) -> dict:
    result = _new_import_result()
    for row in rows:
        status = str(row.get("status") or "unknown").strip().lower()
        if status == "pending":
            result["skipped_pending"] += 1
            continue
        merchant = row.get("merchant")
        if not isinstance(merchant, str) or not merchant.strip():
            result["skipped_invalid"] += 1
            continue
        visible_date_label = row.get("visible_date_label") or row.get("date")
        transaction_date = _resolve_date(visible_date_label, reference_date)
        amount = _amount(row.get("amount"))
        if transaction_date is None or amount is None:
            result["skipped_invalid"] += 1
            continue
        merchant = merchant.strip()
        result["transactions"].append({
            "merchant": merchant,
            "amount": amount,
            "transaction_date": transaction_date,
            "visible_date_label": visible_date_label,
            "payment_method": row.get("payment_method"),
            "category": _category_for(merchant, row.get("category")),
        })
    return result


def _image_hash(image_path: Path) -> str:
    return hashlib.sha256(image_path.read_bytes()).hexdigest()


def _transaction_id(image_hash: str, source_row: int) -> str:
    return hashlib.sha256(f"{image_hash}:{source_row}".encode()).hexdigest()[:16]


def _archive_image(
    image_path: Path,
    image_hash: str,
    processed_dir: Path | None = None,
) -> Path:
    processed_dir = processed_dir or PROCESSED_DIR
    processed_dir.mkdir(parents=True, exist_ok=True)
    destination = processed_dir / f"{image_hash}{image_path.suffix.lower()}"
    if not destination.exists():
        shutil.copy2(image_path, destination)
    return destination


def mark_media_imported(image_path: Path, state_path: Path) -> None:
    """Prevent the periodic activity watcher from importing this attachment later."""
    try:
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
    except json.JSONDecodeError:
        state = {}
    if not isinstance(state, dict):
        state = {}
    state[_image_hash(image_path)] = {
        "status": "imported",
        "kind": "spending",
        "path": str(image_path),
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def _is_duplicate(con, transaction: dict) -> bool:
    transaction_date = transaction["transaction_date"]
    candidates = con.execute(
        """
        SELECT merchant
        FROM spending
        WHERE amount = ? AND transaction_date BETWEEN ? AND ?
        """,
        [
            transaction["amount"],
            transaction_date - timedelta(days=3),
            transaction_date + timedelta(days=3),
        ],
    ).fetchall()
    return any(_same_merchant(transaction["merchant"], row[0]) for row in candidates)


def process_image(
    image_path: Path,
    reference_date: date | None = None,
    db_path: Path | None = None,
    processed_dir: Path | None = None,
) -> dict:
    if image_path.suffix.lower() not in IMAGE_EXTS:
        raise ValueError(f"Unsupported image type: {image_path.suffix or '(none)'}")
    if not image_path.is_file():
        raise FileNotFoundError(image_path)
    db_path = db_path or DB_PATH
    image_hash = _image_hash(image_path)
    reference_date = reference_date or date.today()
    init_db(db_path)
    con = duckdb.connect(str(db_path))
    try:
        existing = con.execute("SELECT COUNT(*) FROM spending WHERE source_image_hash = ?", [image_hash]).fetchone()[0]
    finally:
        con.close()
    if existing:
        result = _new_import_result()
        result["skipped_duplicates"] = existing
        return result

    result = _normalize(_parse_transactions(_query_ollama(image_path)), reference_date)
    con = duckdb.connect(str(db_path))
    try:
        inserted = []
        for source_row, transaction in enumerate(result["transactions"]):
            if _is_duplicate(con, transaction):
                result["skipped_duplicates"] += 1
                continue
            transaction["id"] = _transaction_id(image_hash, source_row)
            con.execute(
                """
                INSERT INTO spending (
                    id, transaction_date, visible_date_label, merchant, amount,
                    payment_method, category, source_image_hash, source_row
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO NOTHING
                """,
                [
                    transaction["id"], transaction["transaction_date"], transaction["visible_date_label"],
                    transaction["merchant"], transaction["amount"], transaction["payment_method"],
                    transaction["category"], image_hash, source_row,
                ],
            )
            inserted.append(transaction)
        result["transactions"] = inserted
    finally:
        con.close()
    _archive_image(image_path, image_hash, processed_dir)
    return result


def update_category(transaction_id: str, category: str) -> dict:
    normalized_category = category.strip().lower()
    if normalized_category not in CATEGORIES:
        raise ValueError(f"Category must be one of: {', '.join(sorted(CATEGORIES))}.")
    con = duckdb.connect(str(DB_PATH))
    try:
        rows = con.execute(
            "SELECT id, merchant, amount, category FROM spending WHERE id LIKE ?", [f"{transaction_id}%"]
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("Transaction ID was not found or is ambiguous.")
        transaction = rows[0]
        con.execute("UPDATE spending SET category = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", [normalized_category, transaction[0]])
        return {"id": transaction[0], "merchant": transaction[1], "amount": str(transaction[2]), "category": normalized_category}
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Import transaction-list spending screenshots.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--image", type=Path, required=True)
    import_parser.add_argument("--reference-date", type=date.fromisoformat, required=True)
    import_parser.add_argument("--media-state-path", type=Path)
    category_parser = subparsers.add_parser("category")
    category_parser.add_argument("transaction_id")
    category_parser.add_argument("category")
    args = parser.parse_args()
    operation = "expense_import" if args.command == "import" else "expense_category"
    metadata = (
        {"image_path": args.image, "reference_date": args.reference_date}
        if args.command == "import"
        else {"transaction_id": args.transaction_id, "category": args.category}
    )
    trace = RequestTrace(
        operation=operation,
        source=os.environ.get("GROUNDHOG_REQUEST_SOURCE", "groundhog_cli"),
    ).start(**metadata)
    with use_trace(trace):
        try:
            if args.command == "import":
                result = process_image(args.image, args.reference_date)
                if args.media_state_path:
                    mark_media_imported(args.image, args.media_state_path)
            else:
                result = update_category(args.transaction_id, args.category)
        except Exception as error:
            trace.end("failed", str(error))
            raise
        trace.end("passed", result=result)
    print(json.dumps(result, default=str, sort_keys=True))


if __name__ == "__main__":
    main()
