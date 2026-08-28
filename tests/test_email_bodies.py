"""Lazy body hydration and the per-message AI summary.

Providers are faked at ``providers.loader.build_provider`` -- one level above the
transport, the same seam ``test_matching.py`` uses -- so these tests describe
"the account could not supply a body" rather than "an HTTPS call 404ed", and stay
true whichever backend the account happens to be.
"""

from __future__ import annotations

import json

import pytest

from app.db import get_conn, utcnow
from app.services import email_bodies as eb
from app.services.providers.base import FetchedEmail

# Captured before the autouse `no_llm` fixture can stub it out. The tests that
# exercise summarise_sync *itself* have to call this rather than
# `eb.summarise_sync`, or they silently test the stub and pass vacuously --
# which is exactly what they did first time round.
REAL_SUMMARISE = eb.summarise_sync


@pytest.fixture
def thread(initialised_db):
    with get_conn(initialised_db) as conn:
        now = utcnow()
        conn.execute(
            "INSERT INTO users (id, username, password_hash, password_salt, "
            "created_at, updated_at) VALUES (1, 'u', 'h', 's', ?, ?)", (now, now),
        )
        conn.execute(
            "INSERT INTO threads (id, owner_id, title, created_at, updated_at) "
            "VALUES (1, 1, 'Atlas', ?, ?)", (now, now),
        )
        conn.execute(
            "INSERT INTO integrations (id, user_id, provider, account_key, "
            "account_label, calendar_enabled, email_enabled, auth_type, "
            "config_json, created_at, updated_at) "
            "VALUES (5, 1, 'google', 'k', 'me@acme.com', 0, 1, 'oauth', '{}', ?, ?)",
            (now, now),
        )
    return initialised_db


def add_email(db_path, *, email_id=1, snippet=None, message_id=None, **extra):
    values = {
        "id": email_id,
        "thread_id": 1,
        "message_id": message_id or f"google:5:m{email_id}",
        "mcp_id": f"m{email_id}",
        "subject": "Re: cutover window",
        "sender": "priya@acme.com",
        "date": "2026-03-17T17:42:00+00:00",
        "snippet": snippet,
        "integration_id": 5,
        "provider": "google",
        "raw_json": "{}",
        "attached_at": utcnow(),
        **extra,
    }
    with get_conn(db_path) as conn:
        conn.execute(
            f"INSERT INTO thread_emails ({', '.join(values)}) "
            f"VALUES ({', '.join('?' * len(values))})",
            list(values.values()),
        )


def row(db_path, email_id=1):
    with get_conn(db_path) as conn:
        return conn.execute(
            "SELECT * FROM thread_emails WHERE id = ?", (email_id,)
        ).fetchone()


class FakeProvider:
    """Records calls, so "was the provider asked again?" is assertable."""

    def __init__(self, result=None, raises=None):
        self.result = result
        self.raises = raises
        self.calls = 0

    async def get_email_message(self, *, native_id, folder_id=None):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.result


@pytest.fixture
def fake(monkeypatch):
    """Install a provider and hand the test the instance to assert against."""
    holder = {}

    def install(provider):
        holder["provider"] = provider
        monkeypatch.setattr(
            eb.providers_svc, "build_provider", lambda conn, r: provider
        )
        return provider

    return install


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    """Summaries off by default. The ones that want one opt in explicitly, so no
    test accidentally depends on an unmocked LLM call."""
    monkeypatch.setattr(
        eb, "summarise_sync", lambda *a, **k: (None, None)
    )


async def hydrate(db_path, **kwargs):
    return await eb.hydrate_thread_emails(db_path, thread_id=1, user_id=1, **kwargs)


# --------------------------------------------------------------------------- #
# Fetching and storing
# --------------------------------------------------------------------------- #


