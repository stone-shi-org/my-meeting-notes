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

# Only these are unpacked from an address local part. "jsmith" is not improved by
# becoming "Jsmith", but "jane.doe" genuinely is "Jane Doe".
_LOCAL_PART_SEPARATORS = re.compile(r"[._-]+")


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

    def to_dict(self) -> dict:
        return asdict(self)


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
