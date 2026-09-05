"""Read-time grouping of a thread's emails into conversations.

Pure functions over dicts, so every test here is a hand-built row list -- no
connection, no provider fake, no fixtures. That is the point of keeping
`email_chains` free of I/O: the risky part of the feature is testable in
complete isolation from the part that talks to Gmail.
"""

from __future__ import annotations

from app.services.email_chains import (
    GENERIC_SUBJECTS,
    MAX_SUBJECT_BUCKET,
    build_chains,
    chain_addresses,
    normalize_message_id,
    normalize_subject,
    parse_references,
)
from app.services.providers.base import MAX_REFERENCES, normalize_address

ME = "me@example.com"


def email(
    message_id: str,
    *,
    subject: str | None = None,
    sender: str | None = None,
    to: str | None = None,
    cc: str | None = None,
    date: str | None = None,
    rfc: str | None = None,
    in_reply_to: str | None = None,
    references: object = None,
    conversation_id: str | None = None,
    direction: str | None = None,
    unread: bool = False,
    reply_dismissed_at: str | None = None,
) -> dict:
    return {
        "message_id": message_id,
        "subject": subject,
        "sender": sender,
        "to_recipients": to,
        "cc_recipients": cc,
        "date": date,
        "rfc_message_id": rfc,
        "in_reply_to": in_reply_to,
        "references_json": references,
        "conversation_id": conversation_id,
        "direction": direction,
        "unread": unread,
        "reply_dismissed_at": reply_dismissed_at,
    }


def keys(chains: list[dict]) -> list[set[str]]:
    """The message_id sets of each chain, for order-insensitive assertions."""
    return [{m["message_id"] for m in c["messages"]} for c in chains]


def chain_holding(chains: list[dict], message_id: str) -> dict:
    return next(c for c in chains if any(m["message_id"] == message_id for m in c["messages"]))


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


def test_normalize_subject_strips_nested_and_localized_prefixes():
    assert normalize_subject("Re: Fwd: RE: Atlas cutover") == "atlas cutover"
    assert normalize_subject("Re[2]: Atlas") == "atlas"
    assert normalize_subject("Aw: Sv: Projekt") == "projekt"
    assert normalize_subject("回复: Atlas") == "atlas"
    assert normalize_subject("Re：Atlas") == "atlas"  # full-width colon
    assert normalize_subject("  Atlas   cutover  ") == "atlas cutover"
    assert normalize_subject(None) == ""


def test_normalize_subject_is_consistent_for_mailing_list_tags():
    """It need only be *consistent*, not maximally aggressive.

    A list tag is not stripped -- it might be real subject text -- but both the
    original and its reply normalise identically, which is all chaining needs.
    """
    assert normalize_subject("[team] Atlas") == normalize_subject("Re: [team] Atlas")


def test_normalize_message_id_returns_none_for_an_absent_header():
    """The guard that stops every header-less row unioning into one chain.

    An empty string is a perfectly good dict key, so returning "" here would
    make "we don't know" behave like a shared identity.
    """
    assert normalize_message_id("<A@X.com>") == "a@x.com"
    assert normalize_message_id("a@x.com") == "a@x.com"
    assert normalize_message_id("") is None
    assert normalize_message_id("   ") is None
    assert normalize_message_id("<>") is None
    assert normalize_message_id(None) is None


def test_parse_references_accepts_header_text_json_and_sequences():
    assert parse_references("<a@x> <b@x>") == ["a@x", "b@x"]
    assert parse_references('["<a@x>", "<b@x>"]') == ["a@x", "b@x"]
    assert parse_references(["<a@x>", "<b@x>"]) == ["a@x", "b@x"]
    assert parse_references(None) == []
    assert parse_references("") == []


def test_parse_references_is_bounded_and_keeps_the_nearest_ancestors():
    refs = [f"<{i}@x>" for i in range(MAX_REFERENCES + 10)]
    parsed = parse_references(refs)

    assert len(parsed) == MAX_REFERENCES
    # The tail, not the head: the oldest ids in a long thread are the ones no
    # attached message will ever cite.
    assert parsed[-1] == f"{MAX_REFERENCES + 9}@x"


