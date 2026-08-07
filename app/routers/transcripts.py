"""Transcript, audio streaming, and speaker naming."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, PlainTextResponse, Response

from app.db import utcnow
from app.deps import CurrentUser, active_user, assert_can_access, get_db
from app.errors import NotFoundError, ValidationError
from app.jobs import queue as queue_mod
from app.logging_config import get_logger
from app.services import threads as threads_svc
from app.services import transcript as transcript_svc
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/meetings", tags=["transcripts"])
log = get_logger("transcripts")

# Browsers only get seeking if the server honours Range; FileResponse does.
AUDIO_MIME = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
    ".webm": "audio/webm",
}


class SpeakerUpdate(BaseModel):
    speaker_id: str = Field(min_length=1, max_length=100)
    display_name: str | None = Field(default=None, max_length=200)
    color: str | None = Field(default=None, max_length=32)
    # None means "leave alone" for these three -- unlike display_name/color
    # above, whose one existing caller always resends the current value.
    # Whether a field was actually present in the request (vs. sent as null)
    # is read via `model_fields_set`, not the value itself.
    hidden: bool | None = None
    is_me: bool | None = None
    merge_into: str | None = Field(default=None, max_length=100)  # "" clears a merge


def _authorised_meeting(conn, meeting_id: int, user: CurrentUser):
    row = threads_svc.get_meeting(conn, meeting_id)
    assert_can_access(row, user)
    return row


@router.get("/{meeting_id}/diarization")
def get_raw_diarization(
    meeting_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    """The service's response, byte for byte. Never rewritten by renames."""
    _authorised_meeting(conn, meeting_id, user)
    row = transcript_svc.load_diarization(conn, meeting_id)
    return Response(content=row["raw_json"], media_type="application/json")


@router.get("/{meeting_id}/transcript")
def get_transcript(
    meeting_id: int,
    format: str = Query("json", pattern="^(json|text|md|vtt)$"),
    apply_names: bool = Query(True),
    include_nonspeech: bool = Query(True),
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
):
    _authorised_meeting(conn, meeting_id, user)
    transcript = transcript_svc.get_transcript(
        conn,
        meeting_id,
        apply_names=apply_names,
        include_nonspeech=include_nonspeech,
    )

    if format == "json":
        # The legend renders talk time and share per speaker, and this is the only
        # call it makes -- so the JSON shape carries the derived stats. The text
        # renderers walk segments and never look at them.
        transcript["speakers"] = transcript_svc.speaker_stats(transcript)
        return transcript

    body = transcript_svc.render(transcript, format)
    media = {"text": "text/plain", "md": "text/markdown", "vtt": "text/vtt"}[format]
    return PlainTextResponse(content=body, media_type=media)


