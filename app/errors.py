"""Application error types and their JSON representation.

Every handled failure comes back as ``{"error": {"code": ..., "message": ...}}`` so the
SPA has one shape to branch on. ``code`` is a stable machine-readable slug; the frontend
keys config-problem deep links off a known subset (DIARIZATION_UNREACHABLE, LLM_AUTH_FAILED,
MCP_TIMEOUT).
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class AppError(Exception):
    """Base class for expected failures. Never leaks a traceback to the client."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ValidationError(AppError):
    status_code = 400
    code = "validation_error"


class AuthRequiredError(AppError):
    status_code = 401
    code = "auth_required"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class PasswordChangeRequiredError(AppError):
    status_code = 409
    code = "password_change_required"

    def __init__(self, message: str = "Password change required"):
        super().__init__(message)


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class AudioError(AppError):
    status_code = 502
    code = "audio_error"


class DiarizationError(AppError):
    status_code = 502
    code = "diarization_error"


class DiarizationUnreachableError(DiarizationError):
    code = "DIARIZATION_UNREACHABLE"


class LLMError(AppError):
    status_code = 502
    code = "llm_error"


class LLMAuthError(LLMError):
    code = "LLM_AUTH_FAILED"


class LLMReasoningTruncatedError(LLMError):
    """The model spent its whole token budget reasoning and emitted no content.

    Distinct from a generic LLMError because it proves the opposite of a
    connection problem: the endpoint, credentials and model routing all
    worked. Only the budget was too small. The summarize path still treats
    this as a failure (it genuinely has no summary), but a connection test
    should report it as reachable.
    """

    code = "LLM_REASONING_TRUNCATED"


class ProviderError(AppError):
    """A connected calendar or inbox failed.

    The generic case. ``MCPError`` below predates the provider abstraction and is
    kept distinct because the SPA deep-links off its code, but a CalDAV or IMAP
    failure is not an MCP failure and should not claim to be one.
    """

    status_code = 502
    code = "provider_error"

    def __init__(self, message: str, *, provider: str = "", kind: str = "", code: str | None = None):
        super().__init__(message, code=code)
        self.provider = provider
        self.kind = kind

    def to_dict(self) -> dict:
        payload = super().to_dict()
        payload["error"]["provider"] = self.provider
        payload["error"]["kind"] = self.kind
        return payload


class MCPError(AppError):
    status_code = 502
    code = "mcp_error"

    def __init__(self, message: str, *, server: str = "", transport: str = "", code: str | None = None):
        super().__init__(message, code=code)
        self.server = server
        self.transport = transport

    def to_dict(self) -> dict:
        payload = super().to_dict()
        payload["error"]["server"] = self.server
        payload["error"]["transport"] = self.transport
        return payload


class MCPTimeoutError(MCPError):
    code = "MCP_TIMEOUT"


class NoIntegrationsError(AppError):
    """The user has not connected any calendar or inbox to search.

    409 rather than 400: nothing about the request is malformed, the account just
    is not set up yet. The SPA normally prevents this by grouping the match button
    behind the integrations summary, so reaching here means a stale bundle -- the
    code is mapped to a Settings deep link for exactly that case.
    """

    status_code = 409
    code = "NO_INTEGRATIONS"


class IntegrationAuthError(AppError):
    """A connected account's credentials no longer work and cannot be refreshed.

    Terminal on purpose: retrying an ``invalid_grant`` forever just burns quota
    and hides the one thing that fixes it, which is the user reconnecting.
    """

    status_code = 502
    code = "NEEDS_REAUTH"


class JobCancelled(Exception):
    """Raised inside a job body at a stage boundary when cancellation was requested."""


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "HTTP error"
        code = {401: "auth_required", 403: "forbidden", 404: "not_found"}.get(
            exc.status_code, "http_error"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": code, "message": detail}},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed",
                    "details": exc.errors(),
                }
            },
        )