def test_normalize_address_handles_display_names_and_malformed_headers():
    assert normalize_address("Alice <A@B.com>") == "a@b.com"
    assert normalize_address("a@b.com") == "a@b.com"
    assert normalize_address("<bare@x.com>") == "bare@x.com"
    assert normalize_address('"Odd, Name" <o@x.com>') == "o@x.com"
    # A display name with no address is not an address.
    assert normalize_address("Priya Raman") is None
    assert normalize_address("") is None
    assert normalize_address(None) is None


def test_normalize_address_keeps_plus_tags():
    """sales@ and sales+eu@ are different mailboxes; collapsing over-merges."""
    assert normalize_address("sales+eu@x.com") == "sales+eu@x.com"
    assert normalize_address("sales@x.com") != normalize_address("sales+eu@x.com")


def test_chain_addresses_reads_all_three_participant_headers():
    row = email("m", sender="Alice <a@x.com>", to="b@x.com, Carl <c@x.com>", cc="d@x.com")
    assert chain_addresses(row) == {"a@x.com", "b@x.com", "c@x.com", "d@x.com"}


def test_chain_addresses_survives_absent_to_and_cc_headers():
    """The `getaddresses` hardening trap, locked down.

    Since the CVE-2023-27043 fix, `getaddresses` returns a single ``[('', '')]``
    -- discarding every address it *did* parse -- if any element of the list is
    malformed, and "" counts as malformed. Passing empty to/cc through would
    therefore yield no addresses at all, silently reducing participant overlap
    to nothing so the subject tier could never merge anything. Most attached
    rows have no to/cc, so this is the common case, not an edge case.
    """
    assert chain_addresses(email("m", sender="alice@x.com")) == {"alice@x.com"}
    assert chain_addresses(email("m", sender="alice@x.com", to="", cc="")) == {"alice@x.com"}
    assert chain_addresses(email("m")) == set()


# --------------------------------------------------------------------------- #
# Tier 1 -- the provider's conversation id
# --------------------------------------------------------------------------- #


def test_a_conversation_id_groups_messages():
    rows = [
        email("m1", conversation_id="google:7:tABC", date="2026-08-01T10:00:00+00:00"),
        email("m2", conversation_id="google:7:tABC", date="2026-08-02T10:00:00+00:00"),
        email("m3", conversation_id="google:7:tZZZ", date="2026-08-03T10:00:00+00:00"),
    ]
    chains = build_chains(rows)

    assert sorted(keys(chains), key=len) == [{"m3"}, {"m1", "m2"}]


def test_two_accounts_holding_one_gmail_thread_do_not_merge():
    """The namespacing guard.

    Gmail's threadId is unique per *mailbox*, so two connected accounts that both
    hold one conversation carry the same native id. `uid_for` namespacing is what
    keeps them apart; this test fails if someone "simplifies" it away.
    """
    rows = [
        email("a", conversation_id="google:1:tABC", subject="X", date="2026-08-01T10:00:00+00:00"),
        email("b", conversation_id="google:2:tABC", subject="Y", date="2026-08-02T10:00:00+00:00"),
    ]
    assert len(build_chains(rows)) == 2


def test_a_null_conversation_id_is_not_a_shared_key():
    rows = [
        email("a", conversation_id=None, subject="Distinct one", sender="p@x.com",
              date="2026-08-01T10:00:00+00:00"),
        email("b", conversation_id="", subject="Distinct two", sender="q@x.com",
              date="2026-08-02T10:00:00+00:00"),
    ]
    assert len(build_chains(rows)) == 2


# --------------------------------------------------------------------------- #
# Tier 2 -- the RFC 2822 header graph
# --------------------------------------------------------------------------- #


