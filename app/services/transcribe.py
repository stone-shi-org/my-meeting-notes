"""The transcription-only service client, for "Diarization only" mode.

Used when the configured diarization backend only produces speaker turns
with no words in them at all -- e.g. pyannote/speaker-diarization-community-1,
confirmed to always return an empty ``text`` on every segment, by design, not
as a failure. A separate OpenAI-compatible ``/v1/audio/transcriptions``
endpoint supplies the actual text, which
``pipeline._combine_diarization_and_transcript`` then aligns against the
diarization turns by timestamp overlap.

Deliberately its own small module rather than reusing diarize.py:

- The response shapes differ. This is a flat ASR transcript --
  ``{"segments": [...], "text": ..., "duration": ...}``, each segment just
  ``start``/``end``/``text`` -- not diarize.py's
  ``{"segments": [...], "speakers": [...], "num_speakers": ...}``.
- diarize.py's ``include_text``/``looks_like_embedded_turns_dump`` checks are
  specific to a combined ASR+diarization backend failing to produce text;
  they don't apply to a backend that was never asked to.
- One error class per service (``TranscribeError`` vs ``DiarizationError``)
  keeps a failure pointing at the Settings panel that's actually misconfigured
  -- the two have separate URL/model/api-key fields and separate Test buttons.

A "live-stt" backend (confirmed shape:
``{"task", "language", "duration", "text", "words"}``) is an
OpenAI-Whisper-API ``verbose_json`` variant with word-level timestamps
instead of segments -- there is no ``segments`` key at all.
``pipeline._combine_diarization_and_transcript`` only ever reads
``start``/``end``/``text`` at segment granularity (it aligns against
diarization turns by timestamp overlap, not word-by-word -- see its own
docstring), so ``_segments_from_words`` below reconstructs that shape by
grouping consecutive words into segments at >1s gaps, rather than emitting
one segment per word: the latter would still be *correct* input to the
overlap match, but it would also make every single word its own line in the
rendered transcript, each carrying its own speaker header.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx

from app.config import effective, get_settings
from app.db import get_conn
from app.errors import TranscribeError, TranscribeUnreachableError
from app.logging_config import get_logger

log = get_logger("transcribe")

# Same tradeoff as diarize.py's PROGRESS_CEILING/HEARTBEAT_INTERVAL_SEC.
PROGRESS_CEILING = 0.95
HEARTBEAT_INTERVAL_SEC = 5.0


def build_form(model: str) -> dict[str, str]:
    # No include_text: that flag is diarize.py's, meaningless to a plain
    # transcription endpoint that only ever produces text.
    return {
        "model": model,
        "response_format": "verbose_json",
    }


def _headers(api_key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


# Gap between two words' timestamps past which they belong to different
# segments rather than the same run-on sentence. Not tuned against a real
# live-stt transcript (none was available); chosen to be well above normal
# inter-word pause and well below "the speaker stopped talking".
_WORD_GAP_SEC = 1.0


def _segments_from_words(words: list[dict], *, gap_sec: float = _WORD_GAP_SEC) -> list[dict]:
    """Reconstruct segment-level ``{start, end, text}`` turns from a
    word-timestamp list (see module docstring for why segment, not word,
    granularity is what the rest of the pipeline needs).

    Each word is assumed shaped ``{"word": ..., "start": ..., "end": ...}``,
    the OpenAI Whisper API's own ``verbose_json`` word shape -- unconfirmed
    against the actual live-stt service, so ``"text"`` is accepted as a
    fallback key in case that assumption is wrong.
    """
    segments: list[dict] = []
    bucket: list[dict] = []
    prev_end: float | None = None

    for w in words:
        word_text = ((w.get("word") if w.get("word") is not None else w.get("text")) or "").strip()
        if not word_text:
            continue
        start = w.get("start")
        if bucket and prev_end is not None and start is not None and (start - prev_end) > gap_sec:
            segments.append(_flush_bucket(bucket, len(segments)))
            bucket = []
        bucket.append(w)
        prev_end = w.get("end")

    if bucket:
        segments.append(_flush_bucket(bucket, len(segments)))
    return segments


def _flush_bucket(bucket: list[dict], next_id: int) -> dict:
    text = " ".join(
        ((w.get("word") if w.get("word") is not None else w.get("text")) or "").strip()
        for w in bucket
    ).strip()
    start = bucket[0].get("start") or 0.0
    end = bucket[-1].get("end")
    if end is None:
        end = bucket[-1].get("start") or start
    return {"id": next_id, "start": start, "end": end, "text": text}


def transcribe_sync(
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
        raise TranscribeUnreachableError(
            f"Could not reach the transcription service at {url}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise TranscribeError(
            f"Transcription timed out after {timeout}s. Increase "
            f"MMN_TRANSCRIBE_TIMEOUT_SEC for very long recordings."
        ) from exc
    except httpx.HTTPError as exc:
        raise TranscribeError(f"Transcription request failed: {exc}") from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)

    if response.status_code >= 400:
        raise TranscribeError(
            f"Transcription service returned {response.status_code}: "
            f"{response.text[:400]}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise TranscribeError(
            f"Transcription service returned non-JSON: {response.text[:200]}"
        ) from exc

    if (
        isinstance(payload, dict)
        and "segments" not in payload
        and isinstance(payload.get("words"), list)
    ):
        # live-stt's verbose_json variant: word-level timestamps, no
        # `segments` key at all. Additive normalization -- every other shape
        # falls through to the check below unchanged.
        payload = {**payload, "segments": _segments_from_words(payload["words"])}

    if not isinstance(payload, dict) or "segments" not in payload:
        raise TranscribeError(
            f"Unexpected transcription response shape: keys={list(payload)[:10]}"
        )

    # Unlike diarize_sync: an empty segments list here just means silence,
    # not a broken request -- there is no include_text-style flag that could
    # have been dropped, so there is nothing to distinguish it from.

    return payload, elapsed_ms


async def transcribe_file(
    ctx,
    path: Path,
    *,
    model: str,
    duration_sec: float | None = None,
    progress_window: tuple[float, float] = (0.0, 1.0),
) -> tuple[dict, int]:
    """Run transcription off the event loop while reporting synthetic
    progress. Mirrors diarize.diarize_file's heartbeat/progress-window
    mechanics exactly -- see that function's docstring for why
    ``progress_window`` exists -- against a different endpoint and response
    shape underneath.
    """
    settings = get_settings()
    with get_conn(ctx.db_path) as conn:
        url = effective(conn, "transcribe_url")
        api_key = effective(conn, "transcribe_api_key")
        timeout = effective(conn, "transcribe_timeout_sec")

    window_start, window_span = progress_window[0], progress_window[1] - progress_window[0]

    expected = None
    if duration_sec:
        expected = duration_sec * settings.diarize_seconds_per_audio_second
        ctx.event(
            f"Estimated {expected / 60:.1f} min of transcription for "
            f"{duration_sec / 60:.1f} min of audio",
            stage="diarizing",
        )

    stop = asyncio.Event()

    async def heartbeat() -> None:
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
                frac = min(elapsed / expected, PROGRESS_CEILING)
                ctx.stage_progress(window_start + frac * window_span)

    ticker = asyncio.create_task(heartbeat())
    try:
        payload, elapsed_ms = await asyncio.to_thread(
            transcribe_sync,
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
        "transcribed %s in %.1fs: %d segments",
        path.name,
        elapsed_ms / 1000,
        len(payload.get("segments", [])),
    )
    return payload, elapsed_ms


def test_connection(
    base_url: str,
    model: str,
    api_key: str | None = None,
    timeout: int = 15,
) -> dict:
    """Check the service is reachable and the configured model exists.

    Same tradeoff as diarize.test_connection: a cheap GET /v1/models probe,
    not a real transcription run.
    """
    started = time.monotonic()
    try:
        models = list_models(base_url, api_key, timeout=timeout)
    except TranscribeError as exc:
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
        raise TranscribeError(f"Could not list transcription models: {exc}") from exc
    return response.json().get("data", [])
