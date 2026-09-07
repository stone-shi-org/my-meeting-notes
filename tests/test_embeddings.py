"""The embedding client behind matching's semantic pre-filter (MMN-7 Part B)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.services import embeddings as embeddings_svc

EMBEDDING_URL = "https://embed.test/v1/embeddings"


@pytest.fixture(autouse=True)
def base_settings(monkeypatch):
    monkeypatch.setenv("MMN_EMBEDDING_BASE_URL", "https://embed.test/v1")
    monkeypatch.setenv("MMN_EMBEDDING_API_KEY", "sk-embed-configured")
    monkeypatch.setenv("MMN_EMBEDDING_MODEL", "text-embedding-test")
    monkeypatch.setenv("MMN_EMBEDDING_TIMEOUT_SEC", "15")
    monkeypatch.setenv("MMN_EMBEDDING_MIN_SCORE", "0.4")
    from app.config import reset_settings_cache

    reset_settings_cache()


def config(**kw):
    return embeddings_svc.EmbeddingConfig(
        base_url=kw.get("base_url", "https://embed.test/v1"),
        api_key=kw.get("api_key", "sk-embed-test"),
        model=kw.get("model", "text-embedding-test"),
        timeout=kw.get("timeout", 15),
        min_score=kw.get("min_score", 0.4),
    )


# --------------------------------------------------------------------------- #
# EmbeddingConfig
# --------------------------------------------------------------------------- #


def test_config_from_db_reads_the_env_backed_defaults(conn):
    cfg = embeddings_svc.EmbeddingConfig.from_db(conn)
    assert cfg.base_url == "https://embed.test/v1"
    assert cfg.api_key == "sk-embed-configured"
    assert cfg.model == "text-embedding-test"
    assert cfg.timeout == 15
    assert cfg.min_score == 0.4


def test_headers_carry_a_bearer_token_when_a_key_is_set():
    assert config(api_key="sk-x").headers["Authorization"] == "Bearer sk-x"


def test_headers_omit_authorization_when_no_key_is_set():
    assert "Authorization" not in config(api_key="").headers


# --------------------------------------------------------------------------- #
# embed_texts()
# --------------------------------------------------------------------------- #


def test_embed_texts_of_an_empty_list_makes_no_request():
    """No respx mock armed at all -- a real request would fail the test."""
    assert embeddings_svc.embed_texts(config(), []) == []


@respx.mock
def test_embed_texts_sends_the_model_and_every_input():
    route = respx.post(EMBEDDING_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [
                {"index": 0, "embedding": [1.0, 0.0]},
                {"index": 1, "embedding": [0.0, 1.0]},
            ]},
        )
    )
    vectors = embeddings_svc.embed_texts(config(), ["context", "candidate"])

    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer sk-embed-test"
    assert json.loads(request.content) == {
        "model": "text-embedding-test", "input": ["context", "candidate"],
    }


@respx.mock
def test_embed_texts_reorders_by_the_response_index_not_arrival_order():
    respx.post(EMBEDDING_URL).mock(
        return_value=httpx.Response(
            200,
            json={"data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]},
        )
    )
    vectors = embeddings_svc.embed_texts(config(), ["first", "second"])
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]


@respx.mock
def test_embed_texts_raises_auth_error_on_401():
    from app.errors import EmbeddingAuthError

    respx.post(EMBEDDING_URL).mock(return_value=httpx.Response(401, text="nope"))
    with pytest.raises(EmbeddingAuthError):
        embeddings_svc.embed_texts(config(), ["x"])


@respx.mock
def test_embed_texts_raises_on_a_4xx_with_a_body_preview():
    from app.errors import EmbeddingError

    respx.post(EMBEDDING_URL).mock(return_value=httpx.Response(400, text="bad model"))
    with pytest.raises(EmbeddingError, match="bad model"):
        embeddings_svc.embed_texts(config(), ["x"])


@respx.mock
def test_embed_texts_raises_on_connect_error():
    from app.errors import EmbeddingError

    respx.post(EMBEDDING_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(EmbeddingError, match="Could not reach"):
        embeddings_svc.embed_texts(config(), ["x"])


@respx.mock
def test_embed_texts_raises_on_timeout():
    from app.errors import EmbeddingError

    respx.post(EMBEDDING_URL).mock(side_effect=httpx.TimeoutException("timed out"))
    with pytest.raises(EmbeddingError, match="timed out"):
        embeddings_svc.embed_texts(config(), ["x"])


@respx.mock
def test_embed_texts_raises_on_non_json_body():
    from app.errors import EmbeddingError

    respx.post(EMBEDDING_URL).mock(return_value=httpx.Response(200, text="not json"))
    with pytest.raises(EmbeddingError, match="non-JSON"):
        embeddings_svc.embed_texts(config(), ["x"])


@respx.mock
def test_embed_texts_raises_on_a_mismatched_vector_count():
    """The service returning fewer/more vectors than inputs must not silently
    zip texts to the wrong candidate's vector further up in matching.py."""
    from app.errors import EmbeddingError

    respx.post(EMBEDDING_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})
    )
    with pytest.raises(EmbeddingError, match="1 vector"):
        embeddings_svc.embed_texts(config(), ["x", "y"])


# --------------------------------------------------------------------------- #
# cosine_similarity()
# --------------------------------------------------------------------------- #


def test_cosine_similarity_of_identical_vectors_is_one():
    assert embeddings_svc.cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_cosine_similarity_of_orthogonal_vectors_is_zero():
    assert embeddings_svc.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_similarity_of_opposite_vectors_is_negative_one():
    assert embeddings_svc.cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_similarity_of_mismatched_lengths_is_zero_not_a_crash():
    assert embeddings_svc.cosine_similarity([1.0, 0.0], [1.0]) == 0.0


def test_cosine_similarity_of_a_zero_vector_is_zero_not_a_divide_by_zero():
    assert embeddings_svc.cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_cosine_similarity_of_empty_vectors_is_zero():
    assert embeddings_svc.cosine_similarity([], []) == 0.0


# --------------------------------------------------------------------------- #
# test_connection()
# --------------------------------------------------------------------------- #


@respx.mock
def test_connection_reports_dimensions_on_success():
    respx.post(EMBEDDING_URL).mock(
        return_value=httpx.Response(
            200, json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]}
        )
    )
    result = embeddings_svc.test_connection(config())
    assert result["ok"] is True
    assert result["response"] == "3 dimensions"


@respx.mock
def test_connection_reports_the_error_on_failure():
    respx.post(EMBEDDING_URL).mock(return_value=httpx.Response(401, text="nope"))
    result = embeddings_svc.test_connection(config())
    assert result["ok"] is False
    assert result["error"]
