import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.memory as memory
from config.settings import OLLAMA_BASE_URL, OLLAMA_EMBEDDINGS_URL


class MemoryEmbeddingTests(unittest.TestCase):
    def test_config_carries_mac_ollama_base_url(self):
        self.assertEqual(OLLAMA_BASE_URL, "http://192.168.1.13:11434")
        self.assertEqual(
            OLLAMA_EMBEDDINGS_URL,
            "http://192.168.1.13:11434/api/embeddings",
        )

    @patch("agent.memory.httpx.post")
    def test_embed_posts_to_configured_ollama_embeddings_url(self, post):
        post.return_value.json.return_value = {"embedding": [0.1, 0.2]}
        post.return_value.raise_for_status.return_value = None

        self.assertEqual(memory._embed("test"), [0.1, 0.2])

        post.assert_called_once_with(
            "http://192.168.1.13:11434/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": "test"},
            timeout=30.0,
        )


if __name__ == "__main__":
    unittest.main()
