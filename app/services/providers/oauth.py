"""Shared OAuth 2.0 authorization-code plumbing.

State is an HMAC-signed expiring value rather than a database row: it lives for
ten minutes, needs no cleanup job, and reuses the stdlib approach already in
``app/security.py``. The nonce inside it is also set as an httpOnly cookie, so a
state lifted from a redirect URL cannot be replayed from another browser.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

import httpx

from app.config import effective
from app.errors import ValidationError
from app.logging_config import get_logger
from app.services import secretstore

log = get_logger("providers.oauth")

STATE_TTL_SEC = 600
STATE_COOKIE = "mmn_oauth_nonce"


@dataclass(frozen=True)
class OAuthClient:
    """The app-level registration a provider authorises against."""

    client_id: str
    client_secret: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    # Google needs these to hand back a refresh token at all.
    extra_authorize_params: tuple[tuple[str, str], ...] = ()


def redirect_uri(conn, provider: str) -> str:
    base = (effective(conn, "public_base_url") or "").rstrip("/")
    if not base:
        raise ValidationError("public_base_url is not configured")
    return f"{base}/api/integrations/oauth/{provider}/callback"


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


def _sign(payload: str) -> str:
    _, key = secretstore.load_key()
    return base64.urlsafe_b64encode(
        hmac.new(key, payload.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")


def make_state(user_id: int, provider: str, nonce: str, *, now: float | None = None) -> str:
    body = json.dumps(
        {
            "u": user_id,
            "p": provider,
            "n": nonce,
            "e": int((now if now is not None else time.time()) + STATE_TTL_SEC),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    encoded = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    return f"{encoded}.{_sign(encoded)}"


def new_nonce() -> str:
    return secrets.token_urlsafe(16)


def parse_state(state: str, nonce: str | None, *, now: float | None = None) -> dict:
    """Validate signature, expiry and the browser's nonce. Raises on any failure."""
    try:
        encoded, signature = state.split(".", 1)
    except ValueError:
        raise ValidationError("Malformed OAuth state") from None

    # compare_digest, not ==: signature comparison must not be short-circuiting.
    if not hmac.compare_digest(signature, _sign(encoded)):
        raise ValidationError("OAuth state failed its signature check")

    padding = "=" * (-len(encoded) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
    except ValueError:
        raise ValidationError("Malformed OAuth state") from None

    if payload.get("e", 0) < (now if now is not None else time.time()):
        raise ValidationError("This sign-in link expired. Start again from Settings.")

    if not nonce or not hmac.compare_digest(str(payload.get("n", "")), nonce):
        raise ValidationError(
            "This sign-in was started in a different browser session. Try again."
        )

    return payload


# --------------------------------------------------------------------------- #
# Token endpoint
# --------------------------------------------------------------------------- #


class OAuthError(Exception):
    """A token endpoint refused us. ``invalid_grant`` is the terminal one."""

    def __init__(self, message: str, *, error_code: str = ""):
        super().__init__(message)
        self.message = message
        self.error_code = error_code

    @property
    def is_terminal(self) -> bool:
        """invalid_grant means the grant is dead -- retrying cannot fix it."""
        return self.error_code == "invalid_grant"


TOKEN_TIMEOUT_SEC = 30


def _post_token(client: OAuthClient, data: dict) -> dict:
    with httpx.Client(timeout=TOKEN_TIMEOUT_SEC) as http:
        response = http.post(
            client.token_url,
            data={**data, "client_id": client.client_id, "client_secret": client.client_secret},
            headers={"Accept": "application/json"},
        )

    try:
        body = response.json()
    except ValueError:
        body = {}

    if response.status_code >= 400 or "error" in body:
        code = str(body.get("error") or f"http_{response.status_code}")
        detail = body.get("error_description") or response.text[:200] or "(no body)"
        raise OAuthError(f"{code}: {detail}", error_code=code)

    return body


def exchange_code(client: OAuthClient, code: str, redirect: str) -> dict:
    return _post_token(
        client,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect},
    )


def refresh(client: OAuthClient, refresh_token: str) -> dict:
    return _post_token(
        client, {"grant_type": "refresh_token", "refresh_token": refresh_token}
    )


def authorize_url(client: OAuthClient, redirect: str, state: str) -> str:
    from urllib.parse import urlencode

    params = {
        "client_id": client.client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": " ".join(client.scopes),
        "state": state,
        **dict(client.extra_authorize_params),
    }
    return f"{client.authorize_url}?{urlencode(params)}"
