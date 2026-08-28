"""The provider contract: structured intent in, normalized candidates out.

Providers receive keywords and a date window and build their *own* native query.
That is the point of the abstraction: Gmail wants ``after:2026/03/11`` search
syntax, IMAP wants ``SINCE 11-Mar-2026`` criteria, Zoho wants a ``searchKey`` and
a mandatory JSON range, and CalDAV has no text search at all. Handing every
provider one pre-built Gmail query string -- which is what the MCP-only design
did -- cannot work.

Results come back as frozen dataclasses, not free-form dicts. `save_match_run`
stores candidates in ``candidates_json`` *and* ``ranked_json``, and
``attach_selected`` writes ``raw_json`` a third time, so letting a raw Gmail
payload through would persist hundreds of KB three times per run. Projecting
through a fixed field set also guarantees the columns ``matching.attached_context``
selects -- and therefore what the summarizer sees -- are always populated.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from email.utils import parseaddr
from typing import Iterable, Protocol, runtime_checkable

# Attendee lists are prefilled as speaker names when a meeting is created from an
# event, so they are bounded: a 300-person all-hands invite is not a speaker list,
# and the whole list would otherwise be persisted three times per match run.
MAX_ATTENDEES = 24

# Bounded for the same reason MAX_ATTENDEES is: a mailing-list `References`
# header carries dozens of ids and the whole candidate is persisted three times
# per match run. The *tail* is kept rather than the head -- chaining only ever
# needs the nearest ancestors, and the oldest ids in a long thread are the ones
# no attached message will ever cite.
MAX_REFERENCES = 20

# Only these are unpacked from an address local part. "jsmith" is not improved by
# becoming "Jsmith", but "jane.doe" genuinely is "Jane Doe".
_LOCAL_PART_SEPARATORS = re.compile(r"[._-]+")

# `parseaddr` returns ('', '') on some malformed headers rather than raising, so
# a bare "<a@b.com>" or a header with a stray quote falls through to this.
_ANGLE_ADDRESS = re.compile(r"<([^<>@\s]+@[^<>@\s]+)>")
_BARE_ADDRESS = re.compile(r"([^<>@\s,;]+@[^<>@\s,;]+)")


def make_uid(provider: str, integration_id: int, native_instance_key: str) -> str:
    """Build the app-owned, instance-scoped event id.

    Provider ids are not safe to use directly. Google with ``singleEvents=true``
    returns the *same* ``iCalUID`` for every occurrence of a recurring event, and
    CalDAV shares one ``UID`` across a recurrence set -- so keying on the
    provider's id collapses a weekly standup into a single candidate. Namespacing
    by integration additionally keeps two connected accounts distinct when both
    can see the same event.
    """
    return f"{provider}:{integration_id}:{native_instance_key}"


def attendee_label(display_name: str | None, email: str | None) -> str | None:
    """A human name for one attendee, or None if there is nothing usable.

    Prefers whatever the server calls them. Falling back to the address, a
    ``first.last@`` local part is unpacked into "First Last" -- these become
    prefilled speaker names, and a bare mailbox reads as a bug in a transcript
    header.
    """
    name = (display_name or "").strip()
    if name:
        return name

    address = (email or "").strip()
    if not address:
        return None

    local = address.split("@", 1)[0]
    if not _LOCAL_PART_SEPARATORS.search(local):
        return address
    return _LOCAL_PART_SEPARATORS.sub(" ", local).strip().title() or address


def normalize_address(value: str | None) -> str | None:
    """The bare mailbox out of a From/To header, casefolded, or None.

    Deliberately *not* ``attendee_label``, which returns a display name:
    comparing "Priya Raman" against a connected account tells you nothing, and
    that is exactly the comparison ``direction`` depends on.

    ``+tag`` suffixes are kept. ``sales@`` and ``sales+eu@`` are different
    mailboxes, and collapsing them would over-merge conversations whose only
    shared participant is a tagged alias.
    """
    text = (value or "").strip()
    if not text:
        return None

    _, address = parseaddr(text)
    address = address.strip()
    if "@" not in address:
        match = _ANGLE_ADDRESS.search(text) or _BARE_ADDRESS.search(text)
        address = match.group(1) if match else ""

    address = address.strip().strip("<>").casefold()
    return address or None


def truncate_references(raw: object) -> tuple[str, ...]:
    """The newest ``MAX_REFERENCES`` message-ids out of a References header.

    Accepts the raw header text or an already-split sequence, because a
    candidate that has been through a ``raw_json`` round trip arrives as a list
    while a provider hands over a header string.
    """
    if raw is None:
        return ()

    if isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        items = re.findall(r"<[^<>]+>", str(raw)) or str(raw).split()

    out = [item.strip() for item in items if item and item.strip()]
    return tuple(out[-MAX_REFERENCES:])


def clean_attendees(labels: Iterable[str | None]) -> tuple[str, ...]:
    """Normalise a provider's attendee labels: no blanks, no repeats, bounded.

    Case-insensitive dedup, because the organizer usually appears in the
    attendee list too and the two spellings rarely match exactly.
    """
    out: list[str] = []
    seen: set[str] = set()
    for label in labels:
        name = (label or "").strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= MAX_ATTENDEES:
            break
    return tuple(out)


def coerce_attendees(raw: object) -> tuple[str, ...]:
    """Best-effort attendees out of a payload whose shape we do not control.

    Zoho and the MCP servers both return free-form JSON here, spelled
    differently, and neither documents the shape as stable -- so an unexpected
    value drops that one attendee rather than failing the whole search. Google
    and CalDAV have documented shapes and map their fields explicitly instead.
    """
    if raw is None:
        return ()

    items = raw if isinstance(raw, (list, tuple)) else [raw]
    labels: list[str | None] = []

    for item in items:
        if isinstance(item, str):
            # Covers "Jane Doe", "jane@x.com" and "Jane Doe <jane@x.com>" alike.
            realname, address = parseaddr(item)
            labels.append(attendee_label(realname, address) or item.strip())
        elif isinstance(item, dict):
            name = next(
                (item[k] for k in ("displayName", "display_name", "dname", "name", "cn")
                 if isinstance(item.get(k), str)),
                None,
            )
            address = next(
                (item[k] for k in ("email", "attendee", "address", "mail")
                 if isinstance(item.get(k), str)),
                None,
            )
            labels.append(attendee_label(name, address))

    return clean_attendees(labels)


@dataclass(frozen=True, slots=True)
class EventCandidate:
    """A calendar event, in the shape the matching pipeline and DB expect."""

    uid: str
    summary: str | None = None
    description: str | None = None
    location: str | None = None
    start: str | None = None
    end: str | None = None
    # Display names, organizer first. Prefilled as speaker names when a meeting
    # is created from the event; never an address list to send anything to.
    attendees: tuple[str, ...] = ()
    calendar_name: str | None = None
    account: str | None = None
    type: str | None = None
    url: str | None = None
    # The provider's own recurrence-set identity (Google iCalUID, CalDAV UID).
    # Dedup across providers keys on (source_uid, start), never on `uid`.
    source_uid: str | None = None
    provider: str | None = None
    integration_id: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EmailCandidate:
    """An email. ``id`` is the provider-native id; it lands in ``thread_emails.mcp_id``.

    ``triage_level``/``tag``/``reason``/``summary``/``score`` come from the
    email-triage MCP server and stay ``None`` everywhere else -- they are not
    synthesised, because a made-up triage level reads as real data.
    """

    message_id: str
    id: str | None = None
    sender: str | None = None
    subject: str | None = None
    date: str | None = None
    snippet: str | None = None
    account: str | None = None
    url: str | None = None
    # The real RFC 2822 Message-ID, kept apart from the composite `message_id`
    # so cross-provider dedup and the Gmail rfc822msgid fallback link still work.
    rfc_message_id: str | None = None
    triage_level: int | None = None
    tag: str | None = None
    reason: str | None = None
    summary: str | None = None
    score: float | None = None
    provider: str | None = None
    integration_id: int | None = None
    # Only Zoho's content endpoint needs this; every other provider leaves it
    # None. Snapshotted through to `thread_emails.folder_id` on attach so a
    # later full-body fetch can still reach it without re-searching.
    folder_id: str | None = None
    # The provider's own conversation identity, namespaced through `uid_for` --
    # Gmail's threadId is unique per *mailbox*, so two connected accounts that
    # both hold one conversation would otherwise collapse into a single chain.
    # NULL where the provider has no such concept; never synthesised, the same
    # rule as the triage fields above.
    conversation_id: str | None = None
    # RFC 2822 threading headers -- the provider-independent reply graph, and the
    # only tier of chaining that works across two accounts or two providers.
    # `references` is bounded to the newest MAX_REFERENCES ids because this
    # dataclass is persisted three times per match run.
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    # Verbatim header text, not a parsed list, so a shape we did not anticipate
    # loses nothing. `sender` describes an inbound message and says nothing at
    # all about an outbound one, which is why these are needed to know who a
    # conversation is actually *with*.
    to_recipients: str | None = None
    cc_recipients: str | None = None
    # 'outbound' | 'inbound' | None. Spelled identically to
    # `thread_emails.direction` so `attach_email` copies it verbatim rather than
    # translating -- a boolean here and a string there is how the two drift. Three
    # states, not two: Gmail's SENT label is authoritative, an address comparison
    # is a good guess, and MCP and dev offer neither.
    direction: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# Deliberately no `body` field on EmailCandidate. Two reasons, and the second is
# the one that matters: candidates are persisted three times per match run (see
# the module docstring) and nothing prunes `match_runs`; and *no* provider has a
# body at search time anyway -- Gmail searches with `format=metadata`, IMAP
# fetches header fields only, Zoho's search payload carries no content and the
# email MCP server has no body in its results. A body only ever exists after a
# second per-message request, which is what `get_email_message` below is for, so
# the field would be permanently None on every path.
@dataclass(frozen=True, slots=True)
class FetchedEmail:
    """One email's body, plus whatever headers the fetch happened to return.

    A separate type from ``EmailCandidate`` because it answers a different
    question: a candidate is "something a search found", this is "the message
    itself". The header fields are all optional because only some providers can
    supply them -- Gmail's ``format=full`` and an IMAP ``FETCH BODY.PEEK[]`` both
    return the whole message, so hydration can backfill threading columns for
    free, while Zoho's content endpoint and the MCP adapter return content alone.
    """

    body: str | None = None
    conversation_id: str | None = None
    rfc_message_id: str | None = None
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    to_recipients: str | None = None
    cc_recipients: str | None = None
    sender: str | None = None
    direction: str | None = None

    def header_updates(self) -> dict:
        """Only the header fields that are actually populated.

        Hydration writes these with COALESCE semantics, so an absent field must
        not appear here at all -- a provider that cannot supply headers must
        never blank ones a search already stored.
        """
        out: dict = {}
        for name in (
            "conversation_id",
            "rfc_message_id",
            "in_reply_to",
            "to_recipients",
            "cc_recipients",
            "direction",
        ):
            value = getattr(self, name)
            if value:
                out[name] = value
        if self.references:
            out["references"] = tuple(self.references)
        return out


@dataclass(frozen=True, slots=True)
class IntegrationRef:
    """Identity of a connected account, as providers and error messages need it."""

    id: int
    provider: str
    account_label: str = ""
    calendar_enabled: bool = False
    email_enabled: bool = False

    @property
    def display(self) -> str:
        """What to call this account in a user-facing error."""
        return self.account_label or self.provider


@dataclass
class Check:
    """One leg of a connection test.

    Apple's most likely failure is CalDAV succeeding while IMAP login is
    rejected, so a single ok/not-ok flag cannot describe the result.
    """

    name: str
    ok: bool
    error: str | None = None


@runtime_checkable
class Provider(Protocol):
    """What ``loader`` returns and ``matching`` consumes."""

    ref: IntegrationRef

    async def search_events(
        self, *, query: str | None, start: datetime, end: datetime
    ) -> list[EventCandidate]: ...

    async def search_emails(
        self, *, keywords: list[str], start: datetime, end: datetime
    ) -> list[EmailCandidate]: ...

    async def test(self) -> dict: ...


class BaseProvider:
    """Shared plumbing. Subclasses override whichever capabilities they support."""

    provider_id: str = ""

    def __init__(self, ref: IntegrationRef, config: dict, secret: dict):
        self.ref = ref
        self.config = config or {}
        self.secret = secret or {}

    @property
    def integration_id(self) -> int:
        return self.ref.id

    def uid_for(self, native_instance_key: str) -> str:
        return make_uid(self.provider_id, self.ref.id, native_instance_key)

    async def search_events(
        self, *, query: str | None, start: datetime, end: datetime
    ) -> list[EventCandidate]:
        raise NotImplementedError(f"{self.provider_id} does not support calendar search")

    async def search_emails(
        self, *, keywords: list[str], start: datetime, end: datetime
    ) -> list[EmailCandidate]:
        raise NotImplementedError(f"{self.provider_id} does not support email search")

    async def get_email_body(
        self, *, native_id: str, folder_id: str | None = None
    ) -> str | None:
        """Full verbatim text of one email, or None if this provider can't fetch it.

        Unlike search_events/search_emails, this is genuinely optional rather than
        a "must override" contract -- the email MCP server exposes no fetch-by-id
        tool at all, so None (not an exception) is the correct default: the caller
        already has a snippet to fall back to.
        """
        return None

    async def get_email_message(
        self, *, native_id: str, folder_id: str | None = None
    ) -> FetchedEmail | None:
        """One email's body *and* whatever headers came back with it.

        A sibling of ``get_email_body`` rather than a widening of it, because
        only Gmail and IMAP can honour the header half: Gmail's ``format=full``
        and ``FETCH BODY.PEEK[]`` both return the whole message, while Zoho's
        content endpoint and the MCP adapter return content alone. Widening
        ``get_email_body``'s ``-> str | None`` contract in place would make it a
        promise three of the five providers cannot keep.

        The default delegates, so a provider that only implements the older
        method still hydrates a body -- it simply backfills no threading. That
        asymmetry is deliberate and visible: it is why hydration is the
        threading backfill for Gmail and Apple only.
        """
        body = await self.get_email_body(native_id=native_id, folder_id=folder_id)
        if body is None:
            return None
        return FetchedEmail(body=body)

    async def test(self) -> dict:
        raise NotImplementedError


def test_result(checks: list[Check], latency_ms: int) -> dict:
    """Assemble the connection-test response every provider returns."""
    failed = [c for c in checks if not c.ok]
    return {
        "ok": not failed,
        "latency_ms": latency_ms,
        "checks": [asdict(c) for c in checks],
        # A single summary line for the banner; `checks` has the detail.
        "error": "; ".join(f"{c.name}: {c.error}" for c in failed) or None,
    }
