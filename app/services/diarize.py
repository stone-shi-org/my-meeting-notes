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
import re
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

# Some ASR backends leak a BCP-47-looking language tag into the transcribed
# text itself -- confirmed on parakeet-cpp-nemotron-3.5-asr-streaming-0.6b,
# which appends e.g. "<en-US>" to what it transcribes. Not a real bracketed
# non-speech marker like "[Music]" (see transcript.is_non_speech): that
# classifies a *whole* segment, this strips a substring out of otherwise-real
# speech, so the two must not be merged into one check. Lowercase language,
# optional uppercase region -- the exact casing BCP-47 uses, and not one a
# meeting about markup literally saying "the div tag" would happen to
# produce -- keeps this narrow rather than eating unrelated bracketed asides.
_LANGUAGE_TAG_RE = re.compile(r"\s*<[a-z]{2,3}(?:-[A-Z]{2,4})?>")


def strip_language_tag(text: str) -> str:
    """Remove a stray "<en-US>"-style tag the model appended to its own
    output. Safe to call on any segment text, tagged or not -- a no-op when
    there is nothing to strip."""
    return _LANGUAGE_TAG_RE.sub("", text or "").strip()


# vibevoice-cpp-asr, on a recording long enough to exceed whatever output
# budget it re-transcribes against, gives up on real per-turn diarization
# and instead dumps its own structured self-transcript --
# `[{"Start":0,"End":1.0,"Speaker":0,"Content":"..."}, ...]`, frequently
# truncated mid-object -- as the *text* of one top-level segment spanning
# start=0/end=0. num_speakers still comes back plausible and that one
# segment's text is non-empty, so neither check above catches it: it
# silently became a "transcript" that was one line of raw, truncated JSON.
# Confirmed on a ~59-minute recording. Real spoken text never opens with a
# JSON array of its own Start/Speaker/Content turns, which keeps this narrow.
_EMBEDDED_TURNS_RE = re.compile(r'^\[\s*\{\s*"start"\s*:', re.IGNORECASE)