class TestHydration:
    async def test_it_stores_converted_text_not_html(self, thread, fake):
        fake(FakeProvider(FetchedEmail(
            body="<p>Hi Priya,</p><p>The rollback is <b>booked</b>.</p>"
        )))
        add_email(thread)

        result = await hydrate(thread)

        assert result["fetched"] == 1
        body = row(thread)["body"]
        assert "<p>" not in body, "markup would force an HTML sanitizer into the SPA"
        assert body == "Hi Priya,\n\nThe rollback is booked."

    async def test_plain_text_is_left_alone(self, thread, fake):
        """A body that merely contains < and > is not markup."""
        fake(FakeProvider(FetchedEmail(body="3 < 5 and a > quote")))
        add_email(thread)

        await hydrate(thread)
        assert row(thread)["body"] == "3 < 5 and a > quote"

    async def test_the_stored_body_is_bounded(self, thread, fake):
        fake(FakeProvider(FetchedEmail(body="x" * (eb.MAX_BODY_CHARS + 5_000))))
        add_email(thread)

        await hydrate(thread)
        assert len(row(thread)["body"]) == eb.MAX_BODY_CHARS

    async def test_it_fills_a_null_snippet(self, thread, fake):
        """Apple/IMAP sets snippet=None by construction, so every iCloud row
        renders blank in a snippet-based UI. This is the free win."""
        fake(FakeProvider(FetchedEmail(body="The rollback rehearsal is booked.")))
        add_email(thread, snippet=None)

        await hydrate(thread)
        assert row(thread)["snippet"] == "The rollback rehearsal is booked."

    async def test_it_never_overwrites_a_providers_own_snippet(self, thread, fake):
        fake(FakeProvider(FetchedEmail(body="A completely different body.")))
        add_email(thread, snippet="Gmail's own snippet")

        await hydrate(thread)
        assert row(thread)["snippet"] == "Gmail's own snippet"

    async def test_a_blank_snippet_counts_as_absent(self, thread, fake):
        fake(FakeProvider(FetchedEmail(body="Real content here.")))
        add_email(thread, snippet="   ")

        await hydrate(thread)
        assert row(thread)["snippet"] == "Real content here."

    async def test_it_backfills_threading_headers(self, thread, fake):
        fake(FakeProvider(FetchedEmail(
            body="text",
            conversation_id="google:5:tABC",
            in_reply_to="<orig@acme.com>",
            references=("<root@acme.com>",),
            to_recipients="me@acme.com",
            direction="inbound",
        )))
        add_email(thread)

        await hydrate(thread)

        r = row(thread)
        assert r["conversation_id"] == "google:5:tABC"
        assert r["in_reply_to"] == "<orig@acme.com>"
        assert json.loads(r["references_json"]) == ["<root@acme.com>"]
        assert r["direction"] == "inbound"

    async def test_backfill_never_blanks_a_value_a_search_already_stored(
        self, thread, fake
    ):
        """COALESCE, for the same reason attach_email's conflict clause uses it:
        hydration must be able to fill these, never to clear them."""
        fake(FakeProvider(FetchedEmail(body="text")))  # no headers at all
        add_email(thread, conversation_id="google:5:tKEEP", direction="outbound")

        await hydrate(thread)

        r = row(thread)
        assert r["conversation_id"] == "google:5:tKEEP"
        assert r["direction"] == "outbound"


# --------------------------------------------------------------------------- #
# The "asked, and this account cannot" state
# --------------------------------------------------------------------------- #


