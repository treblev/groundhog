import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.semantic_search import DOMAIN_STOCK_NOTE, search_documents, sync_stock_note_embeddings
from ingestion import schema
from scripts.stock_notes import add_note, delete_note, edit_note, list_notes, normalize_ticker


def fake_embedder(texts: list[str]) -> list[list[float]]:
    return [
        [1.0, 0.0, 0.0] if "bicycle" in text.lower() else [0.0, 1.0, 0.0]
        for text in texts
    ]


class StockNotesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "groundhog.duckdb"
        schema.init_db(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_add_edit_delete_preserves_revisions_and_updates_index(self):
        created = add_note(
            "shop", "Weekly bullish bicycle activation; watch follow-through.",
            db_path=self.db_path,
            embedder=fake_embedder,
        )
        self.assertEqual(created["ticker"], "SHOP")
        self.assertEqual(created["revision"], 1)
        self.assertEqual(created["embedding"]["embedded"], 1)

        matches = search_documents(
            "bicycle activation",
            domain=DOMAIN_STOCK_NOTE,
            ticker="shop",
            db_path=self.db_path,
            embedder=fake_embedder,
            sync=False,
        )
        self.assertEqual(matches[0]["source_id"], created["id"])
        self.assertIn("bicycle", matches[0]["note"])

        same_day_matches = search_documents(
            "bicycle activation",
            domain=DOMAIN_STOCK_NOTE,
            end_date=date.today().isoformat(),
            db_path=self.db_path,
            embedder=fake_embedder,
            sync=False,
        )
        self.assertEqual(same_day_matches[0]["source_id"], created["id"])

        edited = edit_note(
            created["id"][:8], "Weekly bullish bicycle activation confirmed.",
            db_path=self.db_path,
            embedder=fake_embedder,
        )
        self.assertEqual(edited["revision"], 2)
        self.assertEqual(edited["embedding"]["embedded"], 1)

        deleted = delete_note(created["id"], db_path=self.db_path, embedder=fake_embedder)
        self.assertTrue(deleted["deleted"])
        self.assertEqual(deleted["revision"], 3)
        self.assertEqual(deleted["embedding"]["deleted"], 1)
        self.assertEqual(list_notes("SHOP", db_path=self.db_path), [])
        self.assertEqual(
            search_documents(
                "bicycle activation", domain=DOMAIN_STOCK_NOTE,
                db_path=self.db_path, embedder=fake_embedder, sync=False,
            ),
            [],
        )

        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            revisions = con.execute(
                "SELECT revision, action, note FROM stock_note_revisions WHERE note_id = ? ORDER BY revision",
                [created["id"]],
            ).fetchall()
            self.assertEqual([row[:2] for row in revisions], [(1, "created"), (2, "edited"), (3, "deleted")])
            self.assertEqual(revisions[0][2], "Weekly bullish bicycle activation; watch follow-through.")
            self.assertEqual(
                con.execute("SELECT COUNT(*) FROM semantic_chunks WHERE domain = ?", [DOMAIN_STOCK_NOTE]).fetchone()[0],
                0,
            )
        finally:
            con.close()

    def test_sync_handles_notes_created_outside_command_and_ticker_validation(self):
        con = duckdb.connect(str(self.db_path))
        try:
            con.execute("INSERT INTO stock_notes (id, ticker, note) VALUES ('note-1', 'PLTR', 'Narrative catalyst')")
        finally:
            con.close()
        result = sync_stock_note_embeddings(self.db_path, embedder=fake_embedder)
        self.assertEqual(result["sources"], 1)
        self.assertEqual(result["embedded"], 1)
        self.assertEqual(normalize_ticker("brk.b"), "BRK.B")
        with self.assertRaises(ValueError):
            normalize_ticker("NOT A TICKER")


if __name__ == "__main__":
    unittest.main()
