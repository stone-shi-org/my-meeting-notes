"""The Test button endpoints for LLM, diarization and web search settings."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.services import diarize as diarize_svc
from app.services import llm as llm_svc
from app.services import transcribe as transcribe_svc
from app.services import web_search as web_search_svc

LLM_URL = "https://llm.test/v1/chat/completions"
DIARIZE_MODELS_URL = "http://diarizer.test/v1/models"
TRANSCRIBE_MODELS_URL = "http://transcriber.test/v1/models"
WEB_SEARCH_URL = "https://search.test/v1/search"


@pytest.fixture(autouse=True)
def base_settings(monkeypatch):
    monkeypatch.setenv("MMN_LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("MMN_LLM_MODEL", "test/model")
    monkeypatch.setenv("MMN_LLM_API_KEY", "sk-configured")
    monkeypatch.setenv("MMN_DIARIZATION_URL", "http://diarizer.test/v1/audio/diarization")
    monkeypatch.setenv("MMN_DIARIZATION_MODEL", "vibevoice-cpp-asr")
    monkeypatch.setenv("MMN_TRANSCRIBE_URL", "http://transcriber.test/v1/audio/transcriptions")
    monkeypatch.setenv("MMN_TRANSCRIBE_MODEL", "whisper-large-turbo-q8_0")
    monkeypatch.setenv("MMN_WEB_SEARCH_BASE_URL", "https://search.test")
    monkeypatch.setenv("MMN_WEB_SEARCH_API_KEY", "sk-search-configured")
    from app.config import reset_settings_cache

    reset_settings_cache()


def completion(content: str) -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1},
    }


# --------------------------------------------------------------------------- #
# Service layer
# --------------------------------------------------------------------------- #


class TestLLMTestConnection:
    def config(self, **kw):
        return llm_svc.LLMConfig(
            base_url=kw.get("base_url", "https://llm.test/v1"),
            api_key=kw.get("api_key", "sk-test"),
            model=kw.get("model", "test/model"),
            ssl_verify=True,
            timeout=30,
            temperature=0.2,
        )

    @respx.mock
    def test_success_reports_latency_and_a_response_preview(self):
        respx.post(LLM_URL).mock(return_value=httpx.Response(200, json=completion("ok")))
        result = llm_svc.test_connection(self.config())

        assert result["ok"] is True
        assert result["error"] is None
        assert result["response"] == "ok"
        assert result["latency_ms"] >= 0

    @respx.mock
    def test_uses_the_real_call_shape(self):
        """The test button must exercise stream=false / include_reasoning=false
        too, or a passing test could still mean a broken pipeline."""
        route = respx.post(LLM_URL).mock(return_value=httpx.Response(200, json=completion("ok")))
        llm_svc.test_connection(self.config())

        import json

        body = json.loads(route.calls[0].request.content)
        assert body["stream"] is False
        assert body["include_reasoning"] is False
        # Generous on purpose. A reasoning model's trace is charged against
        # this before any visible content and varies run to run (measured
        # 26-83 tokens for this prompt), so a tight ceiling fails
        # intermittently. Costs nothing: the model stops when it's done.
        assert body["max_tokens"] == 512

    @respx.mock
    def test_the_message_is_not_identical_between_calls(self):
        """Some gateways (omniroute included) cache a completion by (model,
        messages) alone, ignoring max_tokens. A fixed literal prompt means one
        earlier test with too small a budget poisons the cache forever, and
        every later click replays that stale empty-content response --
        regardless of what this call now sends. A nonce breaks the cache key."""
        route = respx.post(LLM_URL).mock(return_value=httpx.Response(200, json=completion("ok")))
        llm_svc.test_connection(self.config())
        llm_svc.test_connection(self.config())

        import json

        first = json.loads(route.calls[0].request.content)["messages"][1]["content"]
        second = json.loads(route.calls[1].request.content)["messages"][1]["content"]
        assert first != second

    @respx.mock
    def test_a_bad_model_is_reported_not_raised(self):
        respx.post(LLM_URL).mock(
            return_value=httpx.Response(500, text="model not found")
        )
        result = llm_svc.test_connection(self.config())
        assert result["ok"] is False
        assert "model not found" in result["error"]
        assert result["response"] is None

    @respx.mock
    def test_a_failure_with_an_empty_body_still_says_something(self):
        """omniroute's own 500 for a bare, unnamespaced model alias (e.g.
        "deepseek-v4-flash" instead of "deepseek/deepseek-v4-flash") comes
        back with no body at all -- the error must not just say "LLM returned
        500: "."""
        respx.post(LLM_URL).mock(return_value=httpx.Response(500, text=""))
        result = llm_svc.test_connection(self.config())
        assert result["ok"] is False
        assert "500" in result["error"]
        assert "empty body" in result["error"]

    @respx.mock
    def test_auth_failure_is_reported_not_raised(self):
        respx.post(LLM_URL).mock(return_value=httpx.Response(401))
        result = llm_svc.test_connection(self.config())
        assert result["ok"] is False
        assert "reject" in result["error"].lower() or "auth" in result["error"].lower()

    @respx.mock
    def test_a_reasoning_model_with_enough_headroom_still_answers(self):
        """The exact shape omniroute's deepseek route returns: reasoning burns
        part of the budget under reasoning_content, not the legacy
        'reasoning' key, and content still arrives if max_tokens has room."""
        respx.post(LLM_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "ok",
                                "reasoning_content": "The user asked me to say ok, so: ok.",
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 2006, "completion_tokens": 32},
                },
            )
        )
        result = llm_svc.test_connection(self.config())
        assert result["ok"] is True
        assert result["response"] == "ok"

    @respx.mock
    def test_reasoning_truncation_is_reachable_not_a_failure(self):
        """The false negative that made a healthy model look broken.

        A reasoning model can burn the whole budget thinking and return no
        visible text. That is not a connection problem -- endpoint, auth and
        model routing all demonstrably worked, and the summarize path sets no
        such ceiling. Reporting ok=False here is a lie that sends the user
        hunting a config bug that doesn't exist.
        """
        respx.post(LLM_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "reasoning_content": "We are asked to say",
                            },
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {"prompt_tokens": 2006, "completion_tokens": 512},
                },
            )
        )
        result = llm_svc.test_connection(self.config())
        assert result["ok"] is True
        assert result["error"] is None
        assert "reasoning model" in result["note"]

    @respx.mock
    def test_reasoning_truncation_still_fails_the_summarize_path(self):
        """Only the *connection test* forgives truncation. A real summary
        genuinely has no content, so chat() must still raise."""
        from app.errors import LLMReasoningTruncatedError

        respx.post(LLM_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "",
                                     "reasoning_content": "thinking..."}}
                    ],
                    "usage": {},
                },
            )
        )
        with pytest.raises(LLMReasoningTruncatedError):
            llm_svc.chat(self.config(), llm_svc.build_payload("m", "s", "u"))


