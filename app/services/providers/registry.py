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
from app.services.providers import apple, dev, google, mcp, zoho

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
    "apple": ProviderSpec(
        id="apple",
        label="Apple iCloud",
        kinds=frozenset({CALENDAR, EMAIL}),
        # Apple offers no OAuth for iCloud; an app-specific password is the only
        # supported route, not a shortcut we chose.
        auth_type="password",
        factory=apple.AppleProvider,
        docs_url="https://appleid.apple.com/account/manage",
    ),
    "zoho": ProviderSpec(
        id="zoho",
        label="Zoho",
        kinds=frozenset({CALENDAR, EMAIL}),
        auth_type="oauth2",
        factory=zoho.ZohoProvider,
        docs_url="https://api-console.zoho.com/",
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
    # Hand-authored fake data. Registered unconditionally so an existing row
    # still resolves to a spec when the flag is off -- `all_specs` is what hides
    # it from the picker, and `loader.build_provider` is what makes it inert.
    "dev": ProviderSpec(
        id="dev",
        label="Development (fake data)",
        kinds=frozenset({CALENDAR, EMAIL}),
        # Nothing to authenticate against: the "account" is a name for a set of
        # rows in this app's own database.
        auth_type="none",
        factory=dev.DevProvider,
    ),
}


def spec(provider_id: str) -> ProviderSpec:
    try:
        return REGISTRY[provider_id]
    except KeyError:
        raise NotFoundError(f"Unknown integration provider {provider_id!r}") from None


def all_specs() -> list[ProviderSpec]:
    """What the "Add integration" picker offers.

    The Development provider is omitted unless this build enables it, which is
    how the flag hides it from the UI without the SPA knowing the flag exists.
    """
    visible = [
        s for s in REGISTRY.values() if s.id != dev.PROVIDER_ID or dev.enabled()
    ]
    return sorted(visible, key=lambda s: s.label)


def supported_kinds(provider_id: str) -> frozenset[str]:
    return spec(provider_id).kinds