def looks_like_embedded_turns_dump(text: str) -> bool:
    return bool(_EMBEDDED_TURNS_RE.match((text or "").lstrip()))


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
    two. Used by the live-caption relay (routers/live_caption.py) for its own
    stateless-chunk fallback path.
    """
    suffix = "/v1/audio/diarization"
    if diarization_url.endswith(suffix):
        return diarization_url[: -len(suffix)] + "/v1/audio/transcriptions"
    # Not the shape we expected (a test double, a future path) -- leave it
    # alone rather than guessing at a rewrite that might be wrong.
    return diarization_url


def realtime_url(diarization_url: str) -> str:
    """Swap .../v1/audio/diarization for a ws(s)://.../v1/realtime URL on
    the same host -- the persistent-session endpoint live captions relay
    through (see routers/live_caption.py's module docstring for why: a
    model's own cache-aware streaming only helps within one continuous
    session, and /v1/audio/transcriptions is a stateless call-per-chunk
    route that can never provide that, no matter how it's called).

    http(s) becomes ws(s): this URL is only ever handed to a websocket
    client, never a browser, so there is no mixed-content concern requiring
    it to match the page's own scheme.
    """
    suffix = "/v1/audio/diarization"
    if not diarization_url.endswith(suffix):
        # Not the shape we expected (a test double, a future path) -- leave
        # it alone rather than guessing at a rewrite that might be wrong.
        return diarization_url
    base = diarization_url[: -len(suffix)]
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return base + "/v1/realtime"


def diarize_sync(
    path: Path,
    *,
    url: str,
    model: str,
    api_key: str | None,
    timeout: int,
    expect_text: bool = True,
) -> tuple[dict, int]:
    """Blocking POST. Returns ``(payload, elapsed_ms)``.

    ``expect_text=False`` is "Diarization only" mode: a pyannote-style
    backend that only ever produces speaker turns, with an empty "text" on
    every one of them *by design* -- confirmed on
    pyannote/speaker-diarization-community-1. The two checks below exist to
    catch a combined ASR+diarization backend silently failing to produce
    text; on a backend that was never asked to produce any, "every segment
    has no text" is the expected, correct shape, not a failure symptom, so
    both checks are skipped entirely rather than made to somehow tell the
    two apart.
    """
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

    if expect_text:
        # The distinguishing symptom of a missing include_text: turns with no words.
        if all(not (s.get("text") or "").strip() for s in payload["segments"]):
            raise DiarizationError(
                "Diarization returned segments with no text. The service ignored "
                "include_text=true or the model does not support transcription."
            )

        dumped = next(
            (s for s in payload["segments"] if looks_like_embedded_turns_dump(s.get("text"))),
            None,
        )
        if dumped is not None:
            raise DiarizationError(
                "Diarization collapsed into a single segment holding its own "
                "embedded (and possibly truncated) turn-by-turn JSON instead of "
                "real segments -- a known failure mode on recordings long enough "
                "to exceed the model's own output budget. Try a shorter recording "
                f"or a different diarization model. segment id={dumped.get('id')!r}."
            )

    return payload, elapsed_ms


async def diarize_file(
    ctx,
    path: Path,
    *,
    model: str,
    duration_sec: float | None = None,
    progress_window: tuple[float, float] = (0.0, 1.0),
    expect_text: bool = True,
) -> tuple[dict, int]:
    """Run diarization off the event loop while reporting synthetic progress.

    ``progress_window`` lets a caller diarizing several chunks in sequence
    (see pipeline._diarize_in_chunks) map this one call's own progress onto a
    slice of the stage's overall progress -- e.g. (0.25, 0.5) for chunk 1 of
    4 -- instead of every chunk restarting the bar from 0, which would go
    backwards the moment chunk 2 starts (JobContext.stage_progress sets an
    absolute value; nothing stops it from moving down as well as up, and
    test_jobs.py's progress tests assert it never does).

    ``expect_text=False`` is "Diarization only" mode -- see diarize_sync.
    """
    settings = get_settings()
    with get_conn(ctx.db_path) as conn:
        url = effective(conn, "diarization_url")
        api_key = effective(conn, "diarization_api_key")
        timeout = effective(conn, "diarization_timeout_sec")

    window_start, window_span = progress_window[0], progress_window[1] - progress_window[0]

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
                frac = min(elapsed / expected, PROGRESS_CEILING)
                ctx.stage_progress(window_start + frac * window_span)

    ticker = asyncio.create_task(heartbeat())
    try:
        payload, elapsed_ms = await asyncio.to_thread(
            diarize_sync,
            path,
            url=url,
            model=model,
            api_key=api_key or None,
            timeout=timeout,
            expect_text=expect_text,
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


LIVE_STT_MODELS = {
    "realtime_eou_120m-v1",
    "nemotron-3.5-asr-streaming-0.6b",
}


def is_live_stt_model(model: str) -> bool:
    if not model:
        return False
    if model in LIVE_STT_MODELS:
        return True
    m_lower = model.lower()
    return "realtime_eou" in m_lower or "nemotron" in m_lower or "live-stt" in m_lower or "livestt" in m_lower


def test_live_stt_connection(target_url: str, model: str, timeout: int = 15) -> dict:
    started = time.monotonic()
    try:
        import grpc
        from app.pb.livestt.v1 import asr_pb2, asr_pb2_grpc

        channel = grpc.insecure_channel(target_url)
        stub = asr_pb2_grpc.StreamingASRStub(channel)
        info = stub.GetServerInfo(asr_pb2.ServerInfoRequest(), timeout=timeout)
        channel.close()
        latency_ms = int((time.monotonic() - started) * 1000)
        return {
            "ok": True,
            "latency_ms": latency_ms,
            "error": None,
            "models_count": 1,
            "model_found": True,
            "server_version": info.version,
        }
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return {
            "ok": False,
            "latency_ms": latency_ms,
            "error": f"Could not reach live-stt service at {target_url}: {exc}",
            "models_count": 0,
            "model_found": False,
        }


def test_connection(
    base_url: str,
    model: str,
    api_key: str | None = None,
    timeout: int = 15,
    live_stt_url: str | None = None,
    backend: str | None = None,
) -> dict:
    """Check the service is reachable and the configured model exists.

    Doesn't run a real diarization -- that takes minutes on real audio and
    would make "Test" a multi-minute button. This is the same tradeoff as the
    model dropdown: cheap enough to click freely, honest about what it did and
    didn't verify.
    """
    if backend == "live_stt" or is_live_stt_model(model):
        target = live_stt_url or "localhost:4030"
        return test_live_stt_connection(target, model, timeout=timeout)


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
    """Populate the model dropdown from the service's own /v1/models.

    Returns exactly what the service reports -- test_connection() counts on
    that for models_count/model_found, and callers that want to also offer
    the live-stt realtime models (which this HTTP service doesn't know about)
    add those themselves, e.g. routers/system.py's dropdown endpoint.
    """
    root = base_url.split("/v1/")[0].rstrip("/")
    try:
        response = httpx.get(
            f"{root}/v1/models", headers=_headers(api_key), timeout=timeout
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise DiarizationError(f"Could not list diarization models: {exc}") from exc
    return response.json().get("data", [])

