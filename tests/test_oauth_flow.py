"""The OAuth connect flow: signed state, and the callback that creates the row.

State is a signed value rather than a database row, so its integrity checks are
the whole security boundary here and get tested directly.
"""

from __future__ import annotations

import time

import httpx
import pytest
import respx

from app.db import get_conn
from app.errors import ValidationError
from app.services.providers import oauth

TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


class TestState:
    def test_a_state_round_trips(self):
        nonce = oauth.new_nonce()
        state = oauth.make_state(42, "google", nonce)
        payload = oauth.parse_state(state, nonce)
        assert payload["u"] == 42
        assert payload["p"] == "google"

    def test_a_tampered_payload_is_rejected(self):
        nonce = oauth.new_nonce()
        state = oauth.make_state(1, "google", nonce)
        body, signature = state.split(".", 1)
        forged = oauth.make_state(999, "google", nonce).split(".", 1)[0]

        with pytest.raises(ValidationError, match="signature"):
            oauth.parse_state(f"{forged}.{signature}", nonce)

    def test_a_truncated_state_is_rejected(self):
        with pytest.raises(ValidationError):
            oauth.parse_state("no-dot-here", "n")

    def test_an_expired_state_is_rejected(self):
        nonce = oauth.new_nonce()
        state = oauth.make_state(1, "google", nonce, now=time.time() - oauth.STATE_TTL_SEC - 5)
        with pytest.raises(ValidationError, match="expired"):
            oauth.parse_state(state, nonce)

    def test_a_state_without_the_matching_nonce_is_rejected(self):
        """Stops a state lifted from a redirect URL being replayed elsewhere."""
        state = oauth.make_state(1, "google", oauth.new_nonce())
        with pytest.raises(ValidationError, match="different browser"):
            oauth.parse_state(state, "some-other-nonce")
        with pytest.raises(ValidationError):
            oauth.parse_state(state, None)


@pytest.fixture
def google_configured(admin_client):
    resp = admin_client.put(
        "/api/settings",
        json={
            "values": {
                "google_client_id": "test-client-id",
                "google_client_secret": "test-client-secret",
                "public_base_url": "http://localhost:4020",
            }
        },
    )
    assert resp.status_code == 200, resp.text
    return admin_client


class TestStart:
    def test_it_returns_an_authorize_url_and_sets_the_nonce_cookie(
        self, google_configured, user_client
    ):
        resp = user_client.get("/api/integrations/oauth/google/start")
        assert resp.status_code == 200, resp.text

        url = resp.json()["authorize_url"]
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "client_id=test-client-id" in url
        # Without access_type=offline Google never issues a refresh token, and
        # the integration would die at the first access-token expiry.
        assert "access_type=offline" in url
        assert "prompt=consent" in url
        assert "gmail.readonly" in url
        assert "calendar.readonly" in url
        assert "redirect_uri=http%3A%2F%2Flocalhost%3A4020" in url

    def test_it_fails_clearly_when_google_is_not_set_up(self, user_client):
        resp = user_client.get("/api/integrations/oauth/google/start")
        assert resp.status_code == 502
        assert "admin" in resp.json()["error"]["message"]

    def test_a_non_oauth_provider_is_rejected(self, google_configured, user_client):
        resp = user_client.get("/api/integrations/oauth/mcp_calendar/start")
        assert resp.status_code == 400

    def test_it_needs_a_login(self, google_configured, client):
        client.cookies.clear()
        assert client.get("/api/integrations/oauth/google/start").status_code == 401