class TestDiarizationTestConnection:
    @respx.mock
    def test_success_when_the_model_is_in_the_list(self):
        respx.get(DIARIZE_MODELS_URL).mock(
            return_value=httpx.Response(
                200, json={"data": [{"id": "vibevoice-cpp-asr"}, {"id": "whisper-large-turbo"}]}
            )
        )
        result = diarize_svc.test_connection(
            "http://diarizer.test/v1/audio/diarization", "vibevoice-cpp-asr"
        )
        assert result["ok"] is True
        assert result["model_found"] is True
        assert result["models_count"] == 2
        assert result["error"] is None

    @respx.mock
    def test_reachable_but_wrong_model_names_what_is_available(self):
        respx.get(DIARIZE_MODELS_URL).mock(
            return_value=httpx.Response(200, json={"data": [{"id": "whisper-large-turbo"}]})
        )
        result = diarize_svc.test_connection(
            "http://diarizer.test/v1/audio/diarization", "vibevoice-cpp-asr"
        )
        assert result["ok"] is False
        assert result["model_found"] is False
        assert "whisper-large-turbo" in result["error"]

    @respx.mock
    def test_unreachable_service_is_reported_not_raised(self):
        respx.get(DIARIZE_MODELS_URL).mock(side_effect=httpx.ConnectError("refused"))
        result = diarize_svc.test_connection(
            "http://diarizer.test/v1/audio/diarization", "vibevoice-cpp-asr"
        )
        assert result["ok"] is False
        assert result["models_count"] == 0

    @respx.mock
    def test_does_not_run_a_real_diarization(self):
        """Only /v1/models is hit -- a real diarization takes minutes."""
        models_route = respx.get(DIARIZE_MODELS_URL).mock(
            return_value=httpx.Response(200, json={"data": [{"id": "vibevoice-cpp-asr"}]})
        )
        diarize_route = respx.post("http://diarizer.test/v1/audio/diarization").mock(
            return_value=httpx.Response(200, json={"segments": []})
        )
        diarize_svc.test_connection(
            "http://diarizer.test/v1/audio/diarization", "vibevoice-cpp-asr"
        )
        assert models_route.called
        assert not diarize_route.called


