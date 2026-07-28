"""MCP client: transport dispatch, result parsing, timeouts and Test connection."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest

from app.errors import MCPError, MCPTimeoutError
from app.services import mcpclient as mcp


# --------------------------------------------------------------------------- #
# Doubles matching the mcp SDK's shapes
# --------------------------------------------------------------------------- #


@dataclass
class Block:
    text: str
    type: str = "text"


@dataclass
class ToolResult:
    content: list
    isError: bool = False


@dataclass
class Tool:
    name: str
    description: str = ""


@dataclass
class ToolList:
    tools: list


class FakeSession:
    def __init__(self, tools=None, result=None, raises=None):
        self._tools = tools or []
        self._result = result
        self._raises = raises
        self.calls = []

    async def list_tools(self):
        if self._raises:
            raise self._raises
        return ToolList([Tool(t) for t in self._tools])

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self._raises:
            raise self._raises
        return self._result


def client_with(session, **cfg_kw):
    config = mcp.MCPServerConfig(
        name=cfg_kw.pop("name", "calendar"),
        kind=cfg_kw.pop("kind", "calendar"),
        transport=cfg_kw.pop("transport", "sse"),
        tool_name=cfg_kw.pop("tool_name", "search_events"),
        base_url=cfg_kw.pop("base_url", "http://mcp.test:4006"),
        auth_token=cfg_kw.pop("auth_token", "tok"),
        **cfg_kw,
    )
    c = mcp.MCPClient(config)

    @asynccontextmanager
    async def fake_session():
        yield session

    c._session = fake_session
    return c


# --------------------------------------------------------------------------- #
# Result parsing -- the FastMCP one-block-per-item shape
# --------------------------------------------------------------------------- #


class TestParseToolResult:
    def test_one_json_object_per_content_block(self):
        """FastMCP emits a block per list item, not one block holding an array.

        Reading content[0] as the whole list silently yields exactly one result.
        """
        result = ToolResult(
            content=[
                Block(json.dumps({"uid": "a", "summary": "First"})),
                Block(json.dumps({"uid": "b", "summary": "Second"})),
                Block(json.dumps({"uid": "c", "summary": "Third"})),
            ]
        )
        parsed = mcp.parse_tool_result(result)
        assert len(parsed) == 3
        assert [p["uid"] for p in parsed] == ["a", "b", "c"]

    def test_a_block_holding_an_array_is_also_flattened(self):
        result = ToolResult(content=[Block(json.dumps([{"uid": "a"}, {"uid": "b"}]))])
        assert len(mcp.parse_tool_result(result)) == 2

    def test_a_plain_text_block_is_preserved_not_dropped(self):
        result = ToolResult(content=[Block("Unknown tool: search_events")])
        parsed = mcp.parse_tool_result(result)
        assert parsed == [{"_text": "Unknown tool: search_events"}]

    def test_empty_content(self):
        assert mcp.parse_tool_result(ToolResult(content=[])) == []

    def test_mixed_json_and_text(self):
        result = ToolResult(
            content=[Block(json.dumps({"uid": "a"})), Block("a warning")]
        )
        parsed = mcp.parse_tool_result(result)
        assert parsed[0]["uid"] == "a"
        assert parsed[1]["_text"] == "a warning"


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


class TestTransport:
    async def test_sse_requires_a_base_url(self):
        c = mcp.MCPClient(
            mcp.MCPServerConfig(
                name="x", kind="calendar", transport="sse",
                tool_name="search_events", base_url=None,
            )
        )
        with pytest.raises(MCPError, match="No base URL"):
            await c.list_tools()

    async def test_stdio_requires_a_command(self):
        c = mcp.MCPClient(
            mcp.MCPServerConfig(
                name="x", kind="calendar", transport="stdio",
                tool_name="search_events", command=None,
            )
        )
        with pytest.raises(MCPError, match="No command"):
            await c.list_tools()

    async def test_an_unknown_transport_is_rejected(self):
        c = mcp.MCPClient(
            mcp.MCPServerConfig(
                name="x", kind="calendar", transport="carrier-pigeon",
                tool_name="t",
            )
        )
        with pytest.raises(MCPError, match="Unsupported transport"):
            await c.list_tools()


# --------------------------------------------------------------------------- #
# Calls
# --------------------------------------------------------------------------- #


class TestCalls:
    async def test_list_tools(self):
        c = client_with(FakeSession(tools=["search_events", "list_calendars"]))
        assert [t["name"] for t in await c.list_tools()] == [
            "search_events", "list_calendars"
        ]

    async def test_search_events_passes_the_profile_and_iso_dates(self):
        session = FakeSession(result=ToolResult(content=[]))
        c = client_with(session, default_profile="stone")

        await c.search_events(start_date="2026-03-11", end_date="2026-03-21", query="atlas")

        name, args = session.calls[0]
        assert name == "search_events"
        assert args["profile"] == "stone"
        assert args["start_date"] == "2026-03-11"  # calendar wants ISO-8601
        assert args["query"] == "atlas"

    async def test_search_events_omits_empty_optionals(self):
        session = FakeSession(result=ToolResult(content=[]))
        c = client_with(session, default_profile="stone")
        await c.search_events(start_date="2026-03-11")

        _, args = session.calls[0]
        assert "query" not in args
        assert "end_date" not in args

    async def test_search_emails_passes_the_query_through(self):
        session = FakeSession(result=ToolResult(content=[]))
        c = client_with(
            session, name="email", kind="email", tool_name="search_emails",
            default_profile="stone",
        )
        await c.search_emails("atlas after:2026/03/11")

        name, args = session.calls[0]
        assert name == "search_emails"
        assert args == {"query": "atlas after:2026/03/11", "profile": "stone"}

    async def test_an_explicit_profile_overrides_the_default(self):
        session = FakeSession(result=ToolResult(content=[]))
        c = client_with(session, default_profile="stone")
        await c.search_events(profile="work")
        assert session.calls[0][1]["profile"] == "work"

    async def test_a_tool_error_is_raised_not_returned(self):
        c = client_with(
            FakeSession(
                result=ToolResult(content=[Block("Unknown tool: nope")], isError=True)
            )
        )
        with pytest.raises(MCPError, match="Unknown tool"):
            await c.call_tool("nope", {})


# --------------------------------------------------------------------------- #
# Failure classification
# --------------------------------------------------------------------------- #


class TestFailures:
    async def test_a_401_is_explained_in_terms_of_the_token(self):
        c = client_with(FakeSession(raises=RuntimeError("HTTP 401 Unauthorized")))
        with pytest.raises(MCPError, match="rejected the token"):
            await c.list_tools()

    async def test_a_taskgroup_wrapper_is_unwrapped_to_the_real_cause(self):
        """The mcp SDK runs transports in an anyio task group, so the real error
        arrives inside an ExceptionGroup whose own message says nothing."""
        group = ExceptionGroup(
            "unhandled errors in a TaskGroup (1 sub-exception)",
            [RuntimeError("HTTP 401 Unauthorized")],
        )
        c = client_with(FakeSession(raises=group))
        with pytest.raises(MCPError) as exc:
            await c.list_tools()

        assert "rejected the token" in exc.value.message
        assert "TaskGroup" not in exc.value.message

    async def test_nested_groups_are_unwrapped(self):
        inner = ExceptionGroup("inner", [ConnectionRefusedError("Connection refused")])
        outer = ExceptionGroup("outer", [inner])
        c = client_with(FakeSession(raises=outer))
        with pytest.raises(MCPError, match="Could not connect"):
            await c.list_tools()

    async def test_a_missing_stdio_interpreter_mentions_the_container(self):
        c = client_with(
            FakeSession(raises=FileNotFoundError("No such file")),
            transport="stdio",
            command="/nonexistent/python",
            base_url=None,
        )
        with pytest.raises(MCPError) as exc:
            await c.list_tools()
        assert "cannot run inside the container" in exc.value.message

    async def test_a_404_names_the_url(self):
        c = client_with(FakeSession(raises=RuntimeError("HTTP 404 Not Found")))
        with pytest.raises(MCPError, match="no /sse endpoint"):
            await c.list_tools()

    async def test_connection_refused_surfaces_as_mcp_error(self):
        c = client_with(FakeSession(raises=ConnectionRefusedError("refused")))
        with pytest.raises(MCPError):
            await c.list_tools()

    async def test_the_outer_timeout_fires(self):
        class Slow(FakeSession):
            async def list_tools(self):
                await asyncio.sleep(5)
                return ToolList([])

        c = client_with(Slow(), timeout_sec=1)
        c.config.timeout_sec = 1
        # Shrink the margin so the test doesn't wait the full headroom.
        original = mcp.OUTER_TIMEOUT_MARGIN
        mcp.OUTER_TIMEOUT_MARGIN = 0.0
        try:
            with pytest.raises(MCPTimeoutError, match="did not respond"):
                await c.list_tools()
        finally:
            mcp.OUTER_TIMEOUT_MARGIN = original

    async def test_the_error_carries_the_server_and_transport(self):
        c = client_with(FakeSession(raises=RuntimeError("boom")), name="email")
        with pytest.raises(MCPError) as exc:
            await c.list_tools()
        assert exc.value.server == "email"
        assert exc.value.transport == "sse"


# --------------------------------------------------------------------------- #
# Test connection
# --------------------------------------------------------------------------- #


class TestTestConnection:
    async def test_success_reports_tools_and_latency(self):
        c = client_with(FakeSession(tools=["search_events", "list_calendars"]))
        result = await c.test()

        assert result["ok"] is True
        assert result["error"] is None
        assert "search_events" in result["tools"]
        assert result["latency_ms"] >= 0

    async def test_connected_but_missing_the_expected_tool(self):
        """The arr-mcp-on-the-wrong-port symptom: a live server, wrong tools."""
        c = client_with(FakeSession(tools=["radarr_get_status", "sonarr_get_health"]))
        result = await c.test()

        assert result["ok"] is False
        assert "does not expose 'search_events'" in result["error"]
        assert "radarr_get_status" in result["error"]

    async def test_a_failure_is_reported_rather_than_raised(self):
        c = client_with(FakeSession(raises=RuntimeError("HTTP 401 Unauthorized")))
        result = await c.test()
        assert result["ok"] is False
        assert "token" in result["error"]
        assert result["tools"] == []


# Config loading moved out of this module: which server to reach and whose
# account to search now comes from a per-user integrations row, covered by
# tests/test_providers_loader.py and tests/test_integrations.py.
