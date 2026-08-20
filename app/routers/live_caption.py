"""Live captions during an active recording.

Realtime relay: the browser streams raw 16kHz mono PCM16 over one websocket,
tagged by channel (0 = whatever the tab/system capture is picking up -- "the
room"; 1 = the local microphone -- see useLiveCaption.ts). This reuses the
channel convention from the channel-separated recording feature
(services/audio.py/diarize.py) for labelling only, never for accuracy: a live
caption is a disposable draft, and unlike diarize_channels_file's output it
is never written to transcripts/diarizations.

Each channel gets its own persistent session on the ASR backend's
/v1/realtime endpoint (see services/diarize.realtime_url) -- audio is
forwarded to that session the moment it arrives off the browser socket, with
no local windowing on our side.

Server VAD is deliberately turned OFF (_transcription_session_update sends
``turn_detection: null``), and this app commits the buffer itself on a fixed
cadence (live_caption_commit_interval_sec) instead. That is not the obvious
choice -- the realtime protocol's own server-side VAD looks like the "right"
way to segment turns -- but it measured badly: with VAD on, nothing is
committed until the backend detects a real pause (its own default is 500ms
of silence), so a person talking continuously without a clean pause produces
*no captions at all* until they stop, which is exactly what shows up as
"big latency until words show up." Attempting to tune that threshold down
(passing extra fields alongside ``turn_detection.type`` in session.update)
was tried against this deployment and made it worse, not better: the extra
fields were silently ignored or -- worse -- the whole VAD detector stopped
firing "speech_stopped" at all for the rest of the session. Manual
``input_audio_buffer.commit`` is rejected outright while VAD is active
("not_implemented"), so there was no middle ground -- it had to be one or
the other. With VAD off, a periodic commit measured ~50-150ms turnaround
per commit even under continuous speech with zero pauses, which is what
actually fixed the reported latency.

This replaced an earlier design that periodically POSTed short windows of
audio to the stateless /v1/audio/transcriptions route. That design could
never benefit from a model's own cache-aware streaming architecture no
matter how it tuned the window/chunk size: every POST was a fresh,
disconnected call with no session id or persistent connection tying one call
to the next, so a model's internal cache was reset on every single call
regardless of chunk size. A persistent /v1/realtime session is the only way
this backend actually exposes cross-chunk continuity, so that is what this
speaks by default (each commit still shares the one open connection/session
per channel, unlike the old per-chunk design's fresh connection every time).

That old per-chunk design is back, though, as a third, explicitly opted-into
backend -- channel_worker_transcriptions, dispatched when
live_caption_backend="transcriptions" -- for a deployment with no realtime
pipeline model and no live-stt gRPC service to point at instead. It still
has no cache-aware-vs-not special case (that conclusion above didn't change:
every call is a fresh, disconnected POST no matter the chunk size, so a
model's cache gets no benefit here regardless), and quality/latency are both
worse than the other two backends -- but it needs nothing beyond the
diarization service's own /v1/audio/transcriptions route (see
services/diarize.transcriptions_url), so it is the one backend that asks
nothing new of the operator.

Confirmed against this deployment: only one model
(live_caption_model's default, "lfm2.5-audio-1.5b-realtime") is registered
as a realtime *pipeline* model. Every other model tried -- including the
batch diarizer's own diarization_model -- gets a /v1/realtime connection
rejected outright with "Model is not a pipeline model" the moment it opens,
regardless of query parameters. A freshly-opened session also defaults to a
full voice-assistant pipeline (spoken replies via server-VAD-triggered
turn_detection.create_response) -- _open_session's session.update switches
it to a passive transcription-only session before any audio is forwarded,
so this never talks back.

Deliberately not a job (see jobs/queue.py). Nothing here is durable or
resumable -- a dropped connection just means the browser reconnects and
captions resume a moment later. There is nothing to recover, unlike an
ingest job that must survive a process restart, so this gets its own
in-memory relay instead of a job type, a jobs row, or any sqlite write.

Besides ``{"type": "caption", ...}`` messages, each channel_worker also
pushes ``{"type": "status", "channel": ..., "state": ...}`` -- idle (no new
audio since the last commit) -> buffering (new audio has arrived) -> calling
(a commit was just sent, transcription in flight) -> idle again -- and
``{"type": "partial", "channel": ..., "text": ...}`` if the backend ever
emits ``conversation.item.input_audio_transcription.delta`` events
mid-commit (not observed against this deployment, but relayed if a future
model/version produces them). Both are purely cosmetic/best-effort, same as
before.

No process-wide concurrency cap on open /v1/realtime sessions or live-stt
gRPC streams (the old design's _ASR_CONCURRENCY, in that form, is gone):
that limit existed to serialize many short-lived *calls* against a backend
observed to hang under concurrent load, which doesn't map cleanly onto a
small number of long-lived *connections* held open for a whole recording.
Concurrent /v1/realtime sessions haven't been load-tested on this
deployment -- if that turns out to be a problem, a cap on total open
sessions (not calls) would need to be reintroduced.

channel_worker_transcriptions is back to exactly the short-lived-calls shape
that limit was built for, so it keeps its own process-wide
_TRANSCRIPTIONS_CONCURRENCY guarding it -- unrelated to (and not shared
with) the other two backends' connection counts.
"""

