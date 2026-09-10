"""Client for the real pyannote.ai cloud API (https://api.pyannote.ai).

Nothing like the "LocalAI-compatible" contract diarize.diarize_sync speaks:
that is one multipart POST of the audio file to a self-hosted URL, returning
segments synchronously. pyannote.ai is an async job-submission REST API over
a fixed hosted endpoint -- there is no equivalent of diarization_url/
diarization_model to configure, only an API key. Four calls per recording:

  1. POST /media/input   -- request a presigned PUT url for a scratch
                             ``media://`` object key.
  2. PUT <presigned url>  -- upload the audio bytes there directly
                             (application/octet-stream, not multipart).
  3. POST /diarize        -- submit the job against that media:// key, with
                             ``transcription: true`` so one job returns a
                             speaker-attributed transcript
                             (turnLevelTranscription) instead of pairing this
                             backend with a separate self-hosted transcribe_*
                             service the way "Diarization only" mode does for
                             the local backend.
  4. GET /jobs/{id}       -- poll until status is succeeded/failed/canceled.

Uploaded media (48h) and job results (24h) are both auto-deleted by
pyannote.ai, so nothing here needs to clean up after itself.

Do not confuse the "live_stt" name elsewhere in this app with pyannote.ai:
``diarize.LIVE_STT_MODELS``/``is_live_stt_model`` is Live Captions' unrelated
gRPC streaming backend, and ``config.RUNTIME_KEYS["diarization_backend"]``'s
own "live_stt" value means "the existing self-hosted HTTP backend", not this
module.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import httpx

from app.errors import DiarizationError, DiarizationUnreachableError

BASE_URL = "https://api.pyannote.ai/v1"

# How often to poll GET /jobs/{id} while a diarization job runs. Independent
# of diarize.HEARTBEAT_INTERVAL_SEC -- that one paces a synthesised progress
# bar for a synchronous call; this one paces real network requests against a
# job that has no progress endpoint of its own, so it stays coarser.
POLL_INTERVAL_SEC = 5.0

# The two `model` values pyannote.ai's own /diarize accepts. "precision-2"
# is their default when the field is omitted; "community-1" is the lighter,
# self-hostable model already covered by the "live_stt"/local backend via
# diarize_only, offered here too since choosing it against the *cloud* API
# still means "no self-hosted service to run", unlike running it locally.
DIARIZE_MODELS = ("precision-2", "community-1")

# The two `transcriptionConfig.model` values pyannote.ai's own STT
# orchestration accepts. "parakeet-tdt-0.6b-v3" (Nvidia) is their default
# when transcriptionConfig is omitted entirely; "faster-whisper-large-v3-turbo"
# is the other one they document.
TRANSCRIBE_MODELS = ("parakeet-tdt-0.6b-v3", "faster-whisper-large-v3-turbo")


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _require_api_key(api_key: str | None) -> str:
    if not api_key:
        raise DiarizationError(
            "pyannote.ai is selected as the diarization backend but no API key is set."
        )
    return api_key


def _upload(path: Path, api_key: str, object_key: str, timeout: int) -> None:
    """Steps 1-2: get a presigned upload URL, then PUT the file to it."""
    try:
        response = httpx.post(
            f"{BASE_URL}/media/input",
            json={"url": f"media://{object_key}"},
            headers={**_headers(api_key), "Content-Type": "application/json"},
            timeout=timeout,
        )
    except httpx.ConnectError as exc:
        raise DiarizationUnreachableError(
            f"Could not reach pyannote.ai at {BASE_URL}"
        ) from exc
    except httpx.HTTPError as exc:
        raise DiarizationError(f"pyannote.ai media request failed: {exc}") from exc

    if response.status_code >= 400:
        raise DiarizationError(
            f"pyannote.ai returned {response.status_code} requesting an upload slot: "
            f"{response.text[:400]}"
        )

    try:
        presigned_url = response.json()["url"]
    except (ValueError, KeyError, TypeError) as exc:
        raise DiarizationError(
            f"pyannote.ai media response missing an upload url: {response.text[:200]}"
        ) from exc

    with path.open("rb") as fh:
        try:
            put_response = httpx.put(
                presigned_url,
                content=fh,
                headers={"Content-Type": "application/octet-stream"},
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise DiarizationError(f"pyannote.ai upload failed: {exc}") from exc

    if put_response.status_code >= 400:
        raise DiarizationError(
            f"pyannote.ai upload returned {put_response.status_code}: "
            f"{put_response.text[:400]}"
        )


def _submit(
    object_key: str, api_key: str, timeout: int, *, model: str, transcribe_model: str
) -> str:
    """Step 3: submit the diarization job, transcription included.

    ``transcriptionConfig`` is sent explicitly rather than only when
    ``transcribe_model`` differs from pyannote.ai's own default: an explicit
    value is deterministic under a future change to what their default even
    is, and it means this deployment's Settings page always reflects what a
    job actually asked for.
    """
    try:
        response = httpx.post(
            f"{BASE_URL}/diarize",
            json={
                "url": f"media://{object_key}",
                "model": model,
                "transcription": True,
                "transcriptionConfig": {"model": transcribe_model},
            },
            headers={**_headers(api_key), "Content-Type": "application/json"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        raise DiarizationError(f"pyannote.ai diarize request failed: {exc}") from exc

    if response.status_code >= 400:
        raise DiarizationError(
            f"pyannote.ai returned {response.status_code} submitting the job: "
            f"{response.text[:400]}"
        )

    try:
        return response.json()["jobId"]
    except (ValueError, KeyError, TypeError) as exc:
        raise DiarizationError(
            f"pyannote.ai diarize response missing a jobId: {response.text[:200]}"
        ) from exc


def _poll(job_id: str, api_key: str, timeout: int) -> dict:
    """Step 4: poll until the job leaves the "running" state.

    ``timeout`` bounds the whole wait, same setting (diarization_timeout_sec)
    the local backend uses to bound its one synchronous POST -- reused rather
    than adding a second timeout setting, since it means the same thing to an
    operator either way: "how long am I willing to wait for one diarization".
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            response = httpx.get(
                f"{BASE_URL}/jobs/{job_id}", headers=_headers(api_key), timeout=30
            )
        except httpx.HTTPError as exc:
            raise DiarizationError(f"pyannote.ai job poll failed: {exc}") from exc

        if response.status_code >= 400:
            raise DiarizationError(
                f"pyannote.ai returned {response.status_code} polling job {job_id}: "
                f"{response.text[:400]}"
            )

        body = response.json()
        status = body.get("status")

        if status == "succeeded":
            output = body.get("output")
            if not isinstance(output, dict):
                raise DiarizationError(f"pyannote.ai job {job_id} succeeded with no output")
            return output

        if status in ("failed", "canceled"):
            raise DiarizationError(
                f"pyannote.ai job {job_id} {status}: {body.get('error') or body}"
            )

        if time.monotonic() > deadline:
            raise DiarizationError(
                f"pyannote.ai job {job_id} did not finish within {timeout}s. "
                f"Increase the diarization timeout for very long recordings."
            )

        time.sleep(POLL_INTERVAL_SEC)


