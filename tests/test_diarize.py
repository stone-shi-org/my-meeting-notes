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

    @respx.mock
    def test_embedded_turns_dump_names_the_actual_cause(self, wav):
        """Reproduces what meeting 24 (a ~59 minute recording) actually got
        back from vibevoice-cpp-asr: one degenerate segment whose "text" is
        the model's own truncated JSON dump of per-turn speech, instead of
        real top-level segments. That segment has non-empty text and
        num_speakers is plausible, so without this check it passed every
        existing validation and silently became the "transcript"."""
        respx.post(URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "task": "diarize",
                    "num_speakers": 1,
                    "segments": [
                        {
                            "id": 0,
                            "speaker": "SPEAKER_00",
                            "label": "0",
                            "start": 0,
                            "end": 0,
                            # Truncated mid-object, same as the real payload.
                            "text": (
                                '[{"Start":0,"End":1.0,"Speaker":0,"Content":"[Silence]"},'
                                '{"Start":1.0,"End":123.0,"Speaker":1,"Content":"Hello.'
                            ),
                        }
                    ],
                    "speakers": [
                        {"id": "SPEAKER_00", "label": "0", "total_speech_duration": 0,
                         "segment_count": 1}
                    ],
                },
            )
        )
        with pytest.raises(DiarizationError, match="embedded"):
            _call(wav)

    def test_looks_like_embedded_turns_dump_ignores_real_speech(self):
        # A meeting about JSON syntax shouldn't trip this just for saying
        # the words -- only a segment that *opens* with the dump's exact
        # shape counts.
        assert not diarize_svc.looks_like_embedded_turns_dump(
            "So the payload looks like [{\"Start\": 1} and then we parse it."
        )
        assert not diarize_svc.looks_like_embedded_turns_dump("Hello, how are you?")
        assert not diarize_svc.looks_like_embedded_turns_dump("")
        assert not diarize_svc.looks_like_embedded_turns_dump(None)

    def test_looks_like_embedded_turns_dump_matches_the_real_shape(self):
        assert diarize_svc.looks_like_embedded_turns_dump(
            '[{"Start":0,"End":1.0,"Speaker":0,"Content":"[Silence]"}'
        )
        # Case-insensitive on the key, and tolerant of leading whitespace.
        assert diarize_svc.looks_like_embedded_turns_dump(
            '  [{"start": 0, "end": 1.0}'
        )


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


class TestTranscriptionsUrl:
    def test_swaps_diarization_for_transcriptions_on_the_same_host(self):
        assert (
            diarize_svc.transcriptions_url("http://x.test:4012/v1/audio/diarization")
            == "http://x.test:4012/v1/audio/transcriptions"
        )

    def test_leaves_an_unrecognised_shape_alone(self):
        """A test double or a future path -- do not guess at a rewrite that
        might be wrong."""
        odd = "http://x.test/some/other/path"
        assert diarize_svc.transcriptions_url(odd) == odd


class TestRealtimeUrl:
    """Builds the ws(s):// URL live captions hold a persistent session
    against -- see routers/live_caption.py's module docstring for why this
    is a different route from transcriptions_url above."""

    def test_swaps_diarization_for_realtime_and_http_for_ws(self):
        assert (
            diarize_svc.realtime_url("http://x.test:4012/v1/audio/diarization")
            == "ws://x.test:4012/v1/realtime"
        )

    def test_swaps_https_for_wss(self):
        assert (
            diarize_svc.realtime_url("https://x.test:4012/v1/audio/diarization")
            == "wss://x.test:4012/v1/realtime"
        )

    def test_leaves_an_unrecognised_shape_alone(self):
        """Same reasoning as transcriptions_url's own version of this test:
        a test double or a future path should not get guessed at."""
        odd = "http://x.test/some/other/path"
        assert diarize_svc.realtime_url(odd) == odd


class TestStripLanguageTag:
    """The parakeet-cpp-nemotron-3.5-asr-streaming-0.6b quirk: a stray
    "<en-US>"-style tag appended to the model's own transcribed text."""

    def test_strips_a_trailing_language_tag(self):
        text = "And once they have these tools, they play with it without go back and forth. <en-US>"
        assert diarize_svc.strip_language_tag(text) == (
            "And once they have these tools, they play with it without go back and forth."
        )

    def test_strips_without_a_leading_space(self):
        assert diarize_svc.strip_language_tag("Hello there<en-US>") == "Hello there"

    def test_leaves_ordinary_text_alone(self):
        assert diarize_svc.strip_language_tag("Let's sync tomorrow.") == "Let's sync tomorrow."

    def test_leaves_a_real_bracketed_non_speech_marker_alone(self):
        # Different problem, different shape -- see transcript.is_non_speech.
        # Uppercase content and no dash-region shape, so this must not match.
        assert diarize_svc.strip_language_tag("[Music]") == "[Music]"

    def test_none_and_empty_are_safe(self):
        assert diarize_svc.strip_language_tag("") == ""
        assert diarize_svc.strip_language_tag(None) == ""

    def test_does_not_eat_an_unrelated_bracketed_aside(self):
        # Uppercase "DIV" (or any non-language-code-shaped content) must
        # survive -- only the specific lowercase-language/uppercase-region
        # BCP-47 shape is stripped.
        assert diarize_svc.strip_language_tag("check the <DIV> tag") == "check the <DIV> tag"
