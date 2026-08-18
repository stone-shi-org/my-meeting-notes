"""The live-caption websocket: auth and the feature-flag gate.

The relay's actual transcription plumbing (windowing, the SSE consumption, the
me/room labelling) is exercised where it is pure and fast to test --
app/services/diarize.py's transcribe_sync and the channel-merge helpers in
test_diarize.py. This file covers what is specific to the socket itself:
nothing gets in without a session, and nothing gets in while the feature is
off, both fast and deterministic without needing to wait out a real
window/interval cycle.
"""

from __future__ import annotations

import array
import asyncio

import httpx
import pytest
import respx
from starlette.websockets import WebSocketDisconnect

from app.db import utcnow
from app.routers import live_caption


def _pcm(*values: int) -> bytes:
    """int16 samples -> raw little-endian PCM bytes, matching what the
    browser actually sends (see useLiveCaption.ts)."""
    return array.array("h", values).tobytes()


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

    def test_accepts_once_enabled(self, admin_client, conn):
        conn.execute(
            "INSERT INTO app_settings (key, value, value_type, is_secret, updated_at) "
            "VALUES ('live_caption_enabled', 'true', 'bool', 0, ?)",
            (utcnow(),),
        )
        conn.commit()
        # No assertion beyond "the handshake completes" -- actually exercising
        # a caption round trip needs a real window/interval wait, which is
        # covered by unit tests on the pieces (transcribe_sync, the merge
        # helpers) rather than a slow, timing-sensitive test here.
        with admin_client.websocket_connect("/api/live-caption/ws"):
            pass