def _to_app_payload(output: dict) -> dict:
    """Map pyannote.ai's own output onto this app's internal diarization
    schema (task/duration/num_speakers/speakers/segments -- see
    transcript.build_transcript) so nothing downstream needs a cloud-specific
    branch.

    ``turnLevelTranscription`` is the source, not ``wordLevelTranscription``
    or the plain ``diarization``/``exclusiveDiarization`` arrays: it is the
    one array that already carries both a speaker and transcribed text per
    turn, which is what a "segment" means everywhere else in this app.
    Tolerates both the camelCase field name the docs show and the snake_case
    one a real export from the pyannote.ai web UI has been observed to use.
    """
    turns = output.get("turnLevelTranscription")
    if turns is None:
        turns = output.get("turn_level_transcription") or []

    speech_seconds: dict[str, float] = {}
    turn_counts: dict[str, int] = {}
    max_end = 0.0
    for turn in turns:
        speaker = turn.get("speaker") or ""
        start = float(turn.get("start") or 0.0)
        end = float(turn.get("end") or 0.0)
        speech_seconds[speaker] = speech_seconds.get(speaker, 0.0) + max(0.0, end - start)
        turn_counts[speaker] = turn_counts.get(speaker, 0) + 1
        max_end = max(max_end, end)

    speakers = [
        {
            "id": speaker,
            "label": speaker,
            "total_speech_duration": round(duration, 3),
            "segment_count": turn_counts[speaker],
        }
        for speaker, duration in sorted(speech_seconds.items(), key=lambda kv: -kv[1])
    ]

    segments = [
        {
            "id": i,
            "speaker": turn.get("speaker") or "",
            "label": turn.get("speaker") or "",
            "start": turn.get("start"),
            "end": turn.get("end"),
            "text": turn.get("text") or "",
        }
        for i, turn in enumerate(turns)
    ]

    return {
        "task": "diarize",
        "duration": max_end,
        "num_speakers": len(speakers),
        "speakers": speakers,
        "segments": segments,
    }


