"""The live-caption websocket: auth, the feature-flag gate, and the
/v1/realtime session relay.

Server VAD is off (see live_caption.py's module docstring for why: it only
commits on a real pause, which starves continuous speech of any caption at
all) -- this app commits each channel's buffer itself on a fixed cadence
(live_caption_commit_interval_sec) instead. Tests that exercise the commit
cadence pass a tiny interval so they run fast rather than waiting out the
real default.

Nothing here opens a real socket: _connect_realtime is the one seam every
test monkeypatches, so the suite stays offline-safe the same way respx keeps
the httpx-based routes offline-safe elsewhere.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
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
    def test_rejects_when_disabled_by_default(self, admin_client):
        """live_caption_enabled defaults False -- see RUNTIME_KEYS and the
        Settings field, both deliberately off out of the box."""
        with pytest.raises(WebSocketDisconnect) as exc:
            with admin_client.websocket_connect("/api/live-caption/ws"):
                pass
        assert exc.value.code == 4404

    def test_accepts_once_enabled(self, admin_client, conn, monkeypatch):
        conn.execute(
            "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) "
            "VALUES ('live_caption_enabled', 'true', 'bool', 0, ?)",
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
            assert info["is_realtime"] is True


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
                "is_realtime": True,
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