class TestCallback:
    @pytest.fixture
    def google_ok(self):
        with respx.mock(assert_all_called=False) as router:
            router.post(TOKEN_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "access_token": "at-1",
                        "refresh_token": "rt-1",
                        "expires_in": 3599,
                        "scope": "openid email calendar.readonly gmail.readonly",
                    },
                )
            )
            router.get(USERINFO_URL).mock(
                return_value=httpx.Response(
                    200, json={"sub": "sub-123", "email": "me@example.com"}
                )
            )
            yield router

    def _start(self, user_client):
        resp = user_client.get("/api/integrations/oauth/google/start")
        from urllib.parse import parse_qs, urlparse

        state = parse_qs(urlparse(resp.json()["authorize_url"]).query)["state"][0]
        return state

    def test_it_creates_the_account_after_confirming_identity(
        self, google_configured, user_client, client, google_ok
    ):
        state = self._start(user_client)

        resp = client.get(
            f"/api/integrations/oauth/google/callback?code=abc&state={state}",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/settings/integrations?connected=google"

        listed = user_client.get("/api/integrations").json()
        google = [i for i in listed if i["provider"] == "google"]
        assert len(google) == 1
        assert google[0]["account_key"] == "sub-123"
        assert google[0]["account_label"] == "me@example.com"
        assert google[0]["status"] == "ok"
        # Both capabilities, since Google supports both.
        assert google[0]["calendar_enabled"] and google[0]["email_enabled"]

    def test_the_refresh_token_is_stored_encrypted(
        self, google_configured, user_client, client, google_ok
    ):
        state = self._start(user_client)
        client.get(
            f"/api/integrations/oauth/google/callback?code=abc&state={state}",
            follow_redirects=False,
        )

        from app.services import secretstore

        with get_conn() as conn:
            row = conn.execute(
                "SELECT secret_json FROM integrations WHERE provider = 'google'"
            ).fetchone()
        assert "rt-1" not in row["secret_json"]
        assert secretstore.decrypt(row["secret_json"])["refresh_token"] == "rt-1"

    def test_reconnecting_updates_rather_than_duplicating(
        self, google_configured, user_client, client, google_ok
    ):
        """Four clicks of Connect must not leave four rows."""
        for _ in range(3):
            state = self._start(user_client)
            client.get(
                f"/api/integrations/oauth/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        google = [i for i in user_client.get("/api/integrations").json() if i["provider"] == "google"]
        assert len(google) == 1

    def test_reconnecting_clears_a_reauth_flag(
        self, google_configured, user_client, client, google_ok
    ):
        state = self._start(user_client)
        client.get(
            f"/api/integrations/oauth/google/callback?code=abc&state={state}",
            follow_redirects=False,
        )
        with get_conn() as conn:
            conn.execute("UPDATE integrations SET status='reauth_required' WHERE provider='google'")

        state = self._start(user_client)
        client.get(
            f"/api/integrations/oauth/google/callback?code=abc&state={state}",
            follow_redirects=False,
        )

        google = [i for i in user_client.get("/api/integrations").json() if i["provider"] == "google"][0]
        assert google["status"] == "ok"

    def test_a_denied_consent_redirects_without_creating_anything(
        self, google_configured, user_client, client
    ):
        resp = client.get(
            "/api/integrations/oauth/google/callback?error=access_denied",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "error=access_denied" in resp.headers["location"]
        assert user_client.get("/api/integrations").json() == []

    def test_a_forged_state_creates_nothing(
        self, google_configured, user_client, client, google_ok
    ):
        self._start(user_client)  # sets a real nonce cookie
        forged = oauth.make_state(1, "google", "not-the-cookie-nonce")

        resp = client.get(
            f"/api/integrations/oauth/google/callback?code=abc&state={forged}",
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert user_client.get("/api/integrations").json() == []

    def test_an_identity_call_without_a_subject_creates_nothing(
        self, google_configured, user_client, client
    ):
        """No identity means no stable account_key, and a NULL key would let the
        same account be connected over and over."""
        with respx.mock(assert_all_called=False) as router:
            router.post(TOKEN_URL).mock(
                return_value=httpx.Response(200, json={"access_token": "at", "expires_in": 60})
            )
            router.get(USERINFO_URL).mock(return_value=httpx.Response(200, json={}))

            state = self._start(user_client)
            resp = client.get(
                f"/api/integrations/oauth/google/callback?code=abc&state={state}",
                follow_redirects=False,
            )

        assert "error=no_identity" in resp.headers["location"]
        assert user_client.get("/api/integrations").json() == []