def diarize_sync_cloud(
    path: Path,
    *,
    api_key: str | None,
    timeout: int,
    model: str = "precision-2",
    transcribe_model: str = "parakeet-tdt-0.6b-v3",
) -> tuple[dict, int]:
    """Blocking end-to-end pyannote.ai run: upload, submit, poll, map.

    Same ``(payload, elapsed_ms)`` return shape as diarize.diarize_sync, so
    diarize_file needs only a backend check, not a different call contract.
    Call via asyncio.to_thread -- this blocks on network I/O and on
    time.sleep between polls, same as diarize_sync already does.
    """
    key = _require_api_key(api_key)

    started = time.monotonic()
    # Unique per call, not per meeting: nothing downstream ever looks this
    # key up again -- pyannote.ai deletes the media itself, and the job's own
    # id (not this key) is what /jobs polls on.
    object_key = f"mmn-{uuid.uuid4().hex}"

    _upload(path, key, object_key, timeout)
    job_id = _submit(object_key, key, timeout, model=model, transcribe_model=transcribe_model)
    output = _poll(job_id, key, timeout)
    payload = _to_app_payload(output)

    if not payload["segments"]:
        raise DiarizationError("pyannote.ai returned no segments — is the audio silent?")

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return payload, elapsed_ms


def test_connection(api_key: str | None, timeout: int = 15) -> dict:
    """Confirm the API key actually authenticates, without spending a real
    diarization job on it (pyannote.ai bills a 20-second minimum per job).

    POST /media/input is the cheapest authenticated call available: it just
    reserves an upload slot and costs nothing, so this exercises real auth
    against the real host rather than a static shape check.
    """
    started = time.monotonic()
    if not api_key:
        return {
            "ok": False,
            "latency_ms": 0,
            "error": "No API key set.",
            "models_count": 0,
            "model_found": False,
        }

    object_key = f"mmn-test-{uuid.uuid4().hex}"
    try:
        response = httpx.post(
            f"{BASE_URL}/media/input",
            json={"url": f"media://{object_key}"},
            headers={**_headers(api_key), "Content-Type": "application/json"},
            timeout=timeout,
        )
    except httpx.ConnectError as exc:
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": f"Could not reach pyannote.ai at {BASE_URL}: {exc}",
            "models_count": 0,
            "model_found": False,
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": f"pyannote.ai request failed: {exc}",
            "models_count": 0,
            "model_found": False,
        }

    latency_ms = int((time.monotonic() - started) * 1000)

    if response.status_code == 401 or response.status_code == 403:
        return {
            "ok": False,
            "latency_ms": latency_ms,
            "error": "pyannote.ai rejected this API key.",
            "models_count": 0,
            "model_found": False,
        }
    if response.status_code >= 400:
        return {
            "ok": False,
            "latency_ms": latency_ms,
            "error": f"pyannote.ai returned {response.status_code}: {response.text[:400]}",
            "models_count": 0,
            "model_found": False,
        }

    return {
        "ok": True,
        "latency_ms": latency_ms,
        "error": None,
        # No model catalog on this backend -- mirrors test_live_stt_connection's
        # own shallow "there is exactly one thing to have found" convention.
        "models_count": 1,
        "model_found": True,
    }
