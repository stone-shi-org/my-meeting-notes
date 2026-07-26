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
