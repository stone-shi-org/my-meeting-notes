"""iCloud over CalDAV and IMAP.

The recurrence tests are the point of this file. The fixture's weekly series
starts six months *before* the search window, has one instance deleted by EXDATE
and another moved by a RECURRENCE-ID override -- exactly the shape that silently
produces wrong results if expansion is done naively or delegated to iCloud's
unreliable <C:expand>.
"""

from __future__ import annotations

import imaplib
from datetime import datetime, timezone

import httpx
import pytest
import respx

from app.errors import IntegrationAuthError
from app.services.providers import _imap
from app.services.providers.apple import AppleProvider
from app.services.providers.base import IntegrationRef
from tests.conftest import FIXTURES

CALDAV = "https://caldav.icloud.com/"
SHARD = "https://p34-caldav.icloud.com"

START = datetime(2026, 3, 11, tzinfo=timezone.utc)
END = datetime(2026, 3, 27, tzinfo=timezone.utc)

PRINCIPAL_XML = """<?xml version="1.0"?>
<multistatus xmlns="DAV:"><response><href>/</href><propstat><prop>
<current-user-principal><href>/123456/principal/</href></current-user-principal>
</prop><status>HTTP/1.1 200 OK</status></propstat></response></multistatus>"""

HOME_XML = f"""<?xml version="1.0"?>
<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<response><href>/123456/principal/</href><propstat><prop>
<c:calendar-home-set><href>{SHARD}/123456/calendars/</href></c:calendar-home-set>
</prop><status>HTTP/1.1 200 OK</status></propstat></response></multistatus>"""

CALENDARS_XML = """<?xml version="1.0"?>
<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<response><href>/123456/calendars/work/</href><propstat><prop>
<resourcetype><collection/><c:calendar/></resourcetype>
<displayname>Work</displayname>
<c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>
</prop><status>HTTP/1.1 200 OK</status></propstat></response>
<response><href>/123456/calendars/reminders/</href><propstat><prop>
<resourcetype><collection/><c:calendar/></resourcetype>
<displayname>Reminders</displayname>
<c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set>
</prop><status>HTTP/1.1 200 OK</status></propstat></response>
<response><href>/123456/calendars/</href><propstat><prop>
<resourcetype><collection/></resourcetype><displayname>Home</displayname>
</prop><status>HTTP/1.1 200 OK</status></propstat></response>
</multistatus>"""


def report_xml(ics: str) -> str:
    return (
        '<?xml version="1.0"?>'
        '<multistatus xmlns="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<response><href>/123456/calendars/work/1.ics</href><propstat><prop>"
        f"<c:calendar-data>{ics}</c:calendar-data>"
        "</prop><status>HTTP/1.1 200 OK</status></propstat></response></multistatus>"
    )


@pytest.fixture
def provider():
    ref = IntegrationRef(
        id=3, provider="apple", account_label="me@icloud.com",
        calendar_enabled=True, email_enabled=True,
    )
    return AppleProvider(ref, {}, {"username": "me@icloud.com", "password": "abcd-efgh-ijkl-mnop"})


@pytest.fixture
def icloud():
    """Discovery plus a REPORT returning the recurrence fixture."""
    ics = (FIXTURES / "icloud_recurring.ics").read_text()
    with respx.mock(assert_all_called=False) as router:
        router.request("PROPFIND", CALDAV).mock(
            return_value=httpx.Response(207, text=PRINCIPAL_XML)
        )
        router.request("PROPFIND", f"{CALDAV}123456/principal/").mock(
            return_value=httpx.Response(207, text=HOME_XML)
        )
        router.request("PROPFIND", f"{SHARD}/123456/calendars/").mock(
            return_value=httpx.Response(207, text=CALENDARS_XML)
        )
        router.request("REPORT", f"{SHARD}/123456/calendars/work/").mock(
            return_value=httpx.Response(207, text=report_xml(ics))
        )
        yield router


