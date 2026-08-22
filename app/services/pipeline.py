"""Job bodies: the stage-by-stage work behind each job type.

Every stage is checkpointed -- it first asks whether its output already exists.
That is what makes a job resumable after a restart without redoing minutes of
work it already finished.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from app.config import effective, get_settings
from app.db import get_conn, utcnow
from app.errors import AudioError, JobCancelled
from app.jobs.queue import JobContext, register_job
from app.logging_config import get_logger
from app.services import audio as audio_svc
from app.services import telegram as telegram_svc
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


async def _diarize_stage(
    ctx: JobContext, meeting_id: int, model: str | None = None, *, force: bool = False
) -> int | None:
    """Send the wav to the diarization service and store the raw response.

    Real implementation lands in the diarization phase; the fake path exists so
    the minutes-long progress UX is testable in seconds.

    ``force`` bypasses the checkpoint below. The `ingest` job needs the
    checkpoint -- it's what lets `recover()` resume a job that already paid
    for diarization without re-spending GPU minutes on a restart. The
    standalone `diarize` job (the "redo transcript" button) is the opposite
    case: it exists *only* to re-run diarization on the same audio, usually
    with the same model, so a caller that skipped the request entirely
    because a previous attempt "got this far" would silently produce nothing
    -- see the `rediarize` route, which always passes force=True.

    A recording longer than ``diarize_chunk_threshold_sec`` goes through
    ``_diarize_in_chunks`` instead of one ``diarize_file`` call -- see that
    function and diarize.py's ``looks_like_embedded_turns_dump`` for why: the
    model has an output-token budget, not a duration budget, and meeting 24
    (a real ~59 minute recording) overran it. Fake mode is checked first and
    never chunks, since it replaces the whole request-to-a-model step and
    there's no real budget to overrun.

    ``diarize_only`` (a Settings toggle, checked ahead of the chunk-duration
    branch) bypasses chunking entirely too: see ``_diarize_and_transcribe``.
    """
    settings = get_settings()

    with get_conn(ctx.db_path) as conn:
        row = threads_svc.require_meeting(conn, meeting_id)
        audio_path = row["audio_path"] or row["original_path"]
        duration = row["audio_duration_sec"]
        chosen_model = model or effective(conn, "diarization_model")
        provider_url = effective(conn, "diarization_url")
        diarize_only = effective(conn, "diarize_only")
        transcribe_model = effective(conn, "transcribe_model")
        # Runtime-overridable (see config.RUNTIME_KEYS), so read via
        # effective() here rather than settings.diarize_chunk_*_sec below --
        # those are only the env-backed fallback effective() itself falls
        # back to when nothing is saved in app_settings.
        chunk_threshold = effective(conn, "diarize_chunk_threshold_sec")
        chunk_size = effective(conn, "diarize_chunk_size_sec")

        # The label this run is stored and checkpointed under. Combining two
        # services' output is a materially different result from either
        # alone, so it gets its own label rather than colliding with (or
        # never matching) a plain run of the diarization model by itself --
        # switching "Diarization only" on/off must not make a stale
        # same-model diarization from before the switch look reusable.
        model_label = f"{chosen_model}+{transcribe_model}" if diarize_only else chosen_model

        # Checkpoint: an existing diarization for this model means a previous
        # attempt got this far. Skipped entirely when force=True.
        existing = None
        if not force:
            existing = conn.execute(
                "SELECT id FROM diarizations WHERE meeting_id = ? AND model = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (meeting_id, model_label),
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

    try:
        if settings.diarize_fake:
            # Fake mode replaces the whole request-to-a-model step, so it
            # never needs chunking -- there's no real output budget to
            # overrun. Keeping this branch first, ahead of every other check,
            # is what keeps every existing fake-diarization test exercising
            # exactly the single-call path it always has.
            payload = await _fake_diarize(ctx, duration)
            request_ms = 0
        elif diarize_only:
            payload, request_ms = await _diarize_and_transcribe(
                ctx,
                Path(audio_path),
                diar_model=chosen_model,
                transcribe_model=transcribe_model,
                duration_sec=duration,
            )
        elif duration and duration > chunk_threshold:
            payload, request_ms = await _diarize_in_chunks(
                ctx,
                Path(audio_path),
                model=chosen_model,
                duration_sec=duration,
                chunk_seconds=chunk_size,
            )
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
            model_label,
            request_ms,
        )
    except JobCancelled:
        raise
    except Exception as exc:
        await asyncio.to_thread(
            telegram_svc.notify_transcript_failed,
            ctx.db_path, meeting_id=meeting_id, error=str(exc),
        )
        raise

    ctx.complete_stage("diarizing")
    await asyncio.to_thread(telegram_svc.notify_transcript_ready, ctx.db_path, meeting_id=meeting_id)
    return diar_id


def _stitch_chunk_payloads(payloads_with_offsets: list[tuple[dict, float]]) -> dict:
    """Combine N chunk-local diarization payloads -- each covering
    [offset, offset + that chunk's own duration) on its own zero-based clock
    -- into one payload shaped exactly like a normal (unchunked) diarization
    response, so everything downstream (persistence, rendering, the
    speaker-merge UI) needs no chunk-awareness at all.

    Each chunk got its own fresh SPEAKER_nn numbering from the model, with no
    memory of the chunk before it -- a person who was SPEAKER_00 in chunk 0
    can come back as SPEAKER_01 in chunk 1, and there is no reliable way to
    tell from here. Segment and speaker ids are namespaced by chunk index
    ("c0:SPEAKER_00") to keep that honest rather than silently treating two
    different people as one (or one person as two) across a boundary --
    reconciling them afterward is the same "merge speakers" move already
    used for a same-chunk over-split, just possibly needed once more.
    """
    merged_segments: list[dict] = []
    merged_speakers: list[dict] = []
    seen_speaker_ids: set[str] = set()
    next_id = 0

    for i, (payload, offset) in enumerate(payloads_with_offsets):
        prefix = f"c{i}:"
        for seg in payload.get("segments") or []:
            new_seg = dict(seg)
            new_seg["id"] = next_id
            next_id += 1
            new_seg["speaker"] = f"{prefix}{seg.get('speaker')}"
            new_seg["start"] = (seg.get("start") or 0) + offset
            new_seg["end"] = (seg.get("end") or 0) + offset
            merged_segments.append(new_seg)
        for sp in payload.get("speakers") or []:
            new_sp = dict(sp)
            new_sp["id"] = f"{prefix}{sp.get('id')}"
            if new_sp["id"] not in seen_speaker_ids:
                seen_speaker_ids.add(new_sp["id"])
                merged_speakers.append(new_sp)

    return {
        "task": "diarize",
        "num_speakers": len(merged_speakers),
        "segments": merged_segments,
        "speakers": merged_speakers,
        # "chunked"/"chunk_count" are breadcrumbs for whoever next looks at a
        # raw_json blob and wonders why the speaker ids look like
        # "c1:SPEAKER_00" -- not read by anything downstream.
        # "chunk_boundaries" IS read downstream (transcript.build_transcript
        # passes it through so the SPA can draw a "Part 2 starts here"
        # divider): each chunk's start offset on the full-recording clock.
        # Deliberately time-based rather than derived from segment/speaker
        # ids at render time -- merging a chunk's speaker into another
        # chunk's changes what that segment's *speaker* id looks like, but
        # never how it should be drawn on the timeline.
        "chunked": True,
        "chunk_count": len(payloads_with_offsets),
        "chunk_boundaries": [offset for _, offset in payloads_with_offsets],
    }


async def _diarize_in_chunks(
    ctx: JobContext,
    path: Path,
    *,
    model: str,
    duration_sec: float,
    chunk_seconds: float,
) -> tuple[dict, int]:
    """Diarize a recording long enough to risk overrunning the model's own
    output budget (see diarize.py's looks_like_embedded_turns_dump -- and
    meeting 24, a real ~59 minute recording that failed exactly that way) by
    splitting it into pieces, diarizing each independently, and stitching the
    results back into one payload. See _diarize_stage for the duration
    threshold that decides whether this runs at all.
    """
    from app.services.diarize import diarize_file

    chunk_dir = Path(tempfile.mkdtemp(prefix="mmn-diar-chunks-"))
    try:
        chunk_paths = await asyncio.to_thread(
            audio_svc.split_into_chunks, path, chunk_dir, int(chunk_seconds)
        )
        total = len(chunk_paths)
        ctx.event(
            f"This recording is long enough to risk the diarizer's own output "
            f"limit, so it's being sent in {total} pieces of ~{chunk_seconds / 60:.0f} "
            "min each instead of one request.",
            stage="diarizing",
        )

        payloads_with_offsets: list[tuple[dict, float]] = []
        total_request_ms = 0
        offset = 0.0
        for i, chunk_path in enumerate(chunk_paths):
            ctx.check_cancelled()
            info = await asyncio.to_thread(audio_svc.probe, chunk_path)
            chunk_duration = info.duration_sec or chunk_seconds
            ctx.event(f"Diarizing part {i + 1} of {total}", stage="diarizing")

            payload, request_ms = await diarize_file(
                ctx,
                chunk_path,
                model=model,
                duration_sec=chunk_duration,
                progress_window=(i / total, (i + 1) / total),
            )
            payloads_with_offsets.append((payload, offset))
            total_request_ms += request_ms
            offset += chunk_duration

        return _stitch_chunk_payloads(payloads_with_offsets), total_request_ms
    finally:
        # These are scratch copies of already-stored audio, not anything the
        # meeting itself keeps -- clean up even if a chunk failed partway.
        shutil.rmtree(chunk_dir, ignore_errors=True)


def _combine_diarization_and_transcript(diar_payload: dict, asr_payload: dict) -> dict:
    """Merge a diarization-only payload (real speaker turns, no words -- see
    diarize.diarize_sync's ``expect_text=False``) with a transcription-only
    payload (real words, no speakers -- transcribe.transcribe_sync) into one
    payload shaped like a normal diarization response, by assigning each
    transcribed segment to whichever speaker turn overlaps it most.

    This is deliberately coarse -- segment-level overlap, not word-level --
    because that's the timestamp resolution both services actually offer;
    a transcribed segment that straddles a real speaker change will be
    attributed to whichever side of it is longer, same as any other
    diarization turn that happens to be a little too coarse.

    A transcribed segment with *no* overlapping speaker turn at all is
    dropped rather than kept unattributed. Confirmed on meeting 24: 48 of
    862 whisper-large-turbo-q8_0 segments fell in this bucket, and the ones
    inspected were hallucinated text ("Thank you." x4) during two minutes of
    real pre-meeting silence that pyannote correctly saw as nothing --
    exactly the shape "no diarization backs this at all" is a good, cheap
    proxy for. A real utterance that just happens to fall in a genuine gap
    between two turns is the false-positive cost of that heuristic; there is
    currently no cheaper way to tell the two apart.
    """
    turns = diar_payload.get("segments") or []

    def overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
        return max(0.0, min(a_end, b_end) - max(a_start, b_start))

    def speaker_for(start: float, end: float) -> str | None:
        best_speaker, best_overlap = None, 0.0
        for turn in turns:
            ov = overlap(start, end, turn.get("start") or 0, turn.get("end") or 0)
            if ov > best_overlap:
                best_overlap, best_speaker = ov, turn.get("speaker")
        return best_speaker

    segments = []
    next_id = 0
    for seg in asr_payload.get("segments") or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        start, end = seg.get("start") or 0, seg.get("end") or 0
        speaker = speaker_for(start, end)
        if speaker is None:
            continue
        segments.append(
            {"id": next_id, "speaker": speaker, "start": start, "end": end, "text": text}
        )
        next_id += 1

    speakers = diar_payload.get("speakers") or []
    return {
        "task": "diarize",
        "num_speakers": len(speakers),
        "segments": segments,
        "speakers": speakers,
        "duration": diar_payload.get("duration") or asr_payload.get("duration"),
        # A breadcrumb for whoever next looks at a raw_json blob, same idea
        # as _stitch_chunk_payloads's "chunked" -- not read by anything else.
        "diarize_only_source": True,
    }


async def _diarize_and_transcribe(
    ctx: JobContext,
    path: Path,
    *,
    diar_model: str,
    transcribe_model: str,
    duration_sec: float | None,
) -> tuple[dict, int]:
    """"Diarization only" mode -- get speaker turns from the diarization
    service (no text expected) and words from a separate transcription
    service, then combine them. Bypasses chunking entirely -- confirmed on
    meeting 24's full ~59 minute recording, both services handled it in one
    request each, in under three minutes combined, versus the ~11 minutes
    the chunked vibevoice-only path took on the same recording.
    """
    from app.services.diarize import diarize_file
    from app.services.transcribe import transcribe_file

    ctx.event("Diarizing (speaker turns only, no transcription expected)", stage="diarizing")
    diar_payload, diar_ms = await diarize_file(
        ctx, path, model=diar_model, duration_sec=duration_sec,
        expect_text=False, progress_window=(0.0, 0.5),
    )

    ctx.event("Transcribing", stage="diarizing")
    asr_payload, asr_ms = await transcribe_file(
        ctx, path, model=transcribe_model, duration_sec=duration_sec,
        progress_window=(0.5, 1.0),
    )

    return _combine_diarization_and_transcript(diar_payload, asr_payload), diar_ms + asr_ms


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
    """Re-run diarization only -- with a different model, or forced to redo
    with the same one (the "redo transcript" button)."""
    meeting_id = int(ctx.payload["meeting_id"])
    diar_id = await _diarize_stage(
        ctx,
        meeting_id,
        ctx.payload.get("model"),
        force=bool(ctx.payload.get("force", False)),
    )

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
