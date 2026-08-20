"""The live-caption websocket: auth, the feature-flag gate, and the
/v1/realtime session relay.

Server VAD is off (see live_caption.py's module docstring for why: it only
commits on a real pause, which starves continuous speech of any caption at
all) -- this app commits each channel's buffer itself on a fixed cadence
(live_caption_commit_interval_sec) instead. Tests that exercise the commit
cadence pass a tiny interval so they run fast rather than waiting out the
real default.

Nothing here opens a real socket: _connect_realtime is the one seam every
realtime/live-stt test monkeypatches, so the suite stays offline-safe.
channel_worker_transcriptions is the exception -- it is a plain httpx POST,
so its tests use respx like every other httpx-based route in this suite.
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
import respx
from starlette.websockets import WebSocketDisconnect

from app.db import utcnow
from app.routers import live_caption


class FakeRealtimeConnection:
    """A fake /v1/realtime connection. `replies` is consumed in order by
    both `recv()` (the two handshake replies _open_session reads) and the
    `async for` event loop that follows -- same as a real websockets
    connection, where `recv()` and iteration share one inbound message
    stream, not two independent ones. `sent` records every outbound frame
    this app pushed (the session.update handshake and forwarded audio
    alike), so a test can assert on what was actually sent without needing
    a real socket.
    """

    def __init__(self, replies):
        self._replies = list(replies)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if not self._replies:
            raise RuntimeError("FakeRealtimeConnection: no more replies queued")
        return self._replies.pop(0)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if not self._replies:
            raise StopAsyncIteration
        return self._replies.pop(0)


class FakeBrowserSocket:
    """Stands in for the browser-facing WebSocket inside channel_worker.
    `send_json` pushes onto an asyncio.Queue rather than a plain list, so a
    test can `await` the next message instead of polling/sleeping for it --
    this relay has no wall-clock cadence to wait out any more.
    """

    def __init__(self):
        self.sent: asyncio.Queue = asyncio.Queue()

    async def send_json(self, payload: dict) -> None:
        await self.sent.put(payload)

    async def next(self, timeout: float = 2.0) -> dict:
        return await asyncio.wait_for(self.sent.get(), timeout=timeout)


SESSION_CREATED = json.dumps({"type": "session.created"})
SESSION_UPDATED = json.dumps({"type": "session.updated"})
MODEL_NOT_PIPELINE_ERROR = json.dumps(
    {"type": "error", "error": {"message": "Model is not a pipeline model", "code": "invalid_model"}}
)


class TestAuth:
    def test_rejects_a_connection_with_no_session(self, client):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/api/live-caption/ws"):
                pass
        assert exc.value.code == 4401

    def test_rejects_a_bogus_cookie(self, client):
        client.cookies.set("mmn_session", "not-a-real-session-token")
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/api/live-caption/ws"):
                pass
        assert exc.value.code == 4401


class TestFeatureFlag:
    def test_rejects_when_disabled_by_default(self, admin_client, monkeypatch):
        """live_caption_enabled defaults False -- see RUNTIME_KEYS and the
        Settings field, both deliberately off out of the box."""
        from app.config import get_settings
        monkeypatch.setenv("MMN_LIVE_CAPTION_ENABLED", "false")
        get_settings.cache_clear()
        try:
            with pytest.raises(WebSocketDisconnect) as exc:
                with admin_client.websocket_connect("/api/live-caption/ws"):
                    pass
            assert exc.value.code == 4404
        finally:
            get_settings.cache_clear()



    def test_accepts_once_enabled(self, admin_client, conn, monkeypatch):
        conn.execute(
            "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) "
            "VALUES ('live_caption_enabled', 'true', 'bool', 0, ?)",
            (utcnow(),),
        )
        # live_caption_backend defaults to "live_stt" (see config.py), which
        # would dispatch to channel_worker_livestt instead of the
        # _connect_realtime seam this test fakes below -- pin it to
        # "realtime" explicitly so this exercises the path it's named for.
        conn.execute(
            "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) "
            "VALUES ('live_caption_backend', 'realtime', 'str', 0, ?)",
            (utcnow(),),
        )
        conn.commit()

        # Fails fast rather than attempting a real network connection --
        # this test only cares that the websocket handshake itself
        # succeeds, not what a channel's session does with it.
        async def fake_connect(url, model, api_key, open_timeout):
            raise RuntimeError("no ASR backend in tests")

        monkeypatch.setattr(live_caption, "_connect_realtime", fake_connect)

        with admin_client.websocket_connect("/api/live-caption/ws") as ws:
            info = ws.receive_json()
            assert info["type"] == "info"
            assert info["backend"] == "realtime"


class TestTranscriptionSessionUpdate:
    def test_omits_language_when_unset(self):
        payload = live_caption._transcription_session_update("some-model", None)
        assert payload == {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "turn_detection": None,
                        "transcription": {"model": "some-model"},
                    }
                },
            },
        }

    def test_turn_detection_is_off(self):
        """Deliberate: server VAD only commits on a real pause, which
        starves continuous speech of any caption at all -- see this
        module's docstring. channel_worker commits on its own cadence
        instead."""
        payload = live_caption._transcription_session_update("some-model", None)
        assert payload["session"]["audio"]["input"]["turn_detection"] is None

    def test_includes_language_when_set(self):
        payload = live_caption._transcription_session_update("some-model", "en")
        assert payload["session"]["audio"]["input"]["transcription"] == {
            "model": "some-model",
            "language": "en",
        }

    def test_empty_string_language_is_treated_as_unset(self):
        """The recorder UI sends '' for an explicit Auto-detect pick (see
        live_caption_ws's own language-param handling) -- that must not
        turn into a literal empty language field the backend might choke
        on the same way it did on an English *name* on the old route."""
        payload = live_caption._transcription_session_update("some-model", "")
        assert "language" not in payload["session"]["audio"]["input"]["transcription"]


class TestHandleRealtimeEvent:
    """_handle_realtime_event's mapping from one realtime-session event to
    what channel_worker should relay -- deliberately pure and side-effect
    free, so this is exercised directly without a socket, the same
    reasoning _transcribe_window's SSE parsing used to get on the old
    per-chunk route."""

    def test_speech_started_and_stopped_are_no_ops(self):
        """Server VAD is off (see _transcription_session_update), so these
        structurally cannot fire against this deployment -- but if a future
        config or model variant ever sent them anyway, they must not be
        mistaken for the buffering/calling signal channel_worker's own
        commit cadence already owns."""
        for etype in (
            "input_audio_buffer.speech_started",
            "input_audio_buffer.speech_stopped",
        ):
            result = live_caption._handle_realtime_event({"type": etype}, partial_so_far="carry me")
            assert result == live_caption._RelayResult(next_partial="carry me")

    def test_delta_accumulates_across_events(self):
        first = live_caption._handle_realtime_event(
            {"type": "conversation.item.input_audio_transcription.delta", "delta": "Hel"},
            partial_so_far="",
        )
        assert first.partial == "Hel"
        assert first.next_partial == "Hel"

        second = live_caption._handle_realtime_event(
            {"type": "conversation.item.input_audio_transcription.delta", "delta": "lo"},
            partial_so_far=first.next_partial,
        )
        assert second.partial == "Hello"
        assert second.next_partial == "Hello"

    def test_completed_commits_a_stripped_caption_and_resets_partial(self):
        result = live_caption._handle_realtime_event(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "  hello there  ",
            },
            partial_so_far="hello",
        )
        assert result.caption == "hello there"
        assert result.status == "idle"
        assert result.next_partial == ""

    def test_completed_with_blank_transcript_does_not_commit_a_caption(self):
        """A dropped caption is invisible to the user; sending an empty one
        would not be -- it would render as a blank line."""
        result = live_caption._handle_realtime_event(
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "   "},
            partial_so_far="",
        )
        assert result.caption is None
        assert result.status == "idle"

    def test_failed_surfaces_a_warning_and_resets_to_idle(self):
        result = live_caption._handle_realtime_event(
            {
                "type": "conversation.item.input_audio_transcription.failed",
                "error": {"message": "boom"},
            },
            partial_so_far="partial text",
        )
        assert result.warning == "boom"
        assert result.status == "idle"
        assert result.caption is None
        assert result.next_partial == ""

    def test_bare_error_event_surfaces_a_warning_without_touching_status(self):
        result = live_caption._handle_realtime_event(
            {"type": "error", "error": {"message": "unknown_thing"}}, partial_so_far="carry me"
        )
        assert result.warning == "unknown_thing"
        assert result.status is None
        # Not reset -- a bare mid-session error is not a transcription
        # outcome, unlike .failed above.
        assert result.next_partial == "carry me"

    def test_an_unrecognised_event_type_is_a_no_op(self):
        result = live_caption._handle_realtime_event(
            {"type": "some.future.event"}, partial_so_far="carry me"
        )
        assert result == live_caption._RelayResult(next_partial="carry me")


class TestSendStatus:
    @pytest.mark.asyncio
    async def test_sends_the_expected_shape(self):
        browser = FakeBrowserSocket()
        await live_caption._send_status(browser, "room", "calling")
        assert await browser.next() == {"type": "status", "channel": "room", "state": "calling"}

    @pytest.mark.asyncio
    async def test_a_dead_socket_does_not_raise(self):
        class DeadWebSocket:
            async def send_json(self, payload):
                raise RuntimeError("socket is gone")

        await live_caption._send_status(DeadWebSocket(), "me", "idle")


class TestSendPartial:
    @pytest.mark.asyncio
    async def test_sends_the_expected_shape(self):
        browser = FakeBrowserSocket()
        await live_caption._send_partial(browser, "me", "hel")
        assert await browser.next() == {"type": "partial", "channel": "me", "text": "hel"}


class TestOpenSession:
    @pytest.mark.asyncio
    async def test_happy_path_sends_the_transcription_session_update(self, monkeypatch):
        conn = FakeRealtimeConnection([SESSION_CREATED, SESSION_UPDATED])
        monkeypatch.setattr(live_caption, "_connect_realtime", _fake_connect(conn))

        result = await live_caption._open_session(
            "ws://asr.test/v1/realtime", "some-model", "key", "en", 5
        )

        assert result is conn
        assert conn.closed is False
        assert json.loads(conn.sent[0]) == live_caption._transcription_session_update(
            "some-model", "en"
        )

    @pytest.mark.asyncio
    async def test_a_rejected_model_raises_and_closes_the_connection(self, monkeypatch):
        conn = FakeRealtimeConnection([MODEL_NOT_PIPELINE_ERROR])
        monkeypatch.setattr(live_caption, "_connect_realtime", _fake_connect(conn))

        with pytest.raises(live_caption._RealtimeSessionError, match="pipeline model"):
            await live_caption._open_session("ws://asr.test/v1/realtime", "bad-model", None, None, 5)

        assert conn.closed is True
        # Never got as far as sending session.update.
        assert conn.sent == []

    @pytest.mark.asyncio
    async def test_a_rejected_session_update_raises_and_closes_the_connection(self, monkeypatch):
        conn = FakeRealtimeConnection([SESSION_CREATED, MODEL_NOT_PIPELINE_ERROR])
        monkeypatch.setattr(live_caption, "_connect_realtime", _fake_connect(conn))

        with pytest.raises(live_caption._RealtimeSessionError):
            await live_caption._open_session("ws://asr.test/v1/realtime", "some-model", None, None, 5)

        assert conn.closed is True
        assert len(conn.sent) == 1


def _fake_connect(conn: FakeRealtimeConnection):
    async def fake_connect(url, model, api_key, open_timeout):
        return conn

    return fake_connect


class TestChannelWorker:
    """channel_worker's own commit cadence is what paces captions now (see
    the module docstring for why server VAD is off) -- tests pass a tiny
    commit_interval_sec so they run fast rather than waiting out the real
    default."""

    @pytest.mark.asyncio
    async def test_relays_a_committed_utterance_and_forwards_queued_audio(self, monkeypatch):
        conn = FakeRealtimeConnection(
            [
                SESSION_CREATED,
                SESSION_UPDATED,
                json.dumps(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "transcript": "hello there",
                    }
                ),
            ]
        )
        monkeypatch.setattr(live_caption, "_connect_realtime", _fake_connect(conn))

        browser = FakeBrowserSocket()
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(b"\x01\x02\x03")

        task = asyncio.create_task(
            live_caption.channel_worker(
                0, browser, queue, "ws://asr.test/v1/realtime", "some-model", None, None, 5, 0.05
            )
        )
        try:
            # forward_audio (buffering) and periodic_commit (calling) fire
            # from two independent coroutines against a fake connection that
            # doesn't correlate replies to requests, so their relative order
            # here isn't meaningful -- only that all three status states and
            # the caption itself all arrive.
            messages = [await browser.next() for _ in range(4)]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        statuses = {m["state"] for m in messages if m["type"] == "status"}
        captions = [m for m in messages if m["type"] == "caption"]
        assert statuses == {"buffering", "calling", "idle"}
        assert captions == [{"type": "caption", "channel": "room", "text": "hello there"}]

        assert conn.closed is True
        sent = [json.loads(s) for s in conn.sent]
        # sent[0] is _open_session's own session.update handshake -- what
        # matters here is that an append (with our audio) and a commit both
        # went out on top of it.
        appends = [s for s in sent if s["type"] == "input_audio_buffer.append"]
        assert len(appends) == 1
        assert base64.b64decode(appends[0]["audio"]) == b"\x01\x02\x03"
        assert any(s["type"] == "input_audio_buffer.commit" for s in sent)

    @pytest.mark.asyncio
    async def test_a_channel_with_no_new_audio_never_commits(self, monkeypatch):
        """pending_audio only becomes true once something is actually
        forwarded -- an idle channel (nothing ever queued) must not pay for
        a round trip on silence, the same reasoning the old design's
        SILENCE_PEAK_THRESHOLD had, just driven by "did anything arrive"
        rather than a local amplitude check."""
        conn = FakeRealtimeConnection([SESSION_CREATED, SESSION_UPDATED])
        monkeypatch.setattr(live_caption, "_connect_realtime", _fake_connect(conn))

        browser = FakeBrowserSocket()
        queue: asyncio.Queue = asyncio.Queue()

        task = asyncio.create_task(
            live_caption.channel_worker(
                0, browser, queue, "ws://asr.test/v1/realtime", "some-model", None, None, 5, 0.05
            )
        )
        try:
            # Give periodic_commit several ticks' worth of time to prove it
            # stays quiet, not just that it hasn't fired *yet*.
            await asyncio.sleep(0.2)
            assert browser.sent.empty()
            # The handshake's own session.update is expected -- nothing
            # beyond it (no append, no commit) for a channel nothing was
            # ever queued on.
            sent_types = [json.loads(s)["type"] for s in conn.sent]
            assert sent_types == ["session.update"]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_a_model_rejected_at_open_leaves_the_channel_idle_and_returns(self, monkeypatch):
        """No session to poll and nothing left to retry -- see
        _RealtimeSessionError's doc comment -- so channel_worker must end on
        its own here, not hang waiting on a connection that will never
        exist."""
        conn = FakeRealtimeConnection([MODEL_NOT_PIPELINE_ERROR])
        monkeypatch.setattr(live_caption, "_connect_realtime", _fake_connect(conn))

        browser = FakeBrowserSocket()
        queue: asyncio.Queue = asyncio.Queue()

        await asyncio.wait_for(
            live_caption.channel_worker(
                1, browser, queue, "ws://asr.test/v1/realtime", "bad-model", None, None, 5, 0.05
            ),
            timeout=2,
        )

        assert await browser.next() == {"type": "status", "channel": "me", "state": "idle"}
        assert browser.sent.empty()


class TestLiveCaptionWsRealtimeRelay:
    """A full round trip through the real websocket endpoint, given a faked
    /v1/realtime connection. The only wall-clock dependency left is
    periodic_commit's cadence, overridden to a tiny interval here rather
    than waiting out the real default."""

    def test_relays_captions_from_both_channels(self, admin_client, conn, monkeypatch):
        conn.execute(
            "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) "
            "VALUES ('live_caption_enabled', 'true', 'bool', 0, ?)",
            (utcnow(),),
        )
        # Real default is 2s -- a tiny override here so the test doesn't
        # have to wait out periodic_commit's real cadence.
        conn.execute(
            "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) "
            "VALUES ('live_caption_commit_interval_sec', '0.05', 'float', 0, ?)",
            (utcnow(),),
        )
        # live_caption_backend defaults to "live_stt" (see config.py), which
        # would dispatch to channel_worker_livestt instead of the
        # _connect_realtime seam this test fakes below -- pin it to
        # "realtime" explicitly so this exercises the path it's named for.
        conn.execute(
            "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) "
            "VALUES ('live_caption_backend', 'realtime', 'str', 0, ?)",
            (utcnow(),),
        )
        conn.commit()

        script = [
            SESSION_CREATED,
            SESSION_UPDATED,
            json.dumps(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "transcript": "hi there",
                }
            ),
        ]

        # Each channel opens its own connection -- a fresh fake per call, so
        # the two channels' event streams never share (or contend over) one
        # FakeRealtimeConnection's internal reply list.
        async def fake_connect(url, model, api_key, open_timeout):
            return FakeRealtimeConnection(list(script))

        monkeypatch.setattr(live_caption, "_connect_realtime", fake_connect)

        with admin_client.websocket_connect("/api/live-caption/ws") as ws:
            info = ws.receive_json()
            assert info == {
                "type": "info",
                "model": "lfm2.5-audio-1.5b-realtime",
                "backend": "realtime",
            }

            ws.send_bytes(b"\x00" + b"\x01\x02")  # channel 0 ("room")
            ws.send_bytes(b"\x01" + b"\x01\x02")  # channel 1 ("me")

            captions: dict[str, str] = {}
            # 4 messages per channel (buffering, calling, idle, caption).
            for _ in range(8):
                msg = ws.receive_json()
                if msg["type"] == "caption":
                    captions[msg["channel"]] = msg["text"]

        assert captions == {"room": "hi there", "me": "hi there"}


class TestIsLiveSttModel:
    def test_identifies_live_stt_models(self):
        assert live_caption.is_live_stt_model("realtime_eou_120m-v1") is True
        assert live_caption.is_live_stt_model("nemotron-3.5-asr-streaming-0.6b") is True
        assert live_caption.is_live_stt_model("lfm2.5-audio-1.5b-realtime") is False
        assert live_caption.is_live_stt_model("") is False


class TestChannelWorkerLiveSTT:
    @pytest.mark.asyncio
    async def test_relays_delta_events_as_captions(self, monkeypatch):
        from app.pb.livestt.v1 import asr_pb2

        class FakeGrpcCall:
            def __init__(self, events):
                self._events = events

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._events:
                    raise StopAsyncIteration
                return self._events.pop(0)

        class FakeGrpcStub:
            def __init__(self, channel):
                pass

            def Transcribe(self, request_iterator):
                events = [
                    asr_pb2.TranscriptionEvent(ready=asr_pb2.Ready(model="realtime_eou_120m-v1")),
                    asr_pb2.TranscriptionEvent(
                        delta=asr_pb2.TranscriptDelta(text="hello from live stt")
                    ),
                    # Deltas only buffer (see channel_worker_livestt's
                    # docstring on why -- fragments must be concatenated with
                    # no separator, not relayed as independent captions); an
                    # EndOfUtterance is the real flush trigger.
                    asr_pb2.TranscriptionEvent(eou=asr_pb2.EndOfUtterance(at_sec=1.0)),
                ]
                return FakeGrpcCall(events)

        class FakeGrpcChannel:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        def fake_insecure_channel(target):
            return FakeGrpcChannel()

        import grpc
        monkeypatch.setattr(grpc.aio, "insecure_channel", fake_insecure_channel)
        monkeypatch.setattr("app.pb.livestt.v1.asr_pb2_grpc.StreamingASRStub", FakeGrpcStub)

        browser = FakeBrowserSocket()
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(b"\x01\x02\x03")

        task = asyncio.create_task(
            live_caption.channel_worker_livestt(
                0, browser, queue, "localhost:4030", "realtime_eou_120m-v1", None, 30.0
            )
        )
        try:
            messages = [await browser.next() for _ in range(4)]
            statuses = [m["state"] for m in messages if m["type"] == "status"]
            captions = [m for m in messages if m["type"] == "caption"]
            assert captions == [{"type": "caption", "channel": "room", "text": "hello from live stt"}]
            assert "calling" in statuses
            assert "idle" in statuses
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


TRANSCRIPTIONS_URL = "http://asr.test/v1/audio/transcriptions"


def _sse_transcript_body(text: str) -> bytes:
    """One SSE frame in the wire shape _transcribe_window expects, same
    shape the old per-chunk route (and chat's LLM streaming) used: a
    ``transcript.text.done`` data line carrying the final text, terminated
    by a literal ``data: [DONE]``."""
    frame = {"type": "transcript.text.done", "text": text}
    return f"data: {json.dumps(frame)}\n\ndata: [DONE]\n\n".encode()


def _sse_transcript_response(text: str) -> httpx.Response:
    return httpx.Response(
        200, content=_sse_transcript_body(text), headers={"content-type": "text/event-stream"}
    )


# 4000 Hz square-ish wave samples, comfortably above SILENCE_PEAK_THRESHOLD's
# 2% of full scale -- a stand-in for "someone is speaking."
LOUD_PCM = (b"\x00\x40" + b"\x00\xc0") * 100
SILENT_PCM = b"\x00\x00" * 200


class TestPeakAmplitude:
    def test_silence_is_below_threshold(self):
        assert live_caption._peak_amplitude(SILENT_PCM) < live_caption.SILENCE_PEAK_THRESHOLD

    def test_loud_audio_is_above_threshold(self):
        assert live_caption._peak_amplitude(LOUD_PCM) > live_caption.SILENCE_PEAK_THRESHOLD

    def test_empty_input_is_zero(self):
        assert live_caption._peak_amplitude(b"") == 0.0

    def test_a_torn_trailing_byte_is_dropped_not_raised(self):
        # An odd-length buffer is a cheap gate's problem to ignore, not a
        # decoder's problem to crash on.
        live_caption._peak_amplitude(LOUD_PCM + b"\x01")


class TestWavBytes:
    def test_wraps_pcm_in_a_wav_container(self):
        import wave
        from io import BytesIO

        wav = live_caption._wav_bytes(LOUD_PCM)
        with wave.open(BytesIO(wav), "rb") as wav_file:
            assert wav_file.getnchannels() == 1
            assert wav_file.getsampwidth() == live_caption.BYTES_PER_SAMPLE
            assert wav_file.getframerate() == live_caption.SAMPLE_RATE
            assert wav_file.readframes(wav_file.getnframes()) == LOUD_PCM


class TestTranscribeWindow:
    @pytest.mark.asyncio
    @respx.mock
    async def test_returns_the_committed_text(self):
        respx.post(TRANSCRIPTIONS_URL).mock(return_value=_sse_transcript_response("hello there"))
        async with httpx.AsyncClient() as client:
            text = await live_caption._transcribe_window(
                client, TRANSCRIPTIONS_URL, "some-model", None, LOUD_PCM, None
            )
        assert text == "hello there"

    @pytest.mark.asyncio
    @respx.mock
    async def test_strips_a_stray_language_tag(self):
        respx.post(TRANSCRIPTIONS_URL).mock(
            return_value=_sse_transcript_response("hello there <en-US>")
        )
        async with httpx.AsyncClient() as client:
            text = await live_caption._transcribe_window(
                client, TRANSCRIPTIONS_URL, "some-model", None, LOUD_PCM, None
            )
        assert text == "hello there"

    @pytest.mark.asyncio
    @respx.mock
    async def test_a_rejected_chunk_returns_empty_string(self):
        respx.post(TRANSCRIPTIONS_URL).mock(return_value=httpx.Response(500, text="boom"))
        async with httpx.AsyncClient() as client:
            text = await live_caption._transcribe_window(
                client, TRANSCRIPTIONS_URL, "some-model", None, LOUD_PCM, None
            )
        assert text == ""

    @pytest.mark.asyncio
    @respx.mock
    async def test_a_connection_error_returns_empty_string_rather_than_raising(self):
        """A dropped chunk is invisible to the user; raising would kill the
        whole channel over one bad call -- same reasoning channel_worker's
        own send-failure handling uses."""
        respx.post(TRANSCRIPTIONS_URL).mock(side_effect=httpx.ConnectError("refused"))
        async with httpx.AsyncClient() as client:
            text = await live_caption._transcribe_window(
                client, TRANSCRIPTIONS_URL, "some-model", None, LOUD_PCM, None
            )
        assert text == ""

    @pytest.mark.asyncio
    @respx.mock
    async def test_sends_language_only_when_set(self):
        route = respx.post(TRANSCRIPTIONS_URL).mock(
            return_value=_sse_transcript_response("hi")
        )
        async with httpx.AsyncClient() as client:
            await live_caption._transcribe_window(
                client, TRANSCRIPTIONS_URL, "some-model", None, LOUD_PCM, "en"
            )
        # Decoded with "replace" rather than plain utf-8: the multipart body
        # also carries the raw (non-utf-8) wav file field, and this only
        # cares about the text-form "language" field alongside it.
        sent = route.calls.last.request.content.decode("utf-8", errors="replace")
        assert 'name="language"' in sent
        assert "en" in sent


class TestChannelWorkerTranscriptions:
    """channel_worker_transcriptions's own commit cadence paces captions the
    same way channel_worker's does -- tests pass a tiny commit_interval_sec
    rather than waiting out a real one."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_relays_a_committed_chunk(self):
        respx.post(TRANSCRIPTIONS_URL).mock(return_value=_sse_transcript_response("hi there"))

        browser = FakeBrowserSocket()
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(LOUD_PCM)

        task = asyncio.create_task(
            live_caption.channel_worker_transcriptions(
                0, browser, queue, TRANSCRIPTIONS_URL, "some-model", None, None, 0.05, 5
            )
        )
        try:
            messages = [await browser.next() for _ in range(4)]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        statuses = {m["state"] for m in messages if m["type"] == "status"}
        captions = [m for m in messages if m["type"] == "caption"]
        assert statuses == {"buffering", "calling", "idle"}
        assert captions == [{"type": "caption", "channel": "room", "text": "hi there"}]

    @pytest.mark.asyncio
    @respx.mock
    async def test_a_silent_chunk_is_dropped_without_a_call(self):
        route = respx.post(TRANSCRIPTIONS_URL).mock(return_value=_sse_transcript_response("hi"))

        browser = FakeBrowserSocket()
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(SILENT_PCM)

        task = asyncio.create_task(
            live_caption.channel_worker_transcriptions(
                0, browser, queue, TRANSCRIPTIONS_URL, "some-model", None, None, 0.05, 5
            )
        )
        try:
            # Give periodic_call several ticks' worth of time to prove it
            # stays quiet, not just that it hasn't fired *yet*.
            await asyncio.sleep(0.2)
            assert route.call_count == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    @respx.mock
    async def test_a_channel_with_no_new_audio_never_calls(self):
        route = respx.post(TRANSCRIPTIONS_URL).mock(return_value=_sse_transcript_response("hi"))

        browser = FakeBrowserSocket()
        queue: asyncio.Queue = asyncio.Queue()

        task = asyncio.create_task(
            live_caption.channel_worker_transcriptions(
                0, browser, queue, TRANSCRIPTIONS_URL, "some-model", None, None, 0.05, 5
            )
        )
        try:
            await asyncio.sleep(0.2)
            assert route.call_count == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class TestLiveCaptionWsTranscriptionsRelay:
    """A full round trip through the real websocket endpoint with
    live_caption_backend="transcriptions", given a respx-faked
    /v1/audio/transcriptions."""

    @respx.mock
    def test_relays_captions_from_both_channels(self, admin_client, conn):
        conn.execute(
            "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) "
            "VALUES ('live_caption_enabled', 'true', 'bool', 0, ?)",
            (utcnow(),),
        )
        conn.execute(
            "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) "
            "VALUES ('live_caption_backend', 'transcriptions', 'str', 0, ?)",
            (utcnow(),),
        )
        conn.execute(
            "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) "
            "VALUES ('live_caption_commit_interval_sec', '0.05', 'float', 0, ?)",
            (utcnow(),),
        )
        # Set explicitly rather than relying on diarization_url's class
        # default: a real deployment's .env overrides that default to a
        # live service address, which would make this test a real network
        # call instead of an offline respx-mocked one (see test_diarize.py
        # for the same convention).
        conn.execute(
            "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) "
            "VALUES ('diarization_url', 'http://diarizer.test/v1/audio/diarization', 'str', 0, ?)",
            (utcnow(),),
        )
        conn.commit()

        # Derived from diarization_url above via transcriptions_url -- see
        # services/diarize.transcriptions_url.
        url = "http://diarizer.test/v1/audio/transcriptions"
        respx.post(url).mock(return_value=_sse_transcript_response("hi there"))

        with admin_client.websocket_connect("/api/live-caption/ws") as ws:
            info = ws.receive_json()
            assert info["type"] == "info"
            assert info["backend"] == "transcriptions"

            ws.send_bytes(b"\x00" + LOUD_PCM)  # channel 0 ("room")
            ws.send_bytes(b"\x01" + LOUD_PCM)  # channel 1 ("me")

            captions: dict[str, str] = {}
            # 4 messages per channel (buffering, calling, idle, caption).
            for _ in range(8):
                msg = ws.receive_json()
                if msg["type"] == "caption":
                    captions[msg["channel"]] = msg["text"]

        assert captions == {"room": "hi there", "me": "hi there"}