class TestTranscribeTestConnection:
    """Mirrors TestDiarizationTestConnection -- same GET /v1/models probe,
    a different service and error class underneath."""

    @respx.mock
    def test_success_when_the_model_is_in_the_list(self):
        respx.get(TRANSCRIBE_MODELS_URL).mock(
            return_value=httpx.Response(
                200, json={"data": [{"id": "whisper-large-turbo-q8_0"}, {"id": "other"}]}
            )
        )
        result = transcribe_svc.test_connection(
            "http://transcriber.test/v1/audio/transcriptions", "whisper-large-turbo-q8_0"
        )
        assert result["ok"] is True
        assert result["model_found"] is True
        assert result["models_count"] == 2
        assert result["error"] is None

    @respx.mock
    def test_reachable_but_wrong_model_names_what_is_available(self):
        respx.get(TRANSCRIBE_MODELS_URL).mock(
            return_value=httpx.Response(200, json={"data": [{"id": "other-model"}]})
        )
        result = transcribe_svc.test_connection(
            "http://transcriber.test/v1/audio/transcriptions", "whisper-large-turbo-q8_0"
        )
        assert result["ok"] is False
        assert result["model_found"] is False
        assert "other-model" in result["error"]

    @respx.mock
    def test_unreachable_service_is_reported_not_raised(self):
        respx.get(TRANSCRIBE_MODELS_URL).mock(side_effect=httpx.ConnectError("refused"))
        result = transcribe_svc.test_connection(
            "http://transcriber.test/v1/audio/transcriptions", "whisper-large-turbo-q8_0"
        )
        assert result["ok"] is False
        assert result["models_count"] == 0

    @respx.mock
    def test_does_not_run_a_real_transcription(self):
        """Only /v1/models is hit -- a real transcription takes minutes."""
        models_route = respx.get(TRANSCRIBE_MODELS_URL).mock(
            return_value=httpx.Response(200, json={"data": [{"id": "whisper-large-turbo-q8_0"}]})
        )
        transcribe_route = respx.post("http://transcriber.test/v1/audio/transcriptions").mock(
            return_value=httpx.Response(200, json={"segments": []})
        )
        transcribe_svc.test_connection(
            "http://transcriber.test/v1/audio/transcriptions", "whisper-large-turbo-q8_0"
        )
        assert models_route.called
        assert not transcribe_route.called


class TestWebSearchTestConnection:
    def config(self, **kw):
        return web_search_svc.WebSearchConfig(
            base_url=kw.get("base_url", "https://search.test"),
            api_key=kw.get("api_key", "sk-search-test"),
            timeout=20,
        )

    @respx.mock
    def test_success_reports_latency_and_a_result_count(self):
        respx.post(WEB_SEARCH_URL).mock(
            return_value=httpx.Response(200, json={"results": [{"title": "x", "url": "y"}]})
        )
        result = web_search_svc.test_connection(self.config())

        assert result["ok"] is True
        assert result["error"] is None
        assert result["response"] == "1 result(s)"
        assert result["latency_ms"] >= 0

    @respx.mock
    def test_auth_failure_is_reported_not_raised(self):
        respx.post(WEB_SEARCH_URL).mock(return_value=httpx.Response(401))
        result = web_search_svc.test_connection(self.config())
        assert result["ok"] is False
        assert "reject" in result["error"].lower() or "auth" in result["error"].lower()

    @respx.mock
    def test_unreachable_service_is_reported_not_raised(self):
        respx.post(WEB_SEARCH_URL).mock(side_effect=httpx.ConnectError("refused"))
        result = web_search_svc.test_connection(self.config())
        assert result["ok"] is False
        assert "reach" in result["error"].lower()


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


@respx.mock
def test_llm_test_endpoint_uses_the_saved_config(admin_client):
    route = respx.post(LLM_URL).mock(return_value=httpx.Response(200, json=completion("ok")))
    resp = admin_client.post("/api/llm/test")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    body = __import__("json").loads(route.calls[0].request.content)
    assert body["model"] == "test/model"


