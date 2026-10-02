"""MMN-16: CJK substring matching through the ``search_fts_tri`` trigram index.

unicode61 indexes a run of Han/Kana/Hangul as one token, so before this a
part of a run never matched. Covers query routing, the 1-2 character LIKE
path, AND semantics across the two indexes, owner isolation, escaping,
reconcile/rebuild of the side index and the home thread filter.
"""

from __future__ import annotations

import pytest

from app.services import notes as notes_svc
from app.services import search as search_svc
from app.services import search_index
from app.services import threads as threads_svc
from tests.search_support import add_transcript, make_user
from tests.test_search_query import FUZZ_CORPUS


@pytest.fixture
def owner(conn):
    return make_user(conn, "alice")


def keyword(conn, owner, q, **kw):
    return search_svc.search(conn, owner_id=owner, q=q, mode="keyword", **kw)


def titles(result):
    return sorted(h["title"] for h in result["hits"])


def note(conn, owner, thread_id, title, body):
    notes_svc.create_note(conn, thread_id=thread_id, meeting_id=None, title=title, body=body,
                          source="manual", user_id=owner)


def _filter(conn, owner, q):
    rows, _ = threads_svc.list_threads(
        conn, scope_sql="owner_id = ?", scope_params=[owner], q=q, archived=None,
        sort="updated_at", order="desc", limit=50, offset=0,
    )
    return sorted(r["title"] for r in rows)


# --------------------------------------------------------------------------- #
# Parsing / routing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw, match, cjk",
    [
        ("budget", '"budget"', ()),
        ("会议", None, ("会议",)),
        ("会议记录", None, ("会议记录",)),
        ("Q3 会议", '"Q3"', ("会议",)),
        ("会议*", None, ("会议",)),                      # substring already is a prefix
        ('"会议 记录"', None, ("会议", "记录")),          # phrase degrades to AND
        ('"Q3 会议" plan*', '"Q3" "plan"*', ("会议",)),
        ("Q3会议", None, ("Q3会议",)),                    # one mixed run stays one term
        ("会议,记录", None, ("会议", "记录")),            # punctuation still separates
        ("会议 会议", None, ("会议",)),                   # deduped
        ("カタカナ 한국어", None, ("カタカナ", "한국어")),
        ("%会_议%", None, ("会", "议")),                  # no LIKE wildcard survives
        ('会"议', None, ("会", "议")),
    ],
)
def test_parse_query_routes_cjk_words_to_the_trigram_arm(raw, match, cjk):
    parsed = search_svc.parse_query(raw)
    assert parsed.match == match
    assert parsed.cjk == cjk
    assert search_svc.to_match_expr(raw) == match


def test_tri_split_by_length():
    parsed = search_svc.parse_query("会议 会议记录 记")
    assert parsed.tri_match == '"会议记录"'
    assert parsed.tri_like == ["会议", "记"]


def test_has_cjk():
    for s in ("会", "カ", "ひ", "한", "々", "𠀀"):
        assert search_svc.has_cjk(s), s
    for s in ("abc", "café", "Ω", "١٢٣", "🙂"):
        assert not search_svc.has_cjk(s), s


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #


def test_two_char_han_substring_matches(conn, owner):
    threads_svc.create_thread(conn, owner_id=owner, title="会议记录 weekly")
    threads_svc.create_thread(conn, owner_id=owner, title="预算 review")
    assert titles(keyword(conn, owner, "会议")) == ["会议记录 weekly"]
    assert titles(keyword(conn, owner, "记录")) == ["会议记录 weekly"]  # mid/end of a run
    assert titles(keyword(conn, owner, "会议记录")) == ["会议记录 weekly"]  # whole run still works
    assert titles(keyword(conn, owner, "议")) == ["会议记录 weekly"]  # single character
    assert keyword(conn, owner, "会谈")["hits"] == []


def test_three_plus_char_terms_use_trigram_match_and_rank(conn, owner):
    t = threads_svc.create_thread(conn, owner_id=owner, title="Container")["id"]
    note(conn, owner, t, "随记", "我们讨论了第三季度预算的问题，然后结束了。")
    note(conn, owner, t, "第三季度预算", "无关内容")
    search_index.index_all_now(conn)
    result = keyword(conn, owner, "季度预算")
    assert [h["title"] for h in result["hits"]] == ["第三季度预算", "随记"]  # title boost
    body_hit = next(h for h in result["hits"] if h["title"] == "随记")
    assert "\x02" in body_hit["snippet"] and "\x03" in body_hit["snippet"]


