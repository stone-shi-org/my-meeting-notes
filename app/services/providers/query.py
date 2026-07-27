"""Native query builders shared between providers.

The two date formats are the classic trap here and they are deliberately separate
functions with separate tests: calendar search wants ISO-8601 (``2026-03-11``)
while Gmail search wants slashes (``after:2026/03/11``). Cross-wiring them returns
nothing, silently.

Gmail syntax lives here rather than in ``google.py`` because the email-triage MCP
server speaks it too.
"""

from __future__ import annotations

from datetime import datetime


def iso_date(value: datetime) -> str:
    """Calendar search wants ISO-8601: 2026-03-11."""
    return value.strftime("%Y-%m-%d")


def gmail_date(value: datetime) -> str:
    """Gmail search wants slashes: after:2026/03/11. Not the same as ISO."""
    return value.strftime("%Y/%m/%d")


def build_gmail_query(keywords: list[str], start: datetime, end: datetime) -> str:
    parts = []
    if keywords:
        top = keywords[:3]
        parts.append(f"({' OR '.join(top)})" if len(top) > 1 else top[0])
    parts.append(f"after:{gmail_date(start)}")
    parts.append(f"before:{gmail_date(end)}")
    return " ".join(parts)


def imap_date(value: datetime) -> str:
    """IMAP SEARCH wants dd-Mon-yyyy: SINCE 11-Mar-2026."""
    return value.strftime("%d-%b-%Y")


def zoho_range(start: datetime, end: datetime) -> dict[str, str]:
    """Zoho Calendar wants yyyyMMdd'T'HHmmss'Z' inside a mandatory range object."""
    return {
        "start": start.strftime("%Y%m%dT%H%M%SZ"),
        "end": end.strftime("%Y%m%dT%H%M%SZ"),
    }
