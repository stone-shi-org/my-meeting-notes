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
    FetchedEmail,
    normalize_address,
    test_result,
    truncate_references,
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
        """Search one mailbox, INBOX by default.

        Known gap, deliberately not fixed here: **this account's own sent mail is
        never found**, so an iCloud-only conversation reads as a monologue from
        the other party and `direction` is 'inbound' for every message in it.
        Gmail is unaffected -- `build_gmail_query` has no `-in:sent`, so it
        already returns both sides.

        Widening the search is not the cheap fix it looks like: `_imap.search`
        selects exactly one mailbox, iCloud's sent folder name is localized, and
        adding it would change which messages match, get ranked and get
        auto-attached. The gap is surfaced in the UI instead, because a chain
        presented as complete when it is not is the failure mode that makes the
        next-step suggestion tell you to send a mail you already sent.
        """
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

    def _direction(self, sender: str | None) -> str | None:
        """Compare the sender against this account's own address.

        IMAP has no equivalent of Gmail's SENT label, so an address comparison is
        the only signal available -- and it is only ever a guess, which is why an
        unparseable sender or an unknown account address yields None rather than
        defaulting to 'inbound'.

        In practice this almost always says 'inbound', because `mailbox` defaults
        to INBOX and iCloud's sent folder is never searched: see the note on
        `search_emails`. That is a known gap, deliberately surfaced in the UI
        rather than papered over here.
        """
        mine = normalize_address(self.imap_username)
        theirs = normalize_address(sender)
        if not mine or not theirs:
            return None
        return "outbound" if mine == theirs else "inbound"

    def _to_email(self, message: dict) -> EmailCandidate:
        rfc_id = message.get("rfc_message_id")
        native = message.get("id") or ""
        sender = message.get("sender")
        return EmailCandidate(
            # IMAP sequence numbers are per-session and get reused, so prefer the
            # stable RFC Message-ID for identity where the server supplied one.
            message_id=f"{self.provider_id}:{self.ref.id}:{rfc_id or native}",
            rfc_message_id=rfc_id,
            id=native,
            sender=sender,
            subject=message.get("subject"),
            date=message.get("date"),
            # Header-only fetch: no body means no snippet, and inventing one from
            # the subject would just duplicate it. Hydration fills this in later
            # from the real body, but only where it is still NULL.
            snippet=None,
            account=self.ref.account_label,
            # iCloud webmail has no linkable per-message URL.
            url=None,
            provider=self.provider_id,
            integration_id=self.ref.id,
            # IMAP has no server-side conversation id at all. Left NULL rather
            # than synthesised from the subject: that is exactly what the
            # subject tier of email_chains is for, and doing it here would
            # promote a guess to the authoritative tier.
            conversation_id=None,
            in_reply_to=message.get("in_reply_to"),
            references=truncate_references(message.get("references")),
            to_recipients=message.get("to_recipients"),
            cc_recipients=message.get("cc_recipients"),
            direction=self._direction(sender),
        )

    async def get_email_body(
        self, *, native_id: str, folder_id: str | None = None
    ) -> str | None:
        message = await self.get_email_message(native_id=native_id, folder_id=folder_id)
        return message.body if message else None

    async def get_email_message(
        self, *, native_id: str, folder_id: str | None = None
    ) -> FetchedEmail | None:
        """``FETCH BODY.PEEK[]`` returns the whole RFC message, headers included.

        So hydration backfills threading for IMAP too, the same as Gmail -- a row
        attached before FETCH_PARTS asked for these headers gets them the first
        time someone opens it.
        """
        # folder_id is meaningless here -- IMAP's only notion of a folder is
        # `mailbox`, which is fixed per integration rather than per message.
        _, password = self._credentials()
        try:
            message = await _imap.fetch_message(
                host=self.config.get("imap_host") or IMAP_HOST,
                username=self.imap_username,
                password=password,
                native_id=native_id,
                mailbox=self.config.get("mailbox") or "INBOX",
            )
        except IntegrationAuthError as exc:
            raise self._mail_auth_error(exc) from exc

        if message is None:
            return None
        return FetchedEmail(
            body=message.get("body"),
            rfc_message_id=message.get("rfc_message_id"),
            in_reply_to=message.get("in_reply_to"),
            references=truncate_references(message.get("references")),
            to_recipients=message.get("to_recipients"),
            cc_recipients=message.get("cc_recipients"),
            sender=message.get("sender"),
            direction=self._direction(message.get("sender")),
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
