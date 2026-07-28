"""Minimal CalDAV over httpx.

Deliberately hand-rolled rather than using the ``caldav`` package: that library is
built on ``requests``, which respx cannot intercept, and pulling it in would make
the whole suite network-dependent. The three requests we need are fixed XML
bodies, and respx routes arbitrary methods, so PROPFIND and REPORT are fakeable.

Recurrence is expanded **locally**. iCloud's support for the ``<C:expand>``
element is erratic -- the same request has been reported returning expanded
instances sometimes and the bare master other times -- and a non-deterministic
server feature is worse than none. What iCloud does do correctly is RFC 4791
§9.9: a ``time-range`` filter is evaluated against the *expanded* recurrence set,
so a weekly meeting whose master DTSTART is a year before the window is still
returned. It just arrives unexpanded, with any RECURRENCE-ID overrides alongside.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import httpx

from app.errors import IntegrationAuthError, MCPError
from app.logging_config import get_logger

log = get_logger("providers.caldav")

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
NS = {"d": DAV, "c": CALDAV}

# An unbounded FREQ=MINUTELY rule is a real thing people have in calendars, and
# expanding one would spin a worker until the request times out.
MAX_INSTANCES = 200

PROPFIND_PRINCIPAL = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>'
)

PROPFIND_HOME = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><c:calendar-home-set/></d:prop></d:propfind>"
)

PROPFIND_CALENDARS = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><d:resourcetype/><d:displayname/>"
    "<c:supported-calendar-component-set/></d:prop></d:propfind>"
)


def _caldav_time(value: datetime) -> str:
    """CalDAV time-range wants a UTC basic-format stamp."""
    return value.strftime("%Y%m%dT%H%M%SZ")


def calendar_query(start: datetime, end: datetime) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
        '<c:filter><c:comp-filter name="VCALENDAR">'
        '<c:comp-filter name="VEVENT">'
        f'<c:time-range start="{_caldav_time(start)}" end="{_caldav_time(end)}"/>'
        "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"
    )


def _parse(body: str) -> ET.Element:
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        raise MCPError(f"CalDAV returned unparseable XML: {exc}") from exc


async def _request(http: httpx.AsyncClient, method: str, url: str, body: str, depth: str) -> str:
    response = await http.request(
        method,
        url,
        content=body.encode(),
        headers={"Content-Type": 'application/xml; charset="utf-8"', "Depth": depth},
    )
    if response.status_code in (401, 403):
        raise IntegrationAuthError(
            "iCloud rejected the Apple ID or app-specific password. "
            "Note it must be an app-specific password, not your account password."
        )
    if response.status_code >= 400:
        raise MCPError(f"CalDAV {method} failed: HTTP {response.status_code}")
    return response.text


async def discover_calendar_home(http: httpx.AsyncClient, base_url: str) -> str:
    """Two hops to the shard that actually holds the user's calendars.

    iCloud answers the well-known endpoint with a principal path, and the
    principal with a calendar-home-set on a *different* host (p34-caldav…), which
    is where every subsequent request has to go.
    """
    body = await _request(http, "PROPFIND", base_url, PROPFIND_PRINCIPAL, "0")
    node = _parse(body).find(".//d:current-user-principal/d:href", NS)
    if node is None or not node.text:
        raise MCPError("iCloud did not return a principal for these credentials")
    principal_url = urljoin(base_url, node.text.strip())

    body = await _request(http, "PROPFIND", principal_url, PROPFIND_HOME, "0")
    home = _parse(body).find(".//c:calendar-home-set/d:href", NS)
    if home is None or not home.text:
        raise MCPError("iCloud did not return a calendar home for this principal")
    return urljoin(principal_url, home.text.strip())


async def list_calendars(http: httpx.AsyncClient, home_url: str) -> list[dict]:
    body = await _request(http, "PROPFIND", home_url, PROPFIND_CALENDARS, "1")
    out = []
    for response in _parse(body).findall("d:response", NS):
        href = response.find("d:href", NS)
        if href is None or not href.text:
            continue
        # Only collections that are calendars *and* hold events. Skipping the
        # component check would drag in reminder and contact collections.
        if response.find(".//d:resourcetype/c:calendar", NS) is None:
            continue
        comps = {
            c.get("name")
            for c in response.findall(".//c:supported-calendar-component-set/c:comp", NS)
        }
        if comps and "VEVENT" not in comps:
            continue
        name = response.find(".//d:displayname", NS)
        out.append(
            {
                "url": urljoin(home_url, href.text.strip()),
                "name": (name.text if name is not None else None) or "Calendar",
            }
        )
    return out


async def fetch_calendar_data(
    http: httpx.AsyncClient, calendar_url: str, start: datetime, end: datetime
) -> list[str]:
    """Raw iCalendar documents for everything overlapping the window."""
    body = await _request(http, "REPORT", calendar_url, calendar_query(start, end), "1")
    return [
        node.text
        for node in _parse(body).findall(".//c:calendar-data", NS)
        if node.text and node.text.strip()
    ]


def expand(ics_documents: list[str], start: datetime, end: datetime) -> list[dict]:
    """Turn raw iCalendar text into concrete instances inside the window.

    ``recurring_ical_events`` does the RRULE/RDATE/EXDATE/RECURRENCE-ID work,
    including expanding in the event's own TZID -- expanding in UTC instead
    silently drifts every instance by an hour after a DST transition.
    """
    import icalendar
    import recurring_ical_events

    out: list[dict] = []
    for document in ics_documents:
        try:
            calendar = icalendar.Calendar.from_ical(document)
        except Exception as exc:  # noqa: BLE001 - one bad resource must not lose the rest
            log.warning("skipping unparseable calendar resource: %s", exc)
            continue

        try:
            instances = recurring_ical_events.of(calendar).between(start, end)
        except Exception as exc:  # noqa: BLE001
            log.warning("skipping calendar resource that failed to expand: %s", exc)
            continue

        for event in instances[:MAX_INSTANCES]:
            out.append(_to_dict(event))
        if len(instances) > MAX_INSTANCES:
            log.warning(
                "capped a recurrence at %d instances (had %d)", MAX_INSTANCES, len(instances)
            )
    return out


def _value(event, key: str) -> str | None:
    raw = event.get(key)
    if raw is None:
        return None
    return str(raw)


def _stamp(event, key: str) -> str | None:
    """ISO-8601, or a bare date for all-day events.

    ``VALUE=DATE`` events give a ``date``, not a ``datetime``. Keeping it as a
    plain date is deliberate: coercing it to midnight in some timezone is how an
    all-day event ends up displayed on the wrong day.
    """
    node = event.get(key)
    if node is None:
        return None
    value = getattr(node, "dt", None)
    if value is None:
        return None
    return value.isoformat()


def _to_dict(event) -> dict:
    return {
        "uid": _value(event, "UID"),
        "summary": _value(event, "SUMMARY"),
        "description": _value(event, "DESCRIPTION"),
        "location": _value(event, "LOCATION"),
        "start": _stamp(event, "DTSTART"),
        "end": _stamp(event, "DTEND"),
        # Distinguishes one occurrence of a series from another; the app-level
        # uid is built from it.
        "recurrence_id": _stamp(event, "RECURRENCE-ID"),
    }


async def collect(
    http: httpx.AsyncClient, base_url: str, start: datetime, end: datetime
) -> list[tuple[str, dict]]:
    """Discovery through expansion. Returns (calendar_name, event) pairs."""
    home = await discover_calendar_home(http, base_url)
    calendars = await list_calendars(http, home)

    async def one(calendar: dict) -> list[tuple[str, dict]]:
        documents = await fetch_calendar_data(http, calendar["url"], start, end)
        return [(calendar["name"], e) for e in expand(documents, start, end)]

    batches = await asyncio.gather(*(one(c) for c in calendars), return_exceptions=True)

    events: list[tuple[str, dict]] = []
    for calendar, batch in zip(calendars, batches):
        if isinstance(batch, BaseException):
            log.warning("calendar %s failed: %s", calendar["name"], batch)
            continue
        events.extend(batch)
    return events