def test_in_reply_to_links_a_reply_to_its_parent():
    rows = [
        email("m1", rfc="<a@x>", subject="Atlas", date="2026-08-01T10:00:00+00:00"),
        email("m2", rfc="<b@x>", in_reply_to="<a@x>", subject="Re: Atlas",
              date="2026-08-02T10:00:00+00:00"),
    ]
    chains = build_chains(rows)

    assert len(chains) == 1
    assert chains[0]["message_count"] == 2


def test_two_replies_to_an_unheld_original_are_one_chain():
    """The common real case, not an edge case.

    A person attaches the replies and never the original, so nothing links them
    except that they both name the same absent parent.
    """
    rows = [
        email("r1", rfc="<r1@x>", in_reply_to="<orig@x>", subject="Re: X",
              sender="a@x.com", date="2026-08-02T10:00:00+00:00"),
        email("r2", rfc="<r2@x>", in_reply_to="<orig@x>", subject="Re: X",
              sender="b@x.com", date="2026-08-03T10:00:00+00:00"),
    ]
    assert keys(build_chains(rows)) == [{"r1", "r2"}]


def test_a_references_header_links_across_a_missing_middle():
    rows = [
        email("m1", rfc="<a@x>", subject="Atlas", date="2026-08-01T10:00:00+00:00"),
        email("m3", rfc="<c@x>", references=["<a@x>", "<b@x>"], subject="Re: Atlas",
              date="2026-08-03T10:00:00+00:00"),
    ]
    assert keys(build_chains(rows)) == [{"m1", "m3"}]


def test_an_empty_message_id_header_never_unions_anything():
    rows = [
        email(f"e{i}", rfc="", subject=f"Distinct {i}", sender=f"p{i}@x.com",
              date="2026-08-01T10:00:00+00:00")
        for i in range(3)
    ]
    assert len(build_chains(rows)) == 3


def test_a_duplicate_rfc_id_seen_through_two_accounts_is_one_chain():
    rows = [
        email("g", rfc="<same@x>", subject="Atlas", date="2026-08-01T10:00:00+00:00"),
        email("z", rfc="<same@x>", subject="Atlas", date="2026-08-01T10:00:00+00:00"),
        email("r", rfc="<r@x>", in_reply_to="<same@x>", subject="Re: Atlas",
              date="2026-08-02T10:00:00+00:00"),
    ]
    assert keys(build_chains(rows)) == [{"g", "z", "r"}]


# --------------------------------------------------------------------------- #
# Tier 3 -- subject + participants, and its five guards
# --------------------------------------------------------------------------- #


def test_a_shared_subject_and_participant_merges():
    rows = [
        email("a", subject="Atlas cutover plan", sender="alice@x.com", to=ME,
              date="2026-08-01T10:00:00+00:00"),
        email("b", subject="Re: Atlas cutover plan", sender=ME, to="alice@x.com",
              date="2026-08-02T10:00:00+00:00"),
    ]
    assert keys(build_chains(rows, account_addresses=[ME])) == [{"a", "b"}]


def test_generic_subjects_never_merge():
    """Guard 1. A wrong chain is read as a fact about the conversation."""
    rows = [
        email(f"m{i}", subject="Re: Invoice", sender=f"p{i}@x.com", to=ME,
              date=f"2026-08-0{i + 1}T10:00:00+00:00")
        for i in range(3)
    ]
    assert len(build_chains(rows, account_addresses=[ME])) == 3


def test_every_generic_subject_entry_is_already_normalised():
    """A GENERIC_SUBJECTS entry that needs stripping would never be matched."""
    for subject in GENERIC_SUBJECTS:
        assert normalize_subject(subject) == subject


def test_an_oversized_subject_bucket_is_left_alone():
    """Guard 2. Thirteen messages sharing a subject is a newsletter."""
    count = MAX_SUBJECT_BUCKET + 1
    rows = [
        email(f"n{i}", subject="Nightly build report", sender="ci@x.com", to=ME,
              date=f"2026-08-{i + 1:02d}T10:00:00+00:00")
        for i in range(count)
    ]
    assert len(build_chains(rows, account_addresses=[ME])) == count