class TestUnavailable:
    async def test_a_provider_that_cannot_fetch_is_stamped_and_not_retried(
        self, thread, fake
    ):
        """The whole reason body_fetched_at exists.

        MCP has no fetch-by-id tool and Zoho needs a folder_id it may not have,
        so without the stamp every page view would ask again forever.
        """
        provider = fake(FakeProvider(None))
        add_email(thread)

        first = await hydrate(thread)
        assert first == {"requested": 1, "fetched": 0, "unavailable": 1, "remaining": 0}
        assert row(thread)["body"] is None
        assert row(thread)["body_fetched_at"] is not None
        assert provider.calls == 1

        second = await hydrate(thread)
        assert second["requested"] == 0, "the row must not be selected again"
        assert provider.calls == 1, "and the provider must not be asked again"

    async def test_force_retries_a_previous_failure(self, thread, fake):
        provider = fake(FakeProvider(None))
        add_email(thread)
        await hydrate(thread)

        provider.result = FetchedEmail(body="it works now")
        await hydrate(thread, force=True, email_id=1)

        assert row(thread)["body"] == "it works now"
        assert provider.calls == 2

    async def test_a_provider_error_is_stamped_rather_than_raised(self, thread, fake):
        from app.errors import ProviderError

        fake(FakeProvider(raises=ProviderError("mailbox gone", kind="email")))
        add_email(thread)

        result = await hydrate(thread)

        assert result["unavailable"] == 1
        assert row(thread)["body_fetched_at"] is not None

    async def test_an_unexpected_exception_does_not_lose_the_other_rows(
        self, thread, monkeypatch
    ):
        class Flaky:
            async def get_email_message(self, *, native_id, folder_id=None):
                if native_id == "m1":
                    raise RuntimeError("boom")
                return FetchedEmail(body="kept")

        monkeypatch.setattr(
            eb.providers_svc, "build_provider", lambda conn, r: Flaky()
        )
        add_email(thread, email_id=1)
        add_email(thread, email_id=2)

        result = await hydrate(thread)

        assert result["fetched"] == 1
        assert row(thread, 2)["body"] == "kept"

    async def test_no_integration_row_means_unavailable_not_a_crash(self, thread, fake):
        """An id naming an account the user disconnected. This is also why
        integration_id is not a foreign key -- see test_matching."""
        fake(FakeProvider(FetchedEmail(body="never reached")))
        add_email(thread, integration_id=99999)

        result = await hydrate(thread)

        assert result["unavailable"] == 1
        assert row(thread)["body_fetched_at"] is not None

    async def test_an_mcp_row_with_a_bare_message_id_is_hydratable(self, thread, fake):
        """The bug fix. `_resolve_email_ref` recovered integration_id by parsing
        the composite message_id, and MCP deliberately emits bare ids -- so every
        MCP-sourced email was silently unfetchable. The column fixes it."""
        fake(FakeProvider(FetchedEmail(body="the full MCP body")))
        add_email(thread, message_id="bare-mcp-id-1", integration_id=5)

        await hydrate(thread)
        assert row(thread)["body"] == "the full MCP body"

    async def test_a_row_with_neither_column_nor_composite_is_unavailable(
        self, thread, fake
    ):
        fake(FakeProvider(FetchedEmail(body="never reached")))
        add_email(thread, message_id="bare-mcp-id-1", integration_id=None)

        assert (await hydrate(thread))["unavailable"] == 1


# --------------------------------------------------------------------------- #
# Bounds and ownership
# --------------------------------------------------------------------------- #


class TestBounds:
    async def test_it_is_bounded_per_call(self, thread, fake):
        fake(FakeProvider(FetchedEmail(body="body")))
        for i in range(1, eb.HYDRATE_MAX_PER_CALL + 4):
            add_email(thread, email_id=i)

        result = await hydrate(thread)
        assert result["requested"] == eb.HYDRATE_MAX_PER_CALL

    async def test_a_single_email_can_be_targeted(self, thread, fake):
        fake(FakeProvider(FetchedEmail(body="body")))
        add_email(thread, email_id=1)
        add_email(thread, email_id=2)

        await hydrate(thread, email_id=2)

        assert row(thread, 1)["body"] is None
        assert row(thread, 2)["body"] == "body"

    async def test_another_users_integration_is_not_reachable(self, thread, fake):
        """Owner-scoped, so hydration cannot become a way to probe someone
        else's connected accounts."""
        fake(FakeProvider(FetchedEmail(body="secret")))
        add_email(thread)

        result = await eb.hydrate_thread_emails(
            thread, thread_id=1, user_id=2  # not the owner
        )

        assert result["unavailable"] == 1
        assert row(thread)["body"] is None

    async def test_an_already_hydrated_row_is_not_refetched(self, thread, fake):
        provider = fake(FakeProvider(FetchedEmail(body="new")))
        add_email(thread, body="already here", body_fetched_at=utcnow())

        assert (await hydrate(thread))["requested"] == 0
        assert provider.calls == 0
        assert row(thread)["body"] == "already here"


