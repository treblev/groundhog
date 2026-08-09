import base64
from io import BytesIO
import json
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import duckdb
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingestion import schema, spending


class SpendingIngestionTests(unittest.TestCase):
    def test_large_wallet_image_is_downscaled_in_memory_for_vision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "wallet.png"
            Image.new("RGB", (1080, 2048), color="white").save(image_path)

            encoded = spending._encode_image(image_path)

            decoded = base64.b64decode(encoded)
            with Image.open(BytesIO(decoded)) as result:
                self.assertEqual(result.size, (633, 1200))
                self.assertEqual(result.format, "JPEG")

    def test_resolves_wallet_relative_dates(self):
        reference = date(2026, 8, 5)  # Wednesday
        self.assertEqual(spending._resolve_date("9 hours ago", reference), reference)
        self.assertEqual(spending._resolve_date("Yesterday", reference), date(2026, 8, 4))
        self.assertEqual(spending._resolve_date("Sunday", reference), date(2026, 8, 2))
        self.assertEqual(spending._resolve_date("7/28/26", reference), date(2026, 7, 28))
        self.assertEqual(spending._resolve_date("Aug 8, 2026", reference), date(2026, 8, 8))
        self.assertEqual(spending._resolve_date("August 8, 2026", reference), date(2026, 8, 8))

    def test_normalize_uses_allowed_categories_and_skips_incomplete_rows(self):
        result = spending._normalize([
            {"merchant": "Intel Chandler - CH6 Cafe", "amount": "$4.46", "visible_date_label": "9 hours ago", "category": "dining"},
            {"merchant": "Unknown", "amount": "$2.00", "visible_date_label": "Yesterday", "category": "transport"},
            {"merchant": "No date", "amount": "$3.00", "category": "beer"},
        ], date(2026, 8, 5))
        rows = result["transactions"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["amount"], Decimal("4.46"))
        self.assertEqual(rows[1]["category"], "other")
        self.assertEqual(result["skipped_invalid"], 1)

    def test_circle_k_category_overrides_vision_category(self):
        result = spending._normalize([
            {"merchant": "Circlek #2703490", "amount": "$8.87", "visible_date_label": "Today", "category": "shopping"},
            {"merchant": "Circle K Store 123", "amount": "$2.83", "visible_date_label": "Today", "category": "other"},
        ], date(2026, 8, 9))

        self.assertEqual([row["category"] for row in result["transactions"]], ["beer", "beer"])

    def test_pending_transactions_are_counted_and_skipped(self):
        result = spending._normalize([
            {"merchant": "Circlek #2703490", "amount": "$10.88", "status": "pending"},
            {"merchant": "Safeway", "amount": "$25.34", "visible_date_label": "Aug 8, 2026", "status": "posted"},
        ], date(2026, 8, 9))

        self.assertEqual(result["skipped_pending"], 1)
        self.assertEqual(len(result["transactions"]), 1)
        self.assertEqual(result["transactions"][0]["transaction_date"], date(2026, 8, 8))

    def test_prompt_distinguishes_transaction_amounts_from_running_balances(self):
        self.assertIn("running balances", spending.PROMPT)
        self.assertIn('"status": "posted|pending|unknown"', spending.PROMPT)

    def test_process_image_is_idempotent_and_archives_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "groundhog.duckdb"
            image_path = root / "wallet.png"
            image_path.write_bytes(b"wallet screenshot")
            schema.init_db(db_path)
            response = '[{"merchant":"Intel Chandler - CH6 Cafe","amount":4.46,"visible_date_label":"9 hours ago","payment_method":"Apple Pay Visa 0241","category":"dining"},{"merchant":"Frys","amount":"$41.69","visible_date_label":"Saturday","payment_method":"Apple Pay","category":"groceries"}]'
            with (
                patch.object(spending, "DB_PATH", db_path),
                patch.object(spending, "PROCESSED_DIR", root / "processed"),
                patch.object(spending, "_query_ollama", return_value=response),
            ):
                first = spending.process_image(image_path, date(2026, 8, 5))
                second = spending.process_image(image_path, date(2026, 8, 5))

            self.assertEqual(len(first["transactions"]), 2)
            self.assertEqual(second["transactions"], [])
            self.assertEqual(second["skipped_duplicates"], 2)
            self.assertTrue(list((root / "processed").glob("*.png")))
            con = duckdb.connect(str(db_path), read_only=True)
            try:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM spending").fetchone()[0], 2)
                self.assertEqual(con.execute("SELECT sum(amount) FROM spending").fetchone()[0], Decimal("46.15"))
            finally:
                con.close()

    def test_duplicate_matching_handles_bank_posting_delay_and_merchant_suffix(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "groundhog.duckdb"
            wallet_image = root / "wallet.png"
            bank_image = root / "bank.png"
            wallet_image.write_bytes(b"wallet")
            bank_image.write_bytes(b"bank")
            schema.init_db(db_path)
            responses = [
                '[{"merchant":"Safeway","amount":25.34,"visible_date_label":"Thursday","category":"groceries","status":"posted"}]',
                '[{"merchant":"SAFEWAY #1566 CHANDLER AZ","amount":25.34,"visible_date_label":"Aug 8, 2026","category":"groceries","status":"posted"}]',
            ]
            with (
                patch.object(spending, "DB_PATH", db_path),
                patch.object(spending, "PROCESSED_DIR", root / "processed"),
                patch.object(spending, "_query_ollama", side_effect=responses),
            ):
                first = spending.process_image(wallet_image, date(2026, 8, 9))
                second = spending.process_image(bank_image, date(2026, 8, 9))

            self.assertEqual(len(first["transactions"]), 1)
            self.assertEqual(second["transactions"], [])
            self.assertEqual(second["skipped_duplicates"], 1)

    def test_identical_charge_outside_posting_window_is_not_duplicate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "groundhog.duckdb"
            schema.init_db(db_path)
            con = duckdb.connect(str(db_path))
            con.execute("""INSERT INTO spending (id, transaction_date, merchant, amount, category, source_image_hash, source_row)
                         VALUES ('existing', '2026-08-01', 'Safeway', 25.34, 'groceries', 'old-image', 0)""")
            transaction = {"transaction_date": date(2026, 8, 8), "merchant": "SAFEWAY #1566", "amount": Decimal("25.34")}
            self.assertFalse(spending._is_duplicate(con, transaction))
            con.close()

    def test_update_category_accepts_short_id_and_rejects_unknown_category(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "groundhog.duckdb"
            schema.init_db(db_path)
            con = duckdb.connect(str(db_path))
            con.execute("""INSERT INTO spending (id, transaction_date, merchant, amount, category, source_image_hash, source_row)
                         VALUES ('abc123def4567890', '2026-08-05', 'Cafe', 4.46, 'dining', 'image', 0)""")
            con.close()
            with patch.object(spending, "DB_PATH", db_path):
                result = spending.update_category("abc123de", "beer")
                self.assertEqual(result["category"], "beer")
                with self.assertRaises(ValueError):
                    spending.update_category("abc123de", "transport")

    def test_mark_media_imported_prevents_the_activity_watcher_from_reprocessing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path = root / "wallet.png"
            image_path.write_bytes(b"wallet screenshot")
            state_path = root / "state.json"

            spending.mark_media_imported(image_path, state_path)

            state = json.loads(state_path.read_text())
            record = state[spending._image_hash(image_path)]
            self.assertEqual(record["kind"], "spending")
            self.assertEqual(record["status"], "imported")