@router.get("/{meeting_id}/audio")
def get_audio(
    meeting_id: int,
    original: bool = Query(False, description="Serve the upload rather than the 16 kHz wav"),
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> FileResponse:
    row = _authorised_meeting(conn, meeting_id, user)

    path_str = row["original_path"] if original else (row["audio_path"] or row["original_path"])
    if not path_str:
        raise NotFoundError("This meeting has no audio")

    path = Path(path_str)
    if not path.exists():
        raise NotFoundError("Audio file is missing from disk")

    # FileResponse implements Range/206. A hand-rolled StreamingResponse would
    # not, and the player's seek bar would be dead.
    return FileResponse(
        path,
        media_type=AUDIO_MIME.get(path.suffix.lower(), "application/octet-stream"),
        filename=row["original_filename"] or path.name,
        headers={"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"},
    )


@router.get("/{meeting_id}/speakers")
def get_speakers(
    meeting_id: int,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    _authorised_meeting(conn, meeting_id, user)

    try:
        transcript = transcript_svc.get_transcript(conn, meeting_id)
        stats = transcript_svc.speaker_stats(transcript)
    except NotFoundError:
        stats = []

    hints = conn.execute(
        "SELECT display_name FROM speaker_map WHERE meeting_id = ? "
        "AND source = 'user_hint' ORDER BY sort_order",
        (meeting_id,),
    ).fetchall()

    return {
        "speakers": stats,
        # Names typed at upload time, offered as suggestions to map by talk time.
        "name_hints": [h["display_name"] for h in hints if h["display_name"]],
    }


@router.put("/{meeting_id}/speakers")
def update_speakers(
    meeting_id: int,
    updates: list[SpeakerUpdate],
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Rename, hide, mark "me", or merge speakers.

    Writes only to speaker_map. The diarization payload is left untouched, so
    every change here is reversible and can never corrupt the source
    transcript.
    """
    _authorised_meeting(conn, meeting_id, user)

    if not updates:
        raise ValidationError("No speaker updates supplied")

    real_ids = {
        r["speaker_id"]
        for r in conn.execute(
            "SELECT speaker_id FROM speaker_map WHERE meeting_id = ? "
            "AND source != 'user_hint'",
            (meeting_id,),
        )
    }
    already_merged = {
        r["speaker_id"]
        for r in conn.execute(
            "SELECT speaker_id FROM speaker_map WHERE meeting_id = ? "
            "AND merged_into IS NOT NULL",
            (meeting_id,),
        )
    }

    # Validate the whole batch before writing anything, so a bad item in a
    # bulk call (e.g. hide-all) can't leave some speakers half-updated.
    for update in updates:
        if "merge_into" not in update.model_fields_set:
            continue
        target = (update.merge_into or "").strip() or None
        if target is None:
            continue
        if target == update.speaker_id:
            raise ValidationError("A speaker cannot be merged into itself")
        if target not in real_ids:
            raise ValidationError(f"Unknown speaker merge target {target!r}")
        if target in already_merged:
            raise ValidationError(
                f"{target!r} is itself merged into another speaker -- "
                "merge into that speaker instead"
            )

    now = utcnow()
    for update in updates:
        provided = update.model_fields_set
        # Empty string clears the name and falls back to the raw SPEAKER_nn --
        # display_name/color always overwrite, same as before this feature.
        name = (
            (update.display_name or "").strip() or None
            if "display_name" in provided
            else None
        )
        color = update.color if "color" in provided else None
        hidden_val = int(bool(update.hidden)) if "hidden" in provided else None
        is_me_val = int(bool(update.is_me)) if "is_me" in provided else None
        merge_target = None
        if "merge_into" in provided:
            merge_target = (update.merge_into or "").strip() or None
            if merge_target:
                # Resolve one hop in case the target was merged earlier in
                # this same batch -- keeps every row a single hop deep.
                row = conn.execute(
                    "SELECT merged_into FROM speaker_map WHERE meeting_id = ? "
                    "AND speaker_id = ?",
                    (meeting_id, merge_target),
                ).fetchone()
                if row and row["merged_into"]:
                    merge_target = row["merged_into"]

        conn.execute(
            """
            INSERT INTO speaker_map (meeting_id, speaker_id, display_name, color,
                                     hidden, is_me, merged_into, source, updated_at)
            VALUES (?, ?, ?, ?, COALESCE(?, 0), COALESCE(?, 0), ?, 'user', ?)
            ON CONFLICT(meeting_id, speaker_id) DO UPDATE SET
                display_name = CASE WHEN ? THEN excluded.display_name ELSE speaker_map.display_name END,
                color        = CASE WHEN ? THEN excluded.color ELSE speaker_map.color END,
                hidden       = CASE WHEN ? THEN excluded.hidden ELSE speaker_map.hidden END,
                is_me        = CASE WHEN ? THEN excluded.is_me ELSE speaker_map.is_me END,
                merged_into  = CASE WHEN ? THEN excluded.merged_into ELSE speaker_map.merged_into END,
                source       = 'user',
                updated_at   = excluded.updated_at
            """,
            (
                meeting_id, update.speaker_id, name, color, hidden_val, is_me_val,
                merge_target, now,
                "display_name" in provided,
                "color" in provided,
                "hidden" in provided,
                "is_me" in provided,
                "merge_into" in provided,
            ),
        )

        winner_for_me = update.speaker_id if ("is_me" in provided and update.is_me) else None

        if "merge_into" in provided and merge_target:
            # Any speaker already merged into this one follows it to the new
            # target, so nothing ever ends up two hops deep.
            conn.execute(
                "UPDATE speaker_map SET merged_into = ?, updated_at = ? "
                "WHERE meeting_id = ? AND merged_into = ?",
                (merge_target, now, meeting_id, update.speaker_id),
            )
            was_me = conn.execute(
                "SELECT is_me FROM speaker_map WHERE meeting_id = ? AND speaker_id = ?",
                (meeting_id, update.speaker_id),
            ).fetchone()["is_me"]
            if was_me:
                # A person doesn't stop being "me" for having been mis-split.
                conn.execute(
                    "UPDATE speaker_map SET is_me = 0, updated_at = ? "
                    "WHERE meeting_id = ? AND speaker_id = ?",
                    (now, meeting_id, update.speaker_id),
                )
                winner_for_me = merge_target

        if winner_for_me:
            conn.execute(
                "UPDATE speaker_map SET is_me = 1, updated_at = ? "
                "WHERE meeting_id = ? AND speaker_id = ?",
                (now, meeting_id, winner_for_me),
            )
            # At most one "me" per meeting.
            conn.execute(
                "UPDATE speaker_map SET is_me = 0, updated_at = ? "
                "WHERE meeting_id = ? AND speaker_id != ? AND is_me = 1",
                (now, meeting_id, winner_for_me),
            )

    conn.execute(
        "UPDATE meetings SET updated_at = ? WHERE id = ?", (utcnow(), meeting_id)
    )

    transcript = transcript_svc.get_transcript(conn, meeting_id)
    return {"ok": True, "speakers": transcript_svc.speaker_stats(transcript)}


@router.post("/{meeting_id}/rediarize", status_code=202)
async def rediarize(
    meeting_id: int,
    model: str | None = None,
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    row = _authorised_meeting(conn, meeting_id, user)
    if not (row["audio_path"] or row["original_path"]):
        raise ValidationError("This meeting has no audio to transcribe")

    job_id = queue_mod.create_job(
        conn,
        job_type="diarize",
        user_id=user.id,
        meeting_id=meeting_id,
        thread_id=row["thread_id"],
        payload={"meeting_id": meeting_id, "model": model},
    )
    conn.commit()
    await queue_mod.get_queue().enqueue(job_id)
    return {"job_id": job_id}
