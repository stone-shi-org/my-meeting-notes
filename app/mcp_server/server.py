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
* **DNS-rebinding protection is off, explicitly.** The SDK's own app factory
  switches it on for a localhost bind, allowing only localhost ``Host`` headers -- which would 421 every request that arrives through the
  LAN address or the reverse proxy. The bearer token is the gate here; a
  rebinding attacker in a victim's browser cannot attach it.
* **A fresh MCPServer + session manager per ``create_app()``.** The session
  manager's ``run()`` may be entered exactly once per instance, and the test
  suite builds (and starts) a new app per test.
* **Every protocol revision, negotiated by the SDK (mcp 2.x).** The session
  manager routes on the ``MCP-Protocol-Version`` header: absent, or one of the
  ``initialize``-handshake revisions (2024-11-05 .. 2025-11-25), goes to the
  legacy stateless path, where ``initialize`` negotiates the version (an
  unknown offer is answered with the newest handshake revision, per spec).
  Anything else goes to the 2026-07-28 per-request path -- no handshake,
  ``server/discover`` advertises what is supported, and an unsupported
  version is refused with ``-32022`` listing the supported ones. Nothing here
  re-implements either era; :data:`SUPPORTED_PROTOCOL_VERSIONS` only reports
  what the SDK speaks, for the Settings page and the tests.

Authentication happens here, before the SDK sees the request. The principal
travels on the ASGI scope (``scope["state"]["mcp_principal"]``); the SDK hands
every handler the originating Starlette request (``ctx.request``) on both
paths, which is how the scope middleware and the tools find it again. A
contextvar would have to survive the session manager's task-group hop; the
request object is passed explicitly.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, ListToolsResult, TextContent, ToolAnnotations
from mcp_types.version import (
    HANDSHAKE_PROTOCOL_VERSIONS,
    MODERN_PROTOCOL_VERSIONS,
)

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


#: Every revision /mcp negotiates, oldest first. Read from the SDK so it can
#: never claim a version the installed transport does not actually speak.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (*HANDSHAKE_PROTOCOL_VERSIONS, *MODERN_PROTOCOL_VERSIONS)


def principal_of(request) -> Principal | None:
    """The principal ``MCPEndpoint`` stored on this request's ASGI scope."""
    if request is None:
        return None
    return request.scope.get("state", {}).get(PRINCIPAL_KEY)


class ScopeMiddleware:
    """Token scope, enforced once for both protocol eras.

    A read-only token never sees the write tools in ``tools/list`` -- hidden,
    not merely refused -- and a ``tools/call`` naming one is answered exactly
    like an unknown tool, because that is what it is to this caller. Runs as
    SDK middleware (before params validation, on the legacy and 2026-07-28
    paths alike) rather than inside each tool, so the rule lives in one place.
    """

    async def __call__(self, ctx, call_next):
        principal = principal_of(ctx.request)
        read_only = principal is not None and not principal.can_write
        if read_only and ctx.method == "tools/call":
            name = (ctx.params or {}).get("name")
            if name in WRITE_TOOL_NAMES:
                return CallToolResult(
                    content=[TextContent(type="text", text=f"Unknown tool: {name}")], is_error=True
                )
        result = await call_next(ctx)
        if read_only and ctx.method == "tools/list":
            result = _without_write_tools(result)
        return result


def _without_write_tools(result):
    if isinstance(result, ListToolsResult):
        return result.model_copy(
            update={"tools": [t for t in result.tools if t.name not in WRITE_TOOL_NAMES]}
        )
    if isinstance(result, dict) and isinstance(result.get("tools"), list):  # pragma: no cover
        return {**result, "tools": [t for t in result["tools"] if t.get("name") not in WRITE_TOOL_NAMES]}
    return result  # pragma: no cover


def _error_text(exc: AppError) -> str:
    return f"[{exc.code}] {exc.message}"


def _make_tool(spec: ToolSpec):
    """Wrap a plain ``tools.py`` function as an MCP tool.

    The schema comes from the function's own signature minus the leading
    ``conn``/``principal`` parameters, so the docstring and type hints in
    tools.py are the single source of what a client sees. A keyword-only
    ``mcp_context`` parameter typed :class:`Context` is appended: the SDK
    injects the request context there and leaves it out of the schema.
    """
    sig = inspect.signature(spec.fn, eval_str=True)
    skip = 1 if spec.is_async else 2
    params = list(sig.parameters.values())[skip:]
    ctx_param = inspect.Parameter("mcp_context", inspect.Parameter.KEYWORD_ONLY, annotation=Context)

    async def runner(mcp_context: Context, **kwargs: Any) -> dict:
        principal = principal_of(mcp_context.request_context.request)
        if principal is None:  # pragma: no cover - the endpoint always sets it
            raise ToolError("[auth_required] no principal on this request")
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
    # Every tool returns a JSON object; the SDK needs the parametrised form to
    # emit it as structuredContent (a bare `dict` is rejected).
    runner.__signature__ = sig.replace(  # type: ignore[attr-defined]
        parameters=[*params, ctx_param], return_annotation=dict[str, Any]
    )
    runner.__annotations__ = {
        **{p.name: p.annotation for p in params},
        "mcp_context": Context,
        "return": dict[str, Any],
    }
    return runner


def _run_sync(spec: ToolSpec, principal: Principal, kwargs: dict) -> dict:
    """One short-lived connection per call, committed on success -- the same
    lifecycle as a REST request's ``get_db``."""
    with get_conn() as conn:
        return spec.fn(conn, principal, **kwargs)


#: Handlers MCPServer registers by default that this server has no use for.
#: Capabilities are *derived from registered handlers*: while
#: `subscriptions/listen` is served, the 2026-07-28 `server/discover` result
#: advertises `listChanged` on tools/prompts/resources and `resources.subscribe`
#: -- promises this server never keeps (its tool list is fixed; it has no
#: prompts or resources). A client that believes them opens a
#: `subscriptions/listen` stream that by design never completes, plus three list
#: calls for nothing. From a browser that stream pins one of the six HTTP/1.1
#: connections per origin for good; with the rest busy, every later request
#: queues until it times out -- the MCP Inspector's "5 requests are
#: unanswered" (MMN-14). Unregistering them makes the advertisement honest:
#: tools only, no change notifications, and an explicit METHOD_NOT_FOUND for
#: anything else.
UNUSED_METHODS: tuple[str, ...] = (
    "subscriptions/listen",
    "prompts/list",
    "prompts/get",
    "resources/list",
    "resources/read",
    "resources/templates/list",
)


def build_server() -> MCPServer:
    server = MCPServer(
        name="my-meeting-notes",
        instructions=INSTRUCTIONS,
        version=__version__,
        middleware=[ScopeMiddleware()],
    )
    for spec in TOOL_SPECS:
        server.add_tool(
            _make_tool(spec),
            name=spec.name,
            title=spec.title,
            annotations=ToolAnnotations(
                title=spec.title,
                read_only_hint=not spec.write,
                destructive_hint=False,
                idempotent_hint=not spec.write,
                # Only the live calendar read reaches outside this app's data.
                open_world_hint=spec.name == "get_upcoming_events",
            ),
            structured_output=True,
        )
    handlers = server._lowlevel_server._request_handlers
    for method in UNUSED_METHODS:
        handlers.pop(method, None)
    return server


class MCPEndpoint:
    """The ASGI app behind ``/mcp``: gate, authenticate, then hand to the SDK."""

    def __init__(self) -> None:
        self.server = build_server()
        self.session_manager = StreamableHTTPSessionManager(
            app=self.server._lowlevel_server,
            json_response=True,
            stateless=True,
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
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
