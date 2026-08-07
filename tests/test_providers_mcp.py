"""The MCP-backed providers: a translation layer over MCPClient.

Faked at MCPClient, same seam test_providers_loader.py uses -- these tests
describe what McpEmailProvider does with a tool result, not the wire protocol
underneath it (that's test_mcpclient.py's job).
"""

from __future__ import annotations

import pytest

from app.services.providers.base import IntegrationRef
from app.services.providers.mcp import McpEmailProvider


@pytest.fixture
def provider():
    ref = IntegrationRef(
        id=9, provider="mcp_email", account_label="Fixtures",
        calendar_enabled=False, email_enabled=True,
    )
    return McpEmailProvider(ref, {}, {})


class TestGetEmailBody:
    async def test_it_returns_the_body_field(self, provider, monkeypatch):
        class FakeClient:
            async def fetch_full_email(self, native_id, **kwargs):
                assert native_id == "msg-1"
                return {"sender": "a@b", "body": "The full text."}

        monkeypatch.setattr(provider, "_client", lambda: FakeClient())
        body = await provider.get_email_body(native_id="msg-1")
        assert body == "The full text."

    async def test_no_result_is_none(self, provider, monkeypatch):
        class FakeClient:
            async def fetch_full_email(self, native_id, **kwargs):
                return None

        monkeypatch.setattr(provider, "_client", lambda: FakeClient())
        assert await provider.get_email_body(native_id="msg-1") is None

    async def test_folder_id_is_accepted_but_unused(self, provider, monkeypatch):
        """The server auto-detects Gmail vs IMAP from its own cache -- unlike
        Zoho, there is no folder concept for this tool to need."""
        class FakeClient:
            async def fetch_full_email(self, native_id, **kwargs):
                return {"body": "x"}

        monkeypatch.setattr(provider, "_client", lambda: FakeClient())
        assert await provider.get_email_body(native_id="msg-1", folder_id="ignored") == "x"