# --------------------------------------------------------------------------- #
# Rules this must not break
# --------------------------------------------------------------------------- #


class TestInvariants:
    async def test_hydration_does_not_bump_the_threads_updated_at(self, thread, fake):
        """Hydrating is not activity. The default sort is last activity, so
        bumping it would send a thread to the top of the home list just for
        being opened -- the same rule as "moving a thread does not bump it"."""
        fake(FakeProvider(FetchedEmail(body="body")))
        add_email(thread)
        with get_conn(thread) as conn:
            before = conn.execute("SELECT updated_at FROM threads WHERE id = 1").fetchone()[0]

        await hydrate(thread)

        with get_conn(thread) as conn:
            after = conn.execute("SELECT updated_at FROM threads WHERE id = 1").fetchone()[0]
        assert after == before

    async def test_hydration_does_not_clear_the_unread_mark(self, thread, fake):
        """`seen_at` is owned by mark_seen. Hydration is the app fetching, not a
        person reading -- clearing the mark would make the sweep's blue dot
        vanish for mail nobody looked at."""
        fake(FakeProvider(FetchedEmail(body="body")))
        add_email(thread, auto_attached=1, seen_at=None)

        await hydrate(thread)

        r = row(thread)
        assert r["auto_attached"] == 1
        assert r["seen_at"] is None


# --------------------------------------------------------------------------- #
# Summaries
# --------------------------------------------------------------------------- #