def test_the_only_shared_participant_being_me_is_not_enough():
    """Guard 3. Every message in your own mailbox shares you."""
    rows = [
        email("a", subject="Atlas plan", sender="x@x.com", to=ME,
              date="2026-08-01T10:00:00+00:00"),
        email("b", subject="Atlas plan", sender="y@x.com", to=ME,
              date="2026-08-02T10:00:00+00:00"),
    ]
    assert len(build_chains(rows, account_addresses=[ME])) == 2
    # And the converse: without the account address subtracted, "we both
    # involve me" would look like evidence. This is what the parameter is for.
    assert len(build_chains(rows)) == 1


def test_an_undated_row_is_never_subject_merged():
    """Guard 4. It cannot pass the gap check, and merging blind is how a
    year-old mail joins today's thread."""
    rows = [
        email("a", subject="Atlas plan", sender="x@x.com", date="2026-08-01T10:00:00+00:00"),
        email("b", subject="Atlas plan", sender="x@x.com", date=None),
    ]
    assert len(build_chains(rows)) == 2


def test_a_nine_month_gap_on_the_same_subject_stays_two_chains():
    """Guard 5. "Re: Invoice" nine months later is a new conversation."""
    rows = [
        email("a", subject="Atlas rollout plan", sender="x@x.com",
              date="2026-01-01T10:00:00+00:00"),
        email("b", subject="Atlas rollout plan", sender="x@x.com",
              date="2026-10-01T10:00:00+00:00"),
    ]
    assert len(build_chains(rows)) == 2


def test_a_slow_conversation_within_the_gap_stays_one_chain():
    """Consecutive pairs, not all pairs: monthly replies remain one chain even
    though the first and last are further apart than the gap allows."""
    rows = [
        email("a", subject="Atlas rollout plan", sender="x@x.com",
              date="2026-01-01T10:00:00+00:00"),
        email("b", subject="Re: Atlas rollout plan", sender="x@x.com",
              date="2026-01-25T10:00:00+00:00"),
        email("c", subject="Re: Atlas rollout plan", sender="x@x.com",
              date="2026-02-18T10:00:00+00:00"),
    ]
    assert keys(build_chains(rows)) == [{"a", "b", "c"}]


def test_subject_merging_never_widens_an_authoritative_chain():
    """Tier 3 considers singletons only.

    m1+m2 are linked by conversation id. m3 shares their subject and a
    participant, but must not be pulled in on a guess.
    """
    rows = [
        email("m1", conversation_id="google:1:tA", subject="Atlas cutover",
              sender="alice@x.com", date="2026-08-01T10:00:00+00:00"),
        email("m2", conversation_id="google:1:tA", subject="Re: Atlas cutover",
              sender="alice@x.com", date="2026-08-02T10:00:00+00:00"),
        email("m3", subject="Re: Atlas cutover", sender="alice@x.com",
              date="2026-08-03T10:00:00+00:00"),
    ]
    chains = build_chains(rows)

    assert sorted(keys(chains), key=len) == [{"m3"}, {"m1", "m2"}]


# --------------------------------------------------------------------------- #
# Lazy backfill
# --------------------------------------------------------------------------- #


def test_all_null_threading_columns_still_chain_by_subject_and_sender():
    """Pre-backfill. Every row ever attached has a subject and a sender, so
    chaining degrades conservatively rather than failing."""
    rows = [
        email("a", subject="Atlas cutover plan", sender="alice@x.com",
              date="2026-08-01T10:00:00+00:00"),
        email("b", subject="Re: Atlas cutover plan", sender="alice@x.com",
              date="2026-08-02T10:00:00+00:00"),
    ]
    assert keys(build_chains(rows)) == [{"a", "b"}]


