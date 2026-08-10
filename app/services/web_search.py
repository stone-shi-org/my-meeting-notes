"""Web search: the AI chat's tool for anything outside this app's own data.

Same shape as ``llm.py``'s client -- a ``Config.from_db()``, a blocking
``httpx`` call with the same connect/timeout/4xx/401 handling, and a
``test_connection()`` for the Settings "Test" button -- because it's the same
kind of dependency: one base URL, one bearer key, one JSON endpoint.
"""

from __future__ import annotations

import time

import httpx

from app.config import effective
from app.db import get_conn
from app.errors import WebSearchAuthError, WebSearchError
from app.logging_config import get_logger

log = get_logger("web_search")

# How many results the model sees per search -- kept small since these are
# read back into a chat reply, not rendered as a picker (same reasoning as
# chat.py's SEARCH_MAX_CANDIDATES).
WEB_SEARCH_MAX_RESULTS = 5


class WebSearchConfig:
    def __init__(self, base_url: str, api_key: str, timeout: int):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    @classmethod
    def from_db(cls, conn) -> "WebSearchConfig":
        return cls(
            base_url=effective(conn, "web_search_base_url"),
            api_key=effective(conn, "web_search_api_key"),
            timeout=effective(conn, "web_search_timeout_sec"),
        )

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


def search(config: WebSearchConfig, query: str, *, max_results: int = WEB_SEARCH_MAX_RESULTS) -> list[dict]:
    """Blocking POST to ``{base_url}/v1/search``. Returns up to ``max_results`` hits."""
    url = f"{config.base_url}/v1/search"
    try:
        response = httpx.post(
            url,
            json={"query": query},
            headers=config.headers,
            timeout=config.timeout,
        )
    except httpx.ConnectError as exc:
        raise WebSearchError(f"Could not reach the web search service at {url}") from exc
    except httpx.TimeoutException as exc:
        raise WebSearchError(f"Web search timed out after {config.timeout}s") from exc
    except httpx.HTTPError as exc:
        raise WebSearchError(f"Web search request failed: {exc}") from exc

    if response.status_code in (401, 403):
        raise WebSearchAuthError(
            f"Web search rejected the API key ({response.status_code}). Check Web Search settings."
        )
    if response.status_code >= 400:
        body_preview = response.text[:300].strip() or "(empty body)"
        raise WebSearchError(f"Web search returned {response.status_code}: {body_preview}")

    try:
        body = response.json()
    except ValueError as exc:
        raise WebSearchError(f"Web search returned non-JSON: {response.text[:200]}") from exc

    results = body.get("results") or []
    return results[:max_results]


def test_connection(config: WebSearchConfig) -> dict:
    """A real, minimal search -- the Settings 'Test' button."""
    started = time.monotonic()
    try:
        results = search(config, "connectivity test", max_results=1)
    except WebSearchError as exc:
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": exc.message,
            "response": None,
        }

    return {
        "ok": True,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "error": None,
        "response": f"{len(results)} result(s)",
    }


def _format_results(query: str, results: list[dict]) -> str:
    if not results:
        return f"[No web results found for: {query}]"
    lines = [f'Web search results for "{query}":']
    for r in results:
        title = r.get("title") or "(untitled)"
        url = r.get("url") or ""
        lines.append(f"- {title} ({url})")
        snippet = (r.get("snippet") or "").strip()
        if snippet:
            lines.append(f"  {snippet}")
    return "\n".join(lines)


def format_tool_result(db_path, query: str) -> str:
    """Run a search and format it as a chat tool result string.

    Opens its own short-lived connection rather than taking one from the
    caller: this runs inside ``asyncio.to_thread`` (chat.py/home_chat.py's
    tool dispatch), and a sqlite3 connection opened on the event loop's
    thread is not safe to hand into a worker thread -- same reasoning as
    chat_followups.generate_sync taking ``db_path`` instead of a live ``conn``.
    """
    query = query.strip()
    if not query:
        return "[web_search needs a query]"

    with get_conn(db_path) as conn:
        config = WebSearchConfig.from_db(conn)

    if not config.base_url:
        return "[Web search is not configured. Add it in Settings → Web Search.]"

    try:
        results = search(config, query)
    except WebSearchError as exc:
        return f"[Web search failed: {exc.message}]"

    return _format_results(query, results)
