"""The diarization service client.

POSTs multipart to a LocalAI-compatible ``/v1/audio/diarization``. The two
non-obvious fields are load-bearing:

  include_text=true        without it the service returns speaker turns with
                           no words in them at all
  response_format=verbose_json   gives the segments/speakers structure rather
                           than a flat transcript

The request runs for minutes on a long recording and the service reports no
progress, so a heartbeat task synthesises one from the audio duration.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx

from app.config import effective, get_settings
from app.db import get_conn
from app.errors import DiarizationError, DiarizationUnreachableError
from app.logging_config import get_logger

log = get_logger("diarize")

# Never let the synthesised bar hit 100% -- a full bar that then keeps waiting
# is worse than an honest one that stalls at 95%.
PROGRESS_CEILING = 0.95
HEARTBEAT_INTERVAL_SEC = 5.0


def build_form(model: str) -> dict[str, str]:
    return {
        "model": model,
        "include_text": "true",
        "response_format": "verbose_json",
    }


def _headers(api_key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def transcriptions_url(diarization_url: str) -> str:
    """Swap .../v1/audio/diarization for .../v1/audio/transcriptions on the
    same host.

    No separate setting for this: it is the same LocalAI instance and the
    same model, just a different route -- one URL to keep in sync rather than
    two. Used for plain ASR (no diarization) on a channel already known to
    hold exactly one speaker -- see transcribe_sync -- and by the live
    caption relay's streaming calls.
    """
    suffix = "/v1/audio/diarization"
    if diarization_url.endswith(suffix):
        return diarization_url[: -len(suffix)] + "/v1/audio/transcriptions"
    # Not the shape we expected (a test double, a future path) -- leave it
    # alone rather than guessing at a rewrite that might be wrong.
    return diarization_url


def diarize_sync(
    path: Path,
    *,
    url: str,
    model: str,
    api_key: str | None,
    timeout: int,
) -> tuple[dict, int]:
    """Blocking POST. Returns ``(payload, elapsed_ms)``."""
    started = time.monotonic()
    try:
        with path.open("rb") as fh:
            files = {"file": (path.name, fh, "audio/wav")}
            response = httpx.post(
                url,
                files=files,
                data=build_form(model),
                headers=_headers(api_key),
                timeout=timeout,
            )
    except httpx.ConnectError as exc:
        raise DiarizationUnreachableError(
            f"Could not reach the diarization service at {url}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise DiarizationError(
            f"Diarization timed out after {timeout}s. Increase "
            f"MMN_DIARIZATION_TIMEOUT_SEC for very long recordings."
        ) from exc
    except httpx.HTTPError as exc:
        raise DiarizationError(f"Diarization request failed: {exc}") from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)

    if response.status_code >= 400:
        raise DiarizationError(
            f"Diarization service returned {response.status_code}: "
            f"{response.text[:400]}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise DiarizationError(
            f"Diarization service returned non-JSON: {response.text[:200]}"
        ) from exc

    if not isinstance(payload, dict) or "segments" not in payload:
        raise DiarizationError(
            f"Unexpected diarization response shape: keys={list(payload)[:10]}"
        )

    if not payload["segments"]:
        raise DiarizationError("Diarization returned no segments — is the audio silent?")

    # The distinguishing symptom of a missing include_text: turns with no words.
    if all(not (s.get("text") or "").strip() for s in payload["segments"]):
        raise DiarizationError(
            "Diarization returned segments with no text. The service ignored "
            "include_text=true or the model does not support transcription."
        )

    return payload, elapsed_ms


def transcribe_sync(
    path: Path,
    *,
    url: str,
    model: str,
    api_key: str | None,
    timeout: int,
) -> tuple[dict, int]:
    """Blocking POST to /v1/audio/transcriptions -- plain ASR, no diarization.

    For a channel already known to hold exactly one speaker: the local
    microphone, always, or the room channel when the user has said it is a
    single remote person (see diarize_channels_file). There is nothing to
    diarize in that case, and asking the model to do it anyway would spend
    the same minutes-long call on an answer already known for free.
    """
    started = time.monotonic()
    try:
        with path.open("rb") as fh:
            files = {"file": (path.name, fh, "audio/wav")}
            response = httpx.post(
                url,
                files=files,
                data={"model": model, "response_format": "verbose_json"},
                headers=_headers(api_key),
                timeout=timeout,
            )
    except httpx.ConnectError as exc:
        raise DiarizationUnreachableError(
            f"Could not reach the transcription service at {url}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise DiarizationError(
            f"Transcription timed out after {timeout}s. Increase "
            f"MMN_DIARIZATION_TIMEOUT_SEC for very long recordings."
        ) from exc
    except httpx.HTTPError as exc:
        raise DiarizationError(f"Transcription request failed: {exc}") from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)

    if response.status_code >= 400:
        raise DiarizationError(
            f"Transcription service returned {response.status_code}: "
            f"{response.text[:400]}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise DiarizationError(
            f"Transcription service returned non-JSON: {response.text[:200]}"
        ) from exc

    if not isinstance(payload, dict) or "segments" not in payload:
        raise DiarizationError(
            f"Unexpected transcription response shape: keys={list(payload)[:10]}"
        )

    if not payload["segments"]:
        raise DiarizationError("Transcription returned no segments — is the audio silent?")

    return payload, elapsed_ms


async def diarize_file(
    ctx,
    path: Path,
    *,
    model: str,
    duration_sec: float | None = None,
) -> tuple[dict, int]:
    """Run diarization off the event loop while reporting synthetic progress."""
    settings = get_settings()
    with get_conn(ctx.db_path) as conn:
        url = effective(conn, "diarization_url")
        api_key = effective(conn, "diarization_api_key")
        timeout = effective(conn, "diarization_timeout_sec")

    expected = None
    if duration_sec:
        expected = duration_sec * settings.diarize_seconds_per_audio_second
        ctx.event(
            f"Estimated {expected / 60:.1f} min of processing for "
            f"{duration_sec / 60:.1f} min of audio",
            stage="diarizing",
        )

    stop = asyncio.Event()

    async def heartbeat() -> None:
        """Keep the bar and the stale-job watchdog alive during a long silence."""
        started = time.monotonic()
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SEC)
                return
            except asyncio.TimeoutError:
                pass
            elapsed = time.monotonic() - started
            ctx.heartbeat()
            if expected:
                ctx.stage_progress(min(elapsed / expected, PROGRESS_CEILING))

    ticker = asyncio.create_task(heartbeat())
    try:
        payload, elapsed_ms = await asyncio.to_thread(
            diarize_sync,
            path,
            url=url,
            model=model,
            api_key=api_key or None,
            timeout=timeout,
        )
    finally:
        stop.set()
        await asyncio.gather(ticker, return_exceptions=True)

    log.info(
        "diarized %s in %.1fs: %d segments, %s speakers",
        path.name,
        elapsed_ms / 1000,
        len(payload.get("segments", [])),
        payload.get("num_speakers"),
    )
    return payload, elapsed_ms


def _label_segments(segments: list[dict], speaker_id: str) -> list[dict]:
    """Stamp every segment from a single-speaker channel with its known identity."""
    return [
        {
            "id": seg.get("id"),
            "speaker": speaker_id,
            "label": speaker_id,
            "start": seg.get("start"),
            "end": seg.get("end"),
            "text": seg.get("text") or "",
        }
        for seg in segments
    ]


def _single_speaker_row(speaker_id: str, segments: list[dict]) -> dict:
    duration = sum(max(0.0, (s.get("end") or 0) - (s.get("start") or 0)) for s in segments)
    return {
        "id": speaker_id,
        "label": speaker_id,
        "total_speech_duration": duration,
        "segment_count": len(segments),
    }


def _prefix_room_speakers(payload: dict) -> tuple[list[dict], list[dict]]:
    """Rename a batch diarization's raw speaker ids (e.g. SPEAKER_00) to
    ROOM_SPEAKER_00.

    Keeps them visually distinct from 'ME' -- the room channel is diarized
    independently of the mic channel here, so nothing stops the model
    reusing "SPEAKER_00" for a remote voice that has nothing to do with the
    local mic. The model's own speaker count and ordering are preserved,
    just namespaced.
    """
    segments = []
    for seg in payload.get("segments") or []:
        raw_id = seg.get("speaker") or "UNKNOWN"
        segments.append(
            {
                "id": seg.get("id"),
                "speaker": f"ROOM_{raw_id}",
                "label": seg.get("label"),
                "start": seg.get("start"),
                "end": seg.get("end"),
                "text": seg.get("text") or "",
            }
        )
    speakers = []
    for sp in payload.get("speakers") or []:
        raw_id = sp.get("id") or "UNKNOWN"
        speakers.append({**sp, "id": f"ROOM_{raw_id}"})
    return segments, speakers


def _merge_channel_segments(me_segments: list[dict], room_segments: list[dict]) -> list[dict]:
    """Interleave two channels' segments into one timeline, renumbering ids
    so they are unique across the merge rather than colliding (both channels
    started counting from 0)."""
    merged = sorted(me_segments + room_segments, key=lambda s: s.get("start") or 0)
    for i, seg in enumerate(merged):
        seg["id"] = i
    return merged


async def diarize_channels_file(
    ctx,
    *,
    room_wav: Path,
    me_wav: Path,
    room_speakers: str,
    model: str,
    duration_sec: float | None = None,
) -> tuple[dict, int]:
    """Channel-separated alternative to diarize_file.

    Used when the recording kept the local microphone and the room's audio on
    two distinct channels (see useRecorder's stereo capture) instead of
    mixing them to mono. Channel identity is ground truth, not a
    voice-clustering guess, so the model diarizer is skipped for the mic
    channel entirely -- there is exactly one speaker on it by construction --
    and for the room channel too when the user has said it holds a single
    remote person. room_speakers == 'multiple' still asks the model to
    diarize the room channel: two channels can only ever prove "me" apart
    from "everyone else", never tell three remote voices apart from one, and
    a person is the only source of that fact.
    """
    settings = get_settings()
    with get_conn(ctx.db_path) as conn:
        url = effective(conn, "diarization_url")
        api_key = effective(conn, "diarization_api_key")
        timeout = effective(conn, "diarization_timeout_sec")
    transcribe_url = transcriptions_url(url)

    stop = asyncio.Event()
    started_at = time.monotonic()

    async def heartbeat() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SEC)
                return
            except asyncio.TimeoutError:
                pass
            ctx.heartbeat()
            if duration_sec:
                elapsed = time.monotonic() - started_at
                expected = duration_sec * settings.diarize_seconds_per_audio_second
                ctx.stage_progress(min(elapsed / expected, PROGRESS_CEILING))

    ticker = asyncio.create_task(heartbeat())
    try:
        me_payload, me_ms = await asyncio.to_thread(
            transcribe_sync,
            me_wav,
            url=transcribe_url,
            model=model,
            api_key=api_key or None,
            timeout=timeout,
        )

        if room_speakers == "single":
            room_payload, room_ms = await asyncio.to_thread(
                transcribe_sync,
                room_wav,
                url=transcribe_url,
                model=model,
                api_key=api_key or None,
                timeout=timeout,
            )
            room_segments = _label_segments(room_payload.get("segments") or [], "ROOM")
            room_speaker_rows = [_single_speaker_row("ROOM", room_segments)]
        else:
            room_payload, room_ms = await asyncio.to_thread(
                diarize_sync,
                room_wav,
                url=url,
                model=model,
                api_key=api_key or None,
                timeout=timeout,
            )
            room_segments, room_speaker_rows = _prefix_room_speakers(room_payload)
    finally:
        stop.set()
        await asyncio.gather(ticker, return_exceptions=True)

    me_segments = _label_segments(me_payload.get("segments") or [], "ME")
    me_speaker_rows = [_single_speaker_row("ME", me_segments)]

    merged = _merge_channel_segments(me_segments, room_segments)
    payload = {
        "task": "diarize",
        "duration": duration_sec or max((s["end"] or 0 for s in merged), default=0.0),
        "num_speakers": len(me_speaker_rows) + len(room_speaker_rows),
        "segments": merged,
        "speakers": me_speaker_rows + room_speaker_rows,
    }
    log.info(
        "channel-diarized %s + %s: %d segments, %d speakers (room_speakers=%s)",
        me_wav.name,
        room_wav.name,
        len(merged),
        payload["num_speakers"],
        room_speakers,
    )
    return payload, me_ms + room_ms


def test_connection(
    base_url: str, model: str, api_key: str | None = None, timeout: int = 15
) -> dict:
    """Check the service is reachable and the configured model exists.

    Doesn't run a real diarization -- that takes minutes on real audio and
    would make "Test" a multi-minute button. This is the same tradeoff as the
    model dropdown: cheap enough to click freely, honest about what it did and
    didn't verify.
    """
    started = time.monotonic()
    try:
        models = list_models(base_url, api_key, timeout=timeout)
    except DiarizationError as exc:
        return {
            "ok": False,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": exc.message,
            "models_count": 0,
            "model_found": False,
        }

    ids = {m.get("id") for m in models}
    found = model in ids
    return {
        "ok": found,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "error": (
            None
            if found
            else f"Reached the service, but {model!r} is not loaded. "
            f"Available: {', '.join(sorted(i for i in ids if i)[:8])}"
        ),
        "models_count": len(models),
        "model_found": found,
    }


def list_models(base_url: str, api_key: str | None = None, timeout: int = 15) -> list[dict]:
    """Populate the model dropdown from the service's own /v1/models."""
    root = base_url.split("/v1/")[0].rstrip("/")
    try:
        response = httpx.get(
            f"{root}/v1/models", headers=_headers(api_key), timeout=timeout
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise DiarizationError(f"Could not list diarization models: {exc}") from exc
    return response.json().get("data", [])
