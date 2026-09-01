"""The transcription-only HTTP client ("Diarization only" mode).

Mirrors test_diarize.py's structure -- same request/response mechanics
against a different endpoint and response shape, and a separate error
class so a failure here doesn't get misreported as a diarization problem.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.errors import TranscribeError, TranscribeUnreachableError
from app.services import transcribe as transcribe_svc
from tests.conftest import FIXTURES

URL = "http://transcriber.test/v1/audio/transcriptions"

WHISPER_SAMPLE = {
    "task": "transcribe",
    "language": "english",
    "duration": 5.0,
    "text": " Hello there. How are you?",
    "segments": [
        {"id": 0, "start": 0.0, "end": 2.5, "text": " Hello there."},
        {"id": 1, "start": 2.5, "end": 5.0, "text": " How are you?"},
    ],
}

# The "live-stt" service's response: an OpenAI-Whisper-API verbose_json
# variant with word-level timestamps and no "segments" key at all. The two
# words after the gap ("How"/"are"/"you?" start at 2.5, 1.9s after "there."
# ends at 0.6) should land in a second reconstructed segment.
LIVE_STT_SAMPLE = {
    "task": "transcribe",
    "language": "english",
    "duration": 3.2,
    "text": "Hello there. How are you?",
    "words": [
        {"word": "Hello", "start": 0.0, "end": 0.3},
        {"word": "there.", "start": 0.3, "end": 0.6},
        {"word": "How", "start": 2.5, "end": 2.7},
        {"word": "are", "start": 2.7, "end": 2.9},
        {"word": "you?", "start": 2.9, "end": 3.2},
    ],
}


@pytest.fixture
def wav(tmp_path):
    path = tmp_path / "audio16k.wav"
    path.write_bytes((FIXTURES / "tiny16k.wav").read_bytes())
    return path


def _call(wav, **kw):
    return transcribe_svc.transcribe_sync(
        wav,
        url=kw.get("url", URL),
        model=kw.get("model", "whisper-large-turbo-q8_0"),
        api_key=kw.get("api_key"),
        timeout=kw.get("timeout", 30),
    )


class TestRequestShape:
    def test_form_has_no_include_text(self):
        # Unlike diarize.py's build_form: a plain transcription endpoint was
        # never asked to skip producing text, so the flag is meaningless here.
        form = transcribe_svc.build_form("whisper-large-turbo-q8_0")
        assert form == {"model": "whisper-large-turbo-q8_0", "response_format": "verbose_json"}

    @respx.mock
    def test_the_posted_body_contains_those_fields(self, wav):
        route = respx.post(URL).mock(return_value=httpx.Response(200, json=WHISPER_SAMPLE))
        _call(wav)

        body = route.calls[0].request.content.decode("utf-8", errors="replace")
        assert 'name="model"' in body
        assert "whisper-large-turbo-q8_0" in body
        assert 'name="response_format"' in body
        assert "verbose_json" in body
        assert 'name="include_text"' not in body
        assert 'name="file"' in body

    @respx.mock
    def test_no_auth_header_when_no_key_is_configured(self, wav):
        route = respx.post(URL).mock(return_value=httpx.Response(200, json=WHISPER_SAMPLE))
        _call(wav, api_key=None)
        assert "authorization" not in route.calls[0].request.headers

    @respx.mock
    def test_bearer_header_when_a_key_is_configured(self, wav):
        route = respx.post(URL).mock(return_value=httpx.Response(200, json=WHISPER_SAMPLE))
        _call(wav, api_key="sk-abc")
        assert route.calls[0].request.headers["authorization"] == "Bearer sk-abc"


class TestSuccess:
    @respx.mock
    def test_parses_the_real_response_shape(self, wav):
        respx.post(URL).mock(return_value=httpx.Response(200, json=WHISPER_SAMPLE))
        payload, elapsed_ms = _call(wav)

        assert payload == WHISPER_SAMPLE
        assert len(payload["segments"]) == 2
        assert elapsed_ms >= 0

    @respx.mock
    def test_returns_the_payload_unmodified(self, wav):
        respx.post(URL).mock(return_value=httpx.Response(200, json=WHISPER_SAMPLE))
        payload, _ = _call(wav)
        assert json.dumps(payload, sort_keys=True) == json.dumps(WHISPER_SAMPLE, sort_keys=True)

    @respx.mock
    def test_empty_segments_is_not_an_error(self, wav):
        """Unlike diarize_sync: a silent clip legitimately transcribes to
        nothing, and there is no include_text-style flag to have been
        dropped, so there is nothing to distinguish that from."""
        respx.post(URL).mock(
            return_value=httpx.Response(200, json={"segments": [], "text": "", "duration": 1.0})
        )
        payload, _ = _call(wav)
        assert payload["segments"] == []


class TestLiveSttWordShape:
    """live-stt returns word-level timestamps and no `segments` key at all
    (see the module docstring). transcribe_sync must normalize that into the
    same segment-level shape every other backend already returns, since
    that's the only granularity pipeline._combine_diarization_and_transcript
    reads."""

    @respx.mock
    def test_words_only_response_is_not_unexpected_shape(self, wav):
        respx.post(URL).mock(return_value=httpx.Response(200, json=LIVE_STT_SAMPLE))
        payload, _ = _call(wav)
        assert "segments" in payload

    @respx.mock
    def test_words_are_grouped_into_segments_at_the_time_gap(self, wav):
        respx.post(URL).mock(return_value=httpx.Response(200, json=LIVE_STT_SAMPLE))
        payload, _ = _call(wav)

        assert len(payload["segments"]) == 2
        first, second = payload["segments"]
        assert first["text"] == "Hello there."
        assert first["start"] == 0.0
        assert first["end"] == 0.6
        assert second["text"] == "How are you?"
        assert second["start"] == 2.5
        assert second["end"] == 3.2

    @respx.mock
    def test_original_words_and_text_are_preserved(self, wav):
        respx.post(URL).mock(return_value=httpx.Response(200, json=LIVE_STT_SAMPLE))
        payload, _ = _call(wav)
        assert payload["words"] == LIVE_STT_SAMPLE["words"]
        assert payload["text"] == LIVE_STT_SAMPLE["text"]

    @respx.mock
    def test_a_response_with_segments_already_is_never_touched(self, wav):
        """A backend that already returns segments must not be re-derived
        from `words`, even if it also happens to include one."""
        shaped = {**WHISPER_SAMPLE, "words": [{"word": "ignored", "start": 0.0, "end": 0.1}]}
        respx.post(URL).mock(return_value=httpx.Response(200, json=shaped))
        payload, _ = _call(wav)
        assert payload["segments"] == WHISPER_SAMPLE["segments"]

    def test_empty_words_list_yields_no_segments(self):
        assert transcribe_svc._segments_from_words([]) == []


class TestFailures:
    @respx.mock
    def test_connection_refused_is_a_config_error(self, wav):
        respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(TranscribeUnreachableError) as exc:
            _call(wav)
        assert exc.value.code == "TRANSCRIBE_UNREACHABLE"

    @respx.mock
    def test_timeout_suggests_the_knob_to_turn(self, wav):
        respx.post(URL).mock(side_effect=httpx.ReadTimeout("too slow"))
        with pytest.raises(TranscribeError, match="MMN_TRANSCRIBE_TIMEOUT_SEC"):
            _call(wav)

    @respx.mock
    def test_http_500_surfaces_the_body(self, wav):
        respx.post(URL).mock(
            return_value=httpx.Response(500, json={"error": {"message": "model not loaded"}})
        )
        with pytest.raises(TranscribeError, match="model not loaded"):
            _call(wav)

    @respx.mock
    def test_non_json_response(self, wav):
        respx.post(URL).mock(return_value=httpx.Response(200, text="<html>oops</html>"))
        with pytest.raises(TranscribeError, match="non-JSON"):
            _call(wav)

    @respx.mock
    def test_unexpected_shape(self, wav):
        respx.post(URL).mock(return_value=httpx.Response(200, json={"unexpected": True}))
        with pytest.raises(TranscribeError, match="Unexpected"):
            _call(wav)


class TestListModels:
    @respx.mock
    def test_lists_models_from_the_service_root(self):
        respx.get("http://transcriber.test/v1/models").mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "whisper-large-turbo-q8_0"}, {"id": "vibevoice-cpp-asr"}]},
            )
        )
        models = transcribe_svc.list_models(URL)
        assert [m["id"] for m in models] == ["whisper-large-turbo-q8_0", "vibevoice-cpp-asr"]

    @respx.mock
    def test_failure_is_wrapped(self):
        respx.get("http://transcriber.test/v1/models").mock(side_effect=httpx.ConnectError("nope"))
        with pytest.raises(TranscribeError):
            transcribe_svc.list_models(URL)
