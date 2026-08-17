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
"""

from __future__ import annotations

import asyncio
import io
import json
import wave
from dataclasses import dataclass, field

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from httpx_sse import SSEError, aconnect_sse

from app.config import effective, get_settings
from app.db import get_conn
from app.logging_config import get_logger
from app.services import users as users_svc
from app.services.diarize import _headers, transcriptions_url

log = get_logger("live_caption")

router = APIRouter(prefix="/api/live-caption", tags=["live-caption"])

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2  # int16 mono
CHANNEL_NAMES = {0: "room", 1: "me"}
# Below this a window is mostly silence padding from a channel that has
# barely spoken yet -- not worth a call to a shared, minutes-latency box.
MIN_BUFFER_SEC = 1.0


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


def _wav_bytes(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(BYTES_PER_SAMPLE)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(pcm)
    return buf.getvalue()


async def _transcribe_window(
    client: httpx.AsyncClient, url: str, model: str, api_key: str | None, pcm: bytes
) -> str:
    """One rolling-window call. Returns the committed text, or '' on any
    failure -- a dropped caption is invisible to the user; raising would
    kill the whole live session over one bad window.
    """
    files = {"file": ("window.wav", _wav_bytes(pcm), "audio/wav")}
    data = {"model": model, "stream": "true"}
    try:
        async with aconnect_sse(
            client, "POST", url, data=data, files=files, headers=_headers(api_key)
        ) as event_source:
            if event_source.response.status_code >= 400:
                log.warning(
                    "live caption window rejected: %s", event_source.response.status_code
                )
                return ""
            text = ""
            async for sse in event_source.aiter_sse():
                if sse.data == "[DONE]":
                    break
                chunk = json.loads(sse.data)
                if chunk.get("type") == "transcript.text.done":
                    text = chunk.get("text") or ""
            return text
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
        api_key = effective(conn, "diarization_api_key")
        diarization_url = effective(conn, "diarization_url")

    if not enabled:
        await websocket.close(code=4404)
        return

    url = transcriptions_url(diarization_url)
    await websocket.accept()

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
                        pcm = buf.window_bytes(window_sec)
                    if len(pcm) < MIN_BUFFER_SEC * SAMPLE_RATE * BYTES_PER_SAMPLE:
                        continue

                    text = await _transcribe_window(client, url, model, api_key or None, pcm)
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
