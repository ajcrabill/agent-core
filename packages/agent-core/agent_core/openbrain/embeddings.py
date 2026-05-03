"""Embedding providers — convert text into a vector.

Real implementations call an embedding model API. Tests use a deterministic
stub that produces a vector from a hash of the input — same input → same
vector — so similarity tests are reproducible.

Production default: Ollama with ``nomic-embed-text`` (matches Esby's working
config). Other providers (OpenAI, Voyage, etc.) plug in by satisfying the
EmbeddingProvider Protocol.
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
import urllib.error
import urllib.request
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ── Protocol ─────────────────────────────────────────────────────────────────


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Produce a fixed-dimension float vector from text."""

    @property
    def model_id(self) -> str:
        """Stable identifier for the embedding model.

        Used to namespace stored embeddings — if you switch models, old
        embeddings should be re-indexed (different vector space).
        Convention: 'provider:model-name', e.g., 'ollama:nomic-embed-text'.
        """

    @property
    def dimensions(self) -> int:
        """Output vector length."""

    def embed(self, text: str) -> list[float]:
        """Return a unit-length-ish embedding for ``text``."""


# ── Stub: deterministic vectors for tests ───────────────────────────────────


class StubEmbeddingProvider:
    """Deterministic stub. Hashes text into a fixed-dimensional vector.

    Properties:
      - Same input → same vector (tests are reproducible)
      - Different inputs → different vectors (real similarity behavior)
      - Similar inputs → vectors are NOT necessarily similar (this is a
        hash, not a real embedding) — for tests that need semantic
        similarity, see SemanticStubProvider below.

    Vectors live in ``[0, 1]^16`` so cosine similarity is always non-negative
    (real production embeddings live in a positive cone too). This avoids
    the artifact where two unrelated short strings get an arbitrarily
    negative cosine that filters out under default thresholds.
    """

    model_id = "stub:hash-256"
    dimensions = 16

    def embed(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # Unpack as 16 little-endian uint16 → normalize to [0, 1]
        ints = struct.unpack("<16H", digest[:32])
        return [i / 65535.0 for i in ints]


class SemanticStubProvider:
    """Stub that gives similar embeddings for text sharing content tokens.

    Implementation: feature-hashing bag-of-words. Each lowercased token
    hashes to one of ``dimensions`` slots and contributes 1.0 there. Two
    texts with overlapping vocabulary get nonzero cosine similarity in
    proportion to overlap; identical texts get identical vectors;
    completely disjoint texts get cosine ≈ 0.

    NOT suitable for production — it's a deterministic offline-friendly
    proxy for testing similarity behavior without needing a model.
    """

    model_id = "stub:semantic-256"
    dimensions = 64

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for tok in text.lower().split():
            # Strip lightweight punctuation so "fox," and "fox" hash the same.
            clean = tok.strip(".,;:!?\"'()[]{}")
            if not clean:
                continue
            slot = (
                int.from_bytes(hashlib.sha256(clean.encode("utf-8")).digest()[:4], "little")
                % self.dimensions
            )
            vec[slot] += 1.0
        return vec


# ── Ollama (production default) ─────────────────────────────────────────────


class OllamaEmbeddingProvider:
    """Ollama-backed embedding provider. Default model: nomic-embed-text
    (matches Esby's working setup)."""

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:11434",
        model: str = "nomic-embed-text",
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._dimensions: int | None = None

    @property
    def model_id(self) -> str:
        return f"ollama:{self.model}"

    @property
    def dimensions(self) -> int:
        if self._dimensions is None:
            # Probe with a tiny embedding — covers the case where the user
            # asks for `dimensions` before calling `embed()`.
            self._dimensions = len(self.embed("dimension probe"))
        return self._dimensions

    def embed(self, text: str) -> list[float]:
        # Cap input — Ollama embedders typically have a context limit;
        # 12k chars is a safe default that matches Esby's openbrain_mcp.py.
        capped = text[:12000]
        payload = json.dumps({"model": self.model, "input": capped}).encode("utf-8")
        url = f"{self.base_url}/api/embed"
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read())
                # Ollama returns either {"embedding": [...]} or
                # {"embeddings": [[...]]} depending on the version.
                if "embedding" in data:
                    vec = data["embedding"]
                elif "embeddings" in data and data["embeddings"]:
                    vec = data["embeddings"][0]
                else:
                    raise RuntimeError(f"unexpected response shape: {list(data.keys())}")
                self._dimensions = len(vec)
                return vec
            except (urllib.error.URLError, TimeoutError, RuntimeError) as e:
                last_err = e
                if attempt < 2:
                    import time

                    time.sleep(2**attempt)  # 1s, 2s
                    continue
                raise
        raise RuntimeError(f"OllamaEmbeddingProvider: {self.base_url}/api/embed failed: {last_err}")


__all__ = [
    "EmbeddingProvider",
    "OllamaEmbeddingProvider",
    "SemanticStubProvider",
    "StubEmbeddingProvider",
]