@pytest.mark.parametrize(
    "text, sub",
    [
        ("カタカナのテキスト", "カタ"),        # Katakana, 2 chars
        ("カタカナのテキスト", "テキスト"),    # Katakana, 4 chars
        ("ひらがなのメモ", "がな"),            # Hiragana
        ("日本語のテキスト", "本語の"),        # mixed kanji/kana run
        ("주간 회의록 정리", "회의"),          # Hangul, 2 chars
        ("주간 회의록 정리", "회의록"),        # Hangul, 3 chars
    ],
)
def test_kana_and_hangul_substrings(conn, owner, text, sub):
    t = threads_svc.create_thread(conn, owner_id=owner, title="Container")["id"]
    note(conn, owner, t, "n", text)
    note(conn, owner, t, "other", "unrelated words only")
    search_index.index_all_now(conn)
    assert [h["kind"] for h in keyword(conn, owner, sub)["hits"]] == ["note"]


def test_like_only_hit_gets_a_marked_snippet(conn, owner):
    t = threads_svc.create_thread(conn, owner_id=owner, title="Container")["id"]
    note(conn, owner, t, "n", "开场白。" * 20 + "我们开了一个会议，讨论预算。" + "结尾。" * 30)
    search_index.index_all_now(conn)
    hit = keyword(conn, owner, "会议")["hits"][0]
    assert "\x02会议\x03" in hit["snippet"]
    assert hit["snippet"].startswith("…") and hit["snippet"].endswith("…")
    assert len(hit["snippet"]) < 120


def test_like_snippet_nested_terms_are_marked_once():
    snip = search_svc.like_snippet("t", "会议记录", ["会", "会议"])
    assert snip == "\x02会议\x03记录"


def test_mixed_query_is_and_across_both_indexes(conn, owner):
    threads_svc.create_thread(conn, owner_id=owner, title="Q3 会议记录")
    threads_svc.create_thread(conn, owner_id=owner, title="Q3 budget")
    threads_svc.create_thread(conn, owner_id=owner, title="Q4 会议")
    assert titles(keyword(conn, owner, "Q3 会议")) == ["Q3 会议记录"]
    assert titles(keyword(conn, owner, "会议 Q3")) == ["Q3 会议记录"]  # order-independent
    assert len(keyword(conn, owner, "Q3")["hits"]) == 2
    assert len(keyword(conn, owner, "会议")["hits"]) == 2
    # And with a 3+ char CJK term alongside a latin one.
    assert titles(keyword(conn, owner, "Q3 会议记录")) == ["Q3 会议记录"]
    assert keyword(conn, owner, "Q4 会议记录")["hits"] == []


def test_two_cjk_terms_are_anded(conn, owner):
    threads_svc.create_thread(conn, owner_id=owner, title="会议 预算")
    threads_svc.create_thread(conn, owner_id=owner, title="会议 招聘")
    assert titles(keyword(conn, owner, "会议 预算")) == ["会议 预算"]
    assert titles(keyword(conn, owner, "预算 会议记")) == []  # 3-char arm also ANDs


def test_cjk_hits_are_matched_by_keyword_and_rrf_merges_them(conn, owner):
    threads_svc.create_thread(conn, owner_id=owner, title="会议记录")
    result = search_svc.search(conn, owner_id=owner, q="会议", mode="hybrid")
    assert result["hits"][0]["matched_by"] == ["keyword"]
    assert result["mode_used"] == "keyword"  # embeddings are off in the suite


def test_cjk_respects_kind_and_thread_filters(conn, owner):
    a = threads_svc.create_thread(conn, owner_id=owner, title="会议 A")["id"]
    b = threads_svc.create_thread(conn, owner_id=owner, title="Other")["id"]
    m = threads_svc.create_meeting(conn, thread_id=b, owner_id=owner, title="周会")["id"]
    add_transcript(conn, m, ("SPEAKER_00", "今天的会议很短"))
    search_index.index_all_now(conn)
    assert {h["kind"] for h in keyword(conn, owner, "会")["hits"]} == {"thread", "meeting", "segment"}
    assert [h["kind"] for h in keyword(conn, owner, "会议", kinds="segment")["hits"]] == ["segment"]
    assert {h["thread_id"] for h in keyword(conn, owner, "会", thread_id=a)["hits"]} == {a}


