"""Import Apple Wallet transaction-list screenshots into Groundhog spending."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import base64
import hashlib
from io import BytesIO
import json
import re
import shutil
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import duckdb
import httpx
from PIL import Image, ImageOps

from config.settings import DB_PATH, DROP_FOLDER, OLLAMA_CHAT_URL, OLLAMA_VISION_MODEL
from ingestion.schema import init_db

IMAGE_EXTS = {".jpg", ".jpeg", ".png"}
PROCESSED_DIR = DROP_FOLDER / "spending" / "processed"
CATEGORIES = {"groceries", "dining", "shopping", "entertainment", "beer", "other"}
MAX_IMAGE_DIMENSION = 1200
WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

PROMPT = """Extract every visible completed transaction from this Apple Wallet transaction-list screenshot.

Return only a JSON array. One object per visible transaction:
[
  {
    "merchant": "merchant name exactly as shown",
    "amount": 4.46,
    "visible_date_label": "9 hours ago|Yesterday|Sunday|7/28/26|...",
    "payment_method": "Apple Pay|Visa 0241|... or null",
    "category": "groceries|dining|shopping|entertainment|beer|other"
  }
]

Rules:
- Include only rows with both a merchant and a monetary amount.
- Preserve the visible date label exactly; do not infer a calendar date.
- Use only these categories. Use other if uncertain.
- Do not include card balance, pending payments, or headings.
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
    return response.json()["message"]["content"]


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
    for fmt in ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y", "%m-%d-%y", "%m-%d-%Y"):
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


def _normalize(rows: list[dict], reference_date: date) -> list[dict]:
    transactions = []
    for row in rows:
        merchant = row.get("merchant")
        if not isinstance(merchant, str) or not merchant.strip():
            continue
        transaction_date = _resolve_date(row.get("visible_date_label"), reference_date)
        amount = _amount(row.get("amount"))
        if transaction_date is None or amount is None:
            continue
        category = str(row.get("category") or "other").strip().lower()
        transactions.append({
            "merchant": merchant.strip(),
            "amount": amount,
            "transaction_date": transaction_date,
            "visible_date_label": row.get("visible_date_label"),
            "payment_method": row.get("payment_method"),
            "category": category if category in CATEGORIES else "other",
        })
    return transactions


def _image_hash(image_path: Path) -> str:
    return hashlib.sha256(image_path.read_bytes()).hexdigest()


def _transaction_id(image_hash: str, source_row: int) -> str:
    return hashlib.sha256(f"{image_hash}:{source_row}".encode()).hexdigest()[:16]


def _archive_image(image_path: Path, image_hash: str) -> Path:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    destination = PROCESSED_DIR / f"{image_hash}{image_path.suffix.lower()}"
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


def process_image(image_path: Path, reference_date: date | None = None) -> list[dict]:
    if image_path.suffix.lower() not in IMAGE_EXTS:
        raise ValueError(f"Unsupported image type: {image_path.suffix or '(none)'}")
    if not image_path.is_file():
        raise FileNotFoundError(image_path)
    image_hash = _image_hash(image_path)
    reference_date = reference_date or date.today()
    transactions = _normalize(_parse_transactions(_query_ollama(image_path)), reference_date)
    if not transactions:
        raise ValueError("Could not extract any dated transactions from the Apple Wallet screenshot.")

    init_db(DB_PATH)
    con = duckdb.connect(str(DB_PATH))
    try:
        existing = con.execute("SELECT COUNT(*) FROM spending WHERE source_image_hash = ?", [image_hash]).fetchone()[0]
        if existing:
            return []
        for source_row, transaction in enumerate(transactions):
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
    finally:
        con.close()
    _archive_image(image_path, image_hash)
    return transactions


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
    parser = argparse.ArgumentParser(description="Import Apple Wallet spending screenshots.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--image", type=Path, required=True)
    import_parser.add_argument("--reference-date", type=date.fromisoformat, required=True)
    import_parser.add_argument("--media-state-path", type=Path)
    category_parser = subparsers.add_parser("category")
    category_parser.add_argument("transaction_id")
    category_parser.add_argument("category")
    args = parser.parse_args()
    if args.command == "import":
        transactions = process_image(args.image, args.reference_date)
        if args.media_state_path:
            mark_media_imported(args.image, args.media_state_path)
        print(json.dumps(transactions, default=str, sort_keys=True))
    else:
        print(json.dumps(update_category(args.transaction_id, args.category), sort_keys=True))


if __name__ == "__main__":
    main()
