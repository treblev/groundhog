import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.memory as memory
from config.settings import (
    OLLAMA_BASE_URL,
    OLLAMA_EMBED_URL,
    OLLAMA_EMBEDDING_MODEL,
)
from ingestion import schema


class MemoryEmbeddingTests(unittest.TestCase):
    def test_config_carries_mac_ollama_base_url(self):
        self.assertEqual(OLLAMA_BASE_URL, "http://Vijays-MacBook-Pro.local:11434")
        self.assertEqual(
            OLLAMA_EMBED_URL,
            "http://Vijays-MacBook-Pro.local:11434/api/embed",
        )
        self.assertEqual(OLLAMA_EMBEDDING_MODEL, "qwen3-embedding:0.6b")

    @patch("agent.embeddings.httpx.post")
    def test_embed_posts_to_configured_ollama_embed_url(self, post):
        post.return_value.json.return_value = {"embeddings": [[0.1, 0.2]]}
        post.return_value.raise_for_status.return_value = None

        self.assertEqual(memory._embed("test"), [0.1, 0.2])

        post.assert_called_once_with(
            OLLAMA_EMBED_URL,
            json={"model": "qwen3-embedding:0.6b", "input": ["test"]},
            timeout=120.0,
        )

    def test_remember_tags_current_model_and_replaces_legacy_embedding(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "groundhog.duckdb"
            schema.init_db(db_path)
            con = duckdb.connect(str(db_path))
            try:
                with patch.object(memory, "_embed", return_value=[0.1, 0.2, 0.3]):
                    memory.remember(con, "prefers rowing")
                row = con.execute(
                    "SELECT embedding_model, array_length(embedding) FROM memory"
                ).fetchone()
                self.assertEqual(row, (OLLAMA_EMBEDDING_MODEL, 3))
            finally:
                con.close()

    def test_sync_reembeds_legacy_memory_and_recall_ignores_mixed_models(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "groundhog.duckdb"
            schema.init_db(db_path)
            con = duckdb.connect(str(db_path))
            try:
                con.execute(
                    "INSERT INTO memory (id, fact, embedding) VALUES ('legacy', 'likes sleds', [1.0, 0.0])"
                )
                with patch.object(memory, "_embed", return_value=[1.0, 0.0, 0.0]):
                    self.assertEqual(memory.recall(con, "sleds"), "No relevant memories found.")
            finally:
                con.close()

            result = memory.sync_memory_embeddings(
                db_path,
                embedder=lambda texts: [[1.0, 0.0, 0.0] for _ in texts],
            )
            self.assertEqual(result["reembedded"], 1)
            self.assertEqual(result["dimensions"], 3)

            con = duckdb.connect(str(db_path))
            try:
                with patch.object(memory, "_embed", return_value=[1.0, 0.0, 0.0]):
                    recalled = memory.recall(con, "sleds")
                self.assertIn("likes sleds", recalled)
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