@respx.mock
def test_llm_test_endpoint_can_try_unsaved_edits(admin_client):
    route = respx.post("https://other-llm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=completion("ok"))
    )
    resp = admin_client.post(
        "/api/llm/test",
        json={"base_url": "https://other-llm.test/v1", "model": "other/model"},
    )
    assert resp.status_code == 200
    assert route.called
    body = __import__("json").loads(route.calls[0].request.content)
    assert body["model"] == "other/model"


@respx.mock
def test_llm_test_endpoint_ignores_a_masked_api_key_echo(admin_client):
    """The form round-trips the masked placeholder; that must not become the
    literal API key sent to the server."""
    route = respx.post(LLM_URL).mock(return_value=httpx.Response(200, json=completion("ok")))
    admin_client.post("/api/llm/test", json={"api_key": "••••1234"})

    assert route.calls[0].request.headers["authorization"] == "Bearer sk-configured"


def test_llm_test_endpoint_is_admin_only(user_client):
    assert user_client.post("/api/llm/test").status_code == 403


@respx.mock
def test_diarization_test_endpoint_uses_the_saved_config(admin_client):
    respx.get(DIARIZE_MODELS_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "vibevoice-cpp-asr"}]})
    )
    resp = admin_client.post("/api/diarization/test")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@respx.mock
