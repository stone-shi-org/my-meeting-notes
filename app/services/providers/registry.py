"""What providers exist and what each one can do.

Deliberately small. It carries identity and capability -- enough to drive the
"Add integration" picker and to validate that nobody enables email on a
calendar-only provider -- and stops short of describing form fields. A declarative
field schema would need a mini-language maintained on both the server and the SPA,
which is more code than the handful of provider-specific cards it would replace.

Adding a provider: write the module, add one entry here.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.errors import NotFoundError
from app.services.providers import google, mcp

CALENDAR = "calendar"
EMAIL = "email"


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    kinds: frozenset[str]
    auth_type: str  # oauth2 | password | token
    factory: type
    docs_url: str = ""
    # Only the email-triage MCP server returns triage_level/tag/reason/score.
    # Everything else leaves them None rather than inventing values.
    supplies_triage: bool = False

    def supports(self, kind: str) -> bool:
        return kind in self.kinds


REGISTRY: dict[str, ProviderSpec] = {
    "google": ProviderSpec(
        id="google",
        label="Google",
        kinds=frozenset({CALENDAR, EMAIL}),
        auth_type="oauth2",
        factory=google.GoogleProvider,
        docs_url="https://console.cloud.google.com/apis/credentials",
    ),
    "mcp_calendar": ProviderSpec(
        id="mcp_calendar",
        label="Calendar MCP server",
        kinds=frozenset({CALENDAR}),
        auth_type="token",
        factory=mcp.McpCalendarProvider,
    ),
    "mcp_email": ProviderSpec(
        id="mcp_email",
        label="Email MCP server",
        kinds=frozenset({EMAIL}),
        auth_type="token",
        factory=mcp.McpEmailProvider,
        supplies_triage=True,
    ),
}


def spec(provider_id: str) -> ProviderSpec:
    try:
        return REGISTRY[provider_id]
    except KeyError:
        raise NotFoundError(f"Unknown integration provider {provider_id!r}") from None


def all_specs() -> list[ProviderSpec]:
    return sorted(REGISTRY.values(), key=lambda s: s.label)


def supported_kinds(provider_id: str) -> frozenset[str]:
    return spec(provider_id).kinds