from __future__ import annotations

import array
import asyncio
import base64
import contextlib
import io
import json
import wave
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import websockets
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from httpx_sse import SSEError, aconnect_sse
from websockets.exceptions import ConnectionClosed

from app.config import effective, get_settings
from app.db import get_conn
from app.logging_config import get_logger
from app.services import users as users_svc
from app.services.diarize import (
    _headers,
    is_live_stt_model,
    realtime_url,
    strip_language_tag,
    transcriptions_url,
)


log = get_logger("live_caption")

router = APIRouter(prefix="/api/live-caption", tags=["live-caption"])

CHANNEL_NAMES = {0: "room", 1: "me"}

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2  # int16 mono

# Below this fraction of int16 full scale (32768), a chunk is treated as
# silence and never sent to channel_worker_transcriptions's ASR call at all
# -- same threshold and reasoning the old per-chunk route had: ~2% of full
# scale is comfortably above a quiet mic's own noise floor and comfortably
# below even a soft speaking voice's peak, so a channel nobody is talking on
# doesn't pay for a round trip every commit_interval_sec for the whole
# recording just to have the model report back "no speech."
SILENCE_PEAK_THRESHOLD = 0.02

# Process-wide, not per-connection: many short-lived
# channel_worker_transcriptions calls hitting a backend that has been
# observed to hang entirely (not just slow down) under concurrent load --
# see this module's docstring. Doesn't apply to channel_worker/
# channel_worker_livestt, which hold a small number of long-lived
# connections open instead of making repeated short calls.
_TRANSCRIPTIONS_CONCURRENCY = asyncio.Semaphore(1)


class _RealtimeSessionError(Exception):
    """A channel's /v1/realtime connection could not be opened or switched
    to a transcription-only session -- e.g. the configured model is not
    registered as a realtime pipeline model on this backend ("Model is not a
    pipeline model", confirmed against this deployment for every model
    except live_caption_model's default). Unlike a mid-session hiccup
    (handled inline in the event-relay loop, which just drops one event and
    keeps going), there is nothing to retry once this happens -- the model
    will reject the next attempt exactly the same way -- so channel_worker
    logs it once and leaves that channel silent for the rest of the
    recording rather than looping forever against a connection that will
    never succeed.
    """


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


async def _send_status(websocket: WebSocket, channel: str, state: str) -> None:
    """Best-effort activity signal for the recorder UI's per-channel dot
    (idle/buffering/calling -- see channel_worker's call sites). Same
    swallow-everything policy as the caption send below it: a dropped status
    update is invisible to the user, and letting it raise would kill
    channel_worker over something that was never essential to begin with.
    """
    try:
        await websocket.send_json({"type": "status", "channel": channel, "state": state})
    except Exception:
        pass


