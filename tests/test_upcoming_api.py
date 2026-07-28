"""The home-screen upcoming list, and creating a meeting from one of its events.

Providers are faked at ``providers.loader.load_for_user`` -- the same seam
``test_matching.py`` uses -- so these tests describe "a calendar account failed"
rather than any one backend's transport.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.errors import ProviderError
from app.services import upcoming as up
from app.services.providers.base import EventCandidate, IntegrationRef


def at(days: float, hour: int = 9) -> str:
    """An ISO stamp `days` from now, so fixtures stay inside a live window."""
    when = datetime.now(timezone.utc) + timedelta(days=days)
    return when.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


class FakeCalendar:
    """One connected calendar account."""

    mode = "ok"
    events: list[dict] = []

    def __init__(self, *, integration_id: int = 1, label: str = "work@x", provider="fake"):
        self.ref = IntegrationRef(
            id=integration_id,
            provider=provider,
            account_label=label,
            calendar_enabled=True,
        )

    async def search_events(self, **kwargs):
        if FakeCalendar.mode == "down":
            raise ProviderError("Calendar unreachable", kind="calendar")
        return [EventCandidate(**e) for e in FakeCalendar.events]


DEFAULT_EVENTS = [
    {
        "uid": "fake:1:standup-tomorrow",
        "source_uid": "standup",
        "summary": "Atlas Migration — Daily Standup",
        "description": "Round the room",
        "location": "Room 4B",
        "start": at(1),
        "end": at(1, hour=10),
        "attendees": ("Priya Raman", "Donna Chen", "Sam Okafor"),
        "calendar_name": "Work",
        "account": "work@x",
        "type": "google",
        "provider": "fake",
        "integration_id": 1,
    },
    {
        "uid": "fake:1:review-next-week",
        "source_uid": "review",
        "summary": "Cutover go/no-go",
        "start": at(8),
        "end": at(8, hour=10),
        "attendees": ("Priya Raman",),
        "calendar_name": "Work",
        "account": "work@x",
        "provider": "fake",
        "integration_id": 1,
    },
]


@pytest.fixture(autouse=True)
def wiring(monkeypatch):
    FakeCalendar.mode = "ok"
    FakeCalendar.events = DEFAULT_EVENTS

    def load(conn, user_id, *, kind=None):
        if user_id is None or kind == "email":
            return []
        return [FakeCalendar()]

    monkeypatch.setattr("app.services.upcoming.providers_svc.load_for_user", load)


def get_upcoming(client, **params):
    resp = client.get("/api/calendar/upcoming", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #


class TestWindow:
    def test_starts_at_midnight_this_morning(self):
        """An event from earlier today is still worth writing up."""
        now = datetime(2026, 7, 28, 14, 30, tzinfo=timezone.utc)
        start, _ = up.window(14, now=now)
        assert start == datetime(2026, 7, 28, 0, 0, tzinfo=timezone.utc)

    def test_ends_the_requested_number_of_days_ahead(self):
        now = datetime(2026, 7, 28, 14, 30, tzinfo=timezone.utc)
        _, end = up.window(14, now=now)
        assert end == now + timedelta(days=14)

    def test_stays_under_zohos_31_day_cap(self):
        """Zoho rejects a longer range outright, so the ceiling must clear it."""
        now = datetime(2026, 7, 28, 14, 30, tzinfo=timezone.utc)
        start, end = up.window(up.MAX_DAYS, now=now)
        assert (end - start) < timedelta(days=31)


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


class TestUpcomingList:
    def test_lists_events_across_the_window(self, user_client):
        body = get_upcoming(user_client)
        assert [e["summary"] for e in body["events"]] == [
            "Atlas Migration — Daily Standup",
            "Cutover go/no-go",
        ]
        assert body["connected"] == 1
        assert body["error"] is None

    def test_events_are_sorted_soonest_first(self, user_client):
        FakeCalendar.events = list(reversed(DEFAULT_EVENTS))
        starts = [e["start"] for e in get_upcoming(user_client)["events"]]
        assert starts == sorted(starts)

    def test_undated_events_sort_last_rather_than_first(self, user_client):
        FakeCalendar.events = [{"uid": "fake:1:undated", "summary": "No date"}, *DEFAULT_EVENTS]
        assert get_upcoming(user_client)["events"][-1]["summary"] == "No date"

    def test_attendees_come_through_for_the_speaker_prefill(self, user_client):
        first = get_upcoming(user_client)["events"][0]
        assert first["attendees"] == ["Priya Raman", "Donna Chen", "Sam Okafor"]

    def test_the_same_event_seen_twice_is_collapsed(self, user_client, monkeypatch):
        """Two accounts, one real event: same source_uid and start, different uid."""
        duplicate = {**DEFAULT_EVENTS[0], "uid": "fake:2:standup-tomorrow", "integration_id": 2}

        class Second(FakeCalendar):
            async def search_events(self, **kwargs):
                return [EventCandidate(**duplicate)]

        monkeypatch.setattr(
            "app.services.upcoming.providers_svc.load_for_user",
            lambda conn, uid, *, kind=None: [
                FakeCalendar(),
                Second(integration_id=2, label="personal@x"),
            ],
        )
        uids = [e["uid"] for e in get_upcoming(user_client)["events"]]
        assert uids.count("fake:1:standup-tomorrow") == 1
        assert "fake:2:standup-tomorrow" not in uids

    def test_no_calendar_connected_is_an_empty_list_not_an_error(
        self, user_client, monkeypatch
    ):
        """This renders on the home screen for every user, connected or not."""
        monkeypatch.setattr(
            "app.services.upcoming.providers_svc.load_for_user",
            lambda conn, uid, *, kind=None: [],
        )
        body = get_upcoming(user_client)
        assert body == {
            "connected": 0,
            "start": body["start"],
            "end": body["end"],
            "events": [],
            "error": None,
            "source_errors": [],
        }

    def test_a_failing_account_is_reported_per_account(self, user_client):
        FakeCalendar.mode = "down"
        body = get_upcoming(user_client)
        assert body["events"] == []
        assert body["source_errors"][0]["account"] == "work@x"
        assert "unreachable" in body["source_errors"][0]["error"].lower()

    def test_one_of_two_accounts_failing_is_not_an_aggregate_error(
        self, user_client, monkeypatch
    ):
        """Or adding a second calendar would put a banner on every healthy load."""

        class Broken(FakeCalendar):
            async def search_events(self, **kwargs):
                raise ProviderError("nope", kind="calendar")

        monkeypatch.setattr(
            "app.services.upcoming.providers_svc.load_for_user",
            lambda conn, uid, *, kind=None: [
                FakeCalendar(),
                Broken(integration_id=2, label="personal@x"),
            ],
        )
        body = get_upcoming(user_client)
        assert body["error"] is None
        assert len(body["source_errors"]) == 1
        assert len(body["events"]) == 2

    def test_every_account_failing_sets_the_aggregate_error(self, user_client):
        FakeCalendar.mode = "down"
        assert get_upcoming(user_client)["error"]

    def test_the_window_is_clamped(self, user_client):
        assert user_client.get("/api/calendar/upcoming", params={"days": 365}).status_code == 422

    def test_someone_elses_attachment_does_not_mark_your_event(
        self, user_client, other_user_client
    ):
        """Two people can be invited to one event and each write it up separately."""
        event = get_upcoming(user_client)["events"][0]
        assert create_from(user_client, event).status_code == 201

        theirs = next(
            e for e in get_upcoming(other_user_client)["events"] if e["uid"] == event["uid"]
        )
        assert theirs["attached"] is None


# --------------------------------------------------------------------------- #
# Creating a meeting from an event
# --------------------------------------------------------------------------- #


def create_from(client, event, **body):
    return client.post(
        "/api/calendar/upcoming/meeting", json={"event": event, **body}
    )


class TestCreateFromEvent:
    def test_creates_a_meeting_prefilled_from_the_event(self, user_client):
        event = get_upcoming(user_client)["events"][0]
        resp = create_from(user_client, event)
        assert resp.status_code == 201, resp.text

        meeting = resp.json()["meeting"]
        assert meeting["title"] == "Atlas Migration — Daily Standup"
        assert meeting["meeting_at"] == event["start"]
        assert meeting["status"] == "new"

    def test_attendees_become_speaker_hints(self, user_client):
        event = get_upcoming(user_client)["events"][0]
        resp = create_from(user_client, event)
        assert resp.json()["speaker_hints"] == 3
        # The returned meeting reflects them, not the row as it was before.
        assert resp.json()["meeting"]["speaker_count"] == 3

        meeting_id = resp.json()["meeting"]["id"]
        detail = user_client.get(f"/api/meetings/{meeting_id}").json()
        assert detail["speaker_count"] == 3

    def test_the_event_is_attached_to_the_new_meeting(self, user_client):
        event = get_upcoming(user_client)["events"][0]
        created = create_from(user_client, event).json()

        events = user_client.get(
            f"/api/threads/{created['thread_id']}/calendar-events"
        ).json()
        attached = events["items"] if isinstance(events, dict) else events
        assert [e["uid"] for e in attached] == [event["uid"]]

    def test_the_listing_then_marks_it_attached(self, user_client):
        event = get_upcoming(user_client)["events"][0]
        created = create_from(user_client, event).json()

        again = get_upcoming(user_client)["events"]
        marked = next(e for e in again if e["uid"] == event["uid"])
        assert marked["attached"]["meeting_id"] == created["meeting"]["id"]
        assert marked["attached"]["meeting_title"] == created["meeting"]["title"]
        # Still listed, not filtered out: "handled" is more useful than absent.
        assert again[1]["attached"] is None

    def test_a_new_thread_is_named_after_the_event(self, user_client):
        event = get_upcoming(user_client)["events"][0]
        created = create_from(user_client, event).json()
        thread = user_client.get(f"/api/threads/{created['thread_id']}").json()
        assert thread["title"] == "Atlas Migration — Daily Standup"

    def test_an_existing_thread_can_be_used_instead(self, user_client):
        thread = user_client.post("/api/threads", json={"title": "Atlas Migration"}).json()
        event = get_upcoming(user_client)["events"][0]

        created = create_from(user_client, event, thread_id=thread["id"]).json()
        assert created["thread_id"] == thread["id"]

    def test_overrides_win_over_the_events_own_values(self, user_client):
        event = get_upcoming(user_client)["events"][0]
        created = create_from(
            user_client,
            event,
            title="Standup — week 3",
            speaker_names=["Just Me"],
        ).json()
        assert created["meeting"]["title"] == "Standup — week 3"
        assert created["speaker_hints"] == 1

    def test_attaching_the_same_event_twice_is_refused(self, user_client):
        """A stale home screen, most likely -- the listing marks attached events."""
        event = get_upcoming(user_client)["events"][0]
        assert create_from(user_client, event).status_code == 201

        second = create_from(user_client, event)
        assert second.status_code == 409
        assert "already attached" in second.json()["error"]["message"]

    def test_someone_elses_thread_is_not_a_target(self, user_client, other_user_client):
        thread = user_client.post("/api/threads", json={"title": "Private"}).json()
        event = get_upcoming(other_user_client)["events"][0]

        resp = create_from(other_user_client, event, thread_id=thread["id"])
        assert resp.status_code == 404  # not 403: that would confirm it exists

    def test_an_event_with_no_uid_is_rejected(self, user_client):
        resp = create_from(user_client, {"uid": "", "summary": "Nope"})
        assert resp.status_code == 422

    def test_an_oversized_description_is_rejected(self, user_client):
        event = {**get_upcoming(user_client)["events"][0], "description": "x" * 20_001}
        assert create_from(user_client, event).status_code == 422
