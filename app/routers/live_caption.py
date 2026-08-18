"""Live captions during an active recording.

Rolling-window relay: the browser streams raw PCM over one websocket, tagged
by channel (0 = whatever the tab/system capture is picking up -- "the room";
1 = the local microphone -- see useLiveCaption.ts). This reuses the channel
convention from the channel-separated recording feature
(services/audio.py/diarize.py) for labelling only, never for accuracy: a live
caption is a disposable draft, and unlike diarize_channels_file's output it
is never written to transcripts/diarizations.

Deliberately not a job (see jobs/queue.py). Nothing here is durable or
resumable -- a dropped connection just means the browser reconnects and
captions resume a moment later. There is nothing to recover, unlike an
ingest job that must survive a process restart, so this gets its own
in-memory relay instead of a job type, a jobs row, or any sqlite write.

Each channel gets its own rolling buffer and its own periodic call to
/v1/audio/transcriptions?stream=true -- the same LocalAI instance and
credentials as batch diarization, just the plain-text streaming route
instead of the diarizing one. That route never carries speaker labels (see
diarize.py's diarize_channels_file docstring); channel identity substitutes
for it here, at zero extra model cost.

The model defaults to whatever batch diarization uses (diarization_model)
but can be overridden separately via live_caption_model -- the streaming
route and the batch route on the same LocalAI instance have been observed
to behave very differently under load for the same model, so an operator
needs to be able to point this feature at a more reliable one without
touching what batch diarization uses.

Besides ``{"type": "caption", ...}`` messages, each channel_worker also
pushes ``{"type": "status", "channel": ..., "state": ...}`` on every state
change -- idle (nothing worth sending) -> buffering (real audio, waiting on
_ASR_CONCURRENCY's one slot) -> calling (request sent) -> reading (SSE
stream started) -> idle again. Purely cosmetic (the recorder UI's activity
dot, see useLiveCaption.ts); nothing here or downstream reads it back.
"""

from __future__ import annotations

import array
import asyncio
import io
import json
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from httpx_sse import SSEError, aconnect_sse

from app.config import effective, get_settings
from app.db import get_conn
from app.logging_config import get_logger
from app.services import users as users_svc
from app.services.diarize import _headers, strip_language_tag, transcriptions_url

log = get_logger("live_caption")

router = APIRouter(prefix="/api/live-caption", tags=["live-caption"])

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2  # int16 mono
CHANNEL_NAMES = {0: "room", 1: "me"}
# Below this a window is mostly silence padding from a channel that has
# barely spoken yet -- not worth a call to a shared, minutes-latency box.
MIN_BUFFER_SEC = 1.0

# Below this fraction of int16 full scale (32768), a window is treated as
# silence and never sent to the ASR backend at all. This is a genuine
# amplitude check, unlike MIN_BUFFER_SEC above (which only gates on how much
# audio has accumulated, not whether any of it is speech) -- a channel
# nobody is talking on would otherwise fire a request every interval_sec for
# the entire session, relying on the model to notice it's silence and
# return empty text after paying for the round trip and an _ASR_CONCURRENCY
# slot. ~2% of full scale: comfortably above a quiet mic's own noise floor,
# comfortably below even a soft speaking voice's peak.
SILENCE_PEAK_THRESHOLD = 0.02

# room and me are two independent asyncio tasks (see channel_worker below)
# with the same interval_sec cadence, so without this they fire their ASR
# calls at essentially the same moment -- two concurrent requests to a
# backend that has been observed to hang entirely (not just slow down) under
# concurrent load, per live_caption_timeout_sec's doc comment in config.py.
# Process-wide, not per-connection: the backend is shared across every
# simultaneous live-caption session this app has open, not just the two
# channels of one, and per-connection serialization alone would still let
# two different users' sessions collide. 1 matches what was actually
# observed to work reliably; raise it only after confirming the backend
# genuinely tolerates more concurrent requests than that.
_ASR_CONCURRENCY = asyncio.Semaphore(1)


async def _authenticate(websocket: WebSocket) -> dict | None:
    """Cookie-only: browsers never send an Authorization header on a
    websocket upgrade, and there is no request body for a bearer token to
    live in even if one did. Mirrors deps.current_user's session lookup.
    """
    settings = get_settings()
    token = websocket.cookies.get(settings.session_cookie_name)
    if not token:
        return None
    with get_conn() as conn:
        resolved = users_svc.resolve_session(conn, token)
        if resolved is None:
            return None
        session, user = resolved
        users_svc.touch_session(conn, session)
    return dict(user)