async def _send_partial(websocket: WebSocket, channel: str, text: str) -> None:
    """In-progress transcript for one still-open utterance -- pushed if the
    backend ever emits a transcription delta event before its matching
    ``completed`` commits a real ``{"type": "caption", ...}``. Same
    best-effort swallow policy as _send_status.
    """
    try:
        await websocket.send_json({"type": "partial", "channel": channel, "text": text})
    except Exception:
        pass


def _transcription_session_update(model: str, language: str | None) -> dict:
    """The session.update payload that switches a freshly-opened
    /v1/realtime connection from its default full voice-assistant pipeline
    (spoken replies, turn_detection.create_response=true, a "helpful voice
    assistant" system prompt) into a passive transcription-only session --
    confirmed against this backend that without this, the session stays in
    full-assistant mode and would try to generate spoken responses instead
    of just transcribing what it hears.

    ``turn_detection: None`` turns off the backend's own server-side VAD --
    see this module's docstring for why: it only commits on a real pause,
    which starves continuous speech of any caption at all, and attempts to
    tune its threshold down were silently ignored or broke it outright.
    channel_worker commits the buffer itself on a fixed cadence instead.

    ``language`` is omitted entirely when unset, same reasoning the old
    per-chunk route had: leaves auto-detection on rather than forcing a
    guess.
    """
    transcription: dict[str, str] = {"model": model}
    if language:
        transcription["language"] = language
    return {
        "type": "session.update",
        "session": {
            "type": "transcription",
            "audio": {
                "input": {
                    "turn_detection": None,
                    "transcription": transcription,
                }
            },
        },
    }


@dataclass
class _RelayResult:
    """What one realtime-session event means for channel_worker to relay to
    the browser -- see _handle_realtime_event. Kept as a plain data carrier,
    separate from the websocket sends themselves, so the event-to-message
    mapping is fast and deterministic to test without a real socket.
    """

    next_partial: str = ""
    status: str | None = None
    partial: str | None = None
    caption: str | None = None
    warning: str | None = None


def _handle_realtime_event(event: dict, partial_so_far: str) -> _RelayResult:
    """Pure mapping from one /v1/realtime event to what channel_worker
    should relay -- see this module's docstring for the event shapes.
    Deliberately side-effect-free (no logging, no websocket sends) so this
    is fast and deterministic to test, the same reasoning _transcribe_window
    the old SSE parsing used to get on the per-chunk route.

    No ``input_audio_buffer.speech_started``/``speech_stopped`` handling
    here on purpose: server VAD is turned off (see
    _transcription_session_update), so those events structurally cannot
    fire. buffering/calling status instead comes from channel_worker's own
    commit cadence, not from anything in this mapping.

    An event type this deployment hasn't been observed to send (or a bare
    ``{"type": "error", ...}`` with nothing else this app understands) is
    not an error on its own -- it is simply not relayed, and partial_so_far
    passes through unchanged.
    """
    etype = event.get("type")
    if etype == "conversation.item.input_audio_transcription.delta":
        updated = partial_so_far + (event.get("delta") or "")
        return _RelayResult(next_partial=updated, partial=updated or None)
    if etype == "conversation.item.input_audio_transcription.completed":
        text = (event.get("transcript") or "").strip()
        return _RelayResult(next_partial="", status="idle", caption=text or None)
    if etype == "conversation.item.input_audio_transcription.failed":
        message = (event.get("error") or {}).get("message") or "transcription failed"
        return _RelayResult(next_partial="", status="idle", warning=message)
    if etype == "error":
        message = (event.get("error") or {}).get("message") or "unknown realtime error"
        return _RelayResult(next_partial=partial_so_far, warning=message)
    return _RelayResult(next_partial=partial_so_far)


