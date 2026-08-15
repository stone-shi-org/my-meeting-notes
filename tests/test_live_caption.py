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

import pytest
from starlette.websockets import WebSocketDisconnect

from app.db import utcnow


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
