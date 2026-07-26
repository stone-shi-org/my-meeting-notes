"""The diarization HTTP client."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from app.errors import DiarizationError, DiarizationUnreachableError
from app.services import diarize as diarize_svc
from tests.conftest import FIXTURES

URL = "http://diarizer.test/v1/audio/diarization"


@pytest.fixture
def wav(tmp_path):
    path = tmp_path / "audio16k.wav"
    path.write_bytes((FIXTURES / "tiny16k.wav").read_bytes())
    return path


def _call(wav, **kw):
    return diarize_svc.diarize_sync(
        wav,
        url=kw.get("url", URL),
        model=kw.get("model", "vibevoice-cpp-asr"),
        api_key=kw.get("api_key"),
        timeout=kw.get("timeout", 30),
    )


class TestRequestShape:
    """These are the regression guards for the two load-bearing form fields."""

    def test_form_carries_include_text_and_verbose_json(self):
        form = diarize_svc.build_form("vibevoice-cpp-asr")
        assert form["model"] == "vibevoice-cpp-asr"
        # Without include_text the service returns turns with no words at all.
        assert form["include_text"] == "true"
        assert form["response_format"] == "verbose_json"

    @respx.mock
    def test_the_posted_body_contains_those_fields(self, wav, sample_diarization):
        route = respx.post(URL).mock(
            return_value=httpx.Response(200, json=sample_diarization)
        )
        _call(wav)

        body = route.calls[0].request.content.decode("utf-8", errors="replace")
        assert 'name="model"' in body
        assert "vibevoice-cpp-asr" in body
        assert 'name="include_text"' in body
        assert "true" in body
        assert 'name="response_format"' in body
        assert "verbose_json" in body
        assert 'name="file"' in body

    @respx.mock
    def test_no_auth_header_when_no_key_is_configured(self, wav, sample_diarization):
        route = respx.post(URL).mock(
            return_value=httpx.Response(200, json=sample_diarization)
        )
        _call(wav, api_key=None)
        assert "authorization" not in route.calls[0].request.headers

    @respx.mock
    def test_bearer_header_when_a_key_is_configured(self, wav, sample_diarization):
        route = respx.post(URL).mock(
            return_value=httpx.Response(200, json=sample_diarization)
        )
        _call(wav, api_key="sk-abc")
        assert route.calls[0].request.headers["authorization"] == "Bearer sk-abc"


class TestSuccess:
    @respx.mock
    def test_parses_the_real_response_shape(self, wav, sample_diarization):
        respx.post(URL).mock(return_value=httpx.Response(200, json=sample_diarization))
        payload, elapsed_ms = _call(wav)

        assert payload == sample_diarization
        assert payload["num_speakers"] == 2
        assert len(payload["segments"]) == 79
        assert elapsed_ms >= 0

    @respx.mock
    def test_returns_the_payload_unmodified(self, wav, sample_diarization):
        """Whatever we store must round-trip exactly; nothing is normalised here."""
        respx.post(URL).mock(return_value=httpx.Response(200, json=sample_diarization))
        payload, _ = _call(wav)
        assert json.dumps(payload, sort_keys=True) == json.dumps(
            sample_diarization, sort_keys=True
        )


class TestFailures:
    @respx.mock
    def test_connection_refused_is_a_config_error(self, wav):
        respx.post(URL).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(DiarizationUnreachableError) as exc:
            _call(wav)
        # The SPA deep-links to Diarization settings off this code.
        assert exc.value.code == "DIARIZATION_UNREACHABLE"

    @respx.mock
    def test_timeout_suggests_the_knob_to_turn(self, wav):
        respx.post(URL).mock(side_effect=httpx.ReadTimeout("too slow"))
        with pytest.raises(DiarizationError, match="MMN_DIARIZATION_TIMEOUT_SEC"):
            _call(wav)

    @respx.mock
    def test_http_500_surfaces_the_body(self, wav):
        respx.post(URL).mock(
            return_value=httpx.Response(500, json={"error": {"message": "model not loaded"}})
        )
        with pytest.raises(DiarizationError, match="model not loaded"):
            _call(wav)

    @respx.mock
    def test_non_json_response(self, wav):
        respx.post(URL).mock(return_value=httpx.Response(200, text="<html>oops</html>"))
        with pytest.raises(DiarizationError, match="non-JSON"):
            _call(wav)

    @respx.mock
    def test_unexpected_shape(self, wav):
        respx.post(URL).mock(return_value=httpx.Response(200, json={"unexpected": True}))
        with pytest.raises(DiarizationError, match="Unexpected"):
            _call(wav)

    @respx.mock
    def test_empty_segments(self, wav):
        respx.post(URL).mock(
            return_value=httpx.Response(200, json={"segments": [], "num_speakers": 0})
        )
        with pytest.raises(DiarizationError, match="no segments"):
            _call(wav)

    @respx.mock
    def test_segments_with_no_text_name_the_actual_cause(self, wav):
        """The exact symptom of a dropped include_text=true."""
        respx.post(URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "segments": [
                        {"id": 0, "speaker": "SPEAKER_00", "start": 0, "end": 1, "text": ""},
                        {"id": 1, "speaker": "SPEAKER_01", "start": 1, "end": 2, "text": "  "},
                    ],
                    "num_speakers": 2,
                },
            )
        )
        with pytest.raises(DiarizationError, match="include_text"):
            _call(wav)

    @respx.mock
    def test_one_empty_segment_among_real_ones_is_fine(self, wav, sample_diarization):
        payload = json.loads(json.dumps(sample_diarization))
        payload["segments"][0]["text"] = ""
        respx.post(URL).mock(return_value=httpx.Response(200, json=payload))
        result, _ = _call(wav)
        assert len(result["segments"]) == 79


class TestListModels:
    @respx.mock
    def test_lists_models_from_the_service_root(self):
        respx.get("http://diarizer.test/v1/models").mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "vibevoice-cpp-asr"}, {"id": "whisper-large-turbo-q8_0"}]},
            )
        )
        models = diarize_svc.list_models(URL)
        assert [m["id"] for m in models] == ["vibevoice-cpp-asr", "whisper-large-turbo-q8_0"]

    @respx.mock
    def test_failure_is_wrapped(self):
        respx.get("http://diarizer.test/v1/models").mock(
            side_effect=httpx.ConnectError("nope")
        )
        with pytest.raises(DiarizationError):
            diarize_svc.list_models(URL)
