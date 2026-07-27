"""OpenAI-compatible chat client.

Two non-negotiable payload fields, both learned the hard way against the
omniroute proxy:

  stream: false            the proxy streams SSE back by default, and .json()
                           then dies on a body that starts with "data: "
  include_reasoning: false a LiteLLM/omniroute extension that suppresses the
                           model's thinking trace, which otherwise eats the
                           whole completion budget before any answer appears
"""

from __future__ import annotations

import json
import re
import secrets
import time

import httpx

from app.config import effective
from app.db import get_conn
from app.errors import LLMAuthError, LLMError, LLMReasoningTruncatedError
from app.logging_config import get_logger

log = get_logger("llm")

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(content: str) -> dict:
    """Pull a JSON object out of a model reply.

    Models wrap JSON in fences, prefix it with prose, and occasionally emit
    single-quote escapes. Copied from email-triage/triage.py and hardened.
    """
    if not content or not content.strip():
        raise LLMError("Model returned an empty response")

    text = content.strip()

    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    def _attempt(candidate: str) -> dict | None:
        for variant in (candidate, candidate.replace("\\'", "'")):
            try:
                parsed = json.loads(variant)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    result = _attempt(text)
    if result is not None:
        return result

    # Slice to the outermost brace pair. Handles both leading prose and the
    # trailing "Hope that helps!" that otherwise reads as JSON "Extra data".
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise LLMError(f"No JSON object found in response: {content[:200]}")

    result = _attempt(text[start : end + 1])
    if result is not None:
        return result

    raise LLMError(f"Model returned malformed JSON: {content[:200]}")


def build_payload(
    model: str,
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        # Both hardcoded, not caller-supplied: see the module docstring.
        "stream": False,
        "include_reasoning": False,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    return payload


class LLMConfig:
    def __init__(self, base_url: str, api_key: str, model: str, ssl_verify: bool,
                 timeout: int, temperature: float):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.ssl_verify = ssl_verify
        self.timeout = timeout
        self.temperature = temperature

    @classmethod
    def from_db(cls, conn, model_override: str | None = None) -> "LLMConfig":
        return cls(
            base_url=effective(conn, "llm_base_url"),
            api_key=effective(conn, "llm_api_key"),
            model=model_override or effective(conn, "llm_model"),
            ssl_verify=effective(conn, "llm_ssl_verify"),
            timeout=effective(conn, "llm_timeout_sec"),
            temperature=effective(conn, "llm_temperature"),
        )

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


def chat(config: LLMConfig, payload: dict) -> tuple[str, dict]:
    """Blocking POST. Returns ``(content, usage)``."""
    url = f"{config.base_url}/chat/completions"
    try:
        response = httpx.post(
            url,
            json=payload,
            headers=config.headers,
            timeout=config.timeout,
            verify=config.ssl_verify,
        )
    except httpx.ConnectError as exc:
        raise LLMError(f"Could not reach the LLM at {url}") from exc
    except httpx.TimeoutException as exc:
        raise LLMError(f"LLM timed out after {config.timeout}s") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"LLM request failed: {exc}") from exc

    if response.status_code in (401, 403):
        raise LLMAuthError(
            f"LLM rejected the API key ({response.status_code}). Check LLM settings."
        )
    if response.status_code >= 400:
        body_preview = response.text[:300].strip() or "(empty body)"
        raise LLMError(f"LLM returned {response.status_code}: {body_preview}")

    # Defensive: if the server streamed anyway, say so plainly rather than
    # dying inside .json() with an opaque decode error.
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("text/event-stream"):
        raise LLMError(
            "LLM streamed a response despite stream=false. The endpoint is "
            "misconfigured for non-streaming use."
        )

    try:
        body = response.json()
    except ValueError as exc:
        raise LLMError(f"LLM returned non-JSON: {response.text[:200]}") from exc

    choices = body.get("choices") or []
    if not choices:
        raise LLMError(f"LLM response had no choices: {json.dumps(body)[:200]}")

    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    if not content.strip():
        # A reasoning-only reply means the model spent the whole budget
        # thinking before it could answer. include_reasoning=false only
        # controls whether that trace is labelled separately -- some
        # providers (e.g. deepseek via omniroute) reason unconditionally and
        # still charge those tokens against max_tokens, just under
        # "reasoning_content" instead of "reasoning".
        if message.get("reasoning") or message.get("reasoning_content"):
            raise LLMReasoningTruncatedError(
                "LLM returned only a reasoning trace and no content. The model "
                "hit max_tokens while thinking -- raise max_tokens or use a "
                "non-reasoning model."
            )
        raise LLMError("LLM returned empty content")

    return content, body.get("usage") or {}


