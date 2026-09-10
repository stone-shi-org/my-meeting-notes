"""The real pyannote.ai cloud diarization backend.

Unlike diarize_sync (one multipart POST), this is upload -> submit -> poll --
each step gets its own respx route.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.errors import DiarizationError, DiarizationUnreachableError
from app.services import pyannote_cloud

MEDIA_URL = "https://api.pyannote.ai/v1/media/input"
DIARIZE_URL = "https://api.pyannote.ai/v1/diarize"
PRESIGNED_URL = "https://storage.pyannote.ai/upload/abc123"


@pytest.fixture
def wav(tmp_path):
    path = tmp_path / "audio16k.wav"
    path.write_bytes(b"RIFF....WAVEfmt ")
    return path


def jobs_url(job_id: str) -> str:
    return f"https://api.pyannote.ai/v1/jobs/{job_id}"


class TestToAppPayload:
    def test_maps_turn_level_transcription_camel_case(self):
        output = {
            "turnLevelTranscription": [
                {"start": 0.0, "end": 2.0, "speaker": "SPEAKER_00", "text": "hello"},
                {"start": 2.0, "end": 5.0, "speaker": "SPEAKER_01", "text": "hi there"},
                {"start": 5.0, "end": 6.5, "speaker": "SPEAKER_00", "text": "bye"},
            ]
        }
        payload = pyannote_cloud._to_app_payload(output)

        assert payload["task"] == "diarize"
        assert payload["num_speakers"] == 2
        assert payload["duration"] == 6.5
        assert len(payload["segments"]) == 3
        assert payload["segments"][0] == {
            "id": 0, "speaker": "SPEAKER_00", "label": "SPEAKER_00",
            "start": 0.0, "end": 2.0, "text": "hello",
        }
        speakers = {s["id"]: s for s in payload["speakers"]}
        assert speakers["SPEAKER_00"]["segment_count"] == 2
        assert speakers["SPEAKER_00"]["total_speech_duration"] == pytest.approx(3.5)
        assert speakers["SPEAKER_01"]["segment_count"] == 1

    def test_tolerates_the_snake_case_variant_seen_from_a_manual_web_ui_export(self):
        """A real export downloaded from pyannote.ai's own website used
        turn_level_transcription, not turnLevelTranscription -- both must
        work rather than silently producing zero segments."""
        output = {
            "turn_level_transcription": [
                {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00", "text": "hi"},
            ]
        }
        payload = pyannote_cloud._to_app_payload(output)
        assert len(payload["segments"]) == 1
        assert payload["segments"][0]["text"] == "hi"

    def test_no_turns_at_all_yields_empty_segments_not_an_error(self):
        """_to_app_payload itself is pure mapping; the "no segments" check
        that turns this into a DiarizationError lives in diarize_sync_cloud."""
        payload = pyannote_cloud._to_app_payload({})
        assert payload["segments"] == []
        assert payload["num_speakers"] == 0


class TestDiarizeSyncCloud:
    @respx.mock
    def test_happy_path_uploads_submits_and_polls_to_completion(self, wav):
        media_route = respx.post(MEDIA_URL).mock(
            return_value=httpx.Response(200, json={"url": PRESIGNED_URL})
        )
        upload_route = respx.put(PRESIGNED_URL).mock(return_value=httpx.Response(200))
        diarize_route = respx.post(DIARIZE_URL).mock(
            return_value=httpx.Response(200, json={"jobId": "job-1", "status": "created"})
        )
        # Succeeds on the very first poll -- a "running" -> "succeeded"
        # sequence would exercise the real POLL_INTERVAL_SEC sleep between
        # polls, which has no reason to slow this test down; see
        # test_polls_again_when_the_job_is_still_running for that path with
        # the interval patched to 0.
        respx.get(jobs_url("job-1")).mock(
            return_value=httpx.Response(
                200,
                json={
                    "status": "succeeded",
                    "output": {
                        "turnLevelTranscription": [
                            {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00", "text": "hi"},
                        ]
                    },
                },
            )
        )

        payload, elapsed_ms = pyannote_cloud.diarize_sync_cloud(
            wav, api_key="pyk-test", timeout=30
        )

        assert len(payload["segments"]) == 1
        assert elapsed_ms >= 0

        # The upload is a raw PUT with the file's own bytes, not multipart.
        assert upload_route.calls[0].request.headers["content-type"] == "application/octet-stream"
        # The job is submitted with transcription on -- that's what makes this
        # backend a one-call replacement for diarize_only + a separate
        # transcribe_* service, not just a bare diarization.
        import json

        body = json.loads(diarize_route.calls[0].request.content)
        assert body["transcription"] is True
        assert body["url"].startswith("media://")
        assert media_route.calls[0].request.headers["authorization"] == "Bearer pyk-test"

    @respx.mock
    def test_polls_again_when_the_job_is_still_running(self, wav, monkeypatch):
        monkeypatch.setattr(pyannote_cloud, "POLL_INTERVAL_SEC", 0)
        respx.post(MEDIA_URL).mock(return_value=httpx.Response(200, json={"url": PRESIGNED_URL}))
        respx.put(PRESIGNED_URL).mock(return_value=httpx.Response(200))
        respx.post(DIARIZE_URL).mock(
            return_value=httpx.Response(200, json={"jobId": "job-poll", "status": "created"})
        )
        poll_route = respx.get(jobs_url("job-poll")).mock(
            side_effect=[
                httpx.Response(200, json={"status": "running"}),
                httpx.Response(200, json={"status": "running"}),
                httpx.Response(
                    200,
                    json={
                        "status": "succeeded",
                        "output": {
                            "turnLevelTranscription": [
                                {"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00", "text": "hi"},
                            ]
                        },
                    },
                ),
            ]
        )

        payload, _ = pyannote_cloud.diarize_sync_cloud(wav, api_key="pyk-test", timeout=30)
        assert len(payload["segments"]) == 1
        assert poll_route.call_count == 3

    def test_missing_api_key_raises_before_any_request(self, wav):
        with pytest.raises(DiarizationError, match="no API key"):
            pyannote_cloud.diarize_sync_cloud(wav, api_key=None, timeout=30)

    @respx.mock
    def test_connect_error_on_upload_is_reported_as_unreachable(self, wav):
        respx.post(MEDIA_URL).mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(DiarizationUnreachableError):
            pyannote_cloud.diarize_sync_cloud(wav, api_key="pyk-test", timeout=30)

    @respx.mock
    def test_failed_job_status_raises(self, wav):
        respx.post(MEDIA_URL).mock(return_value=httpx.Response(200, json={"url": PRESIGNED_URL}))
        respx.put(PRESIGNED_URL).mock(return_value=httpx.Response(200))
        respx.post(DIARIZE_URL).mock(
            return_value=httpx.Response(200, json={"jobId": "job-2", "status": "created"})
        )
        respx.get(jobs_url("job-2")).mock(
            return_value=httpx.Response(200, json={"status": "failed", "error": "boom"})
        )

        with pytest.raises(DiarizationError, match="failed"):
            pyannote_cloud.diarize_sync_cloud(wav, api_key="pyk-test", timeout=30)

    @respx.mock
    def test_no_segments_is_reported_as_diarization_error(self, wav):
        """An empty turnLevelTranscription (e.g. silent audio) must not
        silently store an empty transcript -- same rule diarize_sync
        already enforces for the local backend."""
        respx.post(MEDIA_URL).mock(return_value=httpx.Response(200, json={"url": PRESIGNED_URL}))
        respx.put(PRESIGNED_URL).mock(return_value=httpx.Response(200))
        respx.post(DIARIZE_URL).mock(
            return_value=httpx.Response(200, json={"jobId": "job-3", "status": "created"})
        )
        respx.get(jobs_url("job-3")).mock(
            return_value=httpx.Response(
                200, json={"status": "succeeded", "output": {"turnLevelTranscription": []}}
            )
        )

        with pytest.raises(DiarizationError, match="no segments"):
            pyannote_cloud.diarize_sync_cloud(wav, api_key="pyk-test", timeout=30)


class TestConnection:
    @respx.mock
    def test_ok_when_the_key_authenticates(self):
        respx.post(MEDIA_URL).mock(return_value=httpx.Response(200, json={"url": PRESIGNED_URL}))
        result = pyannote_cloud.test_connection("pyk-test", timeout=15)
        assert result["ok"] is True
        assert result["error"] is None

    @respx.mock
    def test_rejected_key_is_reported_not_raised(self):
        respx.post(MEDIA_URL).mock(return_value=httpx.Response(401, text="unauthorized"))
        result = pyannote_cloud.test_connection("pyk-bad", timeout=15)
        assert result["ok"] is False
        assert "rejected" in result["error"]

    def test_no_key_at_all_short_circuits_without_a_request(self):
        result = pyannote_cloud.test_connection(None, timeout=15)
        assert result["ok"] is False
        assert result["error"]

    @respx.mock
    def test_does_not_submit_a_real_diarization_job(self):
        """A real job bills a 20-second minimum -- the test button must not
        pay for one."""
        media_route = respx.post(MEDIA_URL).mock(
            return_value=httpx.Response(200, json={"url": PRESIGNED_URL})
        )
        diarize_route = respx.post(DIARIZE_URL).mock(
            return_value=httpx.Response(200, json={"jobId": "job-x"})
        )
        pyannote_cloud.test_connection("pyk-test", timeout=15)
        assert media_route.called
        assert not diarize_route.called
