"""Search memories by meaning: a small local embedding model (nomic-embed-text via Ollama, 274 MB, ~15 ms per query).

Every memory gets a vector (content + life details) in the background; a question is turned into a vector and the
closest memories are found (store.by_meaning). If Ollama or the model is missing, everything falls back to keyword
search: nothing breaks, nothing is sent anywhere.
"""
from __future__ import annotations

import httpx

from memory.store import MemoryStore, life_details

MODEL = "nomic-embed-text"


class Embedder:
    def __init__(self, host: str, model: str = MODEL, timeout: float = 30.0):
        self.host, self.model, self.timeout = host, model, timeout
        self.failures = 0

    def _embed(self, texts: list[str], timeout: float | None = None) -> list[list[float]] | None:
        try:
            r = httpx.post(f"{self.host}/api/embed", timeout=timeout or self.timeout,
                           json={"model": self.model, "input": texts, "keep_alive": "24h"})
            r.raise_for_status()
            return r.json()["embeddings"]
        except Exception:
            self.failures += 1
            return None

    def query(self, text: str) -> list[float] | None:
        """nomic models expect "search_query:" / "search_document:" prefixes."""
        v = self._embed([f"search_query: {text[:2000]}"], timeout=1.0)   # never hold up a reply for long
        return v[0] if v else None

    def index(self, store: MemoryStore, batch: int = 32) -> int:
        """Make vectors for new or edited memories. Returns how many were made."""
        n = 0
        while (todo := store.needs_vectors(self.model, batch)):
            vecs = self._embed([f"search_document: {m.content}{life_details(m)}" for m in todo])
            if not vecs:
                break
            for m, v in zip(todo, vecs):
                store.set_vector(m, self.model, v)
            n += len(todo)
        return n
