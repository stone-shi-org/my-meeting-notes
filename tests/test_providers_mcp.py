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


class TestMcpThreading:
    def test_no_threading_field_is_ever_synthesised(self, provider):
        """The tempting shortcut is a conversation id built from the bare native
        id -- MCP already emits `message_id`/`rfc_message_id` verbatim, so it
        looks consistent. It is not: those two are verbatim for back-compat with
        rows attached before namespacing existed, and there is no such hazard
        here because nothing was ever stored. Inventing one would fabricate an
        *authoritative-tier* chain link out of nothing, the same reason the
        triage fields are never synthesised.
        """
        mail = provider._to_email(
            {
                "message_id": "msg-1",
                "subject": "Re: cutover",
                "sender": "priya@acme.com",
                "date": "2026-03-17T17:42:00+00:00",
            }
        )

        assert mail.conversation_id is None
        assert mail.in_reply_to is None
        assert mail.references == ()
        assert mail.to_recipients is None
        assert mail.cc_recipients is None
        assert mail.direction is None
        # And the two that ARE verbatim stay verbatim.
        assert mail.message_id == "msg-1"
        assert mail.rfc_message_id == "msg-1"

    def test_the_integration_id_is_still_carried(self, provider):
        """It always was on the candidate; `attach_email` was dropping it, which
        is why every MCP row was unfetchable -- `_resolve_email_ref` recovered
        the id by parsing the composite `message_id` that MCP does not use."""
        mail = provider._to_email({"message_id": "msg-1", "subject": "S"})
        assert mail.integration_id == 9