def test_hydrating_one_pair_does_not_rearrange_another_chain():
    """The monotonicity property -- the single most important test here.

    Lazy backfill means threading columns arrive over time. Because tier 3 only
    considers rows that are still singletons, hydrating a pair can only split a
    subject-guess into a real chain plus leftovers. It can never reshuffle a
    chain another message is already in, which is what would make a
    half-backfilled thread reorder its cards on every page view.
    """
    before = [
        email("p1", subject="Atlas cutover", sender="alice@x.com",
              date="2026-08-01T10:00:00+00:00"),
        email("p2", subject="Re: Atlas cutover", sender="alice@x.com",
              date="2026-08-02T10:00:00+00:00"),
        email("q1", subject="Titan review", sender="bob@x.com",
              date="2026-08-05T10:00:00+00:00"),
        email("q2", subject="Re: Titan review", sender="bob@x.com",
              date="2026-08-06T10:00:00+00:00"),
    ]
    grouped_before = sorted(keys(build_chains(before)), key=sorted)
    assert grouped_before == [{"p1", "p2"}, {"q1", "q2"}]

    # Hydration lands on the first pair only.
    after = [dict(row) for row in before]
    after[0]["rfc_message_id"] = "<p1@x>"
    after[1]["rfc_message_id"] = "<p2@x>"
    after[1]["in_reply_to"] = "<p1@x>"

    assert sorted(keys(build_chains(after)), key=sorted) == grouped_before


def test_the_chain_key_is_stable_when_an_unrelated_email_is_attached():
    rows = [
        email("m1", rfc="<a@x>", subject="Atlas", date="2026-08-01T10:00:00+00:00"),
        email("m2", rfc="<b@x>", in_reply_to="<a@x>", subject="Re: Atlas",
              date="2026-08-02T10:00:00+00:00"),
    ]
    before = chain_holding(build_chains(rows), "m1")["key"]

    rows.append(
        email("zz", subject="Something else entirely", sender="other@x.com",
              date="2026-08-09T10:00:00+00:00")
    )
    assert chain_holding(build_chains(rows), "m1")["key"] == before


# --------------------------------------------------------------------------- #
# Assembled shape
# --------------------------------------------------------------------------- #


def test_direction_none_yields_no_guess_about_who_is_awaited():
    """No surface may infer a side. A NULL read as inbound is what tells the
    summarizer someone else asked a question the user asked themselves."""
    rows = [email("m", subject="Atlas", sender="a@x.com", date="2026-08-01T10:00:00+00:00")]
    chain = build_chains(rows)[0]

    assert chain["last_message_from"] is None
    assert chain["awaiting"] is None


def test_awaiting_follows_the_newest_message():
    rows = [
        email("a", rfc="<a@x>", subject="Atlas", sender="alice@x.com", to=ME,
              date="2026-08-01T10:00:00+00:00", direction="inbound"),
        email("b", rfc="<b@x>", in_reply_to="<a@x>", subject="Re: Atlas", sender=ME,
              to="alice@x.com", date="2026-08-02T10:00:00+00:00", direction="outbound"),
    ]
    chain = build_chains(rows, account_addresses=[ME])[0]

    # I replied last, so the ball is in their court -- this is precisely the
    # case that must stop next_step suggesting "reply to Alice".
    assert chain["last_message_from"] == "you"
    assert chain["awaiting"] == "them"


def test_awaiting_is_you_when_they_spoke_last():
    rows = [
        email("a", rfc="<a@x>", subject="Atlas", sender=ME, to="alice@x.com",
              date="2026-08-01T10:00:00+00:00", direction="outbound"),
        email("b", rfc="<b@x>", in_reply_to="<a@x>", subject="Re: Atlas",
              sender="alice@x.com", to=ME, date="2026-08-02T10:00:00+00:00",
              direction="inbound"),
    ]
    chain = build_chains(rows, account_addresses=[ME])[0]

    assert chain["awaiting"] == "you"


