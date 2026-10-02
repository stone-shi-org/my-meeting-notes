"""The streamable-HTTP MCP endpoint at ``/mcp`` (MMN-14).

Wiring, and the reasons for each choice:

* **Stateless, JSON responses** (``stateless=True``, ``json_response=True``).
  Every request carries its own bearer and gets a plain JSON reply -- no
  ``Mcp-Session-Id`` to keep sticky behind a reverse proxy, no SSE stream for
  the proxy to buffer. Every tool here is request/response, so nothing is lost.
* **One ``Route`` per path, not ``app.mount``.** Mounting the SDK's Starlette
  app at ``/mcp`` gives ``/mcp/mcp``, and a mount 307-redirects a bare
  ``/mcp`` to ``/mcp/`` -- a redirect several clients do not follow on POST.
  ``/mcp`` and ``/mcp/`` are both registered explicitly instead, ahead of the
  SPA catch-all that would otherwise swallow them.
* **DNS-rebinding protection is off, explicitly.** FastMCP switches it on when
  constructed with the default ``host="127.0.0.1"``, allowing only localhost
  ``Host`` headers -- which would 421 every request that arrives through the
  LAN address or the reverse proxy. The bearer token is the gate here; a
  rebinding attacker in a victim's browser cannot attach it.
* **A fresh FastMCP + session manager per ``create_app()``.** The session
  manager's ``run()`` may be entered exactly once per instance, and the test
  suite builds (and starts) a new app per test.

Authentication happens here, before the SDK sees the request. The principal
travels on the ASGI scope (``scope["state"]["mcp_principal"]``); the SDK hands
each tool call the originating Starlette request, which is how the tools find
it again. A contextvar would have to survive the session manager's task-group
hop; the request object is passed explicitly.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
from mcp.types import Tool as MCPTool
from mcp.types import ToolAnnotations

# Accept modern MCP clients advertising the 2026-07-28 protocol version
if "2026-07-28" not in SUPPORTED_PROTOCOL_VERSIONS:
    SUPPORTED_PROTOCOL_VERSIONS.append("2026-07-28")

from app import __version__
from app.config import effective, get_settings
from app.db import get_conn
from app.errors import AppError
from app.logging_config import get_logger
from app.mcp_server.auth import authenticate, bearer_from_headers
from app.mcp_server.tools import TOOL_SPECS, WRITE_TOOL_NAMES, Principal, ToolSpec

log = get_logger("mcp_server")

PRINCIPAL_KEY = "mcp_principal"

INSTRUCTIONS = """\
My Meeting Notes: recorded meetings with speaker-labelled transcripts and AI
summaries, organised into threads (a project or recurring series), with notes,
emails and calendar events attached to each thread.