# --------------------------------------------------------------------------- #
# Ownership + escaping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("q", ["会议", "会议记录", "Q3 会议"])
def test_owner_isolation_on_the_trigram_arm(conn, owner, q):
    bob = make_user(conn, "bob")
    threads_svc.create_thread(conn, owner_id=owner, title="Q3 会议记录 alice")
    t = threads_svc.create_thread(conn, owner_id=bob, title="Bob")["id"]
    note(conn, owner=bob, thread_id=t, title="Q3 会议记录 bob", body="Q3 会议记录")
    search_index.index_all_now(conn)
    assert titles(keyword(conn, owner, q)) == ["Q3 会议记录 alice"]
    assert titles(keyword(conn, bob, q)) == ["Q3 会议记录 bob"]
    assert _filter(conn, owner, q) == ["Q3 会议记录 alice"]


CJK_FUZZ = [
    "会%", "%会", "_会", "会_", "会\\", '"会', '会"', "会*", "*会", "会 OR 议", "会 NEAR 议",
    "会议记录*", '"会议 记录', "会-议", "(会)", "会:议", "^会", "会\x00议", "🙂会议", "会议🙂记录",
    "一" * 400, "会" + "a" * 300,
]


@pytest.mark.parametrize("q", FUZZ_CORPUS + CJK_FUZZ)
def test_no_input_can_raise_on_either_arm(conn, owner, q):
    threads_svc.create_thread(conn, owner_id=owner, title="Café naïve 会议记录 report 100%")
    keyword(conn, owner, q)  # must not raise sqlite3.OperationalError
    _filter(conn, owner, q)


def test_like_wildcards_are_literal(conn, owner):
    threads_svc.create_thread(conn, owner_id=owner, title="会议记录")
    threads_svc.create_thread(conn, owner_id=owner, title="会 X 议")
    # % and _ are separators, not wildcards: "会%议" is the AND of 会 and 议.
    assert titles(keyword(conn, owner, "会%议")) == ["会 X 议", "会议记录"]
    assert titles(keyword(conn, owner, "会_记")) == ["会议记录"]


# --------------------------------------------------------------------------- #
# Writes, reconcile, rebuild
# --------------------------------------------------------------------------- #


def _tri_count(conn):
    return conn.execute("SELECT COUNT(*) FROM search_fts_tri").fetchone()[0]


def _docs_count(conn):
    return conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0]


def test_every_doc_has_a_trigram_row_and_deletes_remove_it(conn, owner):
    t = threads_svc.create_thread(conn, owner_id=owner, title="会议")["id"]
    note(conn, owner, t, "n", "记录")
    search_index.index_all_now(conn)
    assert _tri_count(conn) == _docs_count(conn) == 2
    nid = conn.execute("SELECT id FROM thread_notes").fetchone()[0]
    search_index.delete_doc(conn, "note", nid)
    assert _tri_count(conn) == _docs_count(conn) == 1
    search_index.delete_thread_scope(conn, t)
    assert _tri_count(conn) == _docs_count(conn) == 0


def test_rewriting_a_doc_replaces_its_trigram_text(conn, owner):
    t = threads_svc.create_thread(conn, owner_id=owner, title="会议记录")["id"]
    assert titles(keyword(conn, owner, "会议")) == ["会议记录"]
    conn.execute("UPDATE threads SET title = '预算评审' WHERE id = ?", (t,))
    search_index.index_thread_doc(conn, t)
    assert keyword(conn, owner, "会议")["hits"] == []
    assert titles(keyword(conn, owner, "评审")) == ["预算评审"]
    assert _tri_count(conn) == 1


def test_reconcile_backfills_a_missing_trigram_table(conn, owner):
    # A pre-MMN-16 install: docs and unicode61 text, no trigram rows at all.
    t = threads_svc.create_thread(conn, owner_id=owner, title="会议记录")["id"]
    note(conn, owner, t, "n", "周报 摘要")
    search_index.index_all_now(conn)
    conn.execute("DELETE FROM search_fts_tri")
    assert keyword(conn, owner, "会议")["hits"] == []

    result = search_index.reconcile(conn)
    assert result["repaired"] >= 2
    search_index.index_all_now(conn)
    assert titles(keyword(conn, owner, "会议")) == ["会议记录"]
    assert [h["kind"] for h in keyword(conn, owner, "摘要")["hits"]] == ["note"]
    assert _tri_count(conn) == _docs_count(conn)


def test_reconcile_drops_orphan_trigram_rows(conn, owner):
    threads_svc.create_thread(conn, owner_id=owner, title="会议")
    search_index.index_all_now(conn)
    conn.execute("INSERT INTO search_fts_tri (rowid, title, body) VALUES (99999, '孤儿', '')")
    search_index.reconcile(conn)
    assert conn.execute("SELECT COUNT(*) FROM search_fts_tri WHERE rowid = 99999").fetchone()[0] == 0
    assert _tri_count(conn) == _docs_count(conn) == 1


