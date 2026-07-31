"""The OpenAI-compatible chat client and its JSON extraction."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.errors import LLMAuthError, LLMError, ValidationError
from app.services import llm as llm_svc

BASE = "https://llm.test/v1"
URL = f"{BASE}/chat/completions"


def config(**kw) -> llm_svc.LLMConfig:
    return llm_svc.LLMConfig(
        base_url=kw.get("base_url", BASE),
        api_key=kw.get("api_key", "sk-test"),
        model=kw.get("model", "some/model"),
        ssl_verify=kw.get("ssl_verify", True),
        timeout=kw.get("timeout", 30),
        temperature=kw.get("temperature", 0.2),
    )


def completion(content: str, **extra) -> dict:
    message = {"role": "assistant", "content": content}
    message.update(extra)
    return {
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


# --------------------------------------------------------------------------- #
# Payload -- the two flags that cost real debugging time
# --------------------------------------------------------------------------- #


class TestPayload:
    def test_stream_and_include_reasoning_are_both_false(self):
        payload = llm_svc.build_payload("m", "sys", "usr")
        # The proxy streams SSE by default, which breaks .json().
        assert payload["stream"] is False
        # Without this the model spends the whole budget on a thinking trace.
        assert payload["include_reasoning"] is False

    def test_messages_are_system_then_user(self):
        payload = llm_svc.build_payload("m", "SYS", "USR")
        assert payload["messages"] == [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "USR"},
        ]

    def test_max_tokens_is_omitted_unless_asked_for(self):
        assert "max_tokens" not in llm_svc.build_payload("m", "s", "u")
        assert llm_svc.build_payload("m", "s", "u", max_tokens=500)["max_tokens"] == 500

    @respx.mock
    def test_the_flags_survive_into_the_wire_request(self):
        route = respx.post(URL).mock(
            return_value=httpx.Response(200, json=completion('{"ok": true}'))
        )
        llm_svc.chat_json(config(), "sys", "usr")

        body = json.loads(route.calls[0].request.content)
        assert body["stream"] is False
        assert body["include_reasoning"] is False

    @respx.mock
    def test_bearer_header_is_sent(self):
        route = respx.post(URL).mock(
            return_value=httpx.Response(200, json=completion('{"ok": true}'))
        )
        llm_svc.chat_json(config(api_key="sk-abc"), "s", "u")
        assert route.calls[0].request.headers["authorization"] == "Bearer sk-abc"

    @respx.mock
    def test_no_auth_header_when_the_key_is_blank(self):
        route = respx.post(URL).mock(
            return_value=httpx.Response(200, json=completion('{"ok": true}'))
        )
        llm_svc.chat_json(config(api_key=""), "s", "u")
        assert "authorization" not in route.calls[0].request.headers


# --------------------------------------------------------------------------- #
# extract_json
# --------------------------------------------------------------------------- #


class TestExtractJson:
    @pytest.mark.parametrize(
        "content,expected",
        [
            ('{"a": 1}', {"a": 1}),
            ('  {"a": 1}  ', {"a": 1}),
            ('```json\n{"a": 1}\n```', {"a": 1}),
            ('```\n{"a": 1}\n```', {"a": 1}),
            ('Here is your JSON:\n{"a": 1}', {"a": 1}),
            ('{"a": 1}\nHope that helps!', {"a": 1}),
            ('```json\n{"nested": {"b": [1, 2]}}\n```', {"nested": {"b": [1, 2]}}),
        ],
    )
    def test_recovers_json_from_the_ways_models_wrap_it(self, content, expected):
        assert llm_svc.extract_json(content) == expected

    def test_handles_escaped_apostrophes(self):
        assert llm_svc.extract_json(r'{"text": "it\'s fine"}') == {"text": "it's fine"}

    def test_empty_content_is_an_error(self):
        with pytest.raises(LLMError, match="empty"):
            llm_svc.extract_json("")
        with pytest.raises(LLMError, match="empty"):
            llm_svc.extract_json("   ")

    def test_no_json_at_all_is_an_error(self):
        with pytest.raises(LLMError, match="No JSON object"):
            llm_svc.extract_json("I'm afraid I can't do that.")

    def test_malformed_json_is_an_error(self):
        with pytest.raises(LLMError, match="malformed"):
            llm_svc.extract_json('{"a": 1,,,}')


# --------------------------------------------------------------------------- #
# Transport failures
# --------------------------------------------------------------------------- #


class TestFailures:
    @respx.mock
    def test_401_is_an_auth_error_with_a_config_code(self):
        respx.post(URL).mock(return_value=httpx.Response(401, json={"error": "bad key"}))
        with pytest.raises(LLMAuthError) as exc:
            llm_svc.chat_json(config(), "s", "u")
        assert exc.value.code == "LLM_AUTH_FAILED"

    @respx.mock
    def test_403_is_also_an_auth_error(self):
        respx.post(URL).mock(return_value=httpx.Response(403))
        with pytest.raises(LLMAuthError):
            llm_svc.chat_json(config(), "s", "u")

    @respx.mock
    def test_500_surfaces_the_body(self):
        respx.post(URL).mock(return_value=httpx.Response(500, text="model unavailable"))
        with pytest.raises(LLMError, match="model unavailable"):
            llm_svc.chat_json(config(), "s", "u")

    @respx.mock
    def test_connection_refused(self):
        respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(LLMError, match="Could not reach"):
            llm_svc.chat_json(config(), "s", "u")

    @respx.mock
    def test_timeout(self):
        respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(LLMError, match="timed out"):
            llm_svc.chat_json(config(), "s", "u")

    @respx.mock
    def test_an_unexpectedly_streamed_response_says_so(self):
        """Guards the exact failure that motivated hardcoding stream=false."""
        respx.post(URL).mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text='data: {"choices":[]}\n\n',
            )
        )
        with pytest.raises(LLMError, match="streamed a response despite"):
            llm_svc.chat_json(config(), "s", "u")

    @respx.mock
    def test_no_choices(self):
        respx.post(URL).mock(return_value=httpx.Response(200, json={"choices": []}))
        with pytest.raises(LLMError, match="no choices"):
            llm_svc.chat_json(config(), "s", "u")

    @respx.mock
    def test_reasoning_only_reply_names_the_real_cause(self):
        respx.post(URL).mock(
            return_value=httpx.Response(
                200, json=completion("", reasoning="Let me think about this...")
            )
        )
        with pytest.raises(LLMError, match="only a reasoning trace"):
            llm_svc.chat_json(config(), "s", "u")


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #


class TestRetry:
    @respx.mock
    def test_bad_json_is_retried_once_then_succeeds(self):
        route = respx.post(URL).mock(
            side_effect=[
                httpx.Response(200, json=completion("Sorry, here's some prose.")),
                httpx.Response(200, json=completion('{"recovered": true}')),
            ]
        )
        parsed, _, _ = llm_svc.chat_json(config(), "s", "u")

        assert parsed == {"recovered": True}
        assert route.call_count == 2

        # The retry shows the model its own bad reply and asks again.
        second = json.loads(route.calls[1].request.content)
        assert len(second["messages"]) == 4
        assert second["messages"][2]["role"] == "assistant"
        assert "not valid JSON" in second["messages"][3]["content"]

    @respx.mock
    def test_two_bad_replies_give_up(self):
        respx.post(URL).mock(
            return_value=httpx.Response(200, json=completion("still not json"))
        )
        with pytest.raises(LLMError):
            llm_svc.chat_json(config(), "s", "u")

    @respx.mock
    def test_retry_can_be_disabled(self):
        route = respx.post(URL).mock(
            return_value=httpx.Response(200, json=completion("not json"))
        )
        with pytest.raises(LLMError):
            llm_svc.chat_json(config(), "s", "u", retry_on_bad_json=False)
        assert route.call_count == 1


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #


@respx.mock
def test_usage_is_returned():
    respx.post(URL).mock(return_value=httpx.Response(200, json=completion('{"a":1}')))
    _, usage, _ = llm_svc.chat_json(config(), "s", "u")
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 50


@respx.mock
def test_list_models():
    respx.get(f"{BASE}/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})
    )
    assert [m["id"] for m in llm_svc.list_models(BASE, "sk")] == ["a", "b"]


@respx.mock
def test_list_models_failure_is_wrapped():
    respx.get(f"{BASE}/models").mock(side_effect=httpx.ConnectError("no"))
    with pytest.raises(LLMError):
        llm_svc.list_models(BASE, "sk")


def test_token_estimate_is_roughly_chars_over_3_6():
    assert llm_svc.estimate_tokens("x" * 360) == 100
    assert llm_svc.estimate_tokens("") == 0


# --------------------------------------------------------------------------- #
# Enabled chat models
# --------------------------------------------------------------------------- #


def _set_chat_models(conn, models: list[str]) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) "
        "VALUES ('llm_chat_models', ?, 'json', 0, '2026-01-01') "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (json.dumps(models),),
    )


def test_enabled_chat_models_defaults_to_just_the_configured_model(conn):
    from app.config import get_settings

    assert llm_svc.enabled_chat_models(conn) == [get_settings().llm_model]


def test_enabled_chat_models_dedupes_with_default_first(conn):
    from app.config import get_settings

    default = get_settings().llm_model
    _set_chat_models(conn, ["a", default, "b"])
    assert llm_svc.enabled_chat_models(conn) == [default, "a", "b"]


def test_resolve_chat_model_none_defers_to_the_configured_default(conn):
    assert llm_svc.resolve_chat_model(conn, None) is None


def test_resolve_chat_model_allows_the_default_even_if_unlisted(conn):
    from app.config import get_settings

    default = get_settings().llm_model
    assert llm_svc.resolve_chat_model(conn, default) == default


def test_resolve_chat_model_allows_a_configured_extra(conn):
    _set_chat_models(conn, ["extra/model"])
    assert llm_svc.resolve_chat_model(conn, "extra/model") == "extra/model"


def test_resolve_chat_model_rejects_anything_not_enabled(conn):
    with pytest.raises(ValidationError):
        llm_svc.resolve_chat_model(conn, "not/allowed")
