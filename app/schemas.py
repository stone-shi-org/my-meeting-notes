"""Request and response models."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    page: int
    page_size: int
    total: int
    total_pages: int


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=512)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=512)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str | None = None
    is_admin: bool
    is_active: bool
    must_change_password: bool
    created_at: str
    last_login_at: str | None = None


class LoginResponse(BaseModel):
    user: UserOut
    must_change_password: bool


class SessionOut(BaseModel):
    id: str
    created_at: str
    expires_at: str
    last_seen_at: str | None = None
    user_agent: str | None = None
    ip: str | None = None
    current: bool = False


# --------------------------------------------------------------------------- #
# Users (admin)
# --------------------------------------------------------------------------- #


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._@-]+$")
    password: str = Field(min_length=1, max_length=512)
    display_name: str | None = Field(default=None, max_length=200)
    is_admin: bool = False


class UserUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    is_admin: bool | None = None
    is_active: bool | None = None


class ResetPasswordRequest(BaseModel):
    """An omitted password means "generate one and show it once"."""

    new_password: str | None = Field(default=None, max_length=512)


class ResetPasswordResponse(BaseModel):
    user: UserOut
    temporary_password: str | None = None


# --------------------------------------------------------------------------- #
# Threads
# --------------------------------------------------------------------------- #


class ThreadOut(BaseModel):
    id: int
    owner_id: int
    title: str
    description: str | None = None
    archived: bool
    created_at: str
    updated_at: str
    meeting_count: int = 0
    last_meeting_at: str | None = None
    email_count: int = 0
    event_count: int = 0
    unread_count: int = 0
    """Auto-attached items nobody has opened. Non-zero puts the dot on the card."""
    auto_match_at: str | None = None
    auto_match_error: str | None = None
    next_step: str | None = None
    next_step_generated_at: str | None = None
    """True when a meeting/email/event has been added since next_step was
    generated, or nothing has been generated yet. Only computed on the
    single-thread GET, not the list."""
    next_step_stale: bool = False


class ThreadCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=5000)


class ThreadUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=5000)
    archived: bool | None = None


# --------------------------------------------------------------------------- #
# Meetings
# --------------------------------------------------------------------------- #


class MeetingOut(BaseModel):
    id: int
    thread_id: int
    owner_id: int
    title: str
    meeting_at: str | None = None
    status: str
    original_filename: str | None = None
    original_bytes: int | None = None
    audio_duration_sec: float | None = None
    audio_sample_rate: int | None = None
    audio_channels: int | None = None
    audio_converted: bool = False
    has_audio: bool = False
    has_transcript: bool = False
    has_summary: bool = False
    notes: str | None = None
    created_at: str
    updated_at: str
    summary_tldr: str | None = None
    open_action_items: int = 0
    speaker_count: int = 0


class MeetingCreateRequest(BaseModel):
    """Used by the JSON create route; uploads use multipart form fields instead."""

    thread_id: int | None = None
    new_thread_title: str | None = Field(default=None, max_length=300)
    new_thread_description: str | None = Field(default=None, max_length=5000)
    title: str = Field(min_length=1, max_length=300)
    meeting_at: str | None = None
    notes: str | None = Field(default=None, max_length=20000)


class MeetingUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    meeting_at: str | None = None
    notes: str | None = Field(default=None, max_length=20000)


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #


class TimelineItem(BaseModel):
    kind: str  # meeting | event | email
    at: str | None
    id: int
    payload: dict


# --------------------------------------------------------------------------- #
# Per-user integrations
# --------------------------------------------------------------------------- #


class ProviderSpecOut(BaseModel):
    """One entry in the "Add integration" picker."""

    id: str
    label: str
    kinds: list[str]
    auth_type: str  # oauth2 | password | token
    docs_url: str = ""


class IntegrationOut(BaseModel):
    id: int
    provider: str
    provider_label: str
    supported_kinds: list[str]
    account_key: str
    account_label: str | None
    calendar_enabled: bool
    email_enabled: bool
    enabled: bool
    auth_type: str
    config: dict
    """Non-secret settings only -- URLs, profile names, region."""
    has_secret: bool
    secret_preview: str | None
    """Masked tail, e.g. ••••1234. The credential itself is never returned."""
    status: str  # ok | error | unverified | reauth_required
    scopes: str | None
    token_expires_at: str | None
    last_test: dict
    created_at: str
    updated_at: str


class CreateIntegrationRequest(BaseModel):
    provider: str
    account_label: str | None = Field(default=None, max_length=200)
    calendar_enabled: bool | None = None
    email_enabled: bool | None = None
    config: dict = Field(default_factory=dict)
    secret: dict[str, str] = Field(default_factory=dict)


class UpdateIntegrationRequest(BaseModel):
    account_label: str | None = Field(default=None, max_length=200)
    calendar_enabled: bool | None = None
    email_enabled: bool | None = None
    enabled: bool | None = None
    config: dict | None = None
    secret: dict[str, str] | None = None
    """Per key: absent leaves it alone, "" clears it, a masked echo is also
    treated as unchanged, anything else replaces it."""


class IntegrationSummaryOut(BaseModel):
    """Drives the enabled/disabled state of the match button."""

    calendar: int
    email: int
    needs_reauth: list[dict]