def test_awaiting_is_none_when_reply_is_dismissed():
    rows = [
        email("a", rfc="<a@x>", subject="Atlas", sender=ME, to="alice@x.com",
              date="2026-08-01T10:00:00+00:00", direction="outbound"),
        email("b", rfc="<b@x>", in_reply_to="<a@x>", subject="Re: Atlas",
              sender="alice@x.com", to=ME, date="2026-08-02T10:00:00+00:00",
              direction="inbound", reply_dismissed_at="2026-08-02T11:00:00+00:00"),
    ]
    chain = build_chains(rows, account_addresses=[ME])[0]

    assert chain["last_message_from"] == "them"
    assert chain["awaiting"] is None



def test_messages_are_chronological_and_the_subject_is_the_earliest():
    rows = [
        email("b", rfc="<b@x>", in_reply_to="<a@x>", subject="Re: Re: Atlas cutover",
              date="2026-08-02T10:00:00+00:00"),
        email("a", rfc="<a@x>", subject="Atlas cutover",
              date="2026-08-01T10:00:00+00:00"),
    ]
    chain = build_chains(rows)[0]

    assert [m["message_id"] for m in chain["messages"]] == ["a", "b"]
    # Named by how it started, not by whatever "Re: Re:" the newest reply carries.
    assert chain["subject"] == "Atlas cutover"
    assert chain["first_message_at"] < chain["last_message_at"]


def test_participants_exclude_my_own_addresses():
    rows = [
        email("a", subject="Atlas", sender="alice@x.com", to=f"{ME}, bob@x.com",
              date="2026-08-01T10:00:00+00:00")
    ]
    chain = build_chains(rows, account_addresses=[ME])[0]

    assert ME not in chain["participants"]
    assert set(chain["participants"]) == {"alice@x.com", "bob@x.com"}


def test_chains_are_sorted_newest_first_with_undated_last():
    rows = [
        email("old", subject="Older thing", sender="a@x.com",
              date="2026-08-01T10:00:00+00:00"),
        email("new", subject="Newer thing", sender="b@x.com",
              date="2026-08-20T10:00:00+00:00"),
        email("none", subject="Undated thing", sender="c@x.com", date=None),
    ]
    chains = build_chains(rows)

    assert [c["messages"][0]["message_id"] for c in chains] == ["new", "old", "none"]


def test_unread_count_is_summed_across_the_chain():
    rows = [
        email("a", rfc="<a@x>", subject="Atlas", date="2026-08-01T10:00:00+00:00"),
        email("b", rfc="<b@x>", in_reply_to="<a@x>", subject="Re: Atlas",
              date="2026-08-02T10:00:00+00:00", unread=True),
        email("c", rfc="<c@x>", in_reply_to="<a@x>", subject="Re: Atlas",
              date="2026-08-03T10:00:00+00:00", unread=True),
    ]
    assert build_chains(rows)[0]["unread_count"] == 2


def test_a_lone_email_is_a_chain_of_one():
    """The timeline renders one card type, so a single email must still arrive
    as a chain rather than needing a second renderer."""
    chains = build_chains([email("solo", subject="Just one", sender="a@x.com",
                                 date="2026-08-01T10:00:00+00:00")])

    assert len(chains) == 1
    assert chains[0]["message_count"] == 1
    assert chains[0]["key"] == "solo"


def test_no_rows_is_no_chains():
    assert build_chains([]) == []


def test_every_row_appears_exactly_once():
    """Union-find must partition, not sample."""
    rows = [
        email("a", conversation_id="g:1:t", subject="A", date="2026-08-01T10:00:00+00:00"),
        email("b", conversation_id="g:1:t", subject="B", date="2026-08-02T10:00:00+00:00"),
        email("c", rfc="<c@x>", subject="C", sender="c@x.com",
              date="2026-08-03T10:00:00+00:00"),
        email("d", rfc="<d@x>", in_reply_to="<c@x>", subject="Re: C",
              date="2026-08-04T10:00:00+00:00"),
        email("e", subject="Lonely", sender="e@x.com", date=None),
    ]
    seen = [m["message_id"] for c in build_chains(rows) for m in c["messages"]]

    assert sorted(seen) == ["a", "b", "c", "d", "e"]
