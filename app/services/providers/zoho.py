"""Zoho Mail and Zoho Calendar.

Three things here differ from Google in ways that fail silently rather than
loudly, so they are worth stating up front:

* The auth header is ``Zoho-oauthtoken``, **not** ``Bearer``.
* Mail has no ``me`` alias -- every request needs a numeric ``accountId`` fetched
  first.
* Calendar's ``range`` is mandatory and capped at 31 days, and descriptions only
  come back when you ask for ``Accept: application/json+large``.

Zoho is regional: an account lives in one data centre and its API hosts carry
that suffix (``.com``, ``.eu``, ``.in``, ``.com.au``, ``.jp``). Talking to the
wrong one authenticates fine and returns nothing.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import httpx

from app.config import effective
from app.db import get_conn
from app.errors import IntegrationAuthError, ValidationError
from app.logging_config import get_logger
from app.services.providers import oauth, tokens
from app.services.providers.base import (
    BaseProvider,
    Check,
    EmailCandidate,
    EventCandidate,
    test_result,
)
from app.services.providers.query import zoho_range

log = get_logger("providers.zoho")

REQUEST_TIMEOUT_SEC = 30
DEFAULT_DC = "com"
# Zoho rejects a wider window outright rather than truncating it.
MAX_RANGE_DAYS = 31

SCOPES = (
    # Zoho has no "email" scope. /oauth/user/info -- the only way to learn which
    # account just authorised us -- requires this one, and a grant is fixed at
    # consent time, so omitting it here cannot be repaired at the token step.
    "AaaServer.profile.READ",
    "ZohoMail.accounts.READ",
    "ZohoMail.messages.READ",
    "ZohoCalendar.calendar.READ",
    "ZohoCalendar.event.READ",
)


def data_centre(conn) -> str:
    return (effective(conn, "zoho_dc") or DEFAULT_DC).strip().lstrip(".") or DEFAULT_DC


def dc_from_accounts_server(url: str | None) -> str | None:
    """Read the data centre out of the ``accounts-server`` the callback carries.

    Zoho appends ``location`` and ``accounts-server`` to the redirect, and a token
    is only valid in the DC that issued it. Trusting that beats trusting a
    configured default, which is exactly the thing a user gets wrong.
    """
    if not url:
        return None
    host = str(url).split("://")[-1].strip("/")
    prefix = "accounts.zoho."
    if prefix not in host:
        return None
    return host.split(prefix, 1)[1] or None


def accounts_host(dc: str) -> str:
    return f"https://accounts.zoho.{dc}"


def client_for(conn) -> oauth.OAuthClient:
    client_id = effective(conn, "zoho_client_id")
    client_secret = effective(conn, "zoho_client_secret")
    if not client_id or not client_secret:
        raise IntegrationAuthError(
            "Zoho is not set up yet: an admin needs to add a Zoho client ID and "
            "secret in Settings."
        )
    base = accounts_host(data_centre(conn))
    return oauth.OAuthClient(
        client_id=client_id,
        client_secret=client_secret,
        authorize_url=f"{base}/oauth/v2/auth",
        token_url=f"{base}/oauth/v2/token",
        scopes=SCOPES,
        # Without access_type=offline Zoho issues no refresh token; prompt=consent
        # forces a new one when reconnecting an already-authorised account.
        extra_authorize_params=(("access_type", "offline"), ("prompt", "consent")),
    )


def resolve_dc(conn=None, hints: dict | None = None) -> str:
    """Prefer what Zoho told us on the redirect over what anyone configured."""
    from_callback = dc_from_accounts_server((hints or {}).get("accounts_server"))
    if from_callback:
        return from_callback
    return data_centre(conn) if conn is not None else DEFAULT_DC


def fetch_identity(access_token: str, conn=None, *, hints: dict | None = None) -> dict:
    dc = resolve_dc(conn, hints)
    with httpx.Client(timeout=REQUEST_TIMEOUT_SEC) as http:
        response = http.get(
            f"{accounts_host(dc)}/oauth/user/info",
            headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
        )

    if response.status_code >= 400:
        # Typed, so the callback can redirect with something readable instead of
        # letting an httpx error surface as a bare 500.
        raise IntegrationAuthError(
            f"Zoho would not identify the account (HTTP {response.status_code} from "
            f"accounts.zoho.{dc}). Usually the AaaServer.profile.READ scope was not "
            "granted, or the account lives in a different data centre."
        )

    body = response.json()
    # Zoho capitalises its keys.
    return {
        "account_key": str(body.get("ZUID") or body.get("Email") or ""),
        "email": body.get("Email"),
        "dc": dc,
    }


def parse_stamp(value: str | None) -> str | None:
    """Zoho uses basic-format stamps (``20260318T090000+0530``); we store ISO.

    Also handles the epoch-milliseconds strings the mail API returns.
    """
    if not value:
        return None
    text = str(value).strip()

    # Length-guarded: an all-day date like "20260318" is also all digits, and
    # reading it as epoch milliseconds silently lands the event in 1970.
    # Millisecond stamps for any plausible date are 13 digits.
    if text.isdigit() and len(text) >= 12:
        try:
            return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc).isoformat()
        except (ValueError, OSError):
            return None

    for fmt in ("%Y%m%dT%H%M%S%z", "%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%Y%m%d":
            return parsed.date().isoformat()  # all-day: keep it a date
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()

    return text  # keep the original rather than losing information


class ZohoProvider(BaseProvider):
    provider_id = "zoho"

    def _dc(self) -> str:
        """Pinned on the row at connect time; the global setting is the fallback
        for rows created before that, and DEFAULT_DC beyond even that."""
        if self.config.get("dc"):
            return str(self.config["dc"])
        try:
            with get_conn() as conn:
                return data_centre(conn)
        except Exception:  # noqa: BLE001 - config lookup must never break a search
            return DEFAULT_DC

    async def _token(self) -> str:
        with get_conn() as conn:
            client = client_for(conn)
        return await tokens.access_token(self.ref.id, client)

    def _http(self, token: str, *, large: bool = False) -> httpx.AsyncClient:
        headers = {"Authorization": f"Zoho-oauthtoken {token}"}
        if large:
            # Without this Zoho omits event descriptions entirely.
            headers["Accept"] = "application/json+large"
        return httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC, headers=headers)

    async def _get(self, http: httpx.AsyncClient, url: str, params: dict | None = None) -> dict:
        response = await http.get(url, params=params or {})
        if response.status_code in (401, 403):
            raise IntegrationAuthError(
                f"{self.ref.display}: Zoho rejected the credentials. Reconnect the account."
            )
        response.raise_for_status()
        return response.json()

    # ----------------------------------------------------------------- calendar

    async def search_events(
        self, *, query: str | None, start: datetime, end: datetime
    ) -> list[EventCandidate]:
        if end - start > timedelta(days=MAX_RANGE_DAYS):
            # Deliberately an error rather than a chunked loop: the default
            # window is ten days, so nobody hits this without asking for it.
            raise ValidationError(
                f"Zoho Calendar accepts at most {MAX_RANGE_DAYS} days per search; "
                "narrow the match window."
            )

        token = await self._token()
        base = f"https://calendar.zoho.{self._dc()}/api/v1/calendars"

        async with self._http(token, large=True) as http:
            listing = await self._get(http, base)
            calendars = listing.get("calendars") or listing.get("data") or []

            events: list[EventCandidate] = []
            for calendar in calendars:
                uid = calendar.get("uid") or calendar.get("id")
                if not uid:
                    continue
                try:
                    body = await self._get(
                        http,
                        f"{base}/{uid}/events",
                        {"range": json.dumps(zoho_range(start, end))},
                    )
                except IntegrationAuthError:
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad calendar, not all
                    log.warning("zoho calendar %s failed: %s", uid, exc)
                    continue
                for item in body.get("events") or []:
                    events.append(self._to_event(item, calendar))

        return events

    def _to_event(self, item: dict, calendar: dict) -> EventCandidate:
        when = item.get("dateandtime") or {}
        native = item.get("uid") or item.get("eventid") or ""
        start = parse_stamp(when.get("start"))
        return EventCandidate(
            # Zoho repeats a series uid across instances, so the start pins the
            # occurrence -- same reasoning as Google's iCalUID.
            uid=self.uid_for(f"{native}:{start or ''}"),
            source_uid=native,
            summary=item.get("title") or item.get("summary"),
            description=item.get("description"),
            location=item.get("location"),
            start=start,
            end=parse_stamp(when.get("end")),
            calendar_name=calendar.get("name") or calendar.get("displayname"),
            account=self.ref.account_label,
            type="zoho",
            url=item.get("viewEventURL") or item.get("url"),
            provider=self.provider_id,
            integration_id=self.ref.id,
        )

    # --------------------------------------------------------------------- mail

    async def _account_id(self, http: httpx.AsyncClient) -> str:
        """Zoho Mail has no ``me`` alias; every path needs the numeric id."""
        cached = self.config.get("account_id")
        if cached:
            return str(cached)

        body = await self._get(http, f"https://mail.zoho.{self._dc()}/api/accounts")
        accounts = body.get("data") or []
        if not accounts:
            raise IntegrationAuthError(
                f"{self.ref.display}: Zoho returned no mail accounts for this login."
            )
        return str(accounts[0].get("accountId"))

    async def search_emails(
        self, *, keywords: list[str], start: datetime, end: datetime
    ) -> list[EmailCandidate]:
        token = await self._token()
        limit = int(self.config.get("max_results") or 25)

        async with self._http(token) as http:
            account_id = await self._account_id(http)
            body = await self._get(
                http,
                f"https://mail.zoho.{self._dc()}/api/accounts/{account_id}/messages/search",
                {
                    # Zoho's searchKey has no OR, so this searches the single
                    # strongest keyword across the whole message and the window
                    # is applied below.
                    "searchKey": f"entire:{keywords[0]}" if keywords else "newMails",
                    "limit": limit,
                    "start": 1,
                },
            )

        messages = body.get("data") or []
        return [
            candidate
            for candidate in (self._to_email(m) for m in messages)
            # Zoho's search takes only an upper time bound, so the lower edge of
            # the window is enforced here rather than server-side.
            if _within(candidate.date, start, end)
        ]

    def _to_email(self, item: dict) -> EmailCandidate:
        native = str(item.get("messageId") or "")
        return EmailCandidate(
            message_id=f"{self.provider_id}:{self.ref.id}:{native}",
            # Zoho's search payload does not carry the RFC 2822 header.
            rfc_message_id=None,
            id=native,
            sender=item.get("fromAddress") or item.get("sender"),
            subject=item.get("subject"),
            date=parse_stamp(item.get("receivedTime") or item.get("sentDateInGMT")),
            snippet=item.get("summary"),
            account=self.ref.account_label,
            url=f"https://mail.zoho.{self._dc()}/zm/#mail/folder/all/{native}"
            if native
            else None,
            provider=self.provider_id,
            integration_id=self.ref.id,
        )

    # --------------------------------------------------------------------- test

    async def test(self) -> dict:
        started = time.monotonic()
        checks: list[Check] = []

        try:
            token = await self._token()
            checks.append(Check(name="authorisation", ok=True))
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            checks.append(Check(name="authorisation", ok=False, error=str(exc)))
            return test_result(checks, int((time.monotonic() - started) * 1000))

        if self.ref.calendar_enabled:
            try:
                async with self._http(token) as http:
                    await self._get(http, f"https://calendar.zoho.{self._dc()}/api/v1/calendars")
                checks.append(Check(name="calendar", ok=True))
            except Exception as exc:  # noqa: BLE001
                checks.append(Check(name="calendar", ok=False, error=str(exc)))

        if self.ref.email_enabled:
            try:
                async with self._http(token) as http:
                    await self._account_id(http)
                checks.append(Check(name="mail", ok=True))
            except Exception as exc:  # noqa: BLE001
                checks.append(Check(name="mail", ok=False, error=str(exc)))

        return test_result(checks, int((time.monotonic() - started) * 1000))


def _within(stamp: str | None, start: datetime, end: datetime) -> bool:
    if not stamp:
        return True  # undated: let the ranker decide rather than dropping it
    try:
        value = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return start <= value <= end
