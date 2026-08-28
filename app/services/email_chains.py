"""Group a thread's attached emails into conversations, at read time.

Chains are **computed, never stored**. The tempting alternative -- a `chain_key`
column written at attach time -- fails on a property of the problem rather than
on convenience: a chain key is a *global* property of the set. Attaching one
email can merge two previously separate chains, so a stored key would need every
row in *both* chains rewritten, a fan-out write on a table that four independent
paths write to (`matching.attach_selected`, `followups.sweep_thread`,
`chat._tool_attach`, and `routers/calendar.py`'s create-meeting-from-event).
Read-time union-find has no such invariant to maintain, and it is also what makes
lazy backfill safe: the inputs fill in over time, and a stored key would go stale
the moment they did.

Corollary worth stating because the next person will want to: do **not** call
`build_chains` from `threads.compute_next_step_fingerprint`. That runs once per
row on every home-page load; the fingerprint stays row-level.

Three tiers, in descending authority:

1. The provider's own conversation id (Gmail's `threadId`, namespaced).
2. The RFC 2822 `In-Reply-To`/`References` graph.
3. Normalized subject plus participant overlap -- a *heuristic*, hedged by five
   guards below, and applied only to rows the first two tiers left alone.

Tier 3 running on singletons only is what makes the whole thing monotone under
lazy backfill: hydrating one pair splits a subject-guess into a real chain plus
leftovers, and can never rearrange a chain someone else is already in. Without
that rule, a half-backfilled thread would reshuffle its cards on every page view.

Pure functions over sequences of mappings -- no DB, no I/O -- so the tests are
hand-built dicts and need neither a connection nor a provider fake.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from app.services.matching import normalize_timestamp
from app.services.providers.base import MAX_REFERENCES, normalize_address

__all__ = [
    "build_chains",
    "normalize_subject",
    "normalize_message_id",
    "parse_references",
    "chain_addresses",
]

# Tier-3 bounds. None of these apply to tiers 1-2: an authoritative link is
# never second-guessed, however unlikely the timing or the participants look.
#
# "Re: Invoice" nine months later is a new conversation however well the
# participants overlap, so the subject tier gets a maximum gap.
SUBJECT_GAP_MAX_DAYS = 30
# Thirteen messages sharing one subject is a newsletter or an automated report,
# not a conversation. Merging them produces one useless mega-chain.
MAX_SUBJECT_BUCKET = 12
# Chain participant lists are displayed and sent to the LLM, so they are bounded
# for the same reason MAX_ATTENDEES is.
MAX_CHAIN_PARTICIPANTS = 12

# A subject that carries no evidence of identity. Merging on one of these groups
# unrelated mail from unrelated people, which is worse than not chaining at all:
# a wrong chain is read as a fact about the conversation.
GENERIC_SUBJECTS = frozenset(
    {
        "",
        "(no subject)",
        "no subject",
        "hi",
        "hey",
        "hello",
        "thanks",
        "thank you",
        "meeting",
        "update",
        "updates",
        "question",
        "quick question",
        "follow up",
        "followup",
        "invoice",
        "reminder",
        "notes",
        "call",
        "checking in",
        "touching base",
        "introduction",
        "intro",
    }
)

# Reply/forward prefixes, including the localized spellings that actually turn up
# in a mixed inbox: German (Aw/Wg), Scandinavian (Sv/Vs), Dutch (Antw),
# Italian (Rif/Ris), Spanish/Portuguese (Res/Enc), French (Tr), and the CJK
# forms Outlook and Gmail emit. An unmatched locale simply does not strip, which
# means it does not merge -- the safe direction.
_SUBJECT_PREFIX = re.compile(
    r"^\s*(?:re|aw|wg|sv|vs|antw|rif|ris|res|enc|fw|fwd|tr|回复|答复|转发|轉寄|回覆)"
    r"\s*(?:\[\d+\]|\(\d+\))?\s*[:：]\s*",
    re.IGNORECASE,
)
_WHITESPACE = re.compile(r"\s+")
_ANGLE_ID = re.compile(r"<[^<>]+>")


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #


def normalize_subject(value: str | None) -> str:
    """Strip every reply/forward prefix and collapse whitespace.

    Looped rather than a single substitution because real subjects nest them:
    "Re: Fwd: RE: [2] Atlas cutover" is one conversation, not four.
    """
    text = (value or "").strip()
    while True:
        stripped = _SUBJECT_PREFIX.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    return _WHITESPACE.sub(" ", text).strip().casefold()


def normalize_message_id(value: str | None) -> str | None:
    """A comparable RFC 2822 Message-ID, or None.

    Returning None rather than "" for an absent header is load-bearing: an empty
    string is a perfectly good dict key, so every header-less row in a thread
    would union into one chain on a value that means "we don't know".
    """
    text = (value or "").strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    text = text.strip("<>").strip().casefold()
    return text or None


def parse_references(value: object) -> list[str]:
    """Normalised ancestor ids from a References header, JSON array, or sequence.

    Accepts all three shapes because the column is written as JSON, providers
    hand over raw header text, and a candidate that has been through a
    ``raw_json`` round trip arrives as a list.
    """
    if value is None:
        return []

    if isinstance(value, (list, tuple)):
        items: list[str] = [str(item) for item in value]
    else:
        text = str(value).strip()
        if not text:
            return []
        if text.startswith("["):
            import json

            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                items = [str(item) for item in parsed]
            else:
                items = _ANGLE_ID.findall(text) or text.split()
        else:
            items = _ANGLE_ID.findall(text) or text.split()

    out = [normalize_message_id(item) for item in items]
    return [item for item in out if item][-MAX_REFERENCES:]


def _ancestors(row: Mapping[str, Any]) -> list[str]:
    """Every message-id this row claims to descend from, nearest first."""
    out: list[str] = []
    direct = normalize_message_id(row.get("in_reply_to"))
    if direct:
        out.append(direct)
    for ref in parse_references(row.get("references_json") or row.get("references")):
        if ref not in out:
            out.append(ref)
    return out


def chain_addresses(row: Mapping[str, Any]) -> set[str]:
    """Every address on one message, from all three participant headers.

    Empty headers are filtered out *before* `getaddresses` rather than passed as
    "". Since the CVE-2023-27043 hardening, `getaddresses` returns a single
    ``[('', '')]`` -- discarding every address it did parse -- if any element of
    the list is malformed, and an empty string counts as malformed. Passing
    ``["alice@x.com", "", ""]`` therefore yields *no* addresses at all, which
    silently reduces participant overlap to nothing and stops the subject tier
    ever merging. Filter first.
    """
    from email.utils import getaddresses

    headers = [
        text
        for text in (
            row.get("sender"),
            row.get("to_recipients"),
            row.get("cc_recipients"),
        )
        if text and str(text).strip()
    ]
    if not headers:
        return set()

    out: set[str] = set()
    for _, raw in getaddresses(headers):
        address = normalize_address(raw)
        if address:
            out.add(address)
    return out


def _stamp(row: Mapping[str, Any]) -> str | None:
    return normalize_timestamp(row.get("date"))


def _parsed_date(row: Mapping[str, Any]) -> datetime | None:
    stamp = _stamp(row)
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def _sort_key(row: Mapping[str, Any]) -> tuple[bool, str]:
    stamp = _stamp(row)
    return (stamp is None, stamp or "")


# --------------------------------------------------------------------------- #
# Union-find
# --------------------------------------------------------------------------- #


class _UnionFind:
    def __init__(self, size: int) -> None:
        self._parent = list(range(size))
        self._size = [1] * size

    def find(self, node: int) -> int:
        root = node
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[node] != root:  # path compression
            self._parent[node], node = root, self._parent[node]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._size[ra] < self._size[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        self._size[ra] += self._size[rb]

    def component_size(self, node: int) -> int:
        return self._size[self.find(node)]


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #


def build_chains(
    rows: Sequence[Mapping[str, Any]],
    *,
    account_addresses: Iterable[str] = (),
) -> list[dict]:
    """Group ``rows`` into conversations, newest chain first.

    ``account_addresses`` are the connected accounts' own addresses. They are
    subtracted before any participant-overlap test, because *every* message in
    your own mailbox shares you as a participant -- so "we both involve me" is
    not evidence of anything, and without this every thread collapses into one
    chain. They are also excluded from the displayed participant list, which is
    meant to say who the conversation is *with*.
    """
    if not rows:
        return []

    mine = {a for a in (normalize_address(x) for x in account_addresses) if a}
    uf = _UnionFind(len(rows))

    # ---- Tier 1: the provider's own conversation id. Authoritative. --------
    # Already namespaced by integration upstream, so two accounts holding one
    # Gmail thread stay two chains rather than colliding on a bare threadId.
    first_by_conversation: dict[str, int] = {}
    for index, row in enumerate(rows):
        key = (row.get("conversation_id") or "").strip()
        if not key:
            continue  # NULL contributes nothing; it is not a shared key
        if key in first_by_conversation:
            uf.union(index, first_by_conversation[key])
        else:
            first_by_conversation[key] = index

    # ---- Tier 2: the RFC 2822 header graph. Authoritative. -----------------
    owner: dict[str, int] = {}
    for index, row in enumerate(rows):
        mid = normalize_message_id(row.get("rfc_message_id"))
        if not mid:
            continue
        if mid in owner:
            # The same message seen through two accounts. Dedupe normally
            # collapses these, but a row attached before dedup keyed on
            # `rfc_message_id` can still slip through -- and it is by definition
            # the same conversation, so union rather than dropping one.
            uf.union(index, owner[mid])
        else:
            # First wins as the canonical holder, so a reply naming this id
            # attaches to one component rather than to whichever it saw last.
            owner[mid] = index

    # (a) rows whose parent we actually hold.
    for index, row in enumerate(rows):
        for ancestor in _ancestors(row):
            holder = owner.get(ancestor)
            if holder is not None:
                uf.union(index, holder)

    # (b) rows citing the same ancestor we do *not* hold. This is the common
    # real case, not an edge case: a person attaches the two replies and never
    # the original, so nothing links them except that they name the same parent.
    cited_by: dict[str, int] = {}
    for index, row in enumerate(rows):
        for ancestor in _ancestors(row):
            if ancestor in owner:
                continue
            if ancestor in cited_by:
                uf.union(index, cited_by[ancestor])
            else:
                cited_by[ancestor] = index

    # ---- Tier 3: normalized subject + participant overlap. Heuristic. ------
    # Singletons only. An authoritative chain is never widened by a guess, and
    # that restriction is also what makes hydration monotone (see module docs).
    singletons = [i for i in range(len(rows)) if uf.component_size(i) == 1]

    buckets: dict[str, list[int]] = {}
    for index in singletons:
        buckets.setdefault(normalize_subject(rows[index].get("subject")), []).append(index)

    for subject, bucket in buckets.items():
        # Guard 1: a generic subject is not evidence of identity.
        if subject in GENERIC_SUBJECTS:
            continue
        # Guard 2: an oversized bucket is a newsletter, not a conversation.
        if len(bucket) > MAX_SUBJECT_BUCKET:
            continue
        if len(bucket) < 2:
            continue

        ordered = sorted(bucket, key=lambda i: _sort_key(rows[i]))
        # Consecutive pairs, not every pair: chaining is transitive through
        # time, so a real conversation with monthly replies stays one chain
        # while two clusters nine months apart stay two.
        for left, right in zip(ordered, ordered[1:]):
            a, b = rows[left], rows[right]

            # Guard 3: the only shared participant being *you* is not evidence.
            if not (chain_addresses(a) & chain_addresses(b)) - mine:
                continue

            da, db = _parsed_date(a), _parsed_date(b)
            # Guard 4: an undated row cannot pass the gap check, and merging it
            # blind is how a year-old mail joins today's thread.
            if da is None or db is None:
                continue
            # Guard 5: a long enough silence is a new conversation.
            if abs(db - da) > timedelta(days=SUBJECT_GAP_MAX_DAYS):
                continue

            uf.union(left, right)

    # ---- Assemble ----------------------------------------------------------
    components: dict[int, list[int]] = {}
    for index in range(len(rows)):
        components.setdefault(uf.find(index), []).append(index)

    chains = [_assemble([rows[i] for i in members], mine) for members in components.values()]
    # Newest first, undated last. `is not None` rather than `is None` because the
    # whole key is reversed: under `reverse=True` a leading True sorts first, so
    # the dated chains have to be the ones carrying True.
    chains.sort(
        key=lambda c: (c["last_message_at"] is not None, c["last_message_at"] or ""),
        reverse=True,
    )
    return chains


def _assemble(members: list[Mapping[str, Any]], mine: set[str]) -> dict:
    messages = sorted(members, key=_sort_key)
    first, last = messages[0], messages[-1]

    participants: list[str] = []
    for message in messages:
        for address in sorted(chain_addresses(message) - mine):
            if address not in participants:
                participants.append(address)
                if len(participants) >= MAX_CHAIN_PARTICIPANTS:
                    break

    # None, not a guess, when the newest message's direction is unknown. Every
    # surface downstream renders "unknown" rather than picking a side -- a NULL
    # read as inbound is what tells the summarizer someone else asked a question
    # the user asked themselves.
    direction = last.get("direction")
    last_from = {"outbound": "you", "inbound": "them"}.get(direction or "")
    awaiting = {"outbound": "them", "inbound": "you"}.get(direction or "")

    return {
        # Stable under additions that do not merge, and changing exactly when two
        # chains genuinely merge. Deliberately not a positional index or a
        # content hash: this key is a React key and an LLM payload field, so a
        # spurious change remounts a card and invalidates a cached suggestion.
        "key": min(str(m.get("message_id") or "") for m in messages),
        # The *earliest* subject, so the chain is named by how it started rather
        # than by whatever "Re: Re: Fwd:" the newest reply happens to carry.
        "subject": first.get("subject"),
        "participants": participants,
        "message_count": len(messages),
        "first_message_at": _stamp(first),
        "last_message_at": _stamp(last),
        "last_message_from": last_from,
        "awaiting": awaiting,
        "unread_count": sum(1 for m in messages if m.get("unread")),
        "messages": [dict(m) for m in messages],
    }
