"""Google Calendar and Gmail.

Both APIs are read-only here. Two shapes worth knowing before reading on:

*Calendar* is cheap -- one ``events.list`` per calendar with ``singleEvents=true``
expands recurrences server-side, so a weekly standup arrives as concrete
instances with their own ids.

*Gmail* is not. ``messages.list`` returns bare ids and every one needs its own
``get``, so the cost is bounded three ways: ask for no more ids than we can keep,
fetch metadata rather than whole messages, and cap concurrency. A failed
individual get drops one email instead of the whole search.
"""

from __future__ import annotations

import asyncio
import base64
import time
from datetime import datetime
from urllib.parse import quote

import httpx

from app.config import effective
from app.db import get_conn
from app.errors import IntegrationAuthError
from app.logging_config import get_logger
from app.services.providers import oauth, tokens
from app.services.providers.base import (
    BaseProvider,
    Check,
    EmailCandidate,
    EventCandidate,
    attendee_label,
    clean_attendees,
    test_result,
)
from app.services.providers.query import build_gmail_query

log = get_logger("providers.google")

CALENDAR_API = "https://www.googleapis.com/calendar/v3"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# gmail.readonly is a RESTRICTED scope. gmail.metadata is not a lighter
# substitute -- it forbids the `q` search parameter, which is the whole feature.
SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.readonly",
)

REQUEST_TIMEOUT_SEC = 30
# Google's own batching guidance sits around 50; 8 keeps us far clear of any
# per-mailbox concurrency ceiling while still turning 25 gets into ~4 round trips.
GMAIL_CONCURRENCY = 8


def client_for(conn) -> oauth.OAuthClient:
    client_id = effective(conn, "google_client_id")
    client_secret = effective(conn, "google_client_secret")
    if not client_id or not client_secret:
        raise IntegrationAuthError(
            "Google is not set up yet: an admin needs to add a Google OAuth client "
            "ID and secret in Settings."
        )
    return oauth.OAuthClient(
        client_id=client_id,
        client_secret=client_secret,
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
        # access_type=offline is what produces a refresh token at all; prompt=consent
        # forces one to be re-issued when the user reconnects an account they had
        # already authorised, which is exactly the reauth case.
        extra_authorize_params=(
            ("access_type", "offline"),
            ("prompt", "consent"),
            ("include_granted_scopes", "true"),
        ),
    )


