import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.memory as memory
from config.settings import (
    OLLAMA_BASE_URL,
    OLLAMA_EMBED_URL,
    OLLAMA_EMBEDDING_MODEL,
)


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


if __name__ == "__main__":
    unittest.main()