Typical flow: `search` (or `list_meetings` with since/until for a date range)
to find the meeting, then `get_meeting_transcript` / `get_meeting_summary`.
Dates are ISO-8601 UTC; responses carry `server_time` so relative dates like
"last week" can be resolved. Long transcripts are paged with `next_offset`.
Notes have a `source`: "manual" is the user's own words, "ai_chat" and "mcp"
are AI-written -- do not cite those back as evidence.
"""


class MeetingNotesMCP(FastMCP):
    """FastMCP with two per-request behaviours the stock server lacks: the
    tool list depends on the caller's token scope, and so does permission to
    call a write tool."""

    def current_principal(self) -> Principal:
        ctx = self.get_context()
        request = ctx.request_context.request if ctx.request_context else None
        principal = request.scope.get("state", {}).get(PRINCIPAL_KEY) if request else None
        if principal is None:  # pragma: no cover - the endpoint always sets it
            raise ToolError("unauthenticated: no principal on this request")
        return principal

    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        if self.current_principal().can_write:
            return tools
        # Hidden, not merely refused: a read-only agent should not be offered
        # tools it can never use.
        return [t for t in tools if t.name not in WRITE_TOOL_NAMES]

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        if name in WRITE_TOOL_NAMES and not self.current_principal().can_write:
            # Same message as an unknown tool, since that is what it is to
            # this caller -- list_tools never offered it.
            raise ToolError(f"Unknown tool: {name}")
        return await super().call_tool(name, arguments)


def _error_text(exc: AppError) -> str:
    return f"[{exc.code}] {exc.message}"


def _make_tool(server: MeetingNotesMCP, spec: ToolSpec):
    """Wrap a plain ``tools.py`` function as an MCP tool.

    The schema comes from the function's own signature minus the leading
    ``conn``/``principal`` parameters, so the docstring and type hints in
    tools.py are the single source of what a client sees.
    """
    sig = inspect.signature(spec.fn, eval_str=True)
    skip = 1 if spec.is_async else 2
    params = list(sig.parameters.values())[skip:]

    async def runner(**kwargs: Any) -> dict:
        principal = server.current_principal()
        try:
            if spec.is_async:
                return await spec.fn(principal, **kwargs)
            return await asyncio.to_thread(_run_sync, spec, principal, kwargs)
        except AppError as exc:
            raise ToolError(_error_text(exc)) from exc
        except ToolError:
            raise
        except Exception as exc:  # pragma: no cover - logged, never leaked
            log.exception("MCP tool %s failed for user %s", spec.name, principal.user.username)
            raise ToolError("[internal_error] The tool failed unexpectedly; see the server log") from exc

    runner.__name__ = spec.name
    runner.__doc__ = inspect.getdoc(spec.fn)
    # Every tool returns a JSON object; FastMCP needs the parametrised form to
    # emit it as structuredContent (a bare `dict` is rejected).
    runner.__signature__ = sig.replace(  # type: ignore[attr-defined]
        parameters=params, return_annotation=dict[str, Any]
    )
    runner.__annotations__ = {
        **{p.name: p.annotation for p in params},
        "return": dict[str, Any],
    }
    return runner


def _run_sync(spec: ToolSpec, principal: Principal, kwargs: dict) -> dict:
    """One short-lived connection per call, committed on success -- the same
    lifecycle as a REST request's ``get_db``."""
    with get_conn() as conn:
        return spec.fn(conn, principal, **kwargs)


def build_server() -> MeetingNotesMCP:
    server = MeetingNotesMCP(
        name="my-meeting-notes",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    # FastMCP stamps its own SDK version into serverInfo otherwise.
    server._mcp_server.version = __version__
    for spec in TOOL_SPECS:
        server.add_tool(
            _make_tool(server, spec),
            name=spec.name,
            title=spec.title,
            annotations=ToolAnnotations(
                title=spec.title,
                readOnlyHint=not spec.write,
                destructiveHint=False,
                idempotentHint=not spec.write,
                # Only the live calendar read reaches outside this app's data.
                openWorldHint=spec.name == "get_upcoming_events",
            ),
            structured_output=True,
        )
    return server


class MCPEndpoint:
    """The ASGI app behind ``/mcp``: gate, authenticate, then hand to the SDK."""

    def __init__(self) -> None:
        self.server = build_server()
        self.session_manager = StreamableHTTPSessionManager(
            app=self.server._mcp_server,
            json_response=True,
            stateless=True,
            security_settings=self.server.settings.transport_security,
        )

    def run(self):
        """Enter in the app lifespan; may be entered exactly once."""
        return self.session_manager.run()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":  # pragma: no cover
            return
        enabled, principal = await asyncio.to_thread(self._gate, scope)
        if not enabled:
            await _json(send, 404, {"error": {"code": "not_found", "message": "The MCP server is switched off"}})
            return
        if principal is None:
            await _json(
                send,
                401,
                {
                    "error": {
                        "code": "auth_required",
                        "message": "Send a personal API token as 'Authorization: Bearer mmn_...' "
                        "(create one under Settings -> MCP server)",
                    }
                },
                extra_headers=[(b"www-authenticate", b'Bearer realm="my-meeting-notes"')],
            )
            return
        scope.setdefault("state", {})[PRINCIPAL_KEY] = principal
        await self.session_manager.handle_request(scope, receive, send)

    @staticmethod
    def _gate(scope) -> tuple[bool, Principal | None]:
        with get_conn(get_settings().db_path) as conn:
            if not effective(conn, "mcp_enabled"):
                return False, None
            return True, authenticate(conn, bearer_from_headers(scope.get("headers") or []))


async def _json(send, status: int, body: dict, extra_headers: list | None = None) -> None:
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                *(extra_headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})
