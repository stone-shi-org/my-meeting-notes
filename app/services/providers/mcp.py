"""The existing MCP servers, as providers.

This is an adapter, not a rewrite: transport, the FastMCP content-block quirk and
the ExceptionGroup unwrapping all still live in ``app.services.mcpclient``. What
changes is only how the app decides *which* server and *whose* account to search --
that now comes from a per-user ``integrations`` row instead of shared
``mcp_servers`` config plus a per-user override table.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from app.logging_config import get_logger
from app.services import mcpclient as mcp_svc
from app.services.providers.base import (
    BaseProvider,
    Check,
    EmailCandidate,
    EventCandidate,
    test_result,
)
from app.services.providers.query import build_gmail_query, iso_date

log = get_logger("providers.mcp")


class _McpBase(BaseProvider):
    """Shared config translation for both MCP-backed providers."""

    kind: str = ""
    default_tool: str = ""

    def _config(self) -> mcp_svc.MCPServerConfig:
        cfg = self.config
        return mcp_svc.MCPServerConfig(
            name=self.provider_id,
            kind=self.kind,
            transport=cfg.get("transport") or "sse",
            tool_name=cfg.get("tool_name") or self.default_tool,
            enabled=True,
            base_url=cfg.get("base_url"),
            auth_token=self.secret.get("auth_token"),
            command=cfg.get("command"),
            args=cfg.get("args") or [],
            cwd=cfg.get("cwd"),
            env=cfg.get("env") or {},
            default_profile=cfg.get("profile"),
            timeout_sec=int(cfg.get("timeout_sec") or 60),
        )

    def _client(self) -> mcp_svc.MCPClient:
        return mcp_svc.MCPClient(self._config())

    async def test(self) -> dict:
        started = time.monotonic()
        result = await self._client().test()
        latency = int((time.monotonic() - started) * 1000)
        checks = [
            Check(name="handshake", ok=bool(result.get("ok")), error=result.get("error")),
        ]
        return test_result(checks, result.get("latency_ms") or latency)


class McpCalendarProvider(_McpBase):
    provider_id = "mcp_calendar"
    kind = "calendar"
    default_tool = "search_events"

    async def search_events(
        self, *, query: str | None, start: datetime, end: datetime
    ) -> list[EventCandidate]:
        client = self._client()
        # Two passes, unlike every other provider: a keyword search plus a bare
        # window sweep. The MCP tool ANDs a single query string, so an event whose
        # title shares no keyword is invisible to the keyworded call. Providers
        # with a real date-range API just sweep the window once and are complete
        # by construction.
        keyworded, windowed = await asyncio.gather(
            client.search_events(
                query=query or None,
                start_date=iso_date(start),
                end_date=iso_date(end),
            ),
            client.search_events(start_date=iso_date(start), end_date=iso_date(end)),
            return_exceptions=True,
        )

        merged: dict[str, dict] = {}
        for batch in (keyworded, windowed):
            if isinstance(batch, BaseException):
                continue
            for item in batch:
                uid = item.get("uid")
                if uid:
                    merged.setdefault(uid, item)

        # Only surface the failure if it cost us everything; a dead keyword pass
        # with a good window sweep is not worth reporting as an error.
        if not merged and isinstance(keyworded, BaseException):
            raise keyworded

        return [self._to_event(item) for item in merged.values()]

    def _to_event(self, item: dict) -> EventCandidate:
        native = item.get("uid") or ""
        return EventCandidate(
            # Verbatim, NOT namespaced through uid_for(). Every event attached
            # before the migration is stored under the bare MCP uid, so
            # namespacing here would make each one fail the already-attached
            # check in gather_candidates and re-attach as a duplicate row.
            uid=native,
            source_uid=native,
            summary=item.get("summary"),
            description=item.get("description"),
            location=item.get("location"),
            start=item.get("start"),
            end=item.get("end"),
            calendar_name=item.get("calendar_name"),
            account=item.get("account"),
            type=item.get("type"),
            url=item.get("url"),
            provider=self.provider_id,
            integration_id=self.ref.id,
        )


class McpEmailProvider(_McpBase):
    provider_id = "mcp_email"
    kind = "email"
    default_tool = "search_emails"

    async def search_emails(
        self, *, keywords: list[str], start: datetime, end: datetime
    ) -> list[EmailCandidate]:
        query = build_gmail_query(keywords, start, end)
        results = await self._client().search_emails(query)
        return [self._to_email(item) for item in results]

    def last_query(self, keywords: list[str], start: datetime, end: datetime) -> str:
        """Exposed so the match run can record the exact query that was sent."""
        return build_gmail_query(keywords, start, end)

    def _to_email(self, item: dict) -> EmailCandidate:
        native = item.get("message_id") or ""
        return EmailCandidate(
            # Verbatim for the same reason as the calendar uid above.
            message_id=native,
            rfc_message_id=native,
            id=item.get("id"),
            sender=item.get("sender"),
            subject=item.get("subject"),
            date=item.get("date"),
            snippet=item.get("snippet"),
            account=item.get("account"),
            url=item.get("url"),
            triage_level=item.get("triage_level"),
            tag=item.get("tag"),
            reason=item.get("reason"),
            summary=item.get("summary"),
            score=item.get("score"),
            provider=self.provider_id,
            integration_id=self.ref.id,
        )