class TestDiscovery:
    async def test_it_follows_the_two_hop_shard_discovery(self, provider, icloud):
        """iCloud answers with a principal, and the principal with a calendar
        home on a *different* host. Missing the second hop 404s everything."""
        await provider.search_events(query=None, start=START, end=END)

        called = [(c.request.method, str(c.request.url)) for c in icloud.calls]
        assert ("PROPFIND", CALDAV) in called
        assert ("PROPFIND", f"{CALDAV}123456/principal/") in called
        assert ("PROPFIND", f"{SHARD}/123456/calendars/") in called

    async def test_non_event_collections_are_skipped(self, provider, icloud):
        """A VTODO collection and a plain collection must not be queried."""
        await provider.search_events(query=None, start=START, end=END)
        reported = [str(c.request.url) for c in icloud.calls if c.request.method == "REPORT"]
        assert reported == [f"{SHARD}/123456/calendars/work/"]

    async def test_a_401_names_the_app_specific_password(self, provider):
        """The overwhelmingly common cause is using the account password."""
        with respx.mock:
            respx.request("PROPFIND", CALDAV).mock(return_value=httpx.Response(401))
            with pytest.raises(IntegrationAuthError) as exc:
                await provider.search_events(query=None, start=START, end=END)
        assert "app-specific password" in str(exc.value)


class TestRecurrence:
    async def test_a_series_starting_before_the_window_still_appears(self, provider, icloud):
        """The master DTSTART is 15 Sep 2025, six months before the window. This
        is what RFC 4791 time-range filtering is for, and what naive parsing
        (just reading DTSTART) gets wrong."""
        events = await provider.search_events(query=None, start=START, end=END)
        standups = [e for e in events if "Standup" in (e.summary or "")]
        assert standups, "the recurring series must be found"

    async def test_each_occurrence_is_a_distinct_candidate(self, provider, icloud):
        """They share one UID; keying on it would collapse them into one."""
        events = await provider.search_events(query=None, start=START, end=END)
        standups = [e for e in events if "Standup" in (e.summary or "")]

        assert len({e.uid for e in standups}) == len(standups) > 1
        assert len({e.source_uid for e in standups}) == 1

    async def test_an_exdate_occurrence_is_absent(self, provider, icloud):
        """18 March is deleted from the series and must not be offered."""
        events = await provider.search_events(query=None, start=START, end=END)
        starts = [e.start for e in events if "Standup" in (e.summary or "")]
        assert not any(s.startswith("2026-03-18") for s in starts)

    async def test_a_moved_occurrence_uses_the_override(self, provider, icloud):
        """25 March was moved 09:00 -> 10:00 and retitled by a RECURRENCE-ID."""
        events = await provider.search_events(query=None, start=START, end=END)
        moved = [e for e in events if e.start and e.start.startswith("2026-03-25")]

        assert len(moved) == 1
        assert "go/no-go" in moved[0].summary
        assert moved[0].start.startswith("2026-03-25T10:00")
        assert moved[0].location == "Room 7A"

    async def test_an_all_day_event_stays_a_date(self, provider, icloud):
        """VALUE=DATE has no time. Coercing it to midnight in the wrong zone is
        how an all-day event shows up on the previous day."""
        events = await provider.search_events(query=None, start=START, end=END)
        [holiday] = [e for e in events if e.summary == "Company holiday"]
        assert holiday.start == "2026-03-20"

    async def test_ordinary_fields_survive_expansion(self, provider, icloud):
        events = await provider.search_events(query=None, start=START, end=END)
        [dentist] = [e for e in events if e.summary == "Dentist"]
        assert dentist.start.startswith("2026-03-19T14:00")
        assert dentist.calendar_name == "Work"
        assert dentist.provider == "apple"
        assert dentist.url is None, "iCloud has no per-event web link"


class TestCalDavRequest:
    async def test_the_report_carries_a_utc_time_range(self, provider, icloud):
        await provider.search_events(query=None, start=START, end=END)
        report = next(c for c in icloud.calls if c.request.method == "REPORT")
        body = report.request.content.decode()

        assert 'start="20260311T000000Z"' in body
        assert 'end="20260327T000000Z"' in body
        assert "calendar-query" in body
        assert report.request.headers["Depth"] == "1"


# --------------------------------------------------------------------------- #
# IMAP
# --------------------------------------------------------------------------- #