def test_diarization_test_endpoint_can_try_unsaved_edits(admin_client):
    route = respx.get("http://other-diarizer.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "custom-model"}]})
    )
    resp = admin_client.post(
        "/api/diarization/test",
        json={"url": "http://other-diarizer.test/v1/audio/diarization", "model": "custom-model"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert route.called


def test_diarization_test_endpoint_is_admin_only(user_client):
    assert user_client.post("/api/diarization/test").status_code == 403


# --------------------------------------------------------------------------- #
# /api/live-caption/test -- each of the three backends independently, with no
# diarization_url/diarization_api_key fallback (see config.RUNTIME_KEYS on why
# the three stopped sharing settings with each other and with Diarization).
# --------------------------------------------------------------------------- #


@respx.mock
def test_live_caption_test_endpoint_uses_the_saved_realtime_config(admin_client):
    admin_client.put(
        "/api/settings",
        json={
            "values": {
                "live_caption_realtime_url": "http://realtime.test/v1/audio/diarization",
                "live_caption_realtime_model": "lfm2.5-audio-1.5b-realtime",
            }
        },
    )
    respx.get("http://realtime.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "lfm2.5-audio-1.5b-realtime"}]})
    )
    resp = admin_client.post("/api/live-caption/test", json={"backend": "realtime"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["model_found"] is True


@respx.mock
def test_live_caption_test_endpoint_uses_the_saved_transcriptions_config(admin_client):
    admin_client.put(
        "/api/settings",
        json={
            "values": {
                "live_caption_transcriptions_url": "http://transcriptions.test/v1/audio/transcriptions",
                "live_caption_transcriptions_model": "whisper-large-turbo-q8_0",
            }
        },
    )
    respx.get("http://transcriptions.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "whisper-large-turbo-q8_0"}]})
    )
    resp = admin_client.post("/api/live-caption/test", json={"backend": "transcriptions"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_live_caption_test_endpoint_dispatches_live_stt_to_the_grpc_probe(admin_client, monkeypatch):
    """live_stt has no HTTP /v1/models to hit at all -- this must go through
    diarize_svc.test_live_stt_connection, not list_models()."""
    admin_client.put(
        "/api/settings",
        json={
            "values": {
                "live_caption_live_stt_url": "livestt.test:4030",
                "live_caption_live_stt_model": "realtime_eou_120m-v1",
            }
        },
    )
    seen = {}

    def fake_test_live_stt_connection(target_url, model, timeout=15):
        seen["target_url"] = target_url
        seen["model"] = model
        return {"ok": True, "latency_ms": 5, "error": None, "models_count": 1, "model_found": True}

    monkeypatch.setattr(diarize_svc, "test_live_stt_connection", fake_test_live_stt_connection)

    resp = admin_client.post("/api/live-caption/test", json={"backend": "live_stt"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert seen == {"target_url": "livestt.test:4030", "model": "realtime_eou_120m-v1"}


@respx.mock
def test_live_caption_test_endpoint_can_try_unsaved_edits(admin_client):
    route = respx.get("http://other-realtime.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "custom-model"}]})
    )
    resp = admin_client.post(
        "/api/live-caption/test",
        json={
            "backend": "realtime",
            "url": "http://other-realtime.test/v1/audio/diarization",
            "model": "custom-model",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert route.called


@respx.mock
def test_live_caption_test_endpoint_never_reflects_a_later_diarization_url_change(admin_client):
    """The whole point of splitting these settings apart: once
    live_caption_realtime_url exists (seeded once at first boot -- see
    services/diarize.migrate_live_caption_backend_settings), changing
    diarization_url afterward (e.g. from the Diarization panel) must not
    move what /live-caption/test for "realtime" probes."""
    before = admin_client.get("/api/settings").json()["settings"]["live_caption_realtime_url"]["value"]
    # Whatever this already-seeded value is (derived once from the
    # env-default diarization_url at boot -- see the migration), mock its
    # own /v1/models so the request the endpoint *should* make completes.
    seeded_models_url = before.split("/v1/")[0].rstrip("/") + "/v1/models"
    respx.get(seeded_models_url).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "whatever"}]})
    )

    admin_client.put(
        "/api/settings",
        json={"values": {"diarization_url": "http://new-diarizer.test/v1/audio/diarization"}},
    )
    new_diarizer_route = respx.get("http://new-diarizer.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "some-model"}]})
    )

    resp = admin_client.post("/api/live-caption/test", json={"backend": "realtime"})

    after = admin_client.get("/api/settings").json()["settings"]["live_caption_realtime_url"]["value"]
    assert resp.status_code == 200
    assert after == before
    assert not new_diarizer_route.called


def test_live_caption_test_endpoint_rejects_an_unknown_backend(admin_client):
    resp = admin_client.post("/api/live-caption/test", json={"backend": "bogus"})
    assert resp.status_code == 400


def test_live_caption_test_endpoint_is_admin_only(user_client):
    assert user_client.post("/api/live-caption/test", json={"backend": "realtime"}).status_code == 403


@respx.mock
def test_transcribe_test_endpoint_uses_the_saved_config(admin_client):
    respx.get(TRANSCRIBE_MODELS_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "whisper-large-turbo-q8_0"}]})
    )
    resp = admin_client.post("/api/transcribe/test")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@respx.mock
def test_transcribe_test_endpoint_can_try_unsaved_edits(admin_client):
    route = respx.get("http://other-transcriber.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "custom-model"}]})
    )
    resp = admin_client.post(
        "/api/transcribe/test",
        json={"url": "http://other-transcriber.test/v1/audio/transcriptions", "model": "custom-model"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert route.called


@respx.mock
def test_transcribe_test_endpoint_ignores_a_masked_api_key_echo(admin_client):
    admin_client.put("/api/settings", json={"values": {"transcribe_api_key": "sk-transcribe-real"}})
    route = respx.get(TRANSCRIBE_MODELS_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "whisper-large-turbo-q8_0"}]})
    )
    admin_client.post("/api/transcribe/test", json={"api_key": "••••1234"})

    assert route.calls[0].request.headers["authorization"] == "Bearer sk-transcribe-real"


def test_transcribe_test_endpoint_is_admin_only(user_client):
    assert user_client.post("/api/transcribe/test").status_code == 403


@respx.mock
def test_web_search_test_endpoint_uses_the_saved_config(admin_client):
    route = respx.post(WEB_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    resp = admin_client.post("/api/web-search/test")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-search-configured"


@respx.mock
def test_web_search_test_endpoint_can_try_unsaved_edits(admin_client):
    route = respx.post("https://other-search.test/v1/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    resp = admin_client.post(
        "/api/web-search/test", json={"base_url": "https://other-search.test"}
    )
    assert resp.status_code == 200
    assert route.called


@respx.mock
def test_web_search_test_endpoint_ignores_a_masked_api_key_echo(admin_client):
    """The form round-trips the masked placeholder; that must not become the
    literal API key sent to the server."""
    route = respx.post(WEB_SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    admin_client.post("/api/web-search/test", json={"api_key": "••••1234"})

    assert route.calls[0].request.headers["authorization"] == "Bearer sk-search-configured"


def test_web_search_test_endpoint_is_admin_only(user_client):
    assert user_client.post("/api/web-search/test").status_code == 403
