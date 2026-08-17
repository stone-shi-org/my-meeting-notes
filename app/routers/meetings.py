"""Meeting CRUD. Upload, transcript and summary routes arrive in later phases."""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
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


def _normalize_channel_fields(
    channel_map: str | None, room_speakers: str | None
) -> tuple[str | None, str]:
    """Validate the two fields useRecorder sends for a channel-separated capture.

    channel_map is empty/None for an ordinary recording -- the overwhelming
    majority. room_speakers only means anything alongside it, and defaults to
    the safe assumption, 'multiple', so an omitted value never silently
    collapses several remote voices into one (see room_speakers' comment in
    db.py).
    """
    if not channel_map:
        return None, "multiple"
    if channel_map != "mic_room":
        raise ValidationError(f"Unknown channel_map {channel_map!r}")
    resolved = room_speakers or "multiple"
    if resolved not in ("single", "multiple"):
        raise ValidationError(
            f"room_speakers must be 'single' or 'multiple', got {resolved!r}"
        )
    return channel_map, resolved


def _parse_multi_channels(mode: str, channels_json: str | None, num_files: int) -> list[dict]:
    """Validate and parse the ``channels`` field for a multi-source upload.

    Each entry becomes one meeting_audio_channels row: {"label": str | None,
    "run_diarization": bool, "start_offset_sec": float}. For 'multi_file' the
    list length must match the number of uploaded files -- channel i comes
    from file i. For 'multi_channel' it declares how many channels the
    single uploaded file is claimed to have; that claim is checked against
    ffprobe's real channel count in the convert stage, not here, since
    nothing here has decoded the file yet.
    """
    if not channels_json:
        raise ValidationError(f"channels is required when mode={mode!r}")
    try:
        parsed = json.loads(channels_json)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"channels is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list) or not parsed:
        raise ValidationError("channels must be a non-empty JSON array")
    if mode == "multi_file" and len(parsed) != num_files:
        raise ValidationError(
            f"channels has {len(parsed)} entries but {num_files} file(s) were uploaded"
        )

    result = []
    for i, entry in enumerate(parsed):
        if not isinstance(entry, dict):
            raise ValidationError(f"channels[{i}] must be an object")
        label = entry.get("label")
        if label is not None and not isinstance(label, str):
            raise ValidationError(f"channels[{i}].label must be a string or null")
        run_diarization = entry.get("run_diarization", False)
        if not isinstance(run_diarization, bool):
            raise ValidationError(f"channels[{i}].run_diarization must be a boolean")
        offset = entry.get("start_offset_sec", 0)
        try:
            offset = float(offset)
        except (TypeError, ValueError):
            raise ValidationError(f"channels[{i}].start_offset_sec must be a number") from None
        if offset < 0:
            raise ValidationError(f"channels[{i}].start_offset_sec must not be negative")
        result.append(
            {"label": label or None, "run_diarization": run_diarization, "start_offset_sec": offset}
        )
    return result


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


def _staged_upload_path(audio_dir: Path, suffix: str) -> Path:
    """A same-filesystem temporary path for an upload awaiting validation.

    Multipart parsing has already received the request, but copying a 100 MB
    spooled file still must not happen while SQLite's single writer lock is
    held. Keeping staging under ``audio_dir`` also makes the final rename
    atomic rather than a cross-filesystem copy.
    """
    stage_dir = audio_dir / ".uploads"
    stage_dir.mkdir(parents=True, exist_ok=True)
    return stage_dir / f"{uuid.uuid4().hex}{suffix}"


def _create_ingest_job(
    conn: sqlite3.Connection,
    *,
    meeting_id: int,
    thread_id: int,
    user_id: int,
    diarization_model: str | None,
    summary_model: str | None,
    auto_summarize: bool,
) -> str:
    """Persist an ingest job in the caller's transaction.

    The routes commit the meeting, filesystem swap and job together before
    enqueueing it. If enqueueing then fails, restart recovery still sees the
    committed queued job.
    """
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
    return job_id


