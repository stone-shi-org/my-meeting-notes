"""iCloud calendar and mail.

Apple offers no OAuth for iCloud, so this is an Apple ID plus an
**app-specific password** (generated at appleid.apple.com), used as Basic auth
for CalDAV and as the IMAP login. That is the only route available, not a
shortcut.

No webhooks either, so everything here is a poll.
"""

from __future__ import annotations

import time
from datetime import datetime

import httpx

from app.errors import IntegrationAuthError
from app.logging_config import get_logger
from app.services.providers import _caldav, _imap
from app.services.providers.base import (
    BaseProvider,
    Check,
    EmailCandidate,
    EventCandidate,
    test_result,
)

log = get_logger("providers.apple")

CALDAV_URL = "https://caldav.icloud.com/"
IMAP_HOST = "imap.mail.me.com"
REQUEST_TIMEOUT_SEC = 30


class AppleProvider(BaseProvider):
    provider_id = "apple"

    # Apple's own domains. An Apple ID outside these is a third-party address,
    # which CalDAV accepts but IMAP does not.
    ICLOUD_DOMAINS = ("@icloud.com", "@me.com", "@mac.com")

    @property
    def username(self) -> str:
        return self.secret.get("username") or self.config.get("username") or ""

    @property
    def imap_username(self) -> str:
        """The mailbox login, which is not always the Apple ID.

        An Apple ID can be any address -- a Gmail one, say -- and CalDAV happily
        authenticates with it. iCloud Mail will not: it wants the account's own
        @icloud.com (or @me.com / @mac.com) address. When those differ, logging in
        with the Apple ID fails with a bare AUTHENTICATIONFAILED that looks
        exactly like a wrong password.
        """
        return self.config.get("imap_username") or self.username

    @property
    def password(self) -> str:
        return self.secret.get("password") or ""

    def _credentials(self) -> tuple[str, str]:
        if not self.username or not self.password:
            raise IntegrationAuthError(
                f"{self.ref.display} has no stored app-specific password. Reconnect it."
            )
        return self.username, self.password

    def _http(self) -> httpx.AsyncClient:
        username, password = self._credentials()
        return httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SEC,
            auth=httpx.BasicAuth(username, password),
            follow_redirects=True,
        )

    # ----------------------------------------------------------------- calendar

    async def search_events(
        self, *, query: str | None, start: datetime, end: datetime
    ) -> list[EventCandidate]:
        """CalDAV has no text search, so the window sweep is the whole search."""
        base = self.config.get("caldav_url") or CALDAV_URL
        async with self._http() as http:
            found = await _caldav.collect(http, base, start, end)
        return [self._to_event(name, event) for name, event in found]

    def _to_event(self, calendar_name: str, event: dict) -> EventCandidate:
        series_uid = event.get("uid") or ""
        # A recurrence set shares one UID, so the instance key has to include
        # which occurrence this is -- otherwise a weekly standup collapses into
        # a single candidate.
        instance = event.get("recurrence_id") or event.get("start") or ""
        return EventCandidate(
            uid=self.uid_for(f"{series_uid}:{instance}"),
            source_uid=series_uid,
            summary=event.get("summary"),
            description=event.get("description"),
            location=event.get("location"),
            start=event.get("start"),
            end=event.get("end"),
            attendees=tuple(event.get("attendees") or ()),
            calendar_name=calendar_name,
            account=self.ref.account_label,
            type="caldav",
            # iCloud has no per-event web URL worth linking to.
            url=None,
            provider=self.provider_id,
            integration_id=self.ref.id,
        )

    # --------------------------------------------------------------------- mail

    def _mail_auth_error(self, exc: Exception) -> IntegrationAuthError:
        """Say which of the two likely causes it actually is.

        "Check your password" is unhelpful and often wrong here -- if CalDAV
        works, the password is fine and the username is the problem.
        """
        user = self.imap_username
        if not user.lower().endswith(self.ICLOUD_DOMAINS):
            return IntegrationAuthError(
                f"iCloud Mail rejected the login for {user}. An Apple ID that is not "
                "an @icloud.com address cannot be used as the mailbox login — set the "
                "iCloud email address on this integration, or turn off email and use "
                "the account for calendar only."
            )
        return IntegrationAuthError(
            f"iCloud Mail rejected the login for {user}. Check the app-specific "
            "password, and that iCloud Mail is switched on for this account."
        )

    async def search_emails(
        self, *, keywords: list[str], start: datetime, end: datetime
    ) -> list[EmailCandidate]:
        _, password = self._credentials()
        try:
            messages = await _imap.search(
                host=self.config.get("imap_host") or IMAP_HOST,
                username=self.imap_username,
                password=password,
                keywords=keywords,
                start=start,
                end=end,
                limit=int(self.config.get("max_results") or 25),
                mailbox=self.config.get("mailbox") or "INBOX",
            )
        except IntegrationAuthError as exc:
            raise self._mail_auth_error(exc) from exc
        return [self._to_email(m) for m in messages]

    def _to_email(self, message: dict) -> EmailCandidate:
        rfc_id = message.get("rfc_message_id")
        native = message.get("id") or ""
        return EmailCandidate(
            # IMAP sequence numbers are per-session and get reused, so prefer the
            # stable RFC Message-ID for identity where the server supplied one.
            message_id=f"{self.provider_id}:{self.ref.id}:{rfc_id or native}",
            rfc_message_id=rfc_id,
            id=native,
            sender=message.get("sender"),
            subject=message.get("subject"),
            date=message.get("date"),
            # Header-only fetch: no body means no snippet, and inventing one from
            # the subject would just duplicate it.
            snippet=None,
            account=self.ref.account_label,
            # iCloud webmail has no linkable per-message URL.
            url=None,
            provider=self.provider_id,
            integration_id=self.ref.id,
        )

    # --------------------------------------------------------------------- test

    async def test(self) -> dict:
        """Check each leg separately.

        CalDAV succeeding while IMAP login is rejected is the single most likely
        Apple failure, and one ok/not-ok flag cannot express it.
        """
        started = time.monotonic()
        checks: list[Check] = []

        if self.ref.calendar_enabled:
            try:
                async with self._http() as http:
                    home = await _caldav.discover_calendar_home(
                        http, self.config.get("caldav_url") or CALDAV_URL
                    )
                    await _caldav.list_calendars(http, home)
                checks.append(Check(name="calendar (CalDAV)", ok=True))
            except Exception as exc:  # noqa: BLE001 - reported, never raised
                checks.append(Check(name="calendar (CalDAV)", ok=False, error=str(exc)))

        if self.ref.email_enabled:
            try:
                _, password = self._credentials()
                await _imap.search(
                    host=self.config.get("imap_host") or IMAP_HOST,
                    username=self.imap_username,
                    password=password,
                    keywords=[],
                    start=datetime.now().replace(microsecond=0),
                    end=datetime.now().replace(microsecond=0),
                    limit=1,
                )
                checks.append(Check(name="mail (IMAP)", ok=True))
            except IntegrationAuthError as exc:
                checks.append(
                    Check(name="mail (IMAP)", ok=False, error=str(self._mail_auth_error(exc)))
                )
            except Exception as exc:  # noqa: BLE001
                checks.append(Check(name="mail (IMAP)", ok=False, error=str(exc)))

        return test_result(checks, int((time.monotonic() - started) * 1000))
