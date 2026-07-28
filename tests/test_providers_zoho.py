"""Zoho Mail and Calendar.

Weighted towards the three Zoho quirks that fail *silently* rather than loudly:
the non-Bearer auth header, the mandatory accountId lookup, and the Accept header
without which event descriptions simply vanish.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.errors import IntegrationAuthError, ValidationError
from app.services.providers.base import IntegrationRef
from app.services.providers.zoho import ZohoProvider, parse_stamp

CALENDARS = "https://calendar.zoho.com/api/v1/calendars"
ACCOUNTS = "https://mail.zoho.com/api/accounts"

START = datetime(2026, 3, 11, tzinfo=timezone.utc)
END = datetime(2026, 3, 21, tzinfo=timezone.utc)


@pytest.fixture
def provider(monkeypatch):
    ref = IntegrationRef(
        id=5, provider="zoho", account_label="me@zoho.com",
        calendar_enabled=True, email_enabled=True,
    )
    p = ZohoProvider(ref, {"dc": "com"}, {"access_token": "at-1"})

    async def token(self=None):
        return "at-1"

    monkeypatch.setattr(ZohoProvider, "_token", token)
    return p


class TestAuthHeader:
    @respx.mock
    async def test_it_uses_zoho_oauthtoken_not_bearer(self, provider):
        """Bearer authenticates as nobody and returns an empty list, not a 401 --
        which is exactly why this is asserted rather than assumed."""
        route = respx.get(CALENDARS).mock(
            return_value=httpx.Response(200, json={"calendars": []})
        )
        await provider.search_events(query=None, start=START, end=END)

        auth = route.calls[0].request.headers["authorization"]
        assert auth == "Zoho-oauthtoken at-1"
        assert not auth.startswith("Bearer")

    @respx.mock
    async def test_a_401_says_reconnect(self, provider):
        respx.get(CALENDARS).mock(return_value=httpx.Response(401, json={}))
        with pytest.raises(IntegrationAuthError) as exc:
            await provider.search_events(query=None, start=START, end=END)
        assert "Reconnect" in str(exc.value)


class TestCalendar:
    @respx.mock
    async def test_it_asks_for_the_large_payload(self, provider):
        """Without Accept: application/json+large, descriptions come back empty."""
        respx.get(CALENDARS).mock(
            return_value=httpx.Response(200, json={"calendars": [{"uid": "c1", "name": "Work"}]})
        )
        route = respx.get(f"{CALENDARS}/c1/events").mock(
            return_value=httpx.Response(200, json={"events": []})
        )
        await provider.search_events(query=None, start=START, end=END)
        assert route.calls[0].request.headers["accept"] == "application/json+large"

    @respx.mock
    async def test_the_mandatory_range_is_sent_in_zoho_basic_format(self, provider):
        respx.get(CALENDARS).mock(
            return_value=httpx.Response(200, json={"calendars": [{"uid": "c1", "name": "W"}]})
        )
        route = respx.get(f"{CALENDARS}/c1/events").mock(
            return_value=httpx.Response(200, json={"events": []})
        )
        await provider.search_events(query=None, start=START, end=END)

        sent = dict(route.calls[0].request.url.params)["range"]
        assert '"start": "20260311T000000Z"' in sent or '"start":"20260311T000000Z"' in sent
        assert "20260321T000000Z" in sent

    async def test_a_window_over_31_days_is_refused_not_silently_truncated(self, provider):
        """Zoho rejects it server-side anyway; failing here says why."""
        with pytest.raises(ValidationError, match="31 days"):
            await provider.search_events(
                query=None, start=START, end=START + timedelta(days=45)
            )

    @respx.mock
    async def test_an_event_maps_onto_the_normalised_shape(self, provider):
        respx.get(CALENDARS).mock(
            return_value=httpx.Response(200, json={"calendars": [{"uid": "c1", "name": "Work"}]})
        )
        respx.get(f"{CALENDARS}/c1/events").mock(
            return_value=httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "uid": "evt-1",
                            "title": "Atlas standup",
                            "description": "daily",
                            "location": "Room 4B",
                            "dateandtime": {
                                "start": "20260318T090000+0000",
                                "end": "20260318T093000+0000",
                            },
                        }
                    ]
                },
            )
        )

        [event] = await provider.search_events(query=None, start=START, end=END)
        assert event.summary == "Atlas standup"
        assert event.start == "2026-03-18T09:00:00+00:00", "basic format coerced to ISO"
        assert event.source_uid == "evt-1"
        assert event.uid.startswith("zoho:5:evt-1:")
        assert event.calendar_name == "Work"

    @respx.mock
    async def test_one_failing_calendar_does_not_lose_the_others(self, provider):
        respx.get(CALENDARS).mock(
            return_value=httpx.Response(
                200,
                json={"calendars": [{"uid": "good", "name": "G"}, {"uid": "bad", "name": "B"}]},
            )
        )
        respx.get(f"{CALENDARS}/good/events").mock(
            return_value=httpx.Response(
                200,
                json={"events": [{"uid": "e1", "title": "Kept",
                                  "dateandtime": {"start": "20260318T090000+0000"}}]},
            )
        )
        respx.get(f"{CALENDARS}/bad/events").mock(return_value=httpx.Response(500))

        events = await provider.search_events(query=None, start=START, end=END)
        assert [e.summary for e in events] == ["Kept"]


class TestMail:
    @respx.mock
    async def test_the_account_id_is_looked_up_before_searching(self, provider):
        """Zoho Mail has no `me` alias; the numeric id is required in the path."""
        accounts = respx.get(ACCOUNTS).mock(
            return_value=httpx.Response(200, json={"data": [{"accountId": "998877"}]})
        )
        search = respx.get(f"{ACCOUNTS}/998877/messages/search").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        await provider.search_emails(keywords=["atlas"], start=START, end=END)
        assert accounts.called
        assert search.called

    @respx.mock
    async def test_a_cached_account_id_skips_the_lookup(self, provider):
        provider.config["account_id"] = "123"
        accounts = respx.get(ACCOUNTS).mock(return_value=httpx.Response(200, json={"data": []}))
        search = respx.get(f"{ACCOUNTS}/123/messages/search").mock(
            return_value=httpx.Response(200, json={"data": []})
        )

        await provider.search_emails(keywords=["atlas"], start=START, end=END)
        assert not accounts.called
        assert search.called

    @respx.mock
    async def test_no_mail_account_says_so_clearly(self, provider):
        respx.get(ACCOUNTS).mock(return_value=httpx.Response(200, json={"data": []}))
        with pytest.raises(IntegrationAuthError, match="no mail accounts"):
            await provider.search_emails(keywords=["x"], start=START, end=END)

    @respx.mock
    async def test_a_message_maps_onto_the_normalised_shape(self, provider):
        respx.get(ACCOUNTS).mock(
            return_value=httpx.Response(200, json={"data": [{"accountId": "1"}]})
        )
        respx.get(f"{ACCOUNTS}/1/messages/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "messageId": "m-1",
                            # Always present on a real search response, and the
                            # deep link is useless without it.
                            "folderId": "f-9",
                            "fromAddress": "priya@acme.com",
                            "subject": "Re: cutover",
                            "summary": "rollback rehearsal",
                            # Zoho returns epoch milliseconds here.
                            "receivedTime": "1773500000000",
                        }
                    ]
                },
            )
        )

        [mail] = await provider.search_emails(keywords=["atlas"], start=START, end=END)
        assert mail.subject == "Re: cutover"
        assert mail.snippet == "rollback rehearsal"
        assert mail.message_id == "zoho:5:m-1"
        assert mail.url == "https://mail.zoho.com/zm/#mail/folder/f-9/m-1"
        # Zoho's search payload has no RFC 2822 header, so the Gmail-style
        # fallback link must not be attempted for it.
        assert mail.rfc_message_id is None

    @respx.mock
    async def test_messages_outside_the_window_are_filtered_locally(self, provider):
        """Zoho's search takes only an upper time bound, so the lower edge has to
        be enforced here or a match returns mail from months earlier."""
        respx.get(ACCOUNTS).mock(
            return_value=httpx.Response(200, json={"data": [{"accountId": "1"}]})
        )
        inside = int(datetime(2026, 3, 15, tzinfo=timezone.utc).timestamp() * 1000)
        outside = int(datetime(2025, 11, 1, tzinfo=timezone.utc).timestamp() * 1000)
        respx.get(f"{ACCOUNTS}/1/messages/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {"messageId": "keep", "subject": "In window", "receivedTime": str(inside)},
                        {"messageId": "drop", "subject": "Too old", "receivedTime": str(outside)},
                    ]
                },
            )
        )

        mails = await provider.search_emails(keywords=["x"], start=START, end=END)
        assert [m.subject for m in mails] == ["In window"]


class TestStampParsing:
    @pytest.mark.parametrize(
        "given,expected",
        [
            ("20260318T090000+0000", "2026-03-18T09:00:00+00:00"),
            ("20260318T090000Z", "2026-03-18T09:00:00+00:00"),
            # All-day stays a date: coercing to midnight lands it on the wrong day.
            # Also all digits, so it must not be mistaken for an epoch stamp.
            ("20260318", "2026-03-18"),
            ("1773500000000", "2026-03-14T14:53:20+00:00"),
            (None, None),
            ("", None),
        ],
    )
    def test_it_normalises_zohos_formats(self, given, expected):
        assert parse_stamp(given) == expected

    def test_an_unrecognised_stamp_is_kept_rather_than_dropped(self):
        assert parse_stamp("next tuesday") == "next tuesday"


class TestDataCentre:
    @respx.mock
    async def test_the_configured_data_centre_is_used(self, monkeypatch):
        """An account lives in one region; the wrong host authenticates fine and
        returns nothing, which is the worst possible failure mode."""
        ref = IntegrationRef(id=6, provider="zoho", account_label="eu@zoho.eu",
                             calendar_enabled=True)
        p = ZohoProvider(ref, {"dc": "eu"}, {"access_token": "at"})

        async def token(self=None):
            return "at"

        monkeypatch.setattr(ZohoProvider, "_token", token)
        route = respx.get("https://calendar.zoho.eu/api/v1/calendars").mock(
            return_value=httpx.Response(200, json={"calendars": []})
        )
        await p.search_events(query=None, start=START, end=END)
        assert route.called


class TestConnectionTest:
    @respx.mock
    async def test_each_capability_is_reported_separately(self, provider):
        respx.get(CALENDARS).mock(return_value=httpx.Response(200, json={"calendars": []}))
        respx.get(ACCOUNTS).mock(return_value=httpx.Response(403, text="scope missing"))

        result = await provider.test()
        checks = {c["name"]: c["ok"] for c in result["checks"]}
        assert checks == {"authorisation": True, "calendar": True, "mail": False}
        assert result["ok"] is False


class TestIdentity:
    """Regression cover for a live failure: the first real connect attempt got a
    401 from /oauth/user/info and surfaced as a bare 500."""

    def test_the_profile_scope_is_requested(self):
        """Zoho has no "email" scope, and /oauth/user/info needs this one. A
        grant is fixed at consent time, so getting it wrong here cannot be
        repaired later in the flow."""
        from app.services.providers.zoho import SCOPES

        assert "AaaServer.profile.READ" in SCOPES
        assert "email" not in SCOPES

    @pytest.mark.parametrize(
        "server,expected",
        [
            ("https://accounts.zoho.com", "com"),
            ("https://accounts.zoho.eu", "eu"),
            ("https://accounts.zoho.com.au", "com.au"),
            ("accounts.zoho.in", "in"),
            ("", None),
            (None, None),
            ("https://example.test", None),
        ],
    )
    def test_the_data_centre_is_read_off_the_callback(self, server, expected):
        """Zoho appends accounts-server to the redirect, and a token is only
        valid in the DC that issued it -- more trustworthy than a config field."""
        from app.services.providers.zoho import dc_from_accounts_server

        assert dc_from_accounts_server(server) == expected

    def test_the_callback_hint_beats_the_configured_default(self):
        from app.services.providers.zoho import resolve_dc

        assert resolve_dc(None, {"accounts_server": "https://accounts.zoho.eu"}) == "eu"
        assert resolve_dc(None, {}) == "com"

    @respx.mock
    def test_a_401_becomes_a_readable_error_not_a_500(self):
        """What the user actually hit. httpx's raise_for_status here escaped as
        an unhandled exception and the browser got "internal error"."""
        from app.services.providers.zoho import fetch_identity

        respx.get("https://accounts.zoho.com/oauth/user/info").mock(
            return_value=httpx.Response(401, json={})
        )
        with pytest.raises(IntegrationAuthError) as exc:
            fetch_identity("at-1")

        message = str(exc.value)
        assert "AaaServer.profile.READ" in message
        assert "data centre" in message

    @respx.mock
    def test_identity_uses_the_zoho_header_and_reports_the_dc(self):
        from app.services.providers.zoho import fetch_identity

        route = respx.get("https://accounts.zoho.eu/oauth/user/info").mock(
            return_value=httpx.Response(
                200, json={"ZUID": "778899", "Email": "me@zoho.eu"}
            )
        )
        identity = fetch_identity(
            "at-1", None, hints={"accounts_server": "https://accounts.zoho.eu"}
        )

        assert route.calls[0].request.headers["authorization"] == "Zoho-oauthtoken at-1"
        assert identity == {"account_key": "778899", "email": "me@zoho.eu", "dc": "eu"}


class TestEmailPresentation:
    """Both found on a real Zoho account: the deep link opened the mailbox but
    no message, and summaries rendered with literal &lt; and &quot;."""

    def _provider(self):
        ref = IntegrationRef(id=4, provider="zoho", account_label="me@zoho.com",
                             email_enabled=True)
        return ZohoProvider(ref, {"dc": "com"}, {"access_token": "at"})

    def test_the_link_uses_the_real_folder_id(self):
        """The hash route needs an actual folder; a placeholder segment loads the
        mailbox and resolves to nothing."""
        mail = self._provider()._to_email(
            {"messageId": "msg-1001", "folderId": "folder-2002", "subject": "S"}
        )
        assert mail.url == (
            "https://mail.zoho.com/zm/#mail/folder/folder-2002/msg-1001"
        )

    def test_no_folder_means_no_link_rather_than_a_broken_one(self):
        """A link that lands on the wrong screen is worse than no link."""
        mail = self._provider()._to_email({"messageId": "123", "subject": "S"})
        assert mail.url is None

    def test_html_entities_are_decoded(self):
        mail = self._provider()._to_email(
            {
                "messageId": "1",
                "folderId": "2",
                "subject": "Re: R&amp;D sync",
                "fromAddress": "&quot;Ada&quot;&lt;ada@example.com&gt;",
                "summary": "notes &lt;attached&gt; &amp; reviewed",
            }
        )
        assert mail.subject == "Re: R&D sync"
        assert mail.sender == '"Ada"<ada@example.com>'
        assert mail.snippet == "notes <attached> & reviewed"

    def test_the_data_centre_is_reflected_in_the_link(self):
        ref = IntegrationRef(id=4, provider="zoho", account_label="x", email_enabled=True)
        mail = ZohoProvider(ref, {"dc": "eu"}, {})._to_email(
            {"messageId": "1", "folderId": "2"}
        )
        assert mail.url.startswith("https://mail.zoho.eu/")
