"""Meeting CRUD. Upload, transcript and summary routes arrive in later phases."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile

from app.config import get_settings
from app.db import utcnow
from app.deps import (
    CurrentUser,
    active_user,
    assert_can_access,
    get_db,
    owner_scope,
    paginate,
)
from app.errors import ValidationError
from app.jobs import queue as queue_mod
from app.logging_config import get_logger
from app.schemas import (
    MeetingCreateRequest,
    MeetingOut,
    MeetingUpdateRequest,
    Page,
)
from app.services import audio as audio_svc
from app.services import threads as threads_svc

router = APIRouter(prefix="/api/meetings", tags=["meetings"])
log = get_logger("meetings")

UPLOAD_CHUNK = 1 << 20  # 1 MiB


def resolve_thread(
    conn: sqlite3.Connection,
    user: CurrentUser,
    *,
    thread_id: int | None,
    new_thread_title: str | None,
    new_thread_description: str | None = None,
) -> int:
    """Pick an existing thread or create one. Shared with the upload route."""
    if thread_id is not None:
        thread = threads_svc.get_thread(conn, thread_id)
        assert_can_access(thread, user)
        return thread_id

    if new_thread_title:
        created = threads_svc.create_thread(
            conn,
            owner_id=user.id,
            title=new_thread_title,
            description=new_thread_description,
        )
        return created["id"]

    raise ValidationError("Provide either thread_id or new_thread_title")


@router.get("", response_model=Page[MeetingOut])
def list_meetings(
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1),
    thread_id: int | None = Query(None),
    all: bool = Query(False),
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> Page[MeetingOut]:
    p, size, offset = paginate(page, page_size)
    scope_sql, scope_params = owner_scope(user, all)

    rows, total = threads_svc.list_meetings(
        conn,
        thread_id=thread_id,
        scope_sql=scope_sql,
        scope_params=scope_params,
        limit=size,
        offset=offset,
    )

    return Page[MeetingOut](
        items=[MeetingOut(**threads_svc.row_to_meeting(r)) for r in rows],
        page=p,
        page_size=size,
        total=total,
        total_pages=max(1, -(-total // size)),
    )


@router.post("", response_model=MeetingOut, status_code=201)
def create_meeting(
    payload: MeetingCreateRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> MeetingOut:
    """Create a meeting with no audio yet. The upload route supersedes this."""
    thread_id = resolve_thread(
        conn,
        user,
        thread_id=payload.thread_id,
        new_thread_title=payload.new_thread_title,
        new_thread_description=payload.new_thread_description,
    )
    row = threads_svc.create_meeting(
        conn,
        thread_id=thread_id,
        owner_id=user.id,
        title=payload.title,
        meeting_at=payload.meeting_at,
        notes=payload.notes,
    )
    return MeetingOut(**threads_svc.row_to_meeting(row))


@router.post("/upload", status_code=202)
async def upload_meeting(
    file: UploadFile = File(...),
    title: str = Form(...),
    thread_id: int | None = Form(None),
    new_thread_title: str | None = Form(None),
    new_thread_description: str | None = Form(None),
    meeting_at: str | None = Form(None),
    notes: str | None = Form(None),
    diarization_model: str | None = Form(None),
    summary_model: str | None = Form(None),
    auto_summarize: bool = Form(True),
    speaker_names: str | None = Form(None, description="Comma-separated, optional"),
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Accept an audio file and queue the full pipeline.

    Returns 202 immediately: diarization takes minutes, so the client follows
    the returned job_id rather than holding the request open.
    """
    settings = get_settings()
    filename = file.filename or "upload"

    if not audio_svc.is_allowed_extension(filename):
        raise ValidationError(
            f"Unsupported file type {Path(filename).suffix!r}. "
            f"Accepted: {', '.join(sorted(audio_svc.ALLOWED_EXTENSIONS))}"
        )

    resolved_thread = resolve_thread(
        conn,
        user,
        thread_id=thread_id,
        new_thread_title=new_thread_title,
        new_thread_description=new_thread_description,
    )

    meeting = threads_svc.create_meeting(
        conn,
        thread_id=resolved_thread,
        owner_id=user.id,
        title=title,
        meeting_at=meeting_at,
        notes=notes,
    )
    meeting_id = meeting["id"]

    target_dir = settings.audio_dir / str(meeting_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / f"original{Path(filename).suffix.lower()}"

    # Stream to disk in chunks: these files run to 100 MB and reading one into
    # memory would be a self-inflicted OOM.
    max_bytes = settings.max_upload_mb * 1024 * 1024
    written = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(UPLOAD_CHUNK):
                written += len(chunk)
                if written > max_bytes:
                    raise ValidationError(
                        f"File exceeds the {settings.max_upload_mb} MB limit"
                    )
                out.write(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        conn.commit()
        raise

    if written == 0:
        dest.unlink(missing_ok=True)
        conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        conn.commit()
        raise ValidationError("Uploaded file is empty")

    conn.execute(
        "UPDATE meetings SET original_filename = ?, original_path = ?, "
        "original_mime = ?, original_bytes = ?, status = 'processing', "
        "updated_at = ? WHERE id = ?",
        (filename, str(dest), file.content_type, written, utcnow(), meeting_id),
    )

    if speaker_names:
        threads_svc.seed_speaker_names(conn, meeting_id, speaker_names.split(","))

    job_id = queue_mod.create_job(
        conn,
        job_type="ingest",
        user_id=user.id,
        meeting_id=meeting_id,
        thread_id=resolved_thread,
        payload={
            "meeting_id": meeting_id,
            "diarization_model": diarization_model,
            "summary_model": summary_model,
            "auto_summarize": auto_summarize,
            "user_id": user.id,
        },
    )
    conn.commit()

    await queue_mod.get_queue().enqueue(job_id)

    log.info(
        "user %s uploaded %s (%d bytes) as meeting %s, job %s",
        user.username, filename, written, meeting_id, job_id,
    )
    return {
        "meeting_id": meeting_id,
        "thread_id": resolved_thread,
        "job_id": job_id,
        "bytes": written,
    }


@router.get("/{meeting_id}", response_model=MeetingOut)
def get_meeting(
    meeting_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> MeetingOut:
    row = threads_svc.get_meeting(conn, meeting_id)
    assert_can_access(row, user)
    return MeetingOut(**threads_svc.row_to_meeting(row))


@router.patch("/{meeting_id}", response_model=MeetingOut)
def update_meeting(
    meeting_id: int,
    payload: MeetingUpdateRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> MeetingOut:
    row = threads_svc.get_meeting(conn, meeting_id)
    assert_can_access(row, user)

    updates: dict = {}
    if payload.title is not None:
        updates["title"] = payload.title
    if payload.meeting_at is not None:
        updates["meeting_at"] = payload.meeting_at
    if payload.notes is not None:
        updates["notes"] = payload.notes

    if updates:
        updates["updated_at"] = utcnow()
        assignments = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE meetings SET {assignments} WHERE id = ?",
            [*updates.values(), meeting_id],
        )
        threads_svc.touch_thread(conn, row["thread_id"])

    return MeetingOut(**threads_svc.row_to_meeting(threads_svc.require_meeting(conn, meeting_id)))


@router.delete("/{meeting_id}")
def delete_meeting(
    meeting_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = threads_svc.get_meeting(conn, meeting_id)
    assert_can_access(row, user)
    thread_id = row["thread_id"]

    target = get_settings().audio_dir / str(meeting_id)
    purged = target.is_dir()
    if purged:
        shutil.rmtree(target, ignore_errors=True)

    conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
    threads_svc.touch_thread(conn, thread_id)

    log.info("user %s deleted meeting %s", user.username, meeting_id)
    return {"ok": True, "purged_audio": purged}
