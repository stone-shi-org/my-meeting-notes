"""MMN-15: MATCH escaping, query semantics, ranking, RRF and owner isolation."""

from __future__ import annotations

import random

import pytest

from app.errors import ValidationError
from app.services import notes as notes_svc
from app.services import search as search_svc
from app.services import search_index
from app.services import threads as threads_svc
from tests.search_support import add_transcript, make_user


@pytest.fixture
def owner(conn):
    return make_user(conn, "alice")


def keyword(conn, owner, q, **kw):
    return search_svc.search(conn, owner_id=owner, q=q, mode="keyword", **kw)


def titles(result):
    return [h["title"] for h in result["hits"]]


# --------------------------------------------------------------------------- #
# to_match_expr
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("budget", '"budget"'),
        ("Q3 budget", '"Q3" "budget"'),
        ('"exact phrase" here', '"exact phrase" "here"'),
        ('"unbalanced phrase', '"unbalanced phrase"'),
        ("migr*", '"migr"*'),
        ("a*", '"a"'),
        ("e-mail", '"e mail"'),
        ("NEAR OR AND NOT", '"NEAR" "OR" "AND" "NOT"'),
        ("col:value", '"col value"'),
        ("-negated", '"negated"'),
        ("", None),
        ("   ", None),
        ("%", None),
        ('"', None),
        ("*", None),
        ("🙂🎉", None),
        ("!!! ??? ()", None),
    ],
)
def test_to_match_expr(raw, expected):
    assert search_svc.to_match_expr(raw) == expected


FUZZ_CORPUS = [
    '"', '""', '"""', "'", "-", "--", "*", "**", "a*b", "^", "^start", "(", ")", "((a)",
    "NEAR(a b)", "a NEAR/2 b", "a OR", "OR", "AND b", "NOT", "a:b", ":", "{a b}", "[x]",
    "\\", "\\\"", "%", "_", "a_b", "🙂", "👩‍💻 dev", "会议记录", "日本語のテキスト", "한국어",
    "café", "naïve façade", "\x00", "​", "tab\tsep", "new\nline", "+plus", "a+b",
    "x" * 500, "  spaced  out  ", "*prefix", "pre*fix", '"open * close"', "1.5", "#tag",
]


@pytest.mark.parametrize("q", FUZZ_CORPUS)
def test_no_input_can_raise_an_fts_syntax_error(conn, owner, q):
    threads_svc.create_thread(conn, owner_id=owner, title="Café naïve 会议记录 report")
    keyword(conn, owner, q)  # must not raise sqlite3.OperationalError


def test_random_fuzz_never_raises(conn, owner):
    threads_svc.create_thread(conn, owner_id=owner, title="Fuzz target")
    alphabet = list('abc XYZ"-*()^:+\\%_.,;!?{}[]/|~`') + ["🙂", "会", "é", "\t"]
    rng = random.Random(15)
    for _ in range(300):
        q = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 30)))
        keyword(conn, owner, q)


# --------------------------------------------------------------------------- #
# Semantics
# --------------------------------------------------------------------------- #


def test_bare_words_are_anded_and_order_independent(conn, owner):
    threads_svc.create_thread(conn, owner_id=owner, title="Q3 budget review")
    threads_svc.create_thread(conn, owner_id=owner, title="Q3 hiring plan")
    assert titles(keyword(conn, owner, "budget q3")) == ["Q3 budget review"]
    assert len(keyword(conn, owner, "q3")["hits"]) == 2


def test_phrase_and_prefix(conn, owner):
    threads_svc.create_thread(conn, owner_id=owner, title="Database migration plan")
    threads_svc.create_thread(conn, owner_id=owner, title="Plan for database work")
    assert titles(keyword(conn, owner, '"database migration"')) == ["Database migration plan"]
    assert titles(keyword(conn, owner, "migr*")) == ["Database migration plan"]
    assert keyword(conn, owner, "migr")["hits"] == []  # whole words only


def test_diacritics_fold_both_ways(conn, owner):
    threads_svc.create_thread(conn, owner_id=owner, title="Café réunion")
    assert titles(keyword(conn, owner, "cafe reunion")) == ["Café réunion"]
    threads_svc.create_thread(conn, owner_id=owner, title="Resume screening")
    assert titles(keyword(conn, owner, "résumé")) == ["Resume screening"]