async def _connect_realtime(url: str, model: str, api_key: str | None, open_timeout: float):
    """Thin wrapper around websockets.connect -- the one seam tests replace
    with a fake connection, so _open_session/channel_worker's own logic
    (handshake, audio forwarding, event relay) is exercised without a real
    socket.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    query = urlencode({"model": model})
    return await websockets.connect(
        f"{url}?{query}", additional_headers=headers, open_timeout=open_timeout
    )


async def _open_session(
    url: str, model: str, api_key: str | None, language: str | None, open_timeout: float
):
    """Open one channel's /v1/realtime connection and switch it to a
    passive transcription-only session. Raises _RealtimeSessionError (or
    lets a websockets/timeout exception through) on any failure -- the
    caller decides what that means for the channel.
    """
    ws = await _connect_realtime(url, model, api_key, open_timeout)
    try:
        created = json.loads(await asyncio.wait_for(ws.recv(), timeout=open_timeout))
        if created.get("type") == "error":
            raise _RealtimeSessionError(
                (created.get("error") or {}).get("message") or "session rejected"
            )
        await ws.send(json.dumps(_transcription_session_update(model, language)))
        updated = json.loads(await asyncio.wait_for(ws.recv(), timeout=open_timeout))
        if updated.get("type") == "error":
            raise _RealtimeSessionError(
                (updated.get("error") or {}).get("message") or "session.update rejected"
            )
    except Exception:
        with contextlib.suppress(Exception):
            await ws.close()
        raise
    return ws


async def channel_worker(
    channel: int,
    websocket: WebSocket,
    queue: asyncio.Queue,
    url: str,
    model: str,
    api_key: str | None,
    language: str | None,
    open_timeout: float,
    commit_interval_sec: float,
) -> None:
    """One channel's whole realtime session, for the life of the recording.

    Three concurrent loops share the one connection: forwarding audio the
    browser has already handed us (via ``queue``, fed by live_caption_ws's
    own receive loop) out to the ASR backend; committing whatever has been
    forwarded since the last commit on a fixed cadence (server VAD is off --
    see this module's docstring for why this app segments turns itself
    instead); and relaying whatever events come back. Cancelling this task
    (see live_caption_ws's teardown) cancels all three -- there is no
    separate stop flag to check.
    """
    name = CHANNEL_NAMES[channel]
    try:
        ws = await _open_session(url, model, api_key, language, open_timeout)
    except Exception as exc:
        log.warning(
            "live caption realtime session for %s could not open: %s: %s",
            name,
            type(exc).__name__,
            exc,
        )
        await _send_status(websocket, name, "idle")
        return

    # Set by forward_audio the moment new audio is forwarded, cleared by
    # periodic_commit right before it commits -- a channel with nothing new
    # since the last tick sends no commit at all, the same "don't pay for a
    # round trip on silence" reasoning the old design's SILENCE_PEAK_THRESHOLD
    # had, just driven by "did anything arrive" rather than a local amplitude
    # check.
    pending_audio = False

    async def forward_audio() -> None:
        nonlocal pending_audio
        while True:
            chunk = await queue.get()
            try:
                await ws.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(chunk).decode("ascii"),
                        }
                    )
                )
            except Exception:
                # The session is gone -- listen_events will hit the same
                # wall and end the whole channel_worker via gather below.
                return
            if not pending_audio:
                pending_audio = True
                await _send_status(websocket, name, "buffering")

    async def periodic_commit() -> None:
        nonlocal pending_audio
        while True:
            await asyncio.sleep(commit_interval_sec)
            if not pending_audio:
                continue
            pending_audio = False
            await _send_status(websocket, name, "calling")
            try:
                await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            except Exception:
                return

    async def listen_events() -> None:
        partial_state = ""
        try:
            async for raw in ws:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                result = _handle_realtime_event(event, partial_state)
                partial_state = result.next_partial
                if result.warning:
                    log.warning("live caption realtime event for %s: %s", name, result.warning)
                if result.status is not None:
                    await _send_status(websocket, name, result.status)
                if result.partial is not None:
                    await _send_partial(websocket, name, result.partial)
                if result.caption is not None:
                    try:
                        await websocket.send_json(
                            {"type": "caption", "channel": name, "text": result.caption}
                        )
                    except Exception:
                        # The browser socket itself is gone -- further sends
                        # will fail the same way, so this one should end
                        # the worker.
                        return
        except ConnectionClosed:
            pass

    try:
        await asyncio.gather(forward_audio(), periodic_commit(), listen_events())
    finally:
        with contextlib.suppress(Exception):
            await ws.close()
        await _send_status(websocket, name, "idle")


async def channel_worker_livestt(
    channel: int,
    websocket: WebSocket,
    queue: asyncio.Queue,
    target_url: str,
    model: str,
    language: str | None,
    commit_interval_sec: float,
) -> None:
    """One channel's live-stt gRPC session, for the life of the recording.

    Connects to live-stt gRPC StreamingASR service, streams PCM16 audio
    chunks from queue, and relays committed text back to the browser
    WebSocket.

    live-stt's TranscriptDelta.text fragments are meant to be *appended*, not
    treated as independently-formatted units -- see asr.proto's doc comment
    on TranscriptDelta: each fragment's own leading space (present or absent)
    is the sole word-boundary signal, e.g. " sen" + "ior man" + "age" ==
    "senior manage" when concatenated with NO separator. An earlier version
    of this function relayed every single delta straight to the browser as
    its own {"type": "caption", ...} message, which broke that contract: the
    frontend (LiveTranscriptPanel.tsx's groupCaptions) treats each caption
    message as one complete, already-correctly-spaced unit and rejoins
    consecutive ones with a literal " " -- exactly right for channel_worker's
    whole-utterance commits above, but wrong for a bare mid-word fragment,
    producing garbled captions like "manager" -> "manag er".
    So: buffer delta text with no separator and only emit a caption message
    on a real utterance boundary -- an EndOfUtterance event (the default
    model's real <EOU>), Final (stream end), or, for a model with no <EOU>
    token at all (e.g. nemotron -- see Ready.supports_turn_detection /
    WARNING_CODE_NO_TURN_DETECTION in asr.proto), a periodic flush on the
    same commit_interval_sec cadence channel_worker uses above, so a
    turn-detection-less model still produces incremental captions instead of
    one giant blob only at Final.
    """
    name = CHANNEL_NAMES[channel]
    try:
        import grpc
        from app.pb.livestt.v1 import asr_pb2, asr_pb2_grpc
    except ImportError as exc:
        log.warning("gRPC stubs or grpcio not available for live-stt (%s)", exc)
        await _send_status(websocket, name, "idle")
        return

    buffer = ""
    buffering = False  # already told the UI "calling" for the in-flight buffer

    async def flush() -> bool:
        """Send whatever's in ``buffer`` as one caption message and clear it.
        Returns False if the browser socket is gone (caller should stop)."""
        nonlocal buffer, buffering
        text = buffer
        buffer = ""
        if not text:
            return True
        try:
            await websocket.send_json({"type": "caption", "channel": name, "text": text})
        except Exception:
            return False
        buffering = False
        await _send_status(websocket, name, "idle")
        return True

    try:
        async with grpc.aio.insecure_channel(target_url) as grpc_channel:
            stub = asr_pb2_grpc.StreamingASRStub(grpc_channel)

            async def request_generator():
                yield asr_pb2.TranscriptionRequest(
                    config=asr_pb2.StreamConfig(
                        call_id=f"live-caption-{name}",
                        encoding=asr_pb2.AUDIO_ENCODING_LINEAR16,
                        sample_rate_hz=16000,
                        language=language or "",
                        model=model,
                        enable_word_timestamps=False,
                    )
                )
                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        break
                    yield asr_pb2.TranscriptionRequest(audio=chunk)

            call = stub.Transcribe(request_generator())

            async def periodic_flush() -> None:
                # Fallback cadence for models with no <EOU> (nemotron) so
                # they still produce incremental captions; harmless no-op
                # for EOU-capable models except covering an unusually long
                # utterance that hasn't hit an <EOU> yet.
                while True:
                    await asyncio.sleep(commit_interval_sec)
                    if not await flush():
                        return

            async def listen_events() -> None:
                nonlocal buffer, buffering
                async for event in call:
                    kind = event.WhichOneof("event")
                    if kind == "ready":
                        await _send_status(websocket, name, "idle")
                    elif kind == "delta":
                        text = event.delta.text or ""
                        if text:
                            buffer += text
                            if not buffering:
                                buffering = True
                                await _send_status(websocket, name, "calling")
                    elif kind == "eou":
                        if not await flush():
                            return
                    elif kind == "final":
                        buffer += event.final.text or ""
                        if not await flush():
                            return
                    elif kind == "warning":
                        log.warning("live-stt warning for %s: %s", name, event.warning.message)
                    elif kind == "recycled":
                        log.info("live-stt recycled worker for %s: %s", name, event.recycled.reason)

            await asyncio.gather(listen_events(), periodic_flush())

    except Exception as exc:
        log.warning(
            "live caption live-stt session for %s failed: %s: %s",
            name,
            type(exc).__name__,
            exc,
        )
    finally:
        await flush()
        await _send_status(websocket, name, "idle")


def _peak_amplitude(pcm: bytes) -> float:
    """Peak absolute sample value over a chunk, 0..1 (int16 full scale is
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
    """Wrap raw PCM16 mono in a WAV container -- /v1/audio/transcriptions
    takes a file upload, unlike /v1/realtime and live-stt's raw frames."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(BYTES_PER_SAMPLE)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(pcm)
    return buf.getvalue()


async def _transcribe_window(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    api_key: str | None,
    pcm: bytes,
    language: str | None,
) -> str:
    """One channel_worker_transcriptions call. Returns the committed text,
    or '' on any failure -- a dropped caption is invisible to the user;
    raising would kill the whole channel over one bad chunk.

    Same SSE wire shape the old per-chunk route used: ``stream=true`` and a
    ``transcript.text.done`` event carrying the final text, terminated by a
    literal ``"[DONE]"`` data line.
    """
    files = {"file": ("chunk.wav", _wav_bytes(pcm), "audio/wav")}
    data = {"model": model, "stream": "true"}
    if language:
        data["language"] = language
    try:
        async with aconnect_sse(
            client, "POST", url, data=data, files=files, headers=_headers(api_key)
        ) as event_source:
            if event_source.response.status_code >= 400:
                log.warning(
                    "live caption transcriptions chunk rejected: %s",
                    event_source.response.status_code,
                )
                return ""
            text = ""
            async for sse in event_source.aiter_sse():
                if sse.data == "[DONE]":
                    break
                try:
                    chunk = json.loads(sse.data)
                except ValueError:
                    continue
                if chunk.get("type") == "transcript.text.done":
                    text = chunk.get("text") or ""
            return strip_language_tag(text)
    except (httpx.HTTPError, SSEError) as exc:
        log.warning(
            "live caption transcriptions call failed: %s: %s", type(exc).__name__, exc
        )
        return ""


async def channel_worker_transcriptions(
    channel: int,
    websocket: WebSocket,
    queue: asyncio.Queue,
    url: str,
    model: str,
    api_key: str | None,
    language: str | None,
    commit_interval_sec: float,
    timeout_sec: float,
) -> None:
    """One channel's periodic /v1/audio/transcriptions relay -- the
    stateless rolling-window design this app used before /v1/realtime (see
    this module's docstring for why that switch happened, and why this is
    now a third, explicitly opted-into backend rather than the default).

    Unlike the old per-chunk route (a shared _ChannelBuffer the websocket
    receive loop wrote into directly, under a lock), audio here arrives off
    this channel's own ``queue`` -- nothing else reads it, so a plain
    ``bytearray`` does the same job as the old lock without needing one,
    the same reasoning channel_worker's own ``pending_audio`` flag relies
    on above.

    One chunking rule for every model, no cache-aware-vs-not special case
    (see this module's docstring for why): accumulate whatever arrives for
    commit_interval_sec, POST it as one call, then start the next chunk
    empty -- non-overlapping, so nothing is ever transcribed twice. A chunk
    under SILENCE_PEAK_THRESHOLD is dropped without a call at all.
    """
    name = CHANNEL_NAMES[channel]
    buf = bytearray()
    pending_audio = False

    async def accumulate() -> None:
        nonlocal pending_audio
        while True:
            chunk = await queue.get()
            buf.extend(chunk)
            if not pending_audio:
                pending_audio = True
                await _send_status(websocket, name, "buffering")

    async def periodic_call(client: httpx.AsyncClient) -> None:
        nonlocal pending_audio
        while True:
            await asyncio.sleep(commit_interval_sec)
            if not buf:
                continue
            pcm = bytes(buf)
            buf.clear()
            pending_audio = False
            if _peak_amplitude(pcm) < SILENCE_PEAK_THRESHOLD:
                await _send_status(websocket, name, "idle")
                continue
            await _send_status(websocket, name, "calling")
            async with _TRANSCRIPTIONS_CONCURRENCY:
                text = await _transcribe_window(client, url, model, api_key, pcm, language)
            if text:
                try:
                    await websocket.send_json({"type": "caption", "channel": name, "text": text})
                except Exception:
                    return
            await _send_status(websocket, name, "idle")

    try:
        async with httpx.AsyncClient(timeout=timeout_sec) as client:
            await asyncio.gather(accumulate(), periodic_call(client))
    except Exception as exc:
        log.warning(
            "live caption transcriptions session for %s failed: %s: %s",
            name,
            type(exc).__name__,
            exc,
        )
    finally:
        await _send_status(websocket, name, "idle")


@router.websocket("/ws")
async def live_caption_ws(websocket: WebSocket) -> None:
    user = await _authenticate(websocket)
    if user is None:
        await websocket.close(code=4401)
        return

    with get_conn() as conn:
        enabled = effective(conn, "live_caption_enabled")
        model = effective(conn, "live_caption_model")
        open_timeout = effective(conn, "live_caption_timeout_sec")
        commit_interval_sec = effective(conn, "live_caption_commit_interval_sec")
        default_language = effective(conn, "live_caption_language")
        api_key = effective(conn, "diarization_api_key")
        diarization_url = effective(conn, "diarization_url")
        live_stt_url = effective(conn, "live_stt_url")
        backend = effective(conn, "live_caption_backend")

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

    # is_live_stt_model(model) always wins to the gRPC backend regardless of
    # the live_caption_backend setting -- a safety net for a model that is
    # obviously live-stt-shaped even if the setting says otherwise. Once that
    # heuristic is out of the way, the setting picks between the other two:
    # "transcriptions" for the reinstated stateless per-chunk POST backend
    # (see channel_worker_transcriptions), everything else (the default,
    # "realtime") for the persistent /v1/realtime session.
    if backend == "live_stt" or is_live_stt_model(model):
        resolved_backend = "live_stt"
    elif backend == "transcriptions":
        resolved_backend = "transcriptions"
    else:
        resolved_backend = "realtime"

    await websocket.accept()
    await websocket.send_json({"type": "info", "model": model, "backend": resolved_backend})

    queues: dict[int, asyncio.Queue] = {0: asyncio.Queue(), 1: asyncio.Queue()}

    if resolved_backend == "live_stt":
        workers = [
            asyncio.create_task(
                channel_worker_livestt(
                    ch,
                    websocket,
                    queues[ch],
                    live_stt_url,
                    model,
                    language or None,
                    commit_interval_sec,
                )
            )
            for ch in queues
        ]

    elif resolved_backend == "transcriptions":
        url = transcriptions_url(diarization_url)
        workers = [
            asyncio.create_task(
                channel_worker_transcriptions(
                    ch,
                    websocket,
                    queues[ch],
                    url,
                    model,
                    api_key or None,
                    language or None,
                    commit_interval_sec,
                    open_timeout,
                )
            )
            for ch in queues
        ]

    else:
        url = realtime_url(diarization_url)
        workers = [
            asyncio.create_task(
                channel_worker(
                    ch,
                    websocket,
                    queues[ch],
                    url,
                    model,
                    api_key or None,
                    language or None,
                    open_timeout,
                    commit_interval_sec,
                )
            )
            for ch in queues
        ]

    try:
        while True:
            message = await websocket.receive_bytes()
            if not message:
                continue
            channel = message[0]
            queue = queues.get(channel)
            if queue is None:
                continue
            payload = message[1:]
            if payload:
                queue.put_nowait(payload)
    except WebSocketDisconnect:
        pass
    finally:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