class FakeIMAP:
    """Stands in for imaplib.IMAP4_SSL."""

    def __init__(self, host, port=993):
        self.host = host
        self.logged_in_as = None
        self.criteria = None
        self.fail_login = False

    def login(self, username, password):
        if self.fail_login:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        self.logged_in_as = (username, password)
        return "OK", [b"ok"]

    def select(self, mailbox, readonly=False):
        self.mailbox = mailbox
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        self.criteria = list(criteria)
        return "OK", [b"1 2"]

    def fetch(self, num, parts):
        raw = (
            b"Subject: =?utf-8?B?UmU6IGN1dG92ZXIgd2luZG93?=\r\n"
            b"From: priya@acme.com\r\n"
            b"Date: Tue, 17 Mar 2026 17:42:00 +0000\r\n"
            b"Message-ID: <cutover-" + num + b"@acme.com>\r\n\r\n"
        )
        return "OK", [(b"1 (BODY[HEADER.FIELDS ...]", raw), b")"]

    def logout(self):
        return "BYE", [b"bye"]


class TestImapCriteria:
    def test_a_single_keyword_needs_no_or(self):
        assert _imap.build_criteria(["atlas"], START, END) == [
            "SINCE", "11-Mar-2026", "BEFORE", "27-Mar-2026", "TEXT", "atlas",
        ]

    def test_three_keywords_nest_as_a_binary_or_tree(self):
        """IMAP's OR is strictly binary and prefix. A flat "OR a b c" is not an
        error -- the server just returns the wrong set."""
        assert _imap.build_criteria(["a", "b", "c"], START, END)[4:] == [
            "OR", "TEXT", "a", "OR", "TEXT", "b", "TEXT", "c",
        ]

    def test_no_keywords_leaves_a_plain_date_window(self):
        assert _imap.build_criteria([], START, END) == [
            "SINCE", "11-Mar-2026", "BEFORE", "27-Mar-2026",
        ]

    def test_the_date_format_is_imap_not_iso(self):
        """dd-Mon-yyyy. ISO is silently rejected by many servers."""
        assert "11-Mar-2026" in _imap.build_criteria([], START, END)


class TestImapSearch:
    async def test_it_decodes_encoded_headers(self):
        results = await _imap.search(
            host="imap.test", username="u", password="p",
            keywords=["atlas"], start=START, end=END, connect_fn=FakeIMAP,
        )
        assert results[0]["subject"] == "Re: cutover window"
        assert results[0]["sender"] == "priya@acme.com"
        assert results[0]["rfc_message_id"].startswith("<cutover-")

    async def test_a_rejected_login_names_the_app_specific_password(self):
        def failing(host, port=993):
            client = FakeIMAP(host, port)
            client.fail_login = True
            return client

        with pytest.raises(IntegrationAuthError) as exc:
            await _imap.search(
                host="imap.test", username="u", password="wrong",
                keywords=[], start=START, end=END, connect_fn=failing,
            )
        assert "app-specific password" in str(exc.value)

    async def test_the_mailbox_is_opened_read_only(self):
        """A search must never mark mail as read."""
        opened = {}

        def spy(host, port=993):
            client = FakeIMAP(host, port)
            original = client.select

            def select(mailbox, readonly=False):
                opened["readonly"] = readonly
                return original(mailbox, readonly)

            client.select = select
            return client

        await _imap.search(
            host="imap.test", username="u", password="p",
            keywords=[], start=START, end=END, connect_fn=spy,
        )
        assert opened["readonly"] is True


class TestAppleEmail:
    async def test_identity_prefers_the_rfc_message_id(self, provider, monkeypatch):
        """IMAP sequence numbers are per-session and get reused, so they cannot
        be the stable identity of a message."""
        async def fake_search(**kwargs):
            return [
                {"id": "1", "subject": "S", "sender": "a@b", "date": "d",
                 "rfc_message_id": "<stable@acme.com>"}
            ]

        monkeypatch.setattr(_imap, "search", fake_search)
        [mail] = await provider.search_emails(keywords=["x"], start=START, end=END)

        assert mail.message_id == "apple:3:<stable@acme.com>"
        assert mail.rfc_message_id == "<stable@acme.com>"
        assert mail.snippet is None, "header-only fetch has no body to snippet"
        assert mail.url is None