def test_cjk_whole_run_matches(conn, owner):
    # Documented limitation: unicode61 does not segment CJK, so the whole run
    # matches but a substring of it does not (trigram index is a follow-up).
    threads_svc.create_thread(conn, owner_id=owner, title="会议记录 weekly")
    assert titles(keyword(conn, owner, "会议记录")) == ["会议记录 weekly"]
    assert keyword(conn, owner, "会议")["hits"] == []


def test_snippet_marks_match(conn, owner):
    t = threads_svc.create_thread(conn, owner_id=owner, title="T")["id"]
    notes_svc.create_note(conn, thread_id=t, meeting_id=None, title="Notes",
                          body="We agreed the budget is final.", source="manual", user_id=owner)
    search_index.index_all_now(conn)
    hit = keyword(conn, owner, "budget")["hits"][0]
    assert "\x02budget\x03" in hit["snippet"]


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #


def test_bm25_title_boost_beats_body_mentions(conn, owner):
    t = threads_svc.create_thread(conn, owner_id=owner, title="Container")["id"]
    notes_svc.create_note(conn, thread_id=t, meeting_id=None, title="Misc",
                          body="The kangaroo budget and the kangaroo plan and more kangaroo.",
                          source="manual", user_id=owner)
    notes_svc.create_note(conn, thread_id=t, meeting_id=None, title="Kangaroo",
                          body="Unrelated words here, a lot of them, padding the body text out.",
                          source="manual", user_id=owner)
    search_index.index_all_now(conn)
    assert titles(keyword(conn, owner, "kangaroo"))[0] == "Kangaroo"


def test_rrf_merge_ordering():
    # doc 2 is 2nd on keyword and 1st on semantic -> best combined.
    merged = search_svc.rrf_merge([1, 2, 3], [2, 4])
    order = [doc for doc, _ in merged]
    assert order == [2, 1, 4, 3]
    scores = dict(merged)
    assert scores[2] == pytest.approx(1 / 62 + 1 / 61)
    assert scores[1] == pytest.approx(1 / 61)
    assert scores[4] == pytest.approx(1 / 62)
    assert scores[3] == pytest.approx(1 / 63)


def test_rrf_tie_keeps_keyword_first():
    merged = search_svc.rrf_merge([10], [20])
    assert [d for d, _ in merged] == [10, 20]


# --------------------------------------------------------------------------- #
# Filters + paging
# --------------------------------------------------------------------------- #


def test_kinds_thread_and_date_filters(conn, owner):
    a = threads_svc.create_thread(conn, owner_id=owner, title="Alpha zeta")["id"]
    b = threads_svc.create_thread(conn, owner_id=owner, title="Beta")["id"]
    m = threads_svc.create_meeting(conn, thread_id=b, owner_id=owner, title="Zeta sync",
                                   meeting_at="2026-03-01T10:00:00+00:00")["id"]
    add_transcript(conn, m, ("SPEAKER_00", "zeta again"))
    search_index.index_all_now(conn)

    assert {h["kind"] for h in keyword(conn, owner, "zeta")["hits"]} == {"thread", "meeting", "segment"}
    assert [h["kind"] for h in keyword(conn, owner, "zeta", kinds="segment")["hits"]] == ["segment"]
    assert {h["thread_id"] for h in keyword(conn, owner, "zeta", thread_id=a)["hits"]} == {a}
    dated = keyword(conn, owner, "zeta", since="2026-02-01", until="2026-03-01")
    assert {h["kind"] for h in dated["hits"]} == {"meeting", "segment"}
    assert keyword(conn, owner, "zeta", until="2026-02-28", kinds="meeting")["hits"] == []


def test_unknown_kind_and_bad_dates_are_validation_errors(conn, owner):
    with pytest.raises(ValidationError):
        keyword(conn, owner, "x", kinds="segment,banana")
    with pytest.raises(ValidationError):
        keyword(conn, owner, "x", since="last tuesday")
    with pytest.raises(ValidationError):
        search_svc.search(conn, owner_id=owner, q="x", mode="fuzzy")