class TestSummariesAreOptIn:
    """Hydration fetches bodies and nothing else.

    One LLM call per message is real money and real latency, and it used to ride
    along with every thread open. Summarising is its own request now.
    """

    async def test_hydration_never_calls_the_llm(self, thread, fake, monkeypatch):
        called = []
        monkeypatch.setattr(
            eb, "summarise_sync", lambda *a, **k: called.append(1) or ("no", "m")
        )
        fake(FakeProvider(FetchedEmail(body="word " * 400)))
        add_email(thread)

        result = await hydrate(thread)

        assert result["fetched"] == 1
        assert called == [], "opening a thread must not spend LLM budget"
        assert row(thread)["ai_summary"] is None
        assert "summarised" not in result

    async def test_summarise_fills_a_stored_body(self, thread, fake, monkeypatch):
        monkeypatch.setattr(
            eb, "summarise_sync",
            lambda *a, **k: ("Priya confirms the Friday window", "test-model"),
        )
        fake(FakeProvider(FetchedEmail(body="word " * 400)))
        add_email(thread)
        await hydrate(thread)

        result = await eb.summarise_thread_emails(thread, thread_id=1)

        assert result == {"requested": 1, "summarised": 1, "failed": 0, "remaining": 0}
        r = row(thread)
        assert r["ai_summary"] == "Priya confirms the Friday window"
        assert r["ai_summary_model"] == "test-model"

    async def test_a_short_body_is_never_selected(self, thread, fake, monkeypatch):
        """The body IS the summary below the threshold: paying to compress four
        lines costs money, adds latency and loses information."""
        called = []
        monkeypatch.setattr(
            eb, "summarise_sync", lambda *a, **k: called.append(1) or ("x", "m")
        )
        fake(FakeProvider(FetchedEmail(body="Short and clear.")))
        add_email(thread)
        await hydrate(thread)

        result = await eb.summarise_thread_emails(thread, thread_id=1)

        assert result["requested"] == 0
        assert called == []

    async def test_a_failed_summary_can_be_retried(self, thread, fake, monkeypatch):
        """The bug this split exists to fix.

        `pending` requires `body IS NULL`, so once a body is stored the row is
        invisible to it forever -- even with force=True, which only relaxes the
        `body_fetched_at` half. A failed summary was therefore permanent.
        `pending_summaries` asks a different question, so pressing the button
        again is a real retry.
        """
        fake(FakeProvider(FetchedEmail(body="word " * 400)))
        add_email(thread)
        await hydrate(thread)

        monkeypatch.setattr(eb, "summarise_sync", lambda *a, **k: (None, None))
        failed = await eb.summarise_thread_emails(thread, thread_id=1)
        assert failed == {"requested": 1, "summarised": 0, "failed": 1, "remaining": 1}
        assert row(thread)["body"].startswith("word"), "the body survives regardless"
        assert row(thread)["ai_summary"] is None

        monkeypatch.setattr(eb, "summarise_sync", lambda *a, **k: ("Now it works", "m"))
        retried = await eb.summarise_thread_emails(thread, thread_id=1)

        assert retried["summarised"] == 1
        assert row(thread)["ai_summary"] == "Now it works"

    async def test_force_hydration_still_cannot_reach_a_summarised_row(
        self, thread, fake, monkeypatch
    ):
        """Documents why the second predicate is necessary rather than a nicety."""
        fake(FakeProvider(FetchedEmail(body="word " * 400)))
        add_email(thread)
        await hydrate(thread)

        assert (await hydrate(thread, force=True))["requested"] == 0

    async def test_an_already_summarised_row_is_not_paid_for_twice(
        self, thread, fake, monkeypatch
    ):
        monkeypatch.setattr(eb, "summarise_sync", lambda *a, **k: ("Once", "m"))
        fake(FakeProvider(FetchedEmail(body="word " * 400)))
        add_email(thread)
        await hydrate(thread)
        await eb.summarise_thread_emails(thread, thread_id=1)

        assert (await eb.summarise_thread_emails(thread, thread_id=1))["requested"] == 0

    async def test_it_can_be_scoped_to_named_ids(self, thread, fake, monkeypatch):
        """How one conversation's button avoids paying for the whole thread."""
        monkeypatch.setattr(eb, "summarise_sync", lambda *a, **k: ("S", "m"))
        fake(FakeProvider(FetchedEmail(body="word " * 400)))
        add_email(thread, email_id=1)
        add_email(thread, email_id=2)
        await hydrate(thread)

        result = await eb.summarise_thread_emails(thread, thread_id=1, email_ids=[2])

        assert result["summarised"] == 1
        assert result["remaining"] == 1
        assert row(thread, 1)["ai_summary"] is None
        assert row(thread, 2)["ai_summary"] == "S"

    async def test_it_is_bounded_per_call(self, thread, fake, monkeypatch):
        monkeypatch.setattr(eb, "summarise_sync", lambda *a, **k: ("S", "m"))
        fake(FakeProvider(FetchedEmail(body="word " * 400)))
        for i in range(1, eb.SUMMARISE_MAX_PER_CALL + 4):
            add_email(thread, email_id=i)
        await hydrate(thread)

        result = await eb.summarise_thread_emails(thread, thread_id=1)

        assert result["requested"] == eb.SUMMARISE_MAX_PER_CALL
        assert result["remaining"] == 3

    async def test_a_row_with_no_body_is_never_selected(self, thread, fake, monkeypatch):
        called = []
        monkeypatch.setattr(
            eb, "summarise_sync", lambda *a, **k: called.append(1) or ("x", "m")
        )
        add_email(thread)  # never hydrated

        assert (await eb.summarise_thread_emails(thread, thread_id=1))["requested"] == 0
        assert called == []


