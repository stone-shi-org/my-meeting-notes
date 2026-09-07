"""OpenAI-compatible embeddings client -- the semantic pre-filter ahead of
the ranking LLM call in ``matching.rank_sync`` (MMN-7 Part B).

Same shape as ``web_search.py``'s client: one base URL, one bearer key, one
JSON endpoint, blocking ``httpx``, a ``Config.from_db()``, and a
``test_connection()`` for the Settings "Test" button.

Deliberately its own base_url/api_key/model/timeout, not inherited from
llm_base_url/llm_api_key/llm_timeout_sec: confirmed on MMN-7 that the
embedding model can be served from a different deployment than the chat
model, even behind the same gateway.

Every caller here (matching._embedding_prefilter) is expected to fail
*open* on any error this module raises -- unlike rank_sync's own "LLM down
=> rank nothing" safety interlock, a broken embedding endpoint is a cost
optimization failing, not a safety concern. An unranked-but-unreviewed
candidate is fine; a candidate silently dropped by a mis-scored pre-filter,
with nobody ever seeing it, is not.
"""

from __future__ import annotations

import math
import time

import httpx

from app.config import effective
from app.errors import EmbeddingAuthError, EmbeddingError
from app.logging_config import get_logger

log = get_logger("embeddings")


class EmbeddingConfig:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: int, min_score: float):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.min_score = min_score

    @classmethod
    def from_db(cls, conn) -> "EmbeddingConfig":
        return cls(
            base_url=effective(conn, "embedding_base_url"),
            api_key=effective(conn, "embedding_api_key"),
            model=effective(conn, "embedding_model"),
            timeout=effective(conn, "embedding_timeout_sec"),
            min_score=effective(conn, "embedding_min_score"),
        )

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


def embed_texts(config: EmbeddingConfig, texts: list[str]) -> list[list[float]]:
    """Blocking POST to ``{base_url}/embeddings`` -- ``base_url`` already ends
    in ``/v1``, same convention as ``llm.chat()``'s ``{base_url}/chat/completions``.
    One call, N inputs: every OpenAI-compatible embeddings endpoint accepts a
    batch. Returned vectors are reordered by the response's own ``index``
    rather than assumed to arrive in request order.
    """
    if not texts:
        return []

    url = f"{config.base_url}/embeddings"
    try:
        response = httpx.post(
            url,
            json={"model": config.model, "input": texts},
            headers=config.headers,
            timeout=config.timeout,
        )
    except httpx.ConnectError as exc:
        raise EmbeddingError(f"Could not reach the embedding service at {url}") from exc
    except httpx.TimeoutException as exc:
        raise EmbeddingError(f"Embedding service timed out after {config.timeout}s") from exc
    except httpx.HTTPError as exc:
        raise EmbeddingError(f"Embedding request failed: {exc}") from exc

    if response.status_code in (401, 403):
        raise EmbeddingAuthError(
            f"Embedding service rejected the API key ({response.status_code}). "
            "Check the embedding settings."
        )
    if response.status_code >= 400:
        body_preview = response.text[:300].strip() or "(empty body)"
        raise EmbeddingError(f"Embedding service returned {response.status_code}: {body_preview}")

    try:
        body = response.json()
    except ValueError as exc:
        raise EmbeddingError(f"Embedding service returned non-JSON: {response.text[:200]}") from exc

    data = body.get("data") or []
    if len(data) != len(texts):
        raise EmbeddingError(
            f"Embedding service returned {len(data)} vector(s) for {len(texts)} input(s)"
        )
    ordered = sorted(data, key=lambda d: d.get("index", 0))
    return [d["embedding"] for d in ordered]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """0.0 for anything degenerate (mismatched length, a zero vector) rather
    than raising -- a malformed vector should read as "no similarity", not
    crash the pre-filter."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def test_connection(config: EmbeddingConfig) -> dict:
    """A real, minimal embedding call -- the Settings 'Test' button."""
    started = time.monotonic()
    try:
        vectors = embed_texts(config, ["connection test"])
    except EmbeddingError as exc:
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": exc.message,
        }

    return {
        "ok": True,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "error": None,
        "response": f"{len(vectors[0]) if vectors else 0} dimensions",
    }