@dataclass
class _ChannelBuffer:
    samples: bytearray = field(default_factory=bytearray)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def window_bytes(self, window_sec: float) -> bytes:
        """Trim to the trailing window and hand back a copy.

        Trimming here, not just on read, is what keeps a channel nobody is
        speaking on from growing its buffer for the entire meeting.
        """
        max_bytes = int(window_sec * SAMPLE_RATE * BYTES_PER_SAMPLE)
        if len(self.samples) > max_bytes:
            del self.samples[: len(self.samples) - max_bytes]
        return bytes(self.samples)

    def pop_chunk_bytes(self, max_chunk_sec: float) -> bytes:
        """Extract and consume up to max_chunk_sec of audio samples for non-overlapping
        cache-aware delta streaming (e.g. for nemotron-3.5-asr-streaming-0.6b).
        """
        max_bytes = int(max_chunk_sec * SAMPLE_RATE * BYTES_PER_SAMPLE)
        chunk_len = min(len(self.samples), max_bytes)
        chunk = bytes(self.samples[:chunk_len])
        del self.samples[:chunk_len]
        return chunk


def _is_cache_aware_model(model: str) -> bool:
    """Returns True if the specified ASR model is a native cache-aware streaming model
    (e.g., nemotron-3.5-asr-streaming-0.6b).
    """
    m = model.lower()
    return "nemotron" in m or "cache-aware" in m or "cache_aware" in m



def _peak_amplitude(pcm: bytes) -> float:
    """Peak absolute sample value over a window, 0..1 (int16 full scale is
    32768). Peak, not RMS -- same choice as the recorder's own level meter
    (see useRecorder.ts's nextLevel): RMS reads near-silent between
    syllables at this granularity, peak does not.

    Assumes little-endian samples, true for every real deployment target
    (the browser's Int16Array and every platform this actually runs on) and
    not worth defending against for a threshold heuristic, not a decode.
    """
    # A torn trailing byte (an odd-length buffer) is dropped rather than
    # raising -- this is a cheap gate, not a decoder; one missing sample
    # changes nothing.
    usable = len(pcm) - (len(pcm) % 2)
    if usable <= 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[:usable])
    return max(abs(min(samples)), abs(max(samples))) / 32768