class TestSummariseSync:
    """The blocking helper itself, exercised through REAL_SUMMARISE."""

    def test_summarise_sync_succeeds_on_a_well_formed_reply(self, thread, monkeypatch):
        """The positive case, so the three below cannot pass vacuously."""
        from app.services import llm as llm_svc

        monkeypatch.setattr(
            llm_svc, "chat_json", lambda *a, **k: ({"summary": "Priya confirms"}, {}, "")
        )
        summary, model = REAL_SUMMARISE(
            thread, body="word " * 400, subject="S", sender="a@b"
        )
        assert summary == "Priya confirms"
        assert model, "the model that produced it is recorded"

    def test_summarise_sync_returns_none_when_the_llm_fails(self, thread, monkeypatch):
        """respx is not even needed: the point is that no exception escapes."""
        from app.services import llm as llm_svc

        def boom(*a, **k):
            raise llm_svc.LLMError("upstream down")

        monkeypatch.setattr(llm_svc, "chat_json", boom)
        assert REAL_SUMMARISE(
            thread, body="word " * 400, subject="S", sender="a@b"
        ) == (None, None)

    def test_summarise_sync_rejects_an_empty_summary(self, thread, monkeypatch):
        from app.services import llm as llm_svc

        monkeypatch.setattr(
            llm_svc, "chat_json", lambda *a, **k: ({"summary": "   "}, {}, "")
        )
        assert REAL_SUMMARISE(
            thread, body="word " * 400, subject="S", sender="a@b"
        ) == (None, None)

    def test_summarise_sync_passes_the_body_through_without_str_format(
        self, thread, monkeypatch
    ):
        """An email body is full of literal braces -- str.format would raise or
        leak field names. prompts.substitute uses str.replace."""
        from app.services import llm as llm_svc

        seen = {}

        def capture(config, system, user, **kwargs):
            seen["user"] = user
            return {"summary": "ok"}, {}, ""

        monkeypatch.setattr(llm_svc, "chat_json", capture)
        REAL_SUMMARISE(
            thread, body='Deploy with {"key": "value"} config', subject="S",
            sender="a@b",
        )
        assert '{"key": "value"}' in seen["user"]

    def test_summarise_sync_bounds_what_it_sends(self, thread, monkeypatch):
        """One enormous newsletter must not dominate a request."""
        from app.services import llm as llm_svc

        seen = {}

        def capture(config, system, user, **kwargs):
            seen["user"] = user
            return {"summary": "ok"}, {}, ""

        monkeypatch.setattr(llm_svc, "chat_json", capture)
        REAL_SUMMARISE(
            thread, body="x" * (eb.SUMMARY_INPUT_CHARS + 5_000), subject="S",
            sender="a@b",
        )
        assert seen["user"].count("x") == eb.SUMMARY_INPUT_CHARS


# --------------------------------------------------------------------------- #
# Read helpers
# --------------------------------------------------------------------------- #


class TestRowColumns:
    def test_the_shared_projection_never_carries_a_body(self, thread):
        """The list and timeline routes use this. SQLite reads whole rows, so a
        32KB body pulled off disk by a read that never shows one is pure cost."""
        add_email(thread, body="a body", body_fetched_at=utcnow())
        with get_conn(thread) as conn:
            r = conn.execute(
                f"SELECT {eb.ROW_COLUMNS} FROM thread_emails WHERE id = 1"
            ).fetchone()

        assert "body" not in r.keys()
        assert "raw_json" not in r.keys()
        # ...but whether there IS one is reported, so the UI can offer to load it.
        assert r["has_body"] == 1

    def test_has_body_is_false_before_hydration(self, thread):
        add_email(thread)
        with get_conn(thread) as conn:
            r = conn.execute(
                f"SELECT {eb.ROW_COLUMNS} FROM thread_emails WHERE id = 1"
            ).fetchone()
        assert r["has_body"] == 0

    def test_body_of_is_thread_scoped(self, thread):
        add_email(thread, body="mine", body_fetched_at=utcnow())
        with get_conn(thread) as conn:
            assert eb.body_of(conn, 1, 1)["body"] == "mine"
            # A different thread's id must not reach this row.
            assert eb.body_of(conn, 2, 1) is None
