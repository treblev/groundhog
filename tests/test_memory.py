import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.memory as memory


class MemoryEmbeddingTests(unittest.TestCase):
    def test_default_embeddings_url_uses_local_ollama(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                memory._ollama_embeddings_url(),
                "http://localhost:11434/api/embeddings",
            )

    def test_embeddings_url_respects_ollama_host(self):
        with patch.dict(os.environ, {"OLLAMA_HOST": "http://192.168.1.13:11434"}):
            self.assertEqual(
                memory._ollama_embeddings_url(),
                "http://192.168.1.13:11434/api/embeddings",
            )

    def test_embeddings_url_adds_scheme_when_missing(self):
        with patch.dict(os.environ, {"OLLAMA_HOST": "192.168.1.13:11434"}):
            self.assertEqual(
                memory._ollama_embeddings_url(),
                "http://192.168.1.13:11434/api/embeddings",
            )

    @patch("agent.memory.httpx.post")
    def test_embed_posts_to_configured_ollama_host(self, post):
        post.return_value.json.return_value = {"embedding": [0.1, 0.2]}
        post.return_value.raise_for_status.return_value = None

        with patch.dict(os.environ, {"OLLAMA_HOST": "http://192.168.1.13:11434"}):
            self.assertEqual(memory._embed("test"), [0.1, 0.2])

        post.assert_called_once_with(
            "http://192.168.1.13:11434/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": "test"},
            timeout=30.0,
        )


if __name__ == "__main__":
    unittest.main()