class TestTranscribeWindow:
    """_transcribe_window's SSE parsing. strip_language_tag's own regex is
    unit-tested in test_diarize.py; this only checks it is actually wired
    into the committed text, not just available."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_a_language_tag_is_stripped_from_the_committed_text(self):
        url = "http://asr.test/v1/audio/transcriptions"
        respx.post(url).mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=(
                    'data: {"type":"transcript.text.delta","delta":"Hello"}\n\n'
                    'data: {"type":"transcript.text.done","text":"Hello there. <en-US>"}\n\n'
                    "data: [DONE]\n\n"
                ),
            )
        )
        async with httpx.AsyncClient() as client:
            text = await live_caption._transcribe_window(
                client, url, "some-model", None, b"\x00\x00" * 100
            )
        assert text == "Hello there."

    @respx.mock
    @pytest.mark.asyncio
    async def test_on_reading_fires_once_the_stream_is_confirmed_good(self):
        """The recorder's activity dot -- see channel_worker's call sites --
        only means "reading" once the response is confirmed not to be an
        error; on_reading is the hook that tells it so."""
        url = "http://asr.test/v1/audio/transcriptions"
        respx.post(url).mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text='data: {"type":"transcript.text.done","text":"hi"}\n\ndata: [DONE]\n\n',
            )
        )
        calls = []

        async def on_reading():
            calls.append(1)

        async with httpx.AsyncClient() as client:
            await live_caption._transcribe_window(
                client, url, "some-model", None, b"\x00\x00" * 100, on_reading=on_reading
            )
        assert calls == [1]

    @respx.mock
    @pytest.mark.asyncio
    async def test_on_reading_does_not_fire_on_a_rejected_response(self):
        url = "http://asr.test/v1/audio/transcriptions"
        respx.post(url).mock(return_value=httpx.Response(500, text="nope"))
        calls = []

        async def on_reading():
            calls.append(1)

        async with httpx.AsyncClient() as client:
            text = await live_caption._transcribe_window(
                client, url, "some-model", None, b"\x00\x00" * 100, on_reading=on_reading
            )
        assert text == ""
        assert calls == []


class TestSendStatus:
    """The recorder activity dot's transport -- best-effort, same
    swallow-everything policy as the caption send it sits next to."""

    @pytest.mark.asyncio
    async def test_sends_the_expected_shape(self):
        sent = []

        class FakeWebSocket:
            async def send_json(self, payload):
                sent.append(payload)

        await live_caption._send_status(FakeWebSocket(), "room", "calling")
        assert sent == [{"type": "status", "channel": "room", "state": "calling"}]

    @pytest.mark.asyncio
    async def test_a_dead_socket_does_not_raise(self):
        class DeadWebSocket:
            async def send_json(self, payload):
                raise RuntimeError("socket is gone")

        # Must not raise -- a dropped status update is invisible to the
        # user, and letting it escape would kill channel_worker over
        # something that was never essential to begin with.
        await live_caption._send_status(DeadWebSocket(), "me", "idle")


class TestAsrConcurrencyLimit:
    """room and me are independent asyncio tasks on the same cadence (see
    channel_worker) -- without _ASR_CONCURRENCY they'd fire simultaneous
    requests at a backend observed to hang under concurrent load. This
    checks the mutual exclusion directly, deterministically (no wall-clock
    sleeps to race against), rather than through a real websocket session."""

    @pytest.mark.asyncio
    async def test_a_second_caller_waits_for_the_first_to_release(self):
        order = []
        hold_a = asyncio.Event()
        release_a = asyncio.Event()
        hold_b = asyncio.Event()
        release_b = asyncio.Event()

        async def hold(tag, hold_event, release_event):
            async with live_caption._ASR_CONCURRENCY:
                order.append(f"{tag}-start")
                hold_event.set()
                await release_event.wait()
                order.append(f"{tag}-end")

        task_a = asyncio.create_task(hold("a", hold_a, release_a))
        await hold_a.wait()  # a now holds the semaphore

        task_b = asyncio.create_task(hold("b", hold_b, release_b))
        # b must not be able to acquire while a still holds it -- give the
        # event loop a beat to prove it *doesn't* start, not just that it
        # hasn't started yet.
        await asyncio.sleep(0)
        assert not hold_b.is_set(), "b acquired the semaphore while a still held it"

        release_a.set()
        await task_a
        await hold_b.wait()
        release_b.set()
        await task_b

        assert order == ["a-start", "a-end", "b-start", "b-end"]


class TestPeakAmplitude:
    """The local silence gate -- see SILENCE_PEAK_THRESHOLD's doc comment
    for why MIN_BUFFER_SEC alone (buffer length, not content) never caught
    a channel that has plenty of buffered audio but none of it is speech."""

    def test_digital_silence_is_zero(self):
        assert live_caption._peak_amplitude(_pcm(0, 0, 0, 0)) == 0.0

    def test_empty_buffer_is_zero(self):
        assert live_caption._peak_amplitude(b"") == 0.0

    def test_a_torn_trailing_byte_is_dropped_not_raised(self):
        # One full sample (0) plus one stray odd byte.
        assert live_caption._peak_amplitude(_pcm(0) + b"\x01") == 0.0

    def test_low_level_noise_floor_stays_below_threshold(self):
        # A quiet mic's own noise floor -- comfortably under 2% of full scale.
        peak = live_caption._peak_amplitude(_pcm(50, -60, 40, -30))
        assert peak < live_caption.SILENCE_PEAK_THRESHOLD

    def test_a_speaking_voice_clears_the_threshold(self):
        peak = live_caption._peak_amplitude(_pcm(100, -20000, 500, 19000))
        assert peak > live_caption.SILENCE_PEAK_THRESHOLD

    def test_peak_is_the_largest_magnitude_regardless_of_sign(self):
        # The most negative sample is the true peak here, not the max().
        peak = live_caption._peak_amplitude(_pcm(100, -30000, 200))
        assert peak == pytest.approx(30000 / 32768)

    def test_all_positive_samples_still_find_their_peak(self):
        # Regression guard: an earlier draft used -min(samples) for the
        # negative side, which is wrong (and negative) when every sample is
        # non-negative -- abs() on both ends is what's actually needed.
        peak = live_caption._peak_amplitude(_pcm(10, 25000, 5))
        assert peak == pytest.approx(25000 / 32768)