def test_pagination(conn, owner):
    for i in range(7):
        threads_svc.create_thread(conn, owner_id=owner, title=f"Ocelot {i}")
    first = keyword(conn, owner, "ocelot", limit=5)
    second = keyword(conn, owner, "ocelot", limit=5, offset=5)
    assert len(first["hits"]) == 5 and first["has_more"] is True
    assert len(second["hits"]) == 2 and second["has_more"] is False
    ids = [h["id"] for h in first["hits"] + second["hits"]]
    assert len(set(ids)) == 7


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #


def test_owner_isolation_keyword(conn, owner):
    bob = make_user(conn, "bob")
    threads_svc.create_thread(conn, owner_id=owner, title="Secret alpaca plan")
    t = threads_svc.create_thread(conn, owner_id=bob, title="Bob thread")["id"]
    notes_svc.create_note(conn, thread_id=t, meeting_id=None, title="n", body="alpaca too",
                          source="manual", user_id=bob)
    search_index.index_all_now(conn)
    assert [h["title"] for h in keyword(conn, owner, "alpaca")["hits"]] == ["Secret alpaca plan"]
    assert [h["kind"] for h in keyword(conn, bob, "alpaca")["hits"]] == ["note"]


# --------------------------------------------------------------------------- #
# The home thread filter (plan section 10a)
# --------------------------------------------------------------------------- #


def _filter(conn, owner, q, **kw):
    rows, total = threads_svc.list_threads(
        conn, scope_sql="owner_id = ?", scope_params=[owner], q=q, archived=None,
        sort="updated_at", order="desc", limit=50, offset=0, **kw,
    )
    return [r["title"] for r in rows], total


def test_thread_filter_uses_words_not_substrings(conn, owner):
    threads_svc.create_thread(conn, owner_id=owner, title="Weekly meeting notes")
    threads_svc.create_thread(conn, owner_id=owner, title="Q3 budget", description="Café plans")
    assert _filter(conn, owner, "meet")[0] == []
    assert _filter(conn, owner, "meet*")[0] == ["Weekly meeting notes"]
    assert _filter(conn, owner, "budget q3")[0] == ["Q3 budget"]
    assert _filter(conn, owner, "cafe")[0] == ["Q3 budget"]


def test_thread_filter_matches_only_thread_title_and_description(conn, owner):
    t = threads_svc.create_thread(conn, owner_id=owner, title="Container")["id"]
    notes_svc.create_note(conn, thread_id=t, meeting_id=None, title="Ibex", body="ibex",
                          source="manual", user_id=owner)
    search_index.index_all_now(conn)
    assert _filter(conn, owner, "ibex") == ([], 0)


def test_thread_filter_falls_back_to_like_for_punctuation_only(conn, owner):
    # Exactly the old behaviour, unchanged: LIKE '%%%' matches every thread.
    # The point is that it neither errors nor returns nothing.
    threads_svc.create_thread(conn, owner_id=owner, title="100% done")
    threads_svc.create_thread(conn, owner_id=owner, title="Other")
    assert sorted(_filter(conn, owner, "%")[0]) == ["100% done", "Other"]
    assert _filter(conn, owner, "!!!")[0] == []


def test_thread_filter_falls_back_to_like_for_unindexed_threads(conn, owner):
    # A thread written before the index existed (first boot after deploy).
    from app.db import utcnow

    now = utcnow()
    conn.execute(
        "INSERT INTO threads (owner_id, title, created_at, updated_at) VALUES (?, 'Legacy planning', ?, ?)",
        (owner, now, now),
    )
    threads_svc.create_thread(conn, owner_id=owner, title="Indexed planning")
    titles_found, total = _filter(conn, owner, "planning")
    assert sorted(titles_found) == ["Indexed planning", "Legacy planning"]
    assert total == 2
    # Substring fallback applies only to the unindexed row.
    assert _filter(conn, owner, "plan")[0] == ["Legacy planning"]


def test_thread_filter_respects_owner_scope(conn, owner):
    bob = make_user(conn, "bob")
    threads_svc.create_thread(conn, owner_id=bob, title="Bob's ibis")
    assert _filter(conn, owner, "ibis") == ([], 0)