def chat_json(
    config: LLMConfig,
    system: str,
    user: str,
    *,
    max_tokens: int | None = None,
    retry_on_bad_json: bool = True,
) -> tuple[dict, dict, str]:
    """Chat and parse a JSON object. Returns ``(parsed, usage, raw_content)``."""
    payload = build_payload(
        config.model, system, user,
        temperature=config.temperature, max_tokens=max_tokens,
    )

    started = time.monotonic()
    content, usage = chat(config, payload)

    try:
        parsed = extract_json(content)
    except LLMError:
        if not retry_on_bad_json:
            raise
        log.warning("LLM returned unparseable JSON; retrying once")
        payload["messages"].extend(
            [
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": "Your previous reply was not valid JSON. Return "
                               "only the JSON object, with no prose and no code fence.",
                },
            ]
        )
        content, usage = chat(config, payload)
        parsed = extract_json(content)

    log.info(
        "llm %s completed in %.1fs (%s prompt / %s completion tokens)",
        config.model,
        time.monotonic() - started,
        usage.get("prompt_tokens", "?"),
        usage.get("completion_tokens", "?"),
    )
    return parsed, usage, content


def test_connection(config: LLMConfig) -> dict:
    """A real, minimal completion -- the Settings 'Test' button.

    Deliberately exercises the actual code path (stream=false,
    include_reasoning=false, JSON-object parsing where relevant) rather than
    just checking the endpoint answers, since a reachable server with a
    misconfigured model still breaks the pipeline.

    max_tokens is deliberately generous. Reasoning models charge their
    internal trace against the budget before emitting any visible content,
    and that trace varies run to run -- measured at 26-83 tokens for this
    trivial prompt against deepseek-v4-flash. A tight budget makes a perfectly
    healthy model fail intermittently, which is far more confusing than a
    clean failure. Because a model stops as soon as it is done, a high ceiling
    costs no extra latency: 64 and 512 both return in ~1.5s.

    The user message includes a random nonce so repeated clicks are never an
    exact cache hit. Some gateways (omniroute included) cache a completion by
    (model, messages) alone, ignoring max_tokens -- without the nonce, a single
    earlier test with too small a budget poisons the cache and every later
    Test click replays that same truncated, empty-content response forever,
    regardless of what this function now sends.
    """
    nonce = secrets.token_hex(4)
    started = time.monotonic()
    try:
        content, usage = chat(
            config,
            build_payload(
                config.model,
                "Reply with exactly one word.",
                f"Reply with the single word: ok (ref {nonce})",
                temperature=0,
                max_tokens=512,
            ),
        )
    except LLMReasoningTruncatedError:
        # Reaching this means the endpoint, credentials and model routing all
        # worked -- the model just reasoned past even a generous budget.
        # Calling that a connection failure would be a false negative, since
        # the summarize path sets no such ceiling.
        return {
            "ok": True,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": None,
            "response": None,
            "note": (
                "Connected. This is a reasoning model and it used the whole "
                "test budget thinking, so it returned no visible text -- "
                "normal summarization is unaffected."
            ),
        }
    except LLMError as exc:
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
        "response": content.strip()[:200],
    }


def list_models(base_url: str, api_key: str, ssl_verify: bool = True,
                timeout: int = 20) -> list[dict]:
    url = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = httpx.get(url, headers=headers, timeout=timeout, verify=ssl_verify)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not list models: {exc}") from exc
    return response.json().get("data", [])


def estimate_tokens(text: str) -> int:
    """Rough token count. Avoids a tiktoken dependency for a threshold check."""
    return int(len(text) / 3.6)
