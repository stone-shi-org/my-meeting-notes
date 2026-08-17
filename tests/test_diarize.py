"""The diarization HTTP client."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.db import utcnow
from app.errors import DiarizationError, DiarizationUnreachableError
from app.jobs.queue import JobContext
from app.services import diarize as diarize_svc
from tests.conftest import FIXTURES

URL = "http://diarizer.test/v1/audio/diarization"
TRANSCRIBE_URL = "http://diarizer.test/v1/audio/transcriptions"


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


class TestTranscribeSync:
    """The plain-ASR client used for a channel already known to be one speaker."""

    def _call(self, wav, **kw):
        return diarize_svc.transcribe_sync(
            wav,
            url=kw.get("url", TRANSCRIBE_URL),
            model=kw.get("model", "vibevoice-cpp-asr"),
            api_key=kw.get("api_key"),
            timeout=kw.get("timeout", 30),
        )

    @respx.mock
    def test_posts_model_and_verbose_json_but_no_diarization_flags(self, wav):
        route = respx.post(TRANSCRIBE_URL).mock(
            return_value=httpx.Response(
                200, json={"text": "hi", "duration": 1.0, "segments": [{"id": 0, "text": "hi"}]}
            )
        )
        self._call(wav)
        body = route.calls[0].request.content.decode("utf-8", errors="replace")
        assert 'name="model"' in body
        assert 'name="response_format"' in body
        # No include_text/diarization flags -- there is nothing to diarize.
        assert "include_text" not in body

    @respx.mock
    def test_parses_the_plain_asr_shape(self, wav):
        respx.post(TRANSCRIBE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "text": "hello there",
                    "duration": 2.0,
                    "segments": [{"id": 0, "start": 0, "end": 2, "text": "hello there"}],
                },
            )
        )
        payload, elapsed_ms = self._call(wav)
        assert payload["segments"][0]["text"] == "hello there"
        assert elapsed_ms >= 0

    @respx.mock
    def test_connection_refused_is_a_config_error(self, wav):
        respx.post(TRANSCRIBE_URL).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(DiarizationUnreachableError):
            self._call(wav)

    @respx.mock
    def test_empty_segments_is_an_error(self, wav):
        respx.post(TRANSCRIBE_URL).mock(
            return_value=httpx.Response(200, json={"text": "", "segments": []})
        )
        with pytest.raises(DiarizationError, match="no segments"):
            self._call(wav)


class TestChannelMergeHelpers:
    def test_label_segments_stamps_the_known_speaker(self):
        labeled = diarize_svc._label_segments(
            [{"id": 0, "start": 0, "end": 1, "text": "hi"}], "ME"
        )
        assert labeled == [
            {"id": 0, "speaker": "ME", "label": "ME", "start": 0, "end": 1, "text": "hi"}
        ]

    def test_single_speaker_row_sums_duration(self):
        segments = diarize_svc._label_segments(
            [
                {"id": 0, "start": 0, "end": 1.5, "text": "a"},
                {"id": 1, "start": 2, "end": 3, "text": "b"},
            ],
            "ME",
        )
        row = diarize_svc._single_speaker_row("ME", segments)
        assert row == {
            "id": "ME",
            "label": "ME",
            "total_speech_duration": pytest.approx(2.5),
            "segment_count": 2,
        }

    def test_prefix_room_speakers_namespaces_ids(self):
        payload = {
            "segments": [
                {"id": 0, "speaker": "SPEAKER_00", "label": "0", "start": 0, "end": 1, "text": "a"},
                {"id": 1, "speaker": "SPEAKER_01", "label": "1", "start": 1, "end": 2, "text": "b"},
            ],
            "speakers": [
                {"id": "SPEAKER_00", "label": "0", "total_speech_duration": 1, "segment_count": 1},
                {"id": "SPEAKER_01", "label": "1", "total_speech_duration": 1, "segment_count": 1},
            ],
        }
        segments, speakers = diarize_svc._prefix_room_speakers(payload)
        assert [s["speaker"] for s in segments] == ["ROOM_SPEAKER_00", "ROOM_SPEAKER_01"]
        assert [s["id"] for s in speakers] == ["ROOM_SPEAKER_00", "ROOM_SPEAKER_01"]
        # The model's own label/duration/count survive the rename.
        assert speakers[0]["total_speech_duration"] == 1

    def test_merge_interleaves_by_start_time_and_renumbers_ids(self):
        me = diarize_svc._label_segments(
            [{"id": 0, "start": 2, "end": 3, "text": "me-second"}], "ME"
        )
        room = diarize_svc._label_segments(
            [{"id": 0, "start": 0, "end": 1, "text": "room-first"}], "ROOM"
        )
        merged = diarize_svc._merge_channel_segments(me, room)
        assert [s["text"] for s in merged] == ["room-first", "me-second"]
        assert [s["id"] for s in merged] == [0, 1]

    def test_prefix_speaker_ids_takes_an_arbitrary_prefix(self):
        payload = {
            "segments": [
                {"id": 0, "speaker": "SPEAKER_00", "label": "0", "start": 0, "end": 1, "text": "a"},
            ],
            "speakers": [
                {"id": "SPEAKER_00", "label": "0", "total_speech_duration": 1, "segment_count": 1},
            ],
        }
        segments, speakers = diarize_svc._prefix_speaker_ids(payload, "Alice_")
        assert [s["speaker"] for s in segments] == ["Alice_SPEAKER_00"]
        assert [s["id"] for s in speakers] == ["Alice_SPEAKER_00"]

    def test_merge_many_segments_interleaves_n_lists(self):
        a = diarize_svc._label_segments([{"id": 0, "start": 2, "end": 3, "text": "third"}], "A")
        b = diarize_svc._label_segments([{"id": 0, "start": 0, "end": 1, "text": "first"}], "B")
        c = diarize_svc._label_segments([{"id": 0, "start": 1, "end": 2, "text": "second"}], "C")
        merged = diarize_svc._merge_many_segments(a, b, c)
        assert [s["text"] for s in merged] == ["first", "second", "third"]
        assert [s["id"] for s in merged] == [0, 1, 2]


class TestTranscribeFlatFile:
    """The 'skip diarization' path: plain ASR wrapped as one speaker."""

    def _ctx(self, db_path) -> JobContext:
        return JobContext("fake-job", "diarize", {}, db_path=db_path)

    def _configure(self, conn):
        for key, value in [
            ("diarization_url", URL),
            ("diarization_api_key", ""),
            ("diarization_timeout_sec", "30"),
        ]:
            conn.execute(
                "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) "
                "VALUES (?, ?, 'str', 0, ?)",
                (key, value, utcnow()),
            )
        conn.commit()

    @respx.mock
    @pytest.mark.asyncio
    async def test_calls_transcribe_not_diarize(self, conn, initialised_db, tmp_path):
        self._configure(conn)
        transcribe_route = respx.post(TRANSCRIBE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "text": "one flat transcript",
                    "segments": [
                        {"id": 0, "start": 0, "end": 1, "text": "hello"},
                        {"id": 1, "start": 1, "end": 2, "text": "world"},
                    ],
                },
            )
        )
        # The batch diarization endpoint must never be called here -- that's
        # the entire point of skipping diarization.
        diarize_route = respx.post(URL).mock(
            return_value=httpx.Response(200, json={"segments": [], "num_speakers": 0})
        )

        path = tmp_path / "audio16k.wav"
        path.write_bytes(b"\x00")

        payload, _ = await diarize_svc.transcribe_flat_file(
            self._ctx(initialised_db),
            path,
            model="vibevoice-cpp-asr",
            duration_sec=5.0,
        )

        assert diarize_route.call_count == 0
        assert transcribe_route.call_count == 1
        assert payload["num_speakers"] == 1
        assert [s["id"] for s in payload["speakers"]] == ["SPEAKER"]
        assert [s["speaker"] for s in payload["segments"]] == ["SPEAKER", "SPEAKER"]
        assert [s["text"] for s in payload["segments"]] == ["hello", "world"]


class TestDiarizeChannelsFile:
    """Integration of the pieces above through the async orchestration."""

    def _ctx(self, db_path) -> JobContext:
        # No real job row needed: diarize_channels_file only calls
        # ctx.heartbeat() (an UPDATE affecting zero rows for an unknown id,
        # not an error) and ctx.stage_progress() (a no-op until ctx.stage()
        # has been called, which this path never does).
        return JobContext("fake-job", "diarize", {}, db_path=db_path)

    def _configure(self, conn):
        for key, value in [
            ("diarization_url", URL),
            ("diarization_api_key", ""),
            ("diarization_timeout_sec", "30"),
        ]:
            conn.execute(
                "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) "
                "VALUES (?, ?, 'str', 0, ?)",
                (key, value, utcnow()),
            )
        conn.commit()

    @respx.mock
    @pytest.mark.asyncio
    async def test_single_room_speaker_skips_the_model_diarizer(self, conn, initialised_db, tmp_path):
        self._configure(conn)
        me_route = respx.post(TRANSCRIBE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "text": "my question",
                    "segments": [{"id": 0, "start": 0, "end": 1, "text": "my question"}],
                },
            )
        )
        # The batch diarization endpoint must never be called in 'single' mode.
        diarize_route = respx.post(URL).mock(
            return_value=httpx.Response(200, json={"segments": [], "num_speakers": 0})
        )

        me_wav = tmp_path / "me16k.wav"
        room_wav = tmp_path / "room16k.wav"
        me_wav.write_bytes(b"\x00")
        room_wav.write_bytes(b"\x00")

        payload, _ = await diarize_svc.diarize_channels_file(
            self._ctx(initialised_db),
            room_wav=room_wav,
            me_wav=me_wav,
            room_speakers="single",
            model="vibevoice-cpp-asr",
            duration_sec=5.0,
        )

        assert diarize_route.call_count == 0
        assert me_route.call_count == 2  # once per channel in 'single' mode
        speaker_ids = {s["id"] for s in payload["speakers"]}
        assert speaker_ids == {"ME", "ROOM"}
        assert payload["num_speakers"] == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_multiple_room_speakers_calls_the_model_diarizer_for_the_room_only(
        self, conn, initialised_db, tmp_path
    ):
        self._configure(conn)
        respx.post(TRANSCRIBE_URL).mock(
            return_value=httpx.Response(
                200, json={"text": "hi", "segments": [{"id": 0, "start": 0, "end": 1, "text": "hi"}]}
            )
        )
        diarize_route = respx.post(URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "segments": [
                        {"id": 0, "speaker": "SPEAKER_00", "start": 0, "end": 1, "text": "a"},
                        {"id": 1, "speaker": "SPEAKER_01", "start": 1, "end": 2, "text": "b"},
                    ],
                    "num_speakers": 2,
                    "speakers": [
                        {"id": "SPEAKER_00", "total_speech_duration": 1, "segment_count": 1},
                        {"id": "SPEAKER_01", "total_speech_duration": 1, "segment_count": 1},
                    ],
                },
            )
        )

        me_wav = tmp_path / "me16k.wav"
        room_wav = tmp_path / "room16k.wav"
        me_wav.write_bytes(b"\x00")
        room_wav.write_bytes(b"\x00")

        payload, _ = await diarize_svc.diarize_channels_file(
            self._ctx(initialised_db),
            room_wav=room_wav,
            me_wav=me_wav,
            room_speakers="multiple",
            model="vibevoice-cpp-asr",
            duration_sec=5.0,
        )

        assert diarize_route.call_count == 1
        speaker_ids = {s["id"] for s in payload["speakers"]}
        assert speaker_ids == {"ME", "ROOM_SPEAKER_00", "ROOM_SPEAKER_01"}
        assert payload["num_speakers"] == 3


class TestDiarizeMultiChannelFile:
    """N-channel generalization: a mix of known (flat) and mixed (diarized) channels."""

    def _ctx(self, db_path) -> JobContext:
        return JobContext("fake-job", "diarize", {}, db_path=db_path)

    def _configure(self, conn):
        for key, value in [
            ("diarization_url", URL),
            ("diarization_api_key", ""),
            ("diarization_timeout_sec", "30"),
        ]:
            conn.execute(
                "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) "
                "VALUES (?, ?, 'str', 0, ?)",
                (key, value, utcnow()),
            )
        conn.commit()

    @respx.mock
    @pytest.mark.asyncio
    async def test_three_channels_two_known_one_mixed(self, conn, initialised_db, tmp_path):
        self._configure(conn)
        respx.post(TRANSCRIBE_URL).mock(
            return_value=httpx.Response(
                200,
                json={"text": "hi", "segments": [{"id": 0, "start": 0, "end": 1, "text": "hi"}]},
            )
        )
        diarize_route = respx.post(URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "segments": [
                        {"id": 0, "speaker": "SPEAKER_00", "start": 0, "end": 1, "text": "a"},
                        {"id": 1, "speaker": "SPEAKER_01", "start": 1, "end": 2, "text": "b"},
                    ],
                    "num_speakers": 2,
                    "speakers": [
                        {"id": "SPEAKER_00", "total_speech_duration": 1, "segment_count": 1},
                        {"id": "SPEAKER_01", "total_speech_duration": 1, "segment_count": 1},
                    ],
                },
            )
        )

        wavs = [tmp_path / f"ch{i}.wav" for i in range(3)]
        for wav in wavs:
            wav.write_bytes(b"\x00")

        meta = [
            diarize_svc.ChannelMeta(label="Alice", run_diarization=False),
            diarize_svc.ChannelMeta(label="Bob", run_diarization=False),
            diarize_svc.ChannelMeta(label=None, run_diarization=True),
        ]

        payload, _ = await diarize_svc.diarize_multi_channel_file(
            self._ctx(initialised_db),
            channel_wavs=wavs,
            channel_meta=meta,
            model="vibevoice-cpp-asr",
            duration_sec=5.0,
        )

        assert diarize_route.call_count == 1  # only the mixed channel
        speaker_ids = {s["id"] for s in payload["speakers"]}
        assert speaker_ids == {"Alice", "Bob", "S2_SPEAKER_00", "S2_SPEAKER_01"}
        assert payload["num_speakers"] == 4

    @pytest.mark.asyncio
    async def test_mismatched_lengths_raise(self, initialised_db):
        with pytest.raises(ValueError):
            await diarize_svc.diarize_multi_channel_file(
                self._ctx(initialised_db),
                channel_wavs=[Path("a.wav")],
                channel_meta=[],
                model="vibevoice-cpp-asr",
            )
