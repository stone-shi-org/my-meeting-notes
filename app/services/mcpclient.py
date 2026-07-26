"""MCP client for the calendar and email servers.

One class, two transports. SSE is the deployed path for both servers; stdio is
kept for host-side debugging and cannot work inside the container (calendarmcp's
venv pins the host's /usr/bin/python3.14).

Sessions are per-call rather than pooled: matching runs a couple of times per
meeting, the handshake is ~200 ms against a diarization that takes minutes, and
a Settings edit then takes effect immediately with no cache to invalidate.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from app.db import utcnow
from app.errors import MCPError, MCPTimeoutError, NotFoundError
from app.logging_config import get_logger

log = get_logger("mcp")

CONNECT_TIMEOUT = 10.0
# Headroom over the per-call timeout so a wedged stdio child can't hang a worker.
OUTER_TIMEOUT_MARGIN = 10.0


@dataclass
class MCPServerConfig:
    name: str
    kind: str
    transport: str
    tool_name: str
    enabled: bool = True
    base_url: str | None = None
    auth_token: str | None = None
    command: str | None = None
    args: list[str] = field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    default_profile: str | None = None
    timeout_sec: int = 60

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "MCPServerConfig":
        return cls(
            name=row["name"],
            kind=row["kind"],
            transport=row["transport"],
            tool_name=row["tool_name"],
            enabled=bool(row["enabled"]),
            base_url=row["base_url"],
            auth_token=row["auth_token"],
            command=row["command"],
            args=json.loads(row["args_json"] or "[]"),
            cwd=row["cwd"],
            env=json.loads(row["env_json"] or "{}"),
            default_profile=row["default_profile"],
            timeout_sec=row["timeout_sec"] or 60,
        )


def load_config(conn: sqlite3.Connection, name: str) -> MCPServerConfig:
    row = conn.execute("SELECT * FROM mcp_servers WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise NotFoundError(f"MCP server {name!r} is not configured")
    return MCPServerConfig.from_row(row)


def list_configs(conn: sqlite3.Connection) -> list[MCPServerConfig]:
    rows = conn.execute("SELECT * FROM mcp_servers ORDER BY name").fetchall()
    return [MCPServerConfig.from_row(r) for r in rows]


def parse_tool_result(result: Any) -> list[dict]:
    """Flatten an MCP tool result into a list of dicts.

    FastMCP returns one content block *per list item*, each holding a standalone
    JSON object -- not a single block containing a JSON array. Treating
    content[0] as the whole list is the easy mistake, and it silently yields
    exactly one result.
    """
    out: list[dict] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # A plain-text block is usually an error message from the server.
            out.append({"_text": text})
            continue

        if isinstance(parsed, list):
            out.extend(p for p in parsed if isinstance(p, dict))
        elif isinstance(parsed, dict):
            out.append(parsed)
        else:
            out.append({"_value": parsed})
    return out


class MCPClient:
    def __init__(self, config: MCPServerConfig):
        self.config = config

    # -- transport --------------------------------------------------------- #

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.sse import sse_client
        from mcp.client.stdio import stdio_client

        cfg = self.config

        if cfg.transport == "sse":
            if not cfg.base_url:
                raise MCPError(
                    "No base URL configured", server=cfg.name, transport=cfg.transport
                )
            headers = (
                {"Authorization": f"Bearer {cfg.auth_token}"} if cfg.auth_token else {}
            )
            async with sse_client(
                f"{cfg.base_url.rstrip('/')}/sse",
                headers=headers,
                timeout=CONNECT_TIMEOUT,
                sse_read_timeout=cfg.timeout_sec,
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

        elif cfg.transport == "stdio":
            if not cfg.command:
                raise MCPError(
                    "No command configured", server=cfg.name, transport=cfg.transport
                )
            params = StdioServerParameters(
                command=cfg.command,
                args=cfg.args,
                cwd=cfg.cwd,
                env={**os.environ, **cfg.env},
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

        else:
            raise MCPError(
                f"Unsupported transport {cfg.transport!r}",
                server=cfg.name,
                transport=cfg.transport,
            )

    def _describe(self, exc: BaseException) -> str:
        """Turn a transport exception into something a human can act on.

        The mcp SDK runs its transports inside an anyio task group, so the real
        cause arrives wrapped in an ExceptionGroup whose own message is the
        useless "unhandled errors in a TaskGroup (1 sub-exception)". Unwrap it
        or the Test connection button reports nothing worth reading.
        """
        cfg = self.config
        causes: list[str] = []

        def walk(err: BaseException, depth: int = 0) -> None:
            if depth > 5:
                return
            subs = getattr(err, "exceptions", None)
            if subs:
                for sub in subs:
                    walk(sub, depth + 1)
                return
            text = str(err).strip()
            causes.append(f"{err.__class__.__name__}: {text}" if text else err.__class__.__name__)

        walk(exc)
        detail = "; ".join(dict.fromkeys(causes)) or exc.__class__.__name__

        if "401" in detail or "Unauthorized" in detail:
            return f"{cfg.name} rejected the token (401). Check the token in Settings."
        if "403" in detail or "Forbidden" in detail:
            return f"{cfg.name} refused the token (403)."
        if "404" in detail:
            return f"{cfg.name} has no /sse endpoint at {cfg.base_url} (404)."
        if any(s in detail for s in ("ConnectError", "Connection refused", "ConnectionRefused")):
            return f"Could not connect to {cfg.base_url or cfg.command}."
        if "FileNotFoundError" in detail:
            return (
                f"Could not launch {cfg.command!r}. Note that a stdio server "
                f"cannot run inside the container."
            )
        return detail

    async def _guarded(self, coro_factory):
        """Run a session-scoped coroutine with a hard outer timeout."""
        cfg = self.config
        try:
            return await asyncio.wait_for(
                coro_factory(), timeout=cfg.timeout_sec + OUTER_TIMEOUT_MARGIN
            )
        except asyncio.TimeoutError as exc:
            raise MCPTimeoutError(
                f"{cfg.name} did not respond within {cfg.timeout_sec}s",
                server=cfg.name,
                transport=cfg.transport,
            ) from exc
        except MCPError:
            raise
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise MCPError(
                self._describe(exc), server=cfg.name, transport=cfg.transport
            ) from exc

    # -- operations -------------------------------------------------------- #

    async def list_tools(self) -> list[dict]:
        async def run():
            async with self._session() as session:
                result = await session.list_tools()
                return [
                    {"name": t.name, "description": t.description}
                    for t in result.tools
                ]

        return await self._guarded(run)

    async def call_tool(self, name: str, arguments: dict) -> list[dict]:
        async def run():
            async with self._session() as session:
                result = await session.call_tool(name, arguments)
                if getattr(result, "isError", False):
                    text = ""
                    for block in getattr(result, "content", None) or []:
                        text += getattr(block, "text", "") or ""
                    raise MCPError(
                        f"{name} failed: {text[:300]}",
                        server=self.config.name,
                        transport=self.config.transport,
                    )
                return parse_tool_result(result)

        return await self._guarded(run)

    async def test(self) -> dict:
        """Connect, handshake, list tools. Powers the Test connection button."""
        cfg = self.config
        started = time.monotonic()
        try:
            tools = await self.list_tools()
        except MCPError as exc:
            return {
                "ok": False,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "tools": [],
                "error": exc.message,
            }

        names = [t["name"] for t in tools]
        if cfg.tool_name and cfg.tool_name not in names:
            return {
                "ok": False,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "tools": names,
                "error": (
                    f"Connected, but the server does not expose {cfg.tool_name!r}. "
                    f"Available: {', '.join(names[:8])}"
                ),
            }

        return {
            "ok": True,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "tools": names,
            "error": None,
        }

    # -- typed wrappers ---------------------------------------------------- #

    async def search_events(
        self,
        *,
        query: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        calendar_type: str | None = None,
        profile: str | None = None,
    ) -> list[dict]:
        args: dict = {"profile": profile or self.config.default_profile or "default"}
        if query:
            args["query"] = query
        if start_date:
            args["start_date"] = start_date
        if end_date:
            args["end_date"] = end_date
        if calendar_type:
            args["calendar_type"] = calendar_type
        return await self.call_tool(self.config.tool_name or "search_events", args)

    async def search_emails(
        self, query: str, *, profile: str | None = None
    ) -> list[dict]:
        return await self.call_tool(
            self.config.tool_name or "search_emails",
            {
                "query": query,
                "profile": profile or self.config.default_profile or "default",
            },
        )


def record_test_result(conn: sqlite3.Connection, name: str, result: dict) -> None:
    conn.execute(
        """
        UPDATE mcp_servers
           SET last_test_at = ?, last_test_ok = ?, last_test_error = ?,
               last_test_tools_json = ?, updated_at = ?
         WHERE name = ?
        """,
        (
            utcnow(),
            int(bool(result.get("ok"))),
            result.get("error"),
            json.dumps(result.get("tools") or []),
            utcnow(),
            name,
        ),
    )
