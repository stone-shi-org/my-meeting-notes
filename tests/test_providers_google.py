"""Google Calendar and Gmail, faked at the HTTP boundary with respx.

The recurrence case is the one that matters most: Google returns the same
``iCalUID`` for every occurrence, so keying candidates on it would silently
collapse a weekly standup into one result.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.services.providers.base import IntegrationRef
from app.services.providers.google import GoogleProvider

CALENDAR_LIST = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
GMAIL_LIST = "https://gmail.googleapis.com/gmail/v1/users/me/messages"

START = datetime(2026, 3, 11, tzinfo=timezone.utc)
END = datetime(2026, 3, 21, tzinfo=timezone.utc)


@pytest.fixture
def provider(monkeypatch):
    ref = IntegrationRef(
        id=7, provider="google", account_label="me@example.com",
        calendar_enabled=True, email_enabled=True,
    )
    p = GoogleProvider(ref, {}, {"access_token": "at-1"})

    async def token(self=None):
        return "at-1"

    monkeypatch.setattr(GoogleProvider, "_token", token)
    return p


def events_route(calendar_id: str):
    from urllib.parse import quote

    return f"https://www.googleapis.com/calendar/v3/calendars/{quote(calendar_id, safe='')}/events"


class TestCalendar:
    @respx.mock
    async def test_it_maps_an_event_onto_the_normalised_shape(self, provider):
        respx.get(CALENDAR_LIST).mock(
            return_value=httpx.Response(
                200, json={"items": [{"id": "me@example.com", "summary": "Work", "selected": True}]}
            )
        )
        respx.get(events_route("me@example.com")).mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "evt_20260318T090000Z",
                            "iCalUID": "series-abc@google.com",
                            "summary": "Atlas standup",
                            "description": "daily",
                            "location": "Room 4B",
                            "start": {"dateTime": "2026-03-18T09:00:00Z"},
                            "end": {"dateTime": "2026-03-18T09:30:00Z"},
                            "htmlLink": "https://calendar.google.com/event?eid=xyz",
                        }
                    ]
                },
            )
        )

        [event] = await provider.search_events(query=None, start=START, end=END)

        assert event.summary == "Atlas standup"
        assert event.url == "https://calendar.google.com/event?eid=xyz"
        assert event.calendar_name == "Work"
        assert event.start == "2026-03-18T09:00:00Z"
        # uid is namespaced and instance-scoped; the series id is kept separately.
        assert event.uid == "google:7:evt_20260318T090000Z"
        assert event.source_uid == "series-abc@google.com"

    @respx.mock
    async def test_recurring_instances_stay_distinct(self, provider):
        """Both occurrences share one iCalUID. Keying on it would lose one."""
        respx.get(CALENDAR_LIST).mock(
            return_value=httpx.Response(200, json={"items": [{"id": "c1", "summary": "Work"}]})
        )
        respx.get(events_route("c1")).mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "evt_20260318T090000Z",
                            "iCalUID": "series@google.com",
                            "summary": "Standup",
                            "start": {"dateTime": "2026-03-18T09:00:00Z"},
                            "end": {"dateTime": "2026-03-18T09:30:00Z"},
                        },
                        {
                            "id": "evt_20260319T090000Z",
                            "iCalUID": "series@google.com",
                            "summary": "Standup",
                            "start": {"dateTime": "2026-03-19T09:00:00Z"},
                            "end": {"dateTime": "2026-03-19T09:30:00Z"},
                        },
                    ]
                },
            )
        )

        events = await provider.search_events(query=None, start=START, end=END)
        assert len({e.uid for e in events}) == 2
        assert len({e.source_uid for e in events}) == 1

    @respx.mock
    async def test_it_asks_the_server_to_expand_recurrences(self, provider):
        route_list = respx.get(CALENDAR_LIST).mock(
            return_value=httpx.Response(200, json={"items": [{"id": "c1", "summary": "W"}]})
        )
        route = respx.get(events_route("c1")).mock(
            return_value=httpx.Response(200, json={"items": []})
        )
        await provider.search_events(query=None, start=START, end=END)

        assert route_list.called
        sent = dict(route.calls[0].request.url.params)
        assert sent["singleEvents"] == "true", "otherwise recurrences arrive unexpanded"
        assert sent["timeMin"] == START.isoformat()
        assert sent["timeMax"] == END.isoformat()

    @respx.mock
    async def test_an_all_day_event_keeps_its_date(self, provider):
        """All-day events carry `date`, not `dateTime`. Today's MCP path never
        produced a date-only start, so this is genuinely new territory."""
        respx.get(CALENDAR_LIST).mock(
            return_value=httpx.Response(200, json={"items": [{"id": "c1", "summary": "W"}]})
        )
        respx.get(events_route("c1")).mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "e1",
                            "iCalUID": "u1",
                            "summary": "Company holiday",
                            "start": {"date": "2026-03-18"},
                            "end": {"date": "2026-03-19"},
                        }
                    ]
                },
            )
        )
        [event] = await provider.search_events(query=None, start=START, end=END)
        assert event.start == "2026-03-18"

    @respx.mock
    async def test_a_cancelled_event_is_dropped(self, provider):
        respx.get(CALENDAR_LIST).mock(
            return_value=httpx.Response(200, json={"items": [{"id": "c1", "summary": "W"}]})
        )
        respx.get(events_route("c1")).mock(
            return_value=httpx.Response(
                200,
                json={
                    "items": [
                        {"id": "e1", "iCalUID": "u1", "status": "cancelled",
                         "start": {"dateTime": "2026-03-18T09:00:00Z"}},
                        {"id": "e2", "iCalUID": "u2", "summary": "Real",
                         "start": {"dateTime": "2026-03-18T10:00:00Z"}},
                    ]
                },
            )
        )
        events = await provider.search_events(query=None, start=START, end=END)
        assert [e.summary for e in events] == ["Real"]

    @respx.mock
    async def test_one_broken_calendar_does_not_lose_the_others(self, provider):
        respx.get(CALENDAR_LIST).mock(
            return_value=httpx.Response(
                200,
                json={"items": [{"id": "good", "summary": "Good"}, {"id": "bad", "summary": "Bad"}]},
            )
        )
        respx.get(events_route("good")).mock(
            return_value=httpx.Response(
                200,
                json={"items": [{"id": "e1", "iCalUID": "u1", "summary": "Kept",
                                 "start": {"dateTime": "2026-03-18T09:00:00Z"}}]},
            )
        )
        respx.get(events_route("bad")).mock(return_value=httpx.Response(500, text="boom"))

        events = await provider.search_events(query=None, start=START, end=END)
        assert [e.summary for e in events] == ["Kept"]

    @respx.mock
    async def test_a_calendar_id_with_an_at_sign_is_encoded(self, provider):
        """Calendar ids are email-like; an unencoded '@' or '#' breaks the path."""
        respx.get(CALENDAR_LIST).mock(
            return_value=httpx.Response(
                200, json={"items": [{"id": "en.uk#holiday@group.v.calendar.google.com", "summary": "H"}]}
            )
        )
        route = respx.get(
            events_route("en.uk#holiday@group.v.calendar.google.com")
        ).mock(return_value=httpx.Response(200, json={"items": []}))

        await provider.search_events(query=None, start=START, end=END)
        assert route.called


class TestGmail:
    @respx.mock
    async def test_it_lists_then_fetches_metadata(self, provider):
        respx.get(GMAIL_LIST).mock(
            return_value=httpx.Response(200, json={"messages": [{"id": "m1"}]})
        )
        respx.get(f"{GMAIL_LIST}/m1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "m1",
                    "snippet": "rollback rehearsal is booked",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Re: cutover window"},
                            {"name": "From", "value": "priya@acme.com"},
                            {"name": "Date", "value": "Tue, 17 Mar 2026 17:42:00 +0000"},
                            {"name": "Message-ID", "value": "<cutover@acme.com>"},
                        ]
                    },
                },
            )
        )

        [email] = await provider.search_emails(keywords=["atlas"], start=START, end=END)

        assert email.subject == "Re: cutover window"
        assert email.sender == "priya@acme.com"
        assert email.snippet == "rollback rehearsal is booked"
        assert email.message_id == "google:7:m1"
        assert email.rfc_message_id == "<cutover@acme.com>"
        assert email.url == "https://mail.google.com/mail/u/0/#all/m1"

    @respx.mock
    async def test_the_search_uses_gmail_date_syntax(self, provider):
        route = respx.get(GMAIL_LIST).mock(return_value=httpx.Response(200, json={}))
        await provider.search_emails(keywords=["atlas", "cutover"], start=START, end=END)

        params = dict(route.calls[0].request.url.params)
        assert "after:2026/03/11" in params["q"], "slashes, not ISO -- Gmail rejects ISO"
        assert "before:2026/03/21" in params["q"]
        assert "(atlas OR cutover)" in params["q"]

    @respx.mock
    async def test_the_id_list_is_bounded_before_fetching(self, provider):
        """Bounding after the fetch would mean paying for gets we throw away."""
        route = respx.get(GMAIL_LIST).mock(return_value=httpx.Response(200, json={}))
        await provider.search_emails(keywords=["x"], start=START, end=END)
        assert dict(route.calls[0].request.url.params)["maxResults"] == "25"

    @respx.mock
    async def test_no_results_makes_no_further_calls(self, provider):
        respx.get(GMAIL_LIST).mock(return_value=httpx.Response(200, json={"messages": []}))
        assert await provider.search_emails(keywords=["x"], start=START, end=END) == []

    @respx.mock
    async def test_one_unreadable_message_does_not_lose_the_rest(self, provider):
        respx.get(GMAIL_LIST).mock(
            return_value=httpx.Response(
                200, json={"messages": [{"id": "m1"}, {"id": "m2"}]}
            )
        )
        respx.get(f"{GMAIL_LIST}/m1").mock(return_value=httpx.Response(500, text="boom"))
        respx.get(f"{GMAIL_LIST}/m2").mock(
            return_value=httpx.Response(
                200,
                json={"id": "m2", "snippet": "kept",
                      "payload": {"headers": [{"name": "Subject", "value": "Kept"}]}},
            )
        )

        emails = await provider.search_emails(keywords=["x"], start=START, end=END)
        assert [e.subject for e in emails] == ["Kept"]

    @respx.mock
    async def test_a_missing_snippet_is_tolerated(self, provider):
        """format=metadata only promises ids, labels and headers."""
        respx.get(GMAIL_LIST).mock(
            return_value=httpx.Response(200, json={"messages": [{"id": "m1"}]})
        )
        respx.get(f"{GMAIL_LIST}/m1").mock(
            return_value=httpx.Response(
                200,
                json={"id": "m1", "payload": {"headers": [{"name": "Subject", "value": "S"}]}},
            )
        )
        [email] = await provider.search_emails(keywords=["x"], start=START, end=END)
        assert email.snippet is None
        assert email.subject == "S"


class TestAuthFailure:
    @respx.mock
    async def test_a_401_says_reconnect_rather_than_leaking_the_status(self, provider):
        from app.errors import IntegrationAuthError

        respx.get(CALENDAR_LIST).mock(return_value=httpx.Response(401, json={}))
        with pytest.raises(IntegrationAuthError) as exc:
            await provider.search_events(query=None, start=START, end=END)
        assert "Reconnect" in str(exc.value)


class TestConnectionTest:
    @respx.mock
    async def test_each_capability_is_checked_separately(self, provider):
        """A half-working account is a real outcome, so one flag is not enough."""
        respx.get(CALENDAR_LIST).mock(return_value=httpx.Response(200, json={"items": []}))
        respx.get("https://gmail.googleapis.com/gmail/v1/users/me/profile").mock(
            return_value=httpx.Response(403, text="Gmail API disabled")
        )

        result = await provider.test()
        checks = {c["name"]: c["ok"] for c in result["checks"]}
        assert checks == {"authorisation": True, "calendar": True, "gmail": False}
        assert result["ok"] is False
        assert "gmail" in result["error"]