def fetch_identity(access_token: str, conn=None, *, hints: dict | None = None) -> dict:
    """Who just authorised us. Called in the callback, before any row exists.

    ``conn`` and ``hints`` are unused here but keep the signature uniform across
    providers; Zoho needs both to know which data centre to ask.
    """
    with httpx.Client(timeout=REQUEST_TIMEOUT_SEC) as http:
        response = http.get(
            USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
    response.raise_for_status()
    body = response.json()
    return {"account_key": body.get("sub") or body.get("email"), "email": body.get("email")}


def _when(node: dict | None) -> str | None:
    """Google gives dateTime for timed events and date for all-day ones."""
    if not node:
        return None
    return node.get("dateTime") or node.get("date")


def _decode_part_data(data: str | None) -> str | None:
    if not data:
        return None
    # Gmail's body.data is base64url, not standard base64 -- padding is also
    # frequently stripped, which urlsafe_b64decode alone will not tolerate.
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return None


def _extract_gmail_text(payload: dict) -> str | None:
    """Walk a ``format=full`` payload for the first text/plain part.

    Falls back to text/html if that's all the message has -- returned as-is
    rather than stripped, since a half-rendered tag soup would be a worse
    "verbatim body" than the markup itself.
    """
    html_fallback: str | None = None

    def walk(node: dict) -> str | None:
        nonlocal html_fallback
        mime = node.get("mimeType") or ""
        if mime == "text/plain":
            text = _decode_part_data((node.get("body") or {}).get("data"))
            if text:
                return text
        if mime == "text/html" and html_fallback is None:
            html_fallback = _decode_part_data((node.get("body") or {}).get("data"))
        for part in node.get("parts") or []:
            found = walk(part)
            if found:
                return found
        return None

    return walk(payload) or html_fallback


class GoogleProvider(BaseProvider):
    provider_id = "google"

    async def _token(self) -> str:
        with get_conn() as conn:
            client = client_for(conn)
        return await tokens.access_token(self.ref.id, client)

    async def _get(self, http: httpx.AsyncClient, url: str, params: dict) -> dict:
        response = await http.get(url, params=params)
        if response.status_code == 401:
            raise IntegrationAuthError(
                f"{self.ref.display}: Google rejected the credentials. Reconnect the account."
            )
        response.raise_for_status()
        return response.json()

    # ----------------------------------------------------------------- calendar

    async def search_events(
        self, *, query: str | None, start: datetime, end: datetime
    ) -> list[EventCandidate]:
        """Sweep the window across every selected calendar.

        No keyword pass: unlike the MCP tool, events.list takes a real date range,
        so one sweep is complete by construction and the LLM does the ranking.
        """
        token = await self._token()
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SEC, headers={"Authorization": f"Bearer {token}"}
        ) as http:
            listing = await self._get(
                http,
                f"{CALENDAR_API}/users/me/calendarList",
                {"fields": "items(id,summary,selected,primary)", "maxResults": 250},
            )
            calendars = [
                item
                for item in listing.get("items", [])
                # `selected` is what the user has ticked in Google's own UI; treat
                # a missing flag as selected so a single-calendar account works.
                if item.get("selected", True) or item.get("primary")
            ]

            async def one(calendar: dict) -> list[EventCandidate]:
                # Calendar ids are email-like ("me@gmail.com", and holiday
                # calendars contain '#'), so they must be percent-encoded.
                calendar_id = quote(calendar["id"], safe="")
                body = await self._get(
                    http,
                    f"{CALENDAR_API}/calendars/{calendar_id}/events",
                    {
                        "timeMin": start.isoformat(),
                        "timeMax": end.isoformat(),
                        # Expands recurrences server-side into real instances.
                        "singleEvents": "true",
                        "orderBy": "startTime",
                        "maxResults": 250,
                        "fields": (
                            "items(id,iCalUID,summary,description,location,start,end,"
                            "htmlLink,status,organizer(displayName,email),"
                            "attendees(displayName,email,responseStatus,resource))"
                        ),
                    },
                )
                return [
                    self._to_event(item, calendar)
                    for item in body.get("items", [])
                    if item.get("status") != "cancelled"
                ]

            batches = await asyncio.gather(
                *(one(c) for c in calendars), return_exceptions=True
            )

        events: list[EventCandidate] = []
        for calendar, batch in zip(calendars, batches):
            if isinstance(batch, BaseException):
                log.warning("calendar %s failed: %s", calendar.get("summary"), batch)
                continue
            events.extend(batch)
        return events

    @staticmethod
    def _attendees(item: dict) -> tuple[str, ...]:
        """Organizer first, then whoever has not declined.

        Rooms and equipment are attendees as far as Google is concerned, and
        "Meeting Room 4" prefilled as a speaker name is worse than nothing.
        """
        organizer = item.get("organizer") or {}
        people = [attendee_label(organizer.get("displayName"), organizer.get("email"))]
        for person in item.get("attendees") or []:
            if person.get("resource") or person.get("responseStatus") == "declined":
                continue
            people.append(attendee_label(person.get("displayName"), person.get("email")))
        return clean_attendees(people)

    def _to_event(self, item: dict, calendar: dict) -> EventCandidate:
        return EventCandidate(
            # `id` is per-instance once singleEvents expands a recurrence;
            # iCalUID is shared across the whole series, so it cannot be the key.
            uid=self.uid_for(item["id"]),
            source_uid=item.get("iCalUID"),
            summary=item.get("summary"),
            description=item.get("description"),
            location=item.get("location"),
            start=_when(item.get("start")),
            end=_when(item.get("end")),
            attendees=self._attendees(item),
            calendar_name=calendar.get("summary"),
            account=self.ref.account_label,
            type="google",
            url=item.get("htmlLink"),
            provider=self.provider_id,
            integration_id=self.ref.id,
        )

    # -------------------------------------------------------------------- gmail

    async def search_emails(
        self, *, keywords: list[str], start: datetime, end: datetime
    ) -> list[EmailCandidate]:
        query = build_gmail_query(keywords, start, end)
        limit = int(self.config.get("max_results") or 25)
        token = await self._token()

        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SEC, headers={"Authorization": f"Bearer {token}"}
        ) as http:
            listing = await self._get(
                http,
                f"{GMAIL_API}/users/me/messages",
                # Bounded here rather than after fetching: paying for 100 gets to
                # then discard 75 is the easy mistake.
                {"q": query, "maxResults": limit, "fields": "messages/id"},
            )
            ids = [m["id"] for m in listing.get("messages", []) or []]
            if not ids:
                return []

            gate = asyncio.Semaphore(GMAIL_CONCURRENCY)

            async def one(message_id: str) -> dict:
                async with gate:
                    return await self._get(
                        http,
                        f"{GMAIL_API}/users/me/messages/{message_id}",
                        {
                            # metadata avoids downloading whole bodies; snippet
                            # still comes back with it in practice.
                            # metadata avoids downloading bodies. The docs only
                            # promise ids/labels/headers, so treat snippet as
                            # present-but-unpromised and tolerate it being absent.
                            "format": "metadata",
                            "metadataHeaders": ["Subject", "From", "Date", "Message-ID"],
                            "fields": "id,threadId,snippet,internalDate,payload/headers",
                        },
                    )

            fetched = await asyncio.gather(
                *(one(i) for i in ids), return_exceptions=True
            )

        emails: list[EmailCandidate] = []
        for message_id, body in zip(ids, fetched):
            if isinstance(body, BaseException):
                # One unreadable message must not lose the other 24.
                log.warning("gmail message %s failed: %s", message_id, body)
                continue
            emails.append(self._to_email(body))
        return emails

    def _to_email(self, body: dict) -> EmailCandidate:
        headers = {
            h.get("name", "").lower(): h.get("value")
            for h in (body.get("payload", {}).get("headers") or [])
        }
        native = body.get("id", "")
        return EmailCandidate(
            message_id=f"{self.provider_id}:{self.ref.id}:{native}",
            rfc_message_id=headers.get("message-id"),
            id=native,
            sender=headers.get("from"),
            subject=headers.get("subject"),
            date=headers.get("date"),
            snippet=body.get("snippet") or None,
            account=self.ref.account_label,
            url=f"https://mail.google.com/mail/u/0/#all/{native}",
            provider=self.provider_id,
            integration_id=self.ref.id,
        )

    async def get_email_body(
        self, *, native_id: str, folder_id: str | None = None
    ) -> str | None:
        token = await self._token()
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SEC, headers={"Authorization": f"Bearer {token}"}
        ) as http:
            body = await self._get(
                http,
                f"{GMAIL_API}/users/me/messages/{native_id}",
                {"format": "full"},
            )
        return _extract_gmail_text(body.get("payload") or {})

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

        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SEC, headers={"Authorization": f"Bearer {token}"}
        ) as http:
            if self.ref.calendar_enabled:
                try:
                    await self._get(
                        http, f"{CALENDAR_API}/users/me/calendarList", {"maxResults": 1}
                    )
                    checks.append(Check(name="calendar", ok=True))
                except Exception as exc:  # noqa: BLE001
                    checks.append(Check(name="calendar", ok=False, error=str(exc)))

            if self.ref.email_enabled:
                try:
                    await self._get(
                        http, f"{GMAIL_API}/users/me/profile", {"fields": "emailAddress"}
                    )
                    checks.append(Check(name="gmail", ok=True))
                except Exception as exc:  # noqa: BLE001
                    checks.append(Check(name="gmail", ok=False, error=str(exc)))

        return test_result(checks, int((time.monotonic() - started) * 1000))
