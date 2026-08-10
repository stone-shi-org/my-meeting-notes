"""The web_search chat tool's HTTP client and result formatting."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.services import web_search as web_search_svc

SEARCH_URL = "https://search.test/v1/search"


@pytest.fixture(autouse=True)
def base_settings(monkeypatch):
    monkeypatch.setenv("MMN_WEB_SEARCH_BASE_URL", "https://search.test")
    monkeypatch.setenv("MMN_WEB_SEARCH_API_KEY", "sk-search-configured")
    from app.config import reset_settings_cache

    reset_settings_cache()


def config(**kw):
    return web_search_svc.WebSearchConfig(
        base_url=kw.get("base_url", "https://search.test"),
        api_key=kw.get("api_key", "sk-search-test"),
        timeout=kw.get("timeout", 20),
    )


# --------------------------------------------------------------------------- #
# WebSearchConfig
# --------------------------------------------------------------------------- #


def test_config_from_db_reads_the_env_backed_defaults(conn):
    cfg = web_search_svc.WebSearchConfig.from_db(conn)
    assert cfg.base_url == "https://search.test"
    assert cfg.api_key == "sk-search-configured"
    assert cfg.timeout == 20


def test_headers_carry_a_bearer_token_when_a_key_is_set():
    assert config(api_key="sk-x").headers["Authorization"] == "Bearer sk-x"


def test_headers_omit_authorization_when_no_key_is_set():
    assert "Authorization" not in config(api_key="").headers


# --------------------------------------------------------------------------- #
# search()
# --------------------------------------------------------------------------- #


@respx.mock
def test_search_returns_results_capped_at_max_results():
    respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={"results": [{"title": f"r{i}", "url": f"https://x/{i}"} for i in range(10)]},
        )
    )
    results = web_search_svc.search(config(), "cutover window", max_results=3)
    assert len(results) == 3
    assert results[0]["title"] == "r0"


@respx.mock
def test_search_sends_the_query_and_bearer_header():
    route = respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    web_search_svc.search(config(api_key="sk-abc"), "rollback rehearsal")

    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer sk-abc"
    import json

    assert json.loads(request.content) == {"query": "rollback rehearsal"}


@respx.mock
def test_search_raises_auth_error_on_401():
    from app.errors import WebSearchAuthError

    respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "Invalid API key"}})
    )
    with pytest.raises(WebSearchAuthError):
        web_search_svc.search(config(), "x")


@respx.mock
def test_search_raises_on_a_4xx_with_a_body_preview():
    from app.errors import WebSearchError

    respx.post(SEARCH_URL).mock(return_value=httpx.Response(400, text="bad query"))
    with pytest.raises(WebSearchError, match="bad query"):
        web_search_svc.search(config(), "x")


@respx.mock
def test_search_raises_on_connect_error():
    from app.errors import WebSearchError

    respx.post(SEARCH_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(WebSearchError, match="Could not reach"):
        web_search_svc.search(config(), "x")


@respx.mock
def test_search_raises_on_timeout():
    from app.errors import WebSearchError

    respx.post(SEARCH_URL).mock(side_effect=httpx.TimeoutException("timed out"))
    with pytest.raises(WebSearchError, match="timed out"):
        web_search_svc.search(config(), "x")


@respx.mock
def test_search_raises_on_non_json_body():
    from app.errors import WebSearchError

    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, text="not json"))
    with pytest.raises(WebSearchError, match="non-JSON"):
        web_search_svc.search(config(), "x")


# --------------------------------------------------------------------------- #
# format_tool_result() -- what the chat tool hop actually sees
# --------------------------------------------------------------------------- #


@respx.mock
def test_format_tool_result_success_includes_title_url_and_snippet(initialised_db):
    respx.post(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Cutover planning",
                        "url": "https://example.com/cutover",
                        "snippet": "rollback rehearsal is booked",
                    }
                ]
            },
        )
    )
    result = web_search_svc.format_tool_result(initialised_db, "cutover planning")
    assert "Cutover planning" in result
    assert "https://example.com/cutover" in result
    assert "rollback rehearsal is booked" in result


@respx.mock
def test_format_tool_result_says_so_when_nothing_is_found(initialised_db):
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    result = web_search_svc.format_tool_result(initialised_db, "an obscure query")
    assert result == "[No web results found for: an obscure query]"


@respx.mock
def test_format_tool_result_wraps_a_search_failure(initialised_db):
    respx.post(SEARCH_URL).mock(return_value=httpx.Response(401))
    result = web_search_svc.format_tool_result(initialised_db, "x")
    assert result.startswith("[Web search failed:")


def test_format_tool_result_rejects_a_blank_query(initialised_db):
    result = web_search_svc.format_tool_result(initialised_db, "   ")
    assert result == "[web_search needs a query]"


def test_format_tool_result_says_so_when_not_configured(initialised_db, monkeypatch):
    monkeypatch.setenv("MMN_WEB_SEARCH_BASE_URL", "")
    from app.config import reset_settings_cache

    reset_settings_cache()

    result = web_search_svc.format_tool_result(initialised_db, "anything")
    assert "not configured" in result