@router.post("/upload", status_code=202)
async def upload_meeting(
    file: UploadFile = File(...),
    extra_files: list[UploadFile] = File(
        default=[], description="Additional files for mode='multi_file' -- file is channel 0"
    ),
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
    channel_map: str | None = Form(
        None, description="'mic_room' if the recorder kept mic and room audio on separate channels"
    ),
    room_speakers: str | None = Form(
        None, description="'single' or 'multiple' -- only meaningful alongside channel_map"
    ),
    skip_diarization: bool = Form(
        False, description="Skip the model diarization call; produce a flat, single-speaker transcript"
    ),
    mode: str = Form(
        "single", description="'single' | 'multi_channel' | 'multi_file' -- multi-source upload shape"
    ),
    channels: str | None = Form(
        None, description="JSON array of {label, run_diarization, start_offset_sec}, one per channel"
    ),
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
    channel_map, room_speakers = _normalize_channel_fields(channel_map, room_speakers)

    all_files = [file, *extra_files]
    multi_channels: list[dict] | None = None
    if mode in ("multi_channel", "multi_file"):
        if channel_map == "mic_room":
            raise ValidationError("mode cannot be combined with channel_map='mic_room'")
        if mode == "multi_channel" and len(all_files) != 1:
            raise ValidationError("mode='multi_channel' takes exactly one file")
        for f in all_files:
            _check_extension(f.filename or "upload")
        multi_channels = _parse_multi_channels(mode, channels, len(all_files))
        channel_map = mode
    elif mode != "single":
        raise ValidationError(f"Unknown mode {mode!r}")

    staged: Path | None = None
    staged_multi: list[tuple[Path, str]] = []
    try:
        if channel_map == "multi_file":
            for f in all_files:
                suffix_i = Path(f.filename or "upload").suffix.lower()
                staged_path = _staged_upload_path(settings.audio_dir, suffix_i)
                await _stream_to_disk(f, staged_path)
                staged_multi.append((staged_path, suffix_i))
        else:
            suffix = Path(filename).suffix.lower()
            staged = _staged_upload_path(settings.audio_dir, suffix)
            written = await _stream_to_disk(file, staged)
    except Exception:
        if staged is not None:
            staged.unlink(missing_ok=True)
        for staged_path, _ in staged_multi:
            staged_path.unlink(missing_ok=True)
        raise

    dest: Path | None = None
    created_meeting_dir: Path | None = None
    try:
        # Nothing writes to SQLite until every file has passed the size and
        # non-empty checks above. A rejected upload therefore creates neither
        # an orphan meeting nor an orphan thread.
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
        meeting_dir = settings.audio_dir / str(meeting_id)
        meeting_dir.mkdir(parents=True, exist_ok=True)
        # Tracked separately from `dest`: for multi_file, a failure partway
        # through moving N files would otherwise leave `dest` unset and skip
        # cleanup below, orphaning whichever files had already landed.
        created_meeting_dir = meeting_dir

        if channel_map == "multi_file":
            written = 0
            for i, (staged_path, suffix_i) in enumerate(staged_multi):
                dest_i = meeting_dir / f"source_{i}{suffix_i}"
                staged_path.replace(dest_i)
                written += dest_i.stat().st_size
            # original_path/original_filename point at source 0 -- there is
            # no single "the" original for N separately uploaded files, and
            # existing code (e.g. the "download original" link) reads those
            # columns generically expecting exactly one path.
            dest = meeting_dir / f"source_0{staged_multi[0][1]}"
            original_filename = all_files[0].filename or "upload"
            original_mime = all_files[0].content_type
        else:
            dest = meeting_dir / f"original{suffix}"
            staged.replace(dest)
            original_filename = filename
            original_mime = file.content_type

        conn.execute(
            "UPDATE meetings SET original_filename = ?, original_path = ?, "
            "original_mime = ?, original_bytes = ?, status = 'processing', "
            "channel_map = ?, room_speakers = ?, skip_diarization = ?, "
            "updated_at = ? WHERE id = ?",
            (
                original_filename, str(dest), original_mime, written,
                channel_map, room_speakers, int(skip_diarization), utcnow(), meeting_id,
            ),
        )

        if multi_channels is not None:
            for i, ch in enumerate(multi_channels):
                source_filename = (
                    all_files[i].filename
                    if channel_map == "multi_file"
                    else (all_files[0].filename or "upload")
                )
                conn.execute(
                    "INSERT INTO meeting_audio_channels "
                    "(meeting_id, channel_index, label, run_diarization, start_offset_sec, source_filename) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        meeting_id, i, ch["label"], int(ch["run_diarization"]),
                        ch["start_offset_sec"], source_filename,
                    ),
                )

        if speaker_names:
            threads_svc.seed_speaker_names(conn, meeting_id, speaker_names.split(","))

        job_id = _create_ingest_job(
            conn,
            meeting_id=meeting_id,
            thread_id=resolved_thread,
            user_id=user.id,
            diarization_model=diarization_model,
            summary_model=summary_model,
            auto_summarize=auto_summarize,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        if created_meeting_dir is not None:
            shutil.rmtree(created_meeting_dir, ignore_errors=True)
        raise
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
        for staged_path, _ in staged_multi:
            staged_path.unlink(missing_ok=True)

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


@router.post("/{meeting_id}/audio", status_code=202)
async def add_meeting_audio(
    meeting_id: int,
    file: UploadFile = File(...),
    extra_files: list[UploadFile] = File(
        default=[], description="Additional files for mode='multi_file' -- file is channel 0"
    ),
    diarization_model: str | None = Form(None),
    summary_model: str | None = Form(None),
    auto_summarize: bool = Form(True),
    speaker_names: str | None = Form(None, description="Comma-separated, optional"),
    channel_map: str | None = Form(
        None, description="'mic_room' if the recorder kept mic and room audio on separate channels"
    ),
    room_speakers: str | None = Form(
        None, description="'single' or 'multiple' -- only meaningful alongside channel_map"
    ),
    skip_diarization: bool = Form(
        False, description="Skip the model diarization call; produce a flat, single-speaker transcript"
    ),
    mode: str = Form(
        "single", description="'single' | 'multi_channel' | 'multi_file' -- multi-source upload shape"
    ),
    channels: str | None = Form(
        None, description="JSON array of {label, run_diarization, start_offset_sec}, one per channel"
    ),
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
    channel_map, room_speakers = _normalize_channel_fields(channel_map, room_speakers)

    all_files = [file, *extra_files]
    multi_channels: list[dict] | None = None
    if mode in ("multi_channel", "multi_file"):
        if channel_map == "mic_room":
            raise ValidationError("mode cannot be combined with channel_map='mic_room'")
        if mode == "multi_channel" and len(all_files) != 1:
            raise ValidationError("mode='multi_channel' takes exactly one file")
        for f in all_files:
            _check_extension(f.filename or "upload")
        multi_channels = _parse_multi_channels(mode, channels, len(all_files))
        channel_map = mode
    elif mode != "single":
        raise ValidationError(f"Unknown mode {mode!r}")

    target_dir = get_settings().audio_dir / str(meeting_id)
    staged: Path | None = None
    staged_multi: list[tuple[Path, str]] = []
    try:
        # Do not touch the failed attempt until its replacement is complete and
        # valid. Browser recordings may have no other surviving copy.
        if channel_map == "multi_file":
            for f in all_files:
                suffix_i = Path(f.filename or "upload").suffix.lower()
                staged_path = _staged_upload_path(get_settings().audio_dir, suffix_i)
                await _stream_to_disk(f, staged_path)
                staged_multi.append((staged_path, suffix_i))
        else:
            suffix = Path(filename).suffix.lower()
            staged = _staged_upload_path(get_settings().audio_dir, suffix)
            written = await _stream_to_disk(file, staged)
    except Exception:
        if staged is not None:
            staged.unlink(missing_ok=True)
        for staged_path, _ in staged_multi:
            staged_path.unlink(missing_ok=True)
        raise

    backup = target_dir.parent / f".{meeting_id}-{uuid.uuid4().hex}.replaced"
    had_previous = target_dir.is_dir()
    try:
        if had_previous:
            target_dir.replace(backup)
        target_dir.mkdir(parents=True, exist_ok=False)

        if channel_map == "multi_file":
            written = 0
            for i, (staged_path, suffix_i) in enumerate(staged_multi):
                dest_i = target_dir / f"source_{i}{suffix_i}"
                staged_path.replace(dest_i)
                written += dest_i.stat().st_size
            dest = target_dir / f"source_0{staged_multi[0][1]}"
            original_filename = all_files[0].filename or "upload"
            original_mime = all_files[0].content_type
        else:
            dest = target_dir / f"original{suffix}"
            staged.replace(dest)
            original_filename = filename
            original_mime = file.content_type

        conn.execute(
            "UPDATE meetings SET original_filename = ?, original_path = ?, "
            "original_mime = ?, original_bytes = ?, status = 'processing', "
            # Reset what described the audio that is no longer there.
            "audio_path = NULL, audio_converted = 0, audio_duration_sec = NULL, "
            "audio_sample_rate = NULL, audio_channels = NULL, "
            "channel_map = ?, room_speakers = ?, skip_diarization = ?, "
            "updated_at = ? WHERE id = ?",
            (
                original_filename, str(dest), original_mime, written,
                channel_map, room_speakers, int(skip_diarization), utcnow(), meeting_id,
            ),
        )

        # A previous attempt's channel rows (if it was also multi-source)
        # describe audio that no longer exists -- same reasoning as the
        # audio_* column reset above, and required for the unique
        # (meeting_id, channel_index) index below to accept the new set.
        conn.execute("DELETE FROM meeting_audio_channels WHERE meeting_id = ?", (meeting_id,))
        if multi_channels is not None:
            for i, ch in enumerate(multi_channels):
                source_filename = (
                    all_files[i].filename
                    if channel_map == "multi_file"
                    else (all_files[0].filename or "upload")
                )
                conn.execute(
                    "INSERT INTO meeting_audio_channels "
                    "(meeting_id, channel_index, label, run_diarization, start_offset_sec, source_filename) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        meeting_id, i, ch["label"], int(ch["run_diarization"]),
                        ch["start_offset_sec"], source_filename,
                    ),
                )

        if speaker_names:
            threads_svc.seed_speaker_names(conn, meeting_id, speaker_names.split(","))
        threads_svc.touch_thread(conn, row["thread_id"])

        job_id = _create_ingest_job(
            conn,
            meeting_id=meeting_id,
            thread_id=row["thread_id"],
            user_id=user.id,
            diarization_model=diarization_model,
            summary_model=summary_model,
            auto_summarize=auto_summarize,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        shutil.rmtree(target_dir, ignore_errors=True)
        if had_previous and backup.exists():
            backup.replace(target_dir)
        raise
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
        for staged_path, _ in staged_multi:
            staged_path.unlink(missing_ok=True)

    if had_previous:
        shutil.rmtree(backup, ignore_errors=True)
    await queue_mod.get_queue().enqueue(job_id)

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
