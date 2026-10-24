"""Summary vectors share canonical rows and remain fresh across failures/updates."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tempfile
import unittest
from datetime import date
from unittest.mock import Mock, patch

import duckdb

from agent import artifact_embeddings as ae, summaries
from agent.semantic_search import search_documents
from config.settings import OLLAMA_EMBEDDING_MODEL
from ingestion.schema import init_db


def embed(texts):
    return [[1.0, 0.0] if "sleep" in text.lower() else [0.0, 1.0] for text in texts]


class ArtifactEmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "test.duckdb"
        init_db(self.db)

    def seed(self, id="a", text="Poor sleep this week", kind="weekly_review", end="2026-09-12"):
        with duckdb.connect(str(self.db)) as con:
            con.execute(
                "INSERT INTO derived_artifacts (id, artifact_type, period_start, period_end, content, model) VALUES (?, ?, ?, ?, ?, 'test')",
                [id, kind, end, end, text],
            )

    def test_schema_migrates_legacy_rows_idempotently(self):
        with duckdb.connect(str(self.db)) as con:
            con.execute("DROP TABLE derived_artifacts")
            con.execute("CREATE TABLE derived_artifacts (id VARCHAR PRIMARY KEY, artifact_type VARCHAR, period_start DATE, period_end DATE, content TEXT, model VARCHAR, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        self.seed()
        init_db(self.db)
        init_db(self.db)
        with duckdb.connect(str(self.db), read_only=True) as con:
            self.assertEqual(con.execute("SELECT content, embedding, embedding_model, embedding_content_hash FROM derived_artifacts").fetchone(), ("Poor sleep this week", None, None, None))

    def test_backfill_both_types_reuses_vectors_without_duplicate_text(self):
        self.seed()
        self.seed("b", "Active day", "daily_summary")
        self.seed("c", "", "weekly_review")
        mock = Mock(side_effect=embed)
        result = ae.sync_artifact_embeddings(self.db, embedder=mock)
        self.assertEqual(result["embedded"], 2)
        self.assertEqual(mock.call_count, 1)
        mock.reset_mock()
        self.assertEqual(ae.sync_artifact_embeddings(self.db, embedder=mock)["embedded"], 0)
        mock.assert_not_called()
        with duckdb.connect(str(self.db), read_only=True) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM semantic_chunks").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM derived_artifacts").fetchone()[0], 3)

    def test_changed_content_and_model_are_reembedded(self):
        self.seed()
        ae.sync_artifact_embeddings(self.db, embedder=embed)
        with duckdb.connect(str(self.db)) as con:
            con.execute("UPDATE derived_artifacts SET content = 'Active week', embedding_model = 'old'")
        self.assertEqual(ae.sync_artifact_embeddings(self.db, embedder=embed)["embedded"], 1)
        with duckdb.connect(str(self.db), read_only=True) as con:
            self.assertEqual(con.execute("SELECT embedding_model, embedding_content_hash FROM derived_artifacts").fetchone(), (OLLAMA_EMBEDDING_MODEL, ae.content_hash("Active week")))

    def test_new_summary_embeds_immediately_and_same_text_is_reused(self):
        with duckdb.connect(str(self.db)) as con, patch.object(ae, "embed_texts", side_effect=embed) as model:
            for _ in range(2):
                summaries._store_artifact(con, "weekly_review", date(2026, 9, 6), date(2026, 9, 12), "Poor sleep")
            self.assertEqual(model.call_count, 1)
            row = con.execute("SELECT content, embedding, embedding_content_hash FROM derived_artifacts").fetchone()
            self.assertEqual(row, ("Poor sleep", [1.0, 0.0], ae.content_hash("Poor sleep")))

    def test_failure_keeps_changed_summary_and_invalidates_old_vector(self):
        with duckdb.connect(str(self.db)) as con:
            args = (con, "weekly_review", date(2026, 9, 6), date(2026, 9, 12))
            with patch.object(ae, "embed_texts", side_effect=embed):
                summaries._store_artifact(*args, "Poor sleep")
            with patch.object(ae, "embed_texts", side_effect=RuntimeError("offline")), self.assertLogs("agent.summaries", level="WARNING"):
                summaries._store_artifact(*args, "Active week")
            self.assertEqual(con.execute("SELECT content, embedding, embedding_content_hash FROM derived_artifacts").fetchone(), ("Active week", None, None))
        self.assertEqual(ae.sync_artifact_embeddings(self.db, embedder=embed)["embedded"], 1)

    def test_search_ranking_and_date_filter_return_canonical_text(self):
        self.seed()
        self.seed("b", "Active week", end="2026-09-19")
        matches = search_documents("sleep", domain="summary", db_path=self.db, embedder=embed)
        self.assertEqual(matches[0]["source_id"], "a")
        self.assertEqual(matches[0]["content"], "Poor sleep this week")
        matches = search_documents("sleep", domain="summary", start_date="2026-09-19", end_date="2026-09-19", db_path=self.db, embedder=embed)
        self.assertEqual([row["source_id"] for row in matches], ["b"])
        with self.assertRaisesRegex(ValueError, "date filters"):
            search_documents("sleep", domain="summary", ticker="TEST", db_path=self.db, embedder=embed)

    def test_search_excludes_stale_model_hash_and_dimensions(self):
        for id in ("good", "model", "hash", "dimensions"):
            self.seed(id)
        ae.sync_artifact_embeddings(self.db, embedder=embed)
        with duckdb.connect(str(self.db)) as con:
            con.execute("UPDATE derived_artifacts SET embedding_model = 'old' WHERE id = 'model'")
            con.execute("UPDATE derived_artifacts SET content = 'Different text' WHERE id = 'hash'")
            con.execute("UPDATE derived_artifacts SET embedding = [1.0, 0.0, 0.0] WHERE id = 'dimensions'")
        matches = ae.search_artifacts("sleep", db_path=self.db, embedder=embed, sync=False)
        self.assertEqual([row["source_id"] for row in matches], ["good"])

    def test_concurrent_content_change_is_not_given_a_stale_vector(self):
        self.seed()
        def concurrent_embed(texts):
            with duckdb.connect(str(self.db)) as con:
                con.execute("UPDATE derived_artifacts SET content = 'Changed during embedding'")
            return embed(texts)
        result = ae.sync_artifact_embeddings(self.db, embedder=concurrent_embed)
        self.assertEqual(result["skipped_changed"], 1)
        with duckdb.connect(str(self.db), read_only=True) as con:
            self.assertIsNone(con.execute("SELECT embedding FROM derived_artifacts").fetchone()[0])

    def test_dry_run_validates_without_writes(self):
        self.seed()
        result = ae.sync_artifact_embeddings(self.db, embedder=embed, dry_run=True)
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["embedded"], 0)
        with duckdb.connect(str(self.db), read_only=True) as con:
            self.assertIsNone(con.execute("SELECT embedding FROM derived_artifacts").fetchone()[0])

    def test_invalid_vectors_do_not_mark_summaries_indexed(self):
        self.seed()
        for bad in ([], [[]], [[0.0, 0.0]], [[float("nan"), 1.0]]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ae.sync_artifact_embeddings(self.db, embedder=lambda texts: bad)
        with duckdb.connect(str(self.db), read_only=True) as con:
            self.assertIsNone(con.execute("SELECT embedding FROM derived_artifacts").fetchone()[0])
