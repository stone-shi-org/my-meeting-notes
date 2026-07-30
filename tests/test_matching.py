"""Keyword extraction, gather/rank/confirm, and the two date formats."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import httpx
import pytest
import respx

from app.db import get_conn
from app.errors import MCPError
from app.services import matching as m
from app.services.providers.base import EmailCandidate, EventCandidate, IntegrationRef
from tests.conftest import FIXTURES

LLM_URL = "https://llm.test/v1/chat/completions"


# --------------------------------------------------------------------------- #
# Keywords
# --------------------------------------------------------------------------- #


class TestKeywords:
    def test_stopwords_and_meeting_noise_are_dropped(self):
        kw = m.extract_keywords("the weekly sync about the atlas migration")
        assert "the" not in kw
        assert "weekly" not in kw
        assert "sync" not in kw
        assert "atlas" in kw
        assert "migration" in kw

    def test_proper_nouns_come_first(self):
        """They are what actually match a calendar entry or a correspondent."""
        kw = m.extract_keywords("migration planning with Contoso and Donna")
        assert kw.index("contoso") < kw.index("planning")
        assert kw.index("donna") < kw.index("planning")

    def test_short_tokens_are_dropped(self):
        assert "ab" not in m.extract_keywords("ab abc abcd")

    def test_duplicates_are_removed_case_insensitively(self):
        assert m.extract_keywords("Atlas atlas ATLAS").count("atlas") == 1

    def test_the_limit_is_respected(self):
        kw = m.extract_keywords(" ".join(f"word{i}" for i in range(50)), limit=8)
        assert len(kw) == 8

    def test_several_sources_are_combined(self):
        kw = m.extract_keywords("Atlas Migration", "Oracle cutover", "Standup")
        assert {"atlas", "migration", "oracle", "cutover"} <= set(kw)

    def test_empty_input(self):
        assert m.extract_keywords("", None or "") == []


# --------------------------------------------------------------------------- #
# The two date formats -- easy to cross-wire, silently returns nothing
# --------------------------------------------------------------------------- #


class TestDateFormats:
    def test_calendar_uses_iso_8601(self):
        assert m.iso_date(datetime(2026, 3, 11, tzinfo=timezone.utc)) == "2026-03-11"

    def test_gmail_uses_slashes(self):
        assert m.gmail_date(datetime(2026, 3, 11, tzinfo=timezone.utc)) == "2026/03/11"

    def test_they_are_deliberately_different(self):
        when = datetime(2026, 3, 11, tzinfo=timezone.utc)
        assert m.iso_date(when) != m.gmail_date(when)

    def test_the_gmail_query_uses_after_and_before(self):
        start = datetime(2026, 3, 11, tzinfo=timezone.utc)
        end = datetime(2026, 3, 21, tzinfo=timezone.utc)
        query = m.build_gmail_query(["atlas", "oracle", "cutover"], start, end)

        assert "after:2026/03/11" in query
        assert "before:2026/03/21" in query
        assert "(atlas OR oracle OR cutover)" in query

    def test_a_single_keyword_is_not_parenthesised(self):
        start = datetime(2026, 3, 11, tzinfo=timezone.utc)
        end = datetime(2026, 3, 21, tzinfo=timezone.utc)
        assert m.build_gmail_query(["atlas"], start, end).startswith("atlas after:")

    def test_no_keywords_still_gives_a_valid_window_query(self):
        start = datetime(2026, 3, 11, tzinfo=timezone.utc)
        end = datetime(2026, 3, 21, tzinfo=timezone.utc)
        assert m.build_gmail_query([], start, end) == "after:2026/03/11 before:2026/03/21"


class TestNormalizeTimestamp:
    def test_iso_passes_through(self):
        assert m.normalize_timestamp("2026-07-20T09:00:00+00:00").startswith("2026-07-20T09:00")

    def test_gmail_rfc_2822_is_converted(self):
        """Gmail returns RFC 2822; stored as-is it sorts above every ISO date."""
        out = m.normalize_timestamp("Wed, 15 Jul 2026 17:42:00 +0000")
        assert out.startswith("2026-07-15T17:42")

    def test_converted_values_sort_correctly_against_iso(self):
        email_date = m.normalize_timestamp("Wed, 15 Jul 2026 17:42:00 +0000")
        meeting_date = m.normalize_timestamp("2026-07-20T09:00:00+00:00")
        assert email_date < meeting_date  # the whole point

    def test_a_naive_rfc_date_is_assumed_utc(self):
        assert m.normalize_timestamp("15 Jul 2026 17:42:00").startswith("2026-07-15T17:42")

    def test_unparseable_input_is_kept_rather_than_lost(self):
        assert m.normalize_timestamp("sometime last Tuesday") == "sometime last Tuesday"

    def test_empty_is_none(self):
        assert m.normalize_timestamp(None) is None
        assert m.normalize_timestamp("") is None


class TestDateWindow:
    def test_the_window_brackets_the_meeting(self):
        start, end = m.date_window("2026-03-18T09:00:00+00:00", 7, 3)
        assert m.iso_date(start) == "2026-03-11"
        assert m.iso_date(end) == "2026-03-21"

    def test_a_naive_timestamp_is_treated_as_utc(self):
        start, _ = m.date_window("2026-03-18T09:00:00", 1, 1)
        assert start.tzinfo is not None

    def test_a_missing_or_unparseable_date_falls_back_to_now(self):
        for value in (None, "not a date"):
            start, end = m.date_window(value, 1, 1)
            assert (end - start).days == 2


# --------------------------------------------------------------------------- #
# Ranking merge
# --------------------------------------------------------------------------- #


class TestApplyRanking:
    def test_scores_are_merged_by_ref(self):
        items = [{"uid": "a"}, {"uid": "b"}]
        ranked = [
            {"ref": "c0", "score": 0.9, "reason": "same time", "suggested": True},
            {"ref": "c1", "score": 0.2, "reason": "unrelated", "suggested": False},
        ]
        out = m.apply_ranking(items, ranked, "c")

        assert out[0]["uid"] == "a"
        assert out[0]["relevance_score"] == 0.9
        assert out[0]["suggested"] is True
        assert out[1]["relevance_score"] == 0.2

    def test_results_are_sorted_by_score(self):
        items = [{"uid": "a"}, {"uid": "b"}]
        ranked = [
            {"ref": "c0", "score": 0.1, "reason": "", "suggested": False},
            {"ref": "c1", "score": 0.8, "reason": "", "suggested": True},
        ]
        assert [o["uid"] for o in m.apply_ranking(items, ranked, "c")] == ["b", "a"]

    def test_a_candidate_the_model_skipped_defaults_to_zero(self):
        out = m.apply_ranking([{"uid": "a"}, {"uid": "b"}], [{"ref": "c0", "score": 0.9}], "c")
        skipped = next(o for o in out if o["uid"] == "b")
        assert skipped["relevance_score"] == 0.0
        assert skipped["relevance_reason"] == "not ranked"
        assert skipped["suggested"] is False

    def test_a_hallucinated_ref_is_dropped(self):
        out = m.apply_ranking(
            [{"uid": "a"}],
            [{"ref": "c0", "score": 0.9}, {"ref": "c99", "score": 1.0}],
            "c",
        )
        assert len(out) == 1
        assert out[0]["uid"] == "a"

    def test_suggested_is_derived_when_the_model_omits_it(self):
        out = m.apply_ranking(
            [{"uid": "a"}, {"uid": "b"}],
            [{"ref": "c0", "score": 0.75}, {"ref": "c1", "score": 0.4}],
            "c",
        )
        assert next(o for o in out if o["uid"] == "a")["suggested"] is True
        assert next(o for o in out if o["uid"] == "b")["suggested"] is False

    def test_out_of_range_and_junk_scores_are_clamped(self):
        out = m.apply_ranking(
            [{"uid": "a"}, {"uid": "b"}],
            [{"ref": "c0", "score": 5}, {"ref": "c1", "score": "nonsense"}],
            "c",
        )
        assert all(0.0 <= o["relevance_score"] <= 1.0 for o in out)


class TestRankPayload:
    def test_refs_are_short_and_positional(self):
        payload = m.build_rank_payload(
            {}, [{"uid": "long-uid-1"}, {"uid": "long-uid-2"}], [{"message_id": "<m1>"}]
        )
        assert [c["ref"] for c in payload["calendar_candidates"]] == ["c0", "c1"]
        assert [c["ref"] for c in payload["email_candidates"]] == ["e0"]

    def test_uids_are_never_exposed_to_the_model(self):
        payload = m.build_rank_payload({}, [{"uid": "secret-uid"}], [])
        assert "secret-uid" not in json.dumps(payload)

    def test_long_text_is_truncated(self):
        payload = m.build_rank_payload(
            {}, [{"uid": "a", "description": "x" * 5000}],
            [{"message_id": "<m>", "snippet": "y" * 5000}],
        )
        assert len(payload["calendar_candidates"][0]["description"]) <= m.DESCRIPTION_LIMIT + 1
        assert len(payload["email_candidates"][0]["snippet"]) <= m.SNIPPET_LIMIT + 1


# --------------------------------------------------------------------------- #
# End to end
# --------------------------------------------------------------------------- #

EVENTS = [
    {
        "uid": "uid-standup", "summary": "Atlas Migration — Daily Standup",
        "description": "", "location": "Room 4B",
        "start": "2026-03-18T09:00:00+00:00", "end": "2026-03-18T09:30:00+00:00",
        "calendar_name": "work@x", "account": "work@x", "type": "google",
    },
    {
        "uid": "uid-dentist", "summary": "Dentist", "description": "",
        "location": "", "start": "2026-03-19T14:00:00+00:00",
        "end": "2026-03-19T15:00:00+00:00",
        "calendar_name": "personal@x", "account": "personal@x", "type": "caldav",
    },
]

EMAILS = [
    {
        "id": "g1", "message_id": "<cutover@x>", "sender": "priya@acme.com",
        "subject": "Re: cutover window", "date": "Tue, 17 Mar 2026 17:42:00 +0000",
        # Deliberately RFC 2822, as the email MCP server actually returns.
        "snippet": "rollback rehearsal is booked", "account": "work@x",
        "triage_level": 1, "tag": "deploy", "reason": "", "summary": "", "score": 0.8,
    },
    {
        "id": "g2", "message_id": "<spam@x>", "sender": "deals@shop.com",
        "subject": "50% off everything", "date": "2026-03-16T08:00:00+00:00",
        "snippet": "shop now", "account": "work@x",
        "triage_level": 4, "tag": None, "reason": "", "summary": "", "score": 0.1,
    },
]

RANKING = {
    "calendar": [
        {"ref": "c0", "score": 0.94, "reason": "Same 9am slot on the meeting date.",
         "suggested": True},
        {"ref": "c1", "score": 0.05, "reason": "Personal appointment.", "suggested": False},
    ],
    "email": [
        {"ref": "e0", "score": 0.81, "reason": "Same cutover topic, day before.",
         "suggested": True},
        {"ref": "e1", "score": 0.02, "reason": "Marketing.", "suggested": False},
    ],
    "notes": "",
}


class FakeProvider:
    """Stands in for one connected account.

    Faked a level above the transport -- at the provider, not at MCPClient -- so
    these tests describe "a calendar account failed" rather than "an SSE handshake
    failed", and stay true whichever backend the account actually uses.
    """

    calendar_mode = "ok"
    email_mode = "ok"

    def __init__(self, kind: str, *, integration_id: int, label: str, provider="fake"):
        self.kind = kind
        self.ref = IntegrationRef(
            id=integration_id,
            provider=provider,
            account_label=label,
            calendar_enabled=kind == "calendar",
            email_enabled=kind == "email",
        )

    async def search_events(self, **kwargs):
        if FakeProvider.calendar_mode == "down":
            raise MCPError("Could not connect", server="calendar")
        return [EventCandidate(**e) for e in EVENTS]

    async def search_emails(self, **kwargs):
        if FakeProvider.email_mode == "down":
            raise MCPError("rejected the token (401)", server="email")
        return [EmailCandidate(**m) for m in EMAILS]


def fake_load_for_user(conn, user_id, *, kind=None):
    """One calendar account and one inbox, matching the pre-refactor topology."""
    if user_id is None:
        return []
    sources = []
    if kind in (None, "calendar"):
        sources.append(FakeProvider("calendar", integration_id=1, label="calendar@x"))
    if kind in (None, "email"):
        sources.append(FakeProvider("email", integration_id=2, label="email@x"))
    return sources


@pytest.fixture(autouse=True)
def wiring(monkeypatch):
    monkeypatch.setenv("MMN_DIARIZE_FAKE", "true")
    monkeypatch.setenv("MMN_DIARIZE_FAKE_DELAY_SEC", "0.05")
    monkeypatch.setenv("MMN_JOB_CONCURRENCY", "1")
    monkeypatch.setenv("MMN_LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("MMN_LLM_MODEL", "test/model")
    from app.config import reset_settings_cache

    reset_settings_cache()

    FakeProvider.calendar_mode = "ok"
    FakeProvider.email_mode = "ok"
    monkeypatch.setattr(
        "app.services.matching.providers_svc.load_for_user", fake_load_for_user
    )


@pytest.fixture
def mock_llm():
    with respx.mock(assert_all_called=False) as router:
        router.post(LLM_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": json.dumps(RANKING)}}],
                    "usage": {"prompt_tokens": 500, "completion_tokens": 200},
                },
            )
        )
        yield router


@pytest.fixture
def meeting(user_client):
    m_ = user_client.post(
        "/api/meetings",
        json={
            "new_thread_title": "Atlas Migration",
            "new_thread_description": "Move billing off Oracle",
            "title": "Cutover go/no-go",
            "meeting_at": "2026-03-18T09:00:00+00:00",
        },
    ).json()
    return m_


def run_match(client, meeting_id, timeout=20.0, **body):
    job_id = client.post(f"/api/meetings/{meeting_id}/match", json=body).json()["job_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    raise AssertionError(f"match job did not finish: {job}")


class TestMatchJob:
    def test_gathers_and_ranks(self, user_client, meeting, mock_llm):
        job = run_match(user_client, meeting["id"])
        assert job["status"] == "succeeded", job.get("error")
        assert job["result"]["events"] == 2
        assert job["result"]["emails"] == 2
        assert job["result"]["suggested"] == 2

    def test_latest_returns_ranked_candidates(self, user_client, meeting, mock_llm):
        run_match(user_client, meeting["id"])
        latest = user_client.get(f"/api/meetings/{meeting['id']}/match/latest").json()

        assert latest["status"] == "ok"
        assert latest["events"][0]["uid"] == "uid-standup"
        assert latest["events"][0]["relevance_score"] == 0.94
        assert latest["events"][0]["suggested"] is True
        assert "9am slot" in latest["events"][0]["relevance_reason"]
        assert latest["events"][1]["suggested"] is False

    def test_the_exact_tool_arguments_are_recorded(self, user_client, meeting, mock_llm):
        """So a zero-result match is debuggable from the UI."""
        run_match(user_client, meeting["id"])
        query = user_client.get(
            f"/api/meetings/{meeting['id']}/match/latest"
        ).json()["query"]

        assert query["calendar"]["start_date"] == "2026-01-17"
        assert "after:2026/03/11" in query["email"]["query"]
        assert "atlas" in query["keywords"]

    def test_a_dead_calendar_still_returns_email_results(
        self, user_client, meeting, mock_llm
    ):
        FakeProvider.calendar_mode = "down"
        job = run_match(user_client, meeting["id"])
        assert job["status"] == "succeeded"

        latest = user_client.get(f"/api/meetings/{meeting['id']}/match/latest").json()
        assert latest["status"] == "partial"
        assert latest["calendar_error"]
        assert latest["events"] == []
        assert len(latest["emails"]) == 2

    def test_a_dead_account_is_named_in_source_errors(self, user_client, meeting, mock_llm):
        """The aggregate says "calendar broke"; this says which account and why."""
        FakeProvider.calendar_mode = "down"
        run_match(user_client, meeting["id"])

        latest = user_client.get(f"/api/meetings/{meeting['id']}/match/latest").json()
        assert len(latest["source_errors"]) == 1
        failure = latest["source_errors"][0]
        assert failure["kind"] == "calendar"
        assert failure["account"] == "calendar@x"
        assert "Could not connect" in failure["error"]

    def test_both_servers_down_is_recorded_not_crashed(self, user_client, meeting, mock_llm):
        FakeProvider.calendar_mode = "down"
        FakeProvider.email_mode = "down"
        job = run_match(user_client, meeting["id"])

        assert job["status"] == "succeeded"
        latest = user_client.get(f"/api/meetings/{meeting['id']}/match/latest").json()
        assert latest["status"] == "failed"
        assert latest["calendar_error"] and latest["email_error"]

    def test_one_healthy_calendar_among_two_is_not_reported_as_an_error(
        self, user_client, meeting, mock_llm, monkeypatch
    ):
        """The property that has to survive users adding accounts.

        With two calendars and one broken, the search still succeeded -- flagging
        it 'partial' would put a warning banner on a perfectly good result, and
        would do so more often the more accounts someone connects.
        """

        class HalfDeadProvider(FakeProvider):
            async def search_events(self, **kwargs):
                if self.ref.id == 99:
                    raise MCPError("Could not connect", server="calendar")
                return [EventCandidate(**e) for e in EVENTS]

        def two_calendars(conn, user_id, *, kind=None):
            sources = []
            if kind in (None, "calendar"):
                sources.append(
                    HalfDeadProvider("calendar", integration_id=1, label="good@x")
                )
                sources.append(
                    HalfDeadProvider("calendar", integration_id=99, label="broken@x")
                )
            if kind in (None, "email"):
                sources.append(FakeProvider("email", integration_id=2, label="email@x"))
            return sources

        monkeypatch.setattr(
            "app.services.matching.providers_svc.load_for_user", two_calendars
        )
        run_match(user_client, meeting["id"])

        latest = user_client.get(f"/api/meetings/{meeting['id']}/match/latest").json()
        assert latest["status"] == "ok"
        assert latest["calendar_error"] is None
        assert len(latest["events"]) == 2, "the healthy calendar's events still arrive"
        # The failure is not hidden, just not escalated to an aggregate.
        assert [e["account"] for e in latest["source_errors"]] == ["broken@x"]

    def test_the_same_event_from_two_accounts_is_shown_once(
        self, user_client, meeting, mock_llm, monkeypatch
    ):
        """A Google account reachable both directly and via the calendar MCP
        server must not produce two identical candidates."""

        def duplicate_calendars(conn, user_id, *, kind=None):
            sources = []
            if kind in (None, "calendar"):
                sources.append(FakeProvider("calendar", integration_id=1, label="a@x"))
                sources.append(FakeProvider("calendar", integration_id=2, label="b@x"))
            return sources

        monkeypatch.setattr(
            "app.services.matching.providers_svc.load_for_user", duplicate_calendars
        )
        run_match(user_client, meeting["id"])

        latest = user_client.get(f"/api/meetings/{meeting['id']}/match/latest").json()
        assert len(latest["events"]) == 2, "deduped to the two distinct events"

    def test_no_connected_accounts_is_a_clear_failure(
        self, user_client, meeting, mock_llm, monkeypatch
    ):
        """Belt to the SPA's braces: the button should already be disabled, so
        reaching here means a stale bundle. The error still has to say what to do."""
        monkeypatch.setattr(
            "app.services.matching.providers_svc.load_for_user",
            lambda conn, user_id, **kw: [],
        )
        job = run_match(user_client, meeting["id"])

        assert job["status"] == "failed"
        assert "Integrations" in (job["error"] or "")

    def test_a_failed_ranking_still_lets_the_user_choose(self, user_client, meeting, mock_llm):
        """Unranked beats nothing: ticking boxes is the point."""
        mock_llm.post(LLM_URL).mock(return_value=httpx.Response(500, text="llm down"))

        job = run_match(user_client, meeting["id"])
        assert job["status"] == "succeeded"

        latest = user_client.get(f"/api/meetings/{meeting['id']}/match/latest").json()
        assert latest["status"] == "partial"
        assert latest["error"]
        assert len(latest["events"]) == 2
        assert latest["events"][0]["relevance_score"] is None

    def test_latest_is_404_before_any_run(self, user_client, meeting):
        assert user_client.get(
            f"/api/meetings/{meeting['id']}/match/latest"
        ).status_code == 404


class TestConfirm:
    def test_attaching_events_and_emails(self, user_client, meeting, mock_llm):
        run_match(user_client, meeting["id"])

        resp = user_client.post(
            f"/api/meetings/{meeting['id']}/match/confirm",
            json={"event_uids": ["uid-standup"], "email_message_ids": ["<cutover@x>"]},
        )
        assert resp.status_code == 200
        assert resp.json()["attached_events"] == 1
        assert resp.json()["attached_emails"] == 1

        thread_id = meeting["thread_id"]
        events = user_client.get(f"/api/threads/{thread_id}/calendar-events").json()
        emails = user_client.get(f"/api/threads/{thread_id}/emails").json()

        assert events[0]["uid"] == "uid-standup"
        assert events[0]["relevance_score"] == 0.94
        # Emails belong to the thread: they are context for the whole run of work.
        assert emails[0]["message_id"] == "<cutover@x>"
        assert emails[0]["subject"] == "Re: cutover window"

    def test_the_event_title_is_appended_to_the_meeting_title(
        self, user_client, meeting, mock_llm
    ):
        run_match(user_client, meeting["id"])
        resp = user_client.post(
            f"/api/meetings/{meeting['id']}/match/confirm",
            json={"event_uids": ["uid-standup"]},
        )

        assert resp.json()["title_changed"] is True
        assert resp.json()["meeting"]["title"] == (
            "Cutover go/no-go — Atlas Migration — Daily Standup"
        )

    def test_confirming_twice_does_not_stack_the_suffix(
        self, user_client, meeting, mock_llm
    ):
        run_match(user_client, meeting["id"])
        body = {"event_uids": ["uid-standup"]}

        first = user_client.post(
            f"/api/meetings/{meeting['id']}/match/confirm", json=body
        ).json()
        second = user_client.post(
            f"/api/meetings/{meeting['id']}/match/confirm", json=body
        ).json()

        assert first["meeting"]["title"] == second["meeting"]["title"]
        assert second["title_changed"] is False
        assert second["meeting"]["title"].count("Daily Standup") == 1

    def test_attaching_twice_does_not_duplicate_rows(self, user_client, meeting, mock_llm):
        run_match(user_client, meeting["id"])
        body = {"event_uids": ["uid-standup"], "email_message_ids": ["<cutover@x>"]}
        user_client.post(f"/api/meetings/{meeting['id']}/match/confirm", json=body)
        user_client.post(f"/api/meetings/{meeting['id']}/match/confirm", json=body)

        thread_id = meeting["thread_id"]
        assert len(user_client.get(f"/api/threads/{thread_id}/calendar-events").json()) == 1
        assert len(user_client.get(f"/api/threads/{thread_id}/emails").json()) == 1

    def test_the_title_append_can_be_declined(self, user_client, meeting, mock_llm):
        run_match(user_client, meeting["id"])
        resp = user_client.post(
            f"/api/meetings/{meeting['id']}/match/confirm",
            json={
                "event_uids": ["uid-standup"],
                "append_event_title_to_meeting_title": False,
            },
        )
        assert resp.json()["title_changed"] is False
        assert resp.json()["meeting"]["title"] == "Cutover go/no-go"

    def test_an_unknown_uid_is_ignored_rather_than_erroring(
        self, user_client, meeting, mock_llm
    ):
        run_match(user_client, meeting["id"])
        resp = user_client.post(
            f"/api/meetings/{meeting['id']}/match/confirm",
            json={"event_uids": ["uid-standup", "uid-nonexistent"]},
        )
        assert resp.status_code == 200
        assert resp.json()["attached_events"] == 1

    def test_confirming_nothing_is_a_no_op(self, user_client, meeting, mock_llm):
        run_match(user_client, meeting["id"])
        resp = user_client.post(f"/api/meetings/{meeting['id']}/match/confirm", json={})
        assert resp.json()["attached_events"] == 0
        assert resp.json()["title_changed"] is False

    def test_confirming_before_a_match_is_404(self, user_client, meeting):
        resp = user_client.post(
            f"/api/meetings/{meeting['id']}/match/confirm",
            json={"event_uids": ["uid-standup"]},
        )
        assert resp.status_code == 404

    def test_already_attached_items_are_excluded_from_a_later_search(
        self, user_client, meeting, mock_llm
    ):
        run_match(user_client, meeting["id"])
        user_client.post(
            f"/api/meetings/{meeting['id']}/match/confirm",
            json={"event_uids": ["uid-standup"], "email_message_ids": ["<cutover@x>"]},
        )

        run_match(user_client, meeting["id"])
        latest = user_client.get(f"/api/meetings/{meeting['id']}/match/latest").json()

        assert [e["uid"] for e in latest["events"]] == ["uid-dentist"]
        assert [m_["message_id"] for m_ in latest["emails"]] == ["<spam@x>"]

    def test_attached_items_appear_on_the_thread_timeline(
        self, user_client, meeting, mock_llm
    ):
        run_match(user_client, meeting["id"])
        user_client.post(
            f"/api/meetings/{meeting['id']}/match/confirm",
            json={"event_uids": ["uid-standup"], "email_message_ids": ["<cutover@x>"]},
        )

        timeline = user_client.get(
            f"/api/threads/{meeting['thread_id']}/timeline"
        ).json()
        kinds = [i["kind"] for i in timeline]
        assert set(kinds) == {"meeting", "event", "email"}
        # Newest first.
        assert [i["at"] for i in timeline] == sorted(
            (i["at"] for i in timeline), reverse=True
        )

    def test_an_rfc_2822_email_date_sorts_below_a_later_meeting(
        self, user_client, meeting, mock_llm
    ):
        """The email is Mar 17, the meeting Mar 18. Sorted as raw strings the
        email's "Tue, 17 Mar..." would sort above, putting it first."""
        run_match(user_client, meeting["id"])
        user_client.post(
            f"/api/meetings/{meeting['id']}/match/confirm",
            json={"event_uids": ["uid-standup"], "email_message_ids": ["<cutover@x>"]},
        )

        timeline = user_client.get(
            f"/api/threads/{meeting['thread_id']}/timeline"
        ).json()

        assert timeline[-1]["kind"] == "email"
        assert timeline[-1]["at"].startswith("2026-03-17T17:42")
        assert [i["kind"] for i in timeline[:2]] == ["meeting", "event"]


class TestOwnership:
    def test_another_user_cannot_match_or_confirm(
        self, user_client, other_user_client, meeting, mock_llm
    ):
        mid = meeting["id"]
        assert other_user_client.post(f"/api/meetings/{mid}/match", json={}).status_code == 404
        assert other_user_client.get(f"/api/meetings/{mid}/match/latest").status_code == 404
        assert other_user_client.post(
            f"/api/meetings/{mid}/match/confirm", json={}
        ).status_code == 404
