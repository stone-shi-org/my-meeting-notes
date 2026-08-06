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
from app.errors import ConflictError, ValidationError
from app.jobs import queue as queue_mod
from app.logging_config import get_logger
from app.schemas import (
    MeetingCreateRequest,
    MeetingOut,
    MeetingUpdateRequest,
    MoveItemRequest,
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


def _check_extension(filename: str) -> None:
    if not audio_svc.is_allowed_extension(filename):
        raise ValidationError(
            f"Unsupported file type {Path(filename).suffix!r}. "
            f"Accepted: {', '.join(sorted(audio_svc.ALLOWED_EXTENSIONS))}"
        )


async def _stream_to_disk(file: UploadFile, dest: Path) -> int:
    """Write the upload out in chunks and return the byte count.

    Chunked because these files run to 100 MB and reading one into memory would
    be a self-inflicted OOM. Deletes the partial file on any failure; the caller
    owns whatever database row it had already created.
    """
    settings = get_settings()
    max_bytes = settings.max_upload_mb * 1024 * 1024
    written = 0

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(UPLOAD_CHUNK):
                written += len(chunk)
                if written > max_bytes:
                    raise ValidationError(
                        f"File exceeds the {settings.max_upload_mb} MB limit"
                    )
                out.write(chunk)
        if written == 0:
            raise ValidationError("Uploaded file is empty")
    except Exception:
        dest.unlink(missing_ok=True)
        raise

    return written


async def _queue_ingest(
    conn: sqlite3.Connection,
    *,
    meeting_id: int,
    thread_id: int,
    user_id: int,
    diarization_model: str | None,
    summary_model: str | None,
    auto_summarize: bool,
) -> str:
    job_id = queue_mod.create_job(
        conn,
        job_type="ingest",
        user_id=user_id,
        meeting_id=meeting_id,
        thread_id=thread_id,
        payload={
            "meeting_id": meeting_id,
            "diarization_model": diarization_model,
            "summary_model": summary_model,
            "auto_summarize": auto_summarize,
            "user_id": user_id,
        },
    )
    conn.commit()
    await queue_mod.get_queue().enqueue(job_id)
    return job_id


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
    _check_extension(filename)

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

    dest = settings.audio_dir / str(meeting_id) / f"original{Path(filename).suffix.lower()}"
    try:
        written = await _stream_to_disk(file, dest)
    except Exception:
        # This route created the meeting, so a failed upload takes it with it --
        # unlike POST /{id}/audio, where the meeting predates the request.
        conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        conn.commit()
        raise

    conn.execute(
        "UPDATE meetings SET original_filename = ?, original_path = ?, "
        "original_mime = ?, original_bytes = ?, status = 'processing', "
        "updated_at = ? WHERE id = ?",
        (filename, str(dest), file.content_type, written, utcnow(), meeting_id),
    )

    if speaker_names:
        threads_svc.seed_speaker_names(conn, meeting_id, speaker_names.split(","))

    job_id = await _queue_ingest(
        conn,
        meeting_id=meeting_id,
        thread_id=resolved_thread,
        user_id=user.id,
        diarization_model=diarization_model,
        summary_model=summary_model,
        auto_summarize=auto_summarize,
    )

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


@router.post("/{meeting_id}/audio", status_code=202)
async def add_meeting_audio(
    meeting_id: int,
    file: UploadFile = File(...),
    diarization_model: str | None = Form(None),
    summary_model: str | None = Form(None),
    auto_summarize: bool = Form(True),
    speaker_names: str | None = Form(None, description="Comma-separated, optional"),
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Attach a recording to a meeting that has none, and run the pipeline.

    A meeting can exist before its audio does: creating one from an upcoming
    calendar event is exactly that, and it is the point of the feature. Without
    this route the only way to get a transcript onto such a meeting is to delete
    it and upload afresh, losing the attached event, the speaker hints and the
    place on the thread's timeline along with it.

    Re-uploading over a failed attempt is allowed -- the previous audio is
    replaced. Re-uploading over a *transcript* is not: the diarization, its
    speaker names and any summaries all belong to the old audio, and silently
    stranding them is worse than making the caller decide.
    """
    row = threads_svc.get_meeting(conn, meeting_id)
    assert_can_access(row, user)

    if row["active_diarization_id"] is not None:
        raise ConflictError(
            "This meeting already has a transcript. Create a new meeting for a "
            "different recording, or delete this one first."
        )
    if row["status"] == "processing":
        raise ConflictError("This meeting is still processing its current recording.")

    filename = file.filename or "upload"
    _check_extension(filename)

    # Clear out a previous failed attempt rather than leaving orphan files
    # beside the new one; the directory is per-meeting.
    target_dir = get_settings().audio_dir / str(meeting_id)
    if target_dir.is_dir():
        shutil.rmtree(target_dir, ignore_errors=True)

    dest = target_dir / f"original{Path(filename).suffix.lower()}"
    written = await _stream_to_disk(file, dest)

    conn.execute(
        "UPDATE meetings SET original_filename = ?, original_path = ?, "
        "original_mime = ?, original_bytes = ?, status = 'processing', "
        # Reset what described the audio that is no longer there.
        "audio_path = NULL, audio_converted = 0, audio_duration_sec = NULL, "
        "audio_sample_rate = NULL, audio_channels = NULL, updated_at = ? WHERE id = ?",
        (filename, str(dest), file.content_type, written, utcnow(), meeting_id),
    )

    if speaker_names:
        threads_svc.seed_speaker_names(conn, meeting_id, speaker_names.split(","))

    threads_svc.touch_thread(conn, row["thread_id"])

    job_id = await _queue_ingest(
        conn,
        meeting_id=meeting_id,
        thread_id=row["thread_id"],
        user_id=user.id,
        diarization_model=diarization_model,
        summary_model=summary_model,
        auto_summarize=auto_summarize,
    )

    log.info(
        "user %s added %s (%d bytes) to existing meeting %s, job %s",
        user.username, filename, written, meeting_id, job_id,
    )
    return {
        "meeting_id": meeting_id,
        "thread_id": row["thread_id"],
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


@router.post("/{meeting_id}/move", response_model=MeetingOut)
def move_meeting(
    meeting_id: int,
    payload: MoveItemRequest,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> MeetingOut:
    row = threads_svc.get_meeting(conn, meeting_id)
    assert_can_access(row, user)
    assert_can_access(threads_svc.get_thread(conn, payload.target_thread_id), user)

    moved = threads_svc.move_meeting(
        conn,
        meeting_id=meeting_id,
        thread_id=row["thread_id"],
        target_thread_id=payload.target_thread_id,
    )
    # Only the destination counts as activity, same call as a single
    # attachment's move: losing something isn't what the sort order means to
    # surface, gaining one is.
    threads_svc.touch_thread(conn, payload.target_thread_id)
    return MeetingOut(**threads_svc.row_to_meeting(moved))


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