def test_reconcile_repairs_a_doc_missing_only_its_unicode61_row(conn, owner):
    threads_svc.create_thread(conn, owner_id=owner, title="会议 alpha")
    search_index.index_all_now(conn)
    doc_id = conn.execute("SELECT id FROM search_docs").fetchone()[0]
    conn.execute("DELETE FROM search_fts WHERE rowid = ?", (doc_id,))
    search_index.index_all_now(conn)
    assert titles(keyword(conn, owner, "alpha")) == ["会议 alpha"]
    assert titles(keyword(conn, owner, "会议")) == ["会议 alpha"]
    # The repaired doc's leftover trigram row was swept, not duplicated.
    assert _tri_count(conn) == _docs_count(conn) == 1


def test_rebuild_clears_and_repopulates_the_trigram_table(conn, owner):
    t = threads_svc.create_thread(conn, owner_id=owner, title="会议记录")["id"]
    note(conn, owner, t, "n", "预算")
    search_index.index_all_now(conn)
    search_index.rebuild(conn)
    assert _tri_count(conn) == 0
    search_index.index_all_now(conn)
    assert _tri_count(conn) == _docs_count(conn) == 2
    assert titles(keyword(conn, owner, "会议")) == ["会议记录"]


def test_render_version_was_bumped_for_the_backfill():
    assert search_index.RENDER_VERSION != "1"
    assert "search_fts_tri" in search_index.FTS_TABLES


# --------------------------------------------------------------------------- #
# Home thread filter parity
# --------------------------------------------------------------------------- #


def test_thread_filter_finds_cjk_substrings(conn, owner):
    threads_svc.create_thread(conn, owner_id=owner, title="会议记录 weekly")
    threads_svc.create_thread(conn, owner_id=owner, title="Q3 plan", description="周会 摘要")
    threads_svc.create_thread(conn, owner_id=owner, title="Other")
    assert _filter(conn, owner, "会议") == ["会议记录 weekly"]
    assert _filter(conn, owner, "会议记") == ["会议记录 weekly"]
    assert _filter(conn, owner, "会") == ["Q3 plan", "会议记录 weekly"]
    assert _filter(conn, owner, "摘要 Q3") == ["Q3 plan"]
    assert _filter(conn, owner, "weekly 会议") == ["会议记录 weekly"]
    assert _filter(conn, owner, "Q3 会议") == []


def test_thread_filter_cjk_matches_search_everything(conn, owner):
    for title in ("会议记录", "会议", "记录", "预算会议纪要", "Other"):
        threads_svc.create_thread(conn, owner_id=owner, title=title)
    for q in ("会议", "记录", "会议纪", "议", "会议 记录"):
        everything = titles(keyword(conn, owner, q, kinds="thread"))
        assert _filter(conn, owner, q) == everything, q


def test_thread_filter_cjk_matches_only_thread_docs(conn, owner):
    t = threads_svc.create_thread(conn, owner_id=owner, title="Container")["id"]
    note(conn, owner, t, "会议", "会议")
    search_index.index_all_now(conn)
    assert _filter(conn, owner, "会议") == []


# --------------------------------------------------------------------------- #
# Settings -> Search
# --------------------------------------------------------------------------- #


def test_status_reports_index_sizes_to_admins(conn, owner, monkeypatch):
    monkeypatch.setattr(search_svc, "_SIZE_CACHE", {})
    t = threads_svc.create_thread(conn, owner_id=owner, title="会议记录")["id"]
    note(conn, owner, t, "n", "预算 " * 200)
    search_index.index_all_now(conn)
    sizes = search_svc.status(conn, owner_id=owner, is_admin=True)["global"]["index_bytes"]
    assert sizes["keyword"] > 0 and sizes["trigram"] > 0
    assert "global" not in search_svc.status(conn, owner_id=owner, is_admin=False)


def test_index_sizes_are_none_without_dbstat(conn, monkeypatch):
    import sqlite3

    monkeypatch.setattr(search_svc, "_SIZE_CACHE", {})

    class NoDbstat:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, *a):
            if "dbstat" in sql:
                raise sqlite3.OperationalError("no such table: dbstat")
            return self.inner.execute(sql, *a)

    assert search_svc.index_sizes(NoDbstat(conn)) == {"keyword": None, "trigram": None}
