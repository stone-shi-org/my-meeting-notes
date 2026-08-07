"""Job bodies: the stage-by-stage work behind each job type.

Every stage is checkpointed -- it first asks whether its output already exists.
That is what makes a job resumable after a restart without redoing minutes of
work it already finished.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.config import effective, get_settings
from app.db import get_conn, utcnow
from app.errors import AudioError, JobCancelled
from app.jobs.queue import JobContext, register_job
from app.logging_config import get_logger
from app.services import audio as audio_svc
from app.services import threads as threads_svc

log = get_logger("pipeline")

FIXTURE_DIARIZATION = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "diarization_sample.json"


def _meeting_dir(meeting_id: int) -> Path:
    return get_settings().audio_dir / str(meeting_id)


async def _probe_stage(ctx: JobContext, meeting_id: int) -> audio_svc.AudioInfo | None:
    """Read stream metadata and record it on the meeting."""
    ctx.stage("probing", "Inspecting the audio file")

    with get_conn(ctx.db_path) as conn:
        row = threads_svc.require_meeting(conn, meeting_id)
        original = row["original_path"]

    if not original:
        ctx.event("No audio attached; nothing to inspect", level="warn")
        return None

    info = await asyncio.to_thread(audio_svc.probe, Path(original))

    with get_conn(ctx.db_path) as conn:
        conn.execute(
            "UPDATE meetings SET audio_duration_sec = ?, audio_sample_rate = ?, "
            "audio_channels = ?, updated_at = ? WHERE id = ?",
            (info.duration_sec, info.sample_rate, info.channels, utcnow(), meeting_id),
        )

    ctx.event(
        f"{info.codec_name} · {info.sample_rate} Hz · "
        f"{info.channels} channel(s) · {info.duration_sec:.0f}s"
        if info.duration_sec
        else f"{info.codec_name} · {info.sample_rate} Hz",
    )
    ctx.complete_stage()
    return info


async def _convert_stage(ctx: JobContext, meeting_id: int, info: audio_svc.AudioInfo | None) -> None:
    """Transcode to 16 kHz mono, unless the upload already is."""
    with get_conn(ctx.db_path) as conn:
        row = threads_svc.require_meeting(conn, meeting_id)
        original = row["original_path"]
        existing = row["audio_path"]

    if not original:
        ctx.skip("converting", "No audio to convert")
        return

    # Checkpoint: a previous attempt may already have produced the wav.
    if existing and Path(existing).exists() and existing != original:
        ctx.skip("converting", "Converted audio already present")
        return

    if info is not None and not audio_svc.needs_conversion(info):
        with get_conn(ctx.db_path) as conn:
            conn.execute(
                "UPDATE meetings SET audio_path = ?, audio_converted = 0, updated_at = ? "
                "WHERE id = ?",
                (original, utcnow(), meeting_id),
            )
        ctx.skip("converting", "Already 16 kHz mono PCM — no conversion needed")
        return

    ctx.stage("converting", "Converting to 16 kHz mono WAV")
    dest = _meeting_dir(meeting_id) / "audio16k.wav"
    await asyncio.to_thread(audio_svc.convert_to_wav16k_mono, Path(original), dest)

    # A browser recording has no duration in its header: MediaRecorder writes
    # WebM as a stream, and a stream does not know how long it will be. The
    # converted wav always does, so re-probe when the probe stage came back
    # blank -- everything downstream (the progress estimate, the player, the
    # meeting card) reads this column.
    duration = info.duration_sec if info else None
    if duration is None:
        try:
            duration = (await asyncio.to_thread(audio_svc.probe, dest)).duration_sec
            if duration:
                ctx.event(f"Length {duration:.0f}s (the source did not declare one)")
        except AudioError as exc:
            log.warning("could not read the length of the converted audio: %s", exc)

    with get_conn(ctx.db_path) as conn:
        conn.execute(
            "UPDATE meetings SET audio_path = ?, audio_converted = 1, "
            "audio_sample_rate = ?, audio_channels = ?, "
            "audio_duration_sec = COALESCE(audio_duration_sec, ?), "
            "updated_at = ? WHERE id = ?",
            (str(dest), audio_svc.TARGET_SAMPLE_RATE, audio_svc.TARGET_CHANNELS,
             duration, utcnow(), meeting_id),
        )

    ctx.event(f"Wrote {dest.name} ({dest.stat().st_size // 1024} KB)")
    ctx.complete_stage()


async def _diarize_stage(ctx: JobContext, meeting_id: int, model: str | None = None) -> int | None:
    """Send the wav to the diarization service and store the raw response.

    Real implementation lands in the diarization phase; the fake path exists so
    the minutes-long progress UX is testable in seconds.
    """
    settings = get_settings()

    with get_conn(ctx.db_path) as conn:
        row = threads_svc.require_meeting(conn, meeting_id)
        audio_path = row["audio_path"] or row["original_path"]
        duration = row["audio_duration_sec"]
        chosen_model = model or effective(conn, "diarization_model")
        provider_url = effective(conn, "diarization_url")

        # Checkpoint: an existing diarization for this model means a previous
        # attempt got this far.
        existing = conn.execute(
            "SELECT id FROM diarizations WHERE meeting_id = ? AND model = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (meeting_id, chosen_model),
        ).fetchone()

    if existing:
        ctx.skip("diarizing", "Transcript already exists for this model")
        return existing["id"]

    if not audio_path:
        ctx.skip("diarizing", "No audio to transcribe")
        return None

    ctx.stage("diarizing", "Sending audio to the diarization service")
    ctx.event(
        "This can take several minutes for a long recording.",
        stage="diarizing",
    )

    if settings.diarize_fake:
        payload = await _fake_diarize(ctx, duration)
        request_ms = 0
    else:
        from app.services.diarize import diarize_file

        payload, request_ms = await diarize_file(
            ctx,
            Path(audio_path),
            model=chosen_model,
            duration_sec=duration,
        )

    diar_id = await asyncio.to_thread(
        _persist_diarization,
        ctx,
        meeting_id,
        payload,
        provider_url,
        chosen_model,
        request_ms,
    )
    ctx.complete_stage("diarizing")
    return diar_id


async def _fake_diarize(ctx: JobContext, duration: float | None) -> dict:
    """Replay the sample response after a short, progress-reporting delay."""
    settings = get_settings()
    total = max(0.1, settings.diarize_fake_delay_sec)
    steps = 10
    for i in range(steps):
        ctx.check_cancelled()
        await asyncio.sleep(total / steps)
        ctx.stage_progress((i + 1) / steps)
    return json.loads(FIXTURE_DIARIZATION.read_text())


def _persist_diarization(
    ctx: JobContext,
    meeting_id: int,
    payload: dict,
    provider_url: str,
    model: str,
    request_ms: int,
) -> int:
    """Store the response verbatim and seed the speaker map."""
    ctx.stage("persisting", "Saving the transcript")

    raw = json.dumps(payload, ensure_ascii=False)
    segments = payload.get("segments") or []
    speakers = payload.get("speakers") or []

    target = _meeting_dir(meeting_id)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / "diarization.json"
    json_path.write_text(raw, encoding="utf-8")

    with get_conn(ctx.db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO diarizations (meeting_id, provider_url, model, raw_json,
                                      json_path, duration_sec, num_speakers,
                                      segment_count, request_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meeting_id,
                provider_url,
                model,
                raw,
                str(json_path),
                payload.get("duration"),
                payload.get("num_speakers"),
                len(segments),
                request_ms,
                utcnow(),
            ),
        )
        diar_id = cur.lastrowid

        # Seed one speaker_map row per speaker. display_name stays NULL so the
        # UI shows the raw SPEAKER_nn until someone names it. source='diarizer'
        # matters: only 'user' rows are protected from later LLM suggestions.
        for order, sp in enumerate(speakers):
            conn.execute(
                """
                INSERT INTO speaker_map (meeting_id, speaker_id, label, sort_order,
                                         source, updated_at)
                VALUES (?, ?, ?, ?, 'diarizer', ?)
                ON CONFLICT(meeting_id, speaker_id) DO NOTHING
                """,
                (meeting_id, sp.get("id"), sp.get("label"), order, utcnow()),
            )

        conn.execute(
            "UPDATE meetings SET active_diarization_id = ?, updated_at = ? WHERE id = ?",
            (diar_id, utcnow(), meeting_id),
        )

    ctx.event(
        f"{len(segments)} segments, {payload.get('num_speakers', '?')} speakers",
        stage="persisting",
    )
    ctx.complete_stage("persisting")
    return diar_id


async def _summarize_stage(ctx: JobContext, meeting_id: int, **kwargs) -> int | None:
    """LLM summary. Wired up in the summary phase."""
    try:
        from app.services.summarize import summarize_meeting
    except ImportError:
        ctx.skip("summarizing", "Summarization not available yet")
        return None

    ctx.stage("summarizing", "Generating the summary")
    summary_id = await summarize_meeting(ctx, meeting_id, **kwargs)
    ctx.complete_stage("summarizing")
    return summary_id


# --------------------------------------------------------------------------- #
# Job bodies
# --------------------------------------------------------------------------- #


@register_job("ingest")
async def run_ingest(ctx: JobContext) -> dict:
    meeting_id = int(ctx.payload["meeting_id"])

    try:
        return await _run_ingest(ctx, meeting_id)
    except JobCancelled:
        raise
    except Exception:
        # A terminal failure here must not leave the meeting reading
        # "processing" forever -- that also blocks re-upload.
        mark_meeting_failed(ctx.db_path, meeting_id)
        raise


async def _run_ingest(ctx: JobContext, meeting_id: int) -> dict:
    ctx.stage("received", "Upload received")
    ctx.complete_stage()

    info = await _probe_stage(ctx, meeting_id)
    await _convert_stage(ctx, meeting_id, info)

    diar_id = await _diarize_stage(ctx, meeting_id, ctx.payload.get("diarization_model"))

    summary_id = None
    if ctx.payload.get("auto_summarize", True) and diar_id is not None:
        try:
            summary_id = await _summarize_stage(
                ctx, meeting_id, model=ctx.payload.get("summary_model")
            )
        except Exception as exc:
            # A summary failure must not throw away a transcript that took
            # minutes to produce; the user can retry from the meeting page.
            log.warning("summary failed for meeting %s: %s", meeting_id, exc)
            ctx.event(f"Summary failed: {exc}", stage="summarizing", level="error")

    ctx.stage("done", "Finished")
    with get_conn(ctx.db_path) as conn:
        conn.execute(
            "UPDATE meetings SET status = 'ready', updated_at = ? WHERE id = ?",
            (utcnow(), meeting_id),
        )
        row = threads_svc.get_meeting(conn, meeting_id)
        if row is not None:
            threads_svc.touch_thread(conn, row["thread_id"])
    ctx.complete_stage()

    return {"meeting_id": meeting_id, "diarization_id": diar_id, "summary_id": summary_id}


@register_job("diarize")
async def run_diarize(ctx: JobContext) -> dict:
    """Re-run diarization only, e.g. with a different model."""
    meeting_id = int(ctx.payload["meeting_id"])
    diar_id = await _diarize_stage(ctx, meeting_id, ctx.payload.get("model"))

    ctx.stage("done", "Finished")
    with get_conn(ctx.db_path) as conn:
        conn.execute(
            "UPDATE meetings SET status = 'ready', updated_at = ? WHERE id = ?",
            (utcnow(), meeting_id),
        )
    ctx.complete_stage()
    return {"meeting_id": meeting_id, "diarization_id": diar_id}


@register_job("match")
async def run_match_job(ctx: JobContext) -> dict:
    from app.services.matching import run_match

    return await run_match(ctx)


@register_job("summarize")
async def run_summarize(ctx: JobContext) -> dict:
    meeting_id = int(ctx.payload["meeting_id"])
    summary_id = await _summarize_stage(
        ctx,
        meeting_id,
        model=ctx.payload.get("model"),
        prompt_name=ctx.payload.get("prompt_name"),
        prompt_override=ctx.payload.get("prompt_override"),
        temperature=ctx.payload.get("temperature"),
        created_by=ctx.payload.get("user_id"),
    )

    ctx.stage("done", "Finished")
    ctx.complete_stage()
    return {"meeting_id": meeting_id, "summary_id": summary_id}


def mark_meeting_failed(db_path, meeting_id: int) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE meetings SET status = 'failed', updated_at = ? WHERE id = ?",
            (utcnow(), meeting_id),
        )