def _wav_bytes(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(BYTES_PER_SAMPLE)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(pcm)
    return buf.getvalue()


async def _send_status(websocket: WebSocket, channel: str, state: str) -> None:
    """Best-effort activity signal for the recorder UI's per-channel dot
    (idle/buffering/calling/reading -- see channel_worker's call sites).
    Same swallow-everything policy as the caption send below it: a dropped
    status update is invisible to the user, and letting it raise would kill
    channel_worker over something that was never essential to begin with.
    """
    try:
        await websocket.send_json({"type": "status", "channel": channel, "state": state})
    except Exception:
        pass


async def _transcribe_window(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    api_key: str | None,
    pcm: bytes,
    language: str | None = None,
    on_reading: Callable[[], Awaitable[None]] | None = None,
) -> str:
    """One rolling-window call. Returns the committed text, or '' on any
    failure -- a dropped caption is invisible to the user; raising would
    kill the whole live session over one bad window.

    ``on_reading``, when given, fires once the response is confirmed good
    and we're about to start consuming the SSE stream -- the "reading" state
    in channel_worker's activity dot. Only meaningful on the happy path: a
    rejected or errored call never gets far enough to call it.

    ``language`` is omitted entirely when unset, which leaves per-window
    auto-detection on -- the default, and the right one for a genuinely
    multilingual meeting. A rolling window is only a few seconds of audio
    (live_caption_window_sec), which is little enough for language ID to
    misfire on an accented phrase, a name or a stretch of silence -- pinning
    a language removes that per-window guesswork for anyone who mostly
    speaks one language. Must be an ISO-639-1 code ("en"), not the English
    name ("english"): confirmed against parakeet-cpp-nemotron-3.5-asr-
    streaming-0.6b that the latter doesn't get rejected, it silently breaks
    streaming entirely -- every window then fails with "loaded model is not
    a cache-aware streaming model", an error that has nothing to do with
    language and would be a nightmare to trace back to this field.
    """
    files = {"file": ("window.wav", _wav_bytes(pcm), "audio/wav")}
    data = {"model": model, "stream": "true"}
    if language:
        data["language"] = language
    try:
        async with aconnect_sse(
            client, "POST", url, data=data, files=files, headers=_headers(api_key)
        ) as event_source:
            if event_source.response.status_code >= 400:
                log.warning(
                    "live caption window rejected: %s", event_source.response.status_code
                )
                return ""
            if on_reading is not None:
                await on_reading()
            text = ""
            async for sse in event_source.aiter_sse():
                if sse.data == "[DONE]":
                    break
                chunk = json.loads(sse.data)
                if chunk.get("type") == "transcript.text.done":
                    text = chunk.get("text") or ""
            return strip_language_tag(text)
    except (httpx.HTTPError, SSEError, ValueError) as exc:
        # ValueError alongside the transport errors: a non-JSON SSE data line
        # (json.loads above) is the same "drop this window, keep going" case
        # as a timeout, not a reason to let the exception escape and kill the
        # channel_worker task -- see that function's docstring. The type name
        # is logged explicitly because httpx's own timeout exceptions
        # stringify to '' (confirmed: str(httpx.ReadTimeout('')) == ''), which
        # otherwise reads as a message that silently went missing.
        log.warning("live caption window failed: %s: %s", type(exc).__name__, exc)
        return ""


@router.websocket("/ws")
async def live_caption_ws(websocket: WebSocket) -> None:
    user = await _authenticate(websocket)
    if user is None:
        await websocket.close(code=4401)
        return

    with get_conn() as conn:
        enabled = effective(conn, "live_caption_enabled")
        window_sec = effective(conn, "live_caption_window_sec")
        interval_sec = effective(conn, "live_caption_interval_sec")
        timeout_sec = effective(conn, "live_caption_timeout_sec")
        # Empty live_caption_model means "use whatever batch diarization
        # uses" -- see RUNTIME_KEYS in config.py for why this is overridable
        # separately: the two routes on the same LocalAI instance have been
        # observed to behave very differently under load for the same model.
        model = effective(conn, "live_caption_model") or effective(conn, "diarization_model")
        default_language = effective(conn, "live_caption_language")
        api_key = effective(conn, "diarization_api_key")
        diarization_url = effective(conn, "diarization_url")

    if not enabled:
        await websocket.close(code=4404)
        return

    # The recorder UI picks a language before Start (see useLiveCaption.ts)
    # and always sends it, even '' for an explicit "Auto-detect" -- which is
    # why this checks "was the param given at all" rather than truthiness:
    # an explicit '' must be able to override a non-empty Settings default
    # for just this recording, not be treated as "no opinion, use the
    # default". Only a request with no param at all (not made through the
    # recorder UI) falls back to the Settings-page default.
    raw_language = websocket.query_params.get("language")
    language = raw_language if raw_language is not None else default_language

    url = transcriptions_url(diarization_url)
    await websocket.accept()
    await websocket.send_json({
        "type": "info",
        "model": model,
        "is_cache_aware": _is_cache_aware_model(model),
    })

    buffers: dict[int, _ChannelBuffer] = {0: _ChannelBuffer(), 1: _ChannelBuffer()}
    stop = asyncio.Event()

    async def channel_worker(channel: int) -> None:
        buf = buffers[channel]
        name = CHANNEL_NAMES[channel]
        # timeout_sec, not interval_sec + 20: this bounds one ASR call against
        # a backend the batch diarizer itself waits up to 30 minutes for (see
        # live_caption_timeout_sec's doc comment in config.py). interval_sec
        # is just the polling cadence below, unrelated to how long a call is
        # allowed to take once it starts.
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval_sec)
                    return
                except asyncio.TimeoutError:
                    pass

                try:
                    async with buf.lock:
                        if len(buf.samples) < MIN_BUFFER_SEC * SAMPLE_RATE * BYTES_PER_SAMPLE:
                            await _send_status(websocket, name, "idle")
                            continue
                        if _is_cache_aware_model(model):
                            pcm = buf.pop_chunk_bytes(interval_sec)
                        else:
                            pcm = buf.window_bytes(window_sec)

                    if _peak_amplitude(pcm) < SILENCE_PEAK_THRESHOLD:
                        await _send_status(websocket, name, "idle")
                        continue

                    # Real audio, gates cleared -- "buffering" covers the
                    # (usually brief) wait for _ASR_CONCURRENCY's one slot,
                    # which the other channel or another session's worker may
                    # currently hold.
                    await _send_status(websocket, name, "buffering")

                    # Serialized across every channel and every session --
                    # see _ASR_CONCURRENCY's doc comment -- not just this
                    # worker's own pacing. A window still gets buffered and
                    # timed independently per channel; only the network call
                    # itself queues behind whichever other channel/session
                    # got there first.
                    async with _ASR_CONCURRENCY:
                        await _send_status(websocket, name, "calling")
                        text = await _transcribe_window(
                            client,
                            url,
                            model,
                            api_key or None,
                            pcm,
                            language or None,
                            on_reading=lambda: _send_status(websocket, name, "reading"),
                        )
                    await _send_status(websocket, name, "idle")
                    if not text.strip():
                        continue
                except Exception:
                    # Anything that reaches here is a bug in this loop, not an
                    # ASR failure -- those are already turned into "" inside
                    # _transcribe_window. Log and keep going regardless: for a
                    # plain mic recording this is the *only* worker, and
                    # letting an exception escape ends live captions for the
                    # rest of the recording with nothing visible to the user.
                    log.exception("live caption window for %s crashed", name)
                    await _send_status(websocket, name, "idle")
                    continue

                try:
                    await websocket.send_json({"type": "caption", "channel": name, "text": text})
                except Exception:
                    # The socket itself is gone -- further sends will fail the
                    # same way, so this one *should* end the worker.
                    return

    workers = [asyncio.create_task(channel_worker(ch)) for ch in buffers]

    try:
        while True:
            message = await websocket.receive_bytes()
            if not message:
                continue
            channel = message[0]
            buf = buffers.get(channel)
            if buf is None:
                continue
            async with buf.lock:
                buf.samples.extend(message[1:])
    except WebSocketDisconnect:
        pass
    finally:
        stop.set()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
