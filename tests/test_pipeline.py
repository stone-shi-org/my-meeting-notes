"""Diarizing a recording too long for one request: splitting, stitching, and
the duration threshold that decides whether any of this runs at all.

Meeting 24 (a real ~59 minute recording) is the motivating case: it came back
from vibevoice-cpp-asr as one degenerate segment holding a truncated JSON
dump instead of real turns, because the model has an output-token budget, not
a duration budget, and a long enough / talkative enough recording can exceed
it. diarize.py's looks_like_embedded_turns_dump now catches that shape and
fails the job loudly -- this file covers the actual prevention: chunk a long
recording into pieces small enough to stay under that budget, diarize each
independently, and stitch the results back into one payload.
"""

from __future__ import annotations

import subprocess
import shutil
from pathlib import Path

import pytest

from app.db import utcnow
from app.jobs import queue as queue_mod
from app.jobs.queue import JobContext
from app.services import pipeline as pipeline_mod


@pytest.fixture
def seeded(conn):
    """A user and thread to hang a meeting and a diarize job off."""
    now = utcnow()
    conn.execute(
        "INSERT INTO users (id, username, password_hash, password_salt, created_at, updated_at) "
        "VALUES (1, 'u', 'h', 's', ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO threads (id, owner_id, title, created_at, updated_at) "
        "VALUES (1, 1, 'T', ?, ?)",
        (now, now),
    )
    conn.commit()
    return conn


def _meeting(conn, *, audio_path, duration_sec):
    now = utcnow()
    conn.execute(
        "INSERT INTO meetings (id, thread_id, owner_id, title, audio_path, "
        "audio_duration_sec, created_at, updated_at) "
        "VALUES (1, 1, 1, 'M', ?, ?, ?, ?)",
        (audio_path, duration_sec, now, now),
    )
    conn.commit()


def _diarize_job_context(conn, db_path, *, payload=None) -> JobContext:
    """A real `jobs` row plus the JobContext for it -- job_events has a
    foreign key onto jobs.id, so ctx.stage()/.event() calls need a row to
    point at, the same way test_jobs.py's `_job()` helper provides one."""
    job_id = queue_mod.create_job(
        conn, job_type="diarize", user_id=1, meeting_id=1, thread_id=1,
        payload=payload or {},
    )
    conn.commit()
    return JobContext(job_id, "diarize", payload or {}, db_path=db_path)


# --------------------------------------------------------------------------- #
# _stitch_chunk_payloads -- pure, no I/O
# --------------------------------------------------------------------------- #


def _chunk_payload(*, speaker_ids: list[str], text_prefix: str) -> dict:
    return {
        "segments": [
            {"id": i, "speaker": sid, "start": 0.0, "end": 1.0, "text": f"{text_prefix}{i}"}
            for i, sid in enumerate(speaker_ids)
        ],
        "speakers": [
            {"id": sid, "label": sid, "total_speech_duration": 1.0, "segment_count": 1}
            for sid in dict.fromkeys(speaker_ids)  # de-dup within the chunk, order preserved
        ],
    }


class TestStitchChunkPayloads:
    def test_namespaces_speaker_and_segment_ids_per_chunk(self):
        chunk0 = _chunk_payload(speaker_ids=["SPEAKER_00", "SPEAKER_01"], text_prefix="a")
        chunk1 = _chunk_payload(speaker_ids=["SPEAKER_00"], text_prefix="b")

        merged = pipeline_mod._stitch_chunk_payloads([(chunk0, 0.0), (chunk1, 100.0)])

        speakers = {s["id"] for s in merged["speakers"]}
        assert speakers == {"c0:SPEAKER_00", "c0:SPEAKER_01", "c1:SPEAKER_00"}
        assert {s["speaker"] for s in merged["segments"]} == speakers

    def test_never_merges_the_same_label_across_chunks(self):
        # The whole point: chunk 0's SPEAKER_00 and chunk 1's SPEAKER_00 are
        # not guaranteed to be the same physical person, so this must not
        # collapse them into one speakers entry.
        chunk0 = _chunk_payload(speaker_ids=["SPEAKER_00"], text_prefix="a")
        chunk1 = _chunk_payload(speaker_ids=["SPEAKER_00"], text_prefix="b")

        merged = pipeline_mod._stitch_chunk_payloads([(chunk0, 0.0), (chunk1, 500.0)])

        assert merged["num_speakers"] == 2
        assert len(merged["speakers"]) == 2

    def test_offsets_segment_times_onto_the_full_recording_clock(self):
        chunk0 = _chunk_payload(speaker_ids=["SPEAKER_00"], text_prefix="a")
        chunk1 = _chunk_payload(speaker_ids=["SPEAKER_00"], text_prefix="b")

        merged = pipeline_mod._stitch_chunk_payloads([(chunk0, 0.0), (chunk1, 1500.0)])

        by_chunk = sorted(merged["segments"], key=lambda s: s["start"])
        assert by_chunk[0]["start"] == 0.0 and by_chunk[0]["end"] == 1.0
        assert by_chunk[1]["start"] == 1500.0 and by_chunk[1]["end"] == 1501.0

    def test_segment_ids_are_unique_and_sequential_across_chunks(self):
        chunk0 = _chunk_payload(speaker_ids=["SPEAKER_00", "SPEAKER_01"], text_prefix="a")
        chunk1 = _chunk_payload(speaker_ids=["SPEAKER_00"], text_prefix="b")

        merged = pipeline_mod._stitch_chunk_payloads([(chunk0, 0.0), (chunk1, 100.0)])

        assert [s["id"] for s in merged["segments"]] == [0, 1, 2]

    def test_marks_itself_as_chunked_for_whoever_reads_the_raw_payload_later(self):
        chunk0 = _chunk_payload(speaker_ids=["SPEAKER_00"], text_prefix="a")
        merged = pipeline_mod._stitch_chunk_payloads([(chunk0, 0.0)])
        assert merged["chunked"] is True
        assert merged["chunk_count"] == 1

    def test_records_each_chunks_start_offset_for_the_frontend_divider(self):
        chunk0 = _chunk_payload(speaker_ids=["SPEAKER_00"], text_prefix="a")
        chunk1 = _chunk_payload(speaker_ids=["SPEAKER_00"], text_prefix="b")
        chunk2 = _chunk_payload(speaker_ids=["SPEAKER_00"], text_prefix="c")

        merged = pipeline_mod._stitch_chunk_payloads(
            [(chunk0, 0.0), (chunk1, 1500.0), (chunk2, 2980.5)]
        )

        # Time-based, not derived from speaker/segment ids -- a merge changes
        # what a segment's *speaker* id resolves to, never where it sits on
        # the recording's own clock.
        assert merged["chunk_boundaries"] == [0.0, 1500.0, 2980.5]


# --------------------------------------------------------------------------- #
# _diarize_in_chunks -- real ffmpeg split, faked diarize_file
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
class TestDiarizeInChunks:
    @pytest.fixture
    def five_second_wav(self, tmp_path) -> Path:
        path = tmp_path / "five_seconds.wav"
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-y",
                "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                "-t", "5", "-c:a", "pcm_s16le",
                str(path),
            ],
            capture_output=True, check=True,
        )
        return path

    @pytest.mark.asyncio
    async def test_splits_diarizes_each_piece_and_stitches(
        self, seeded, initialised_db, five_second_wav, monkeypatch
    ):
        _meeting(seeded, audio_path=str(five_second_wav), duration_sec=5.0)
        ctx = _diarize_job_context(seeded, initialised_db)
        ctx.stage("diarizing")

        calls: list[dict] = []

        async def fake_diarize_file(ctx, path, *, model, duration_sec=None, progress_window=(0.0, 1.0)):
            calls.append({"path": path, "duration_sec": duration_sec, "window": progress_window})
            i = len(calls) - 1
            return _chunk_payload(speaker_ids=["SPEAKER_00"], text_prefix=f"chunk{i}-"), 100

        monkeypatch.setattr("app.services.diarize.diarize_file", fake_diarize_file)

        payload, request_ms = await pipeline_mod._diarize_in_chunks(
            ctx, five_second_wav, model="vibevoice-cpp-asr", duration_sec=5.0, chunk_seconds=2,
        )

        # 2s + 2s + 1s.
        assert len(calls) == 3
        assert request_ms == 300
        assert len(payload["segments"]) == 3
        assert payload["chunked"] is True
        assert payload["chunk_count"] == 3

    @pytest.mark.asyncio
    async def test_progress_windows_are_contiguous_and_non_overlapping(
        self, seeded, initialised_db, five_second_wav, monkeypatch
    ):
        _meeting(seeded, audio_path=str(five_second_wav), duration_sec=5.0)
        ctx = _diarize_job_context(seeded, initialised_db)
        ctx.stage("diarizing")

        windows: list[tuple[float, float]] = []

        async def fake_diarize_file(ctx, path, *, model, duration_sec=None, progress_window=(0.0, 1.0)):
            windows.append(progress_window)
            return _chunk_payload(speaker_ids=["SPEAKER_00"], text_prefix="x"), 0

        monkeypatch.setattr("app.services.diarize.diarize_file", fake_diarize_file)

        await pipeline_mod._diarize_in_chunks(
            ctx, five_second_wav, model="m", duration_sec=5.0, chunk_seconds=2,
        )

        assert windows[0][0] == pytest.approx(0.0)
        for (_, end), (start, _) in zip(windows, windows[1:]):
            assert end == pytest.approx(start)
        assert windows[-1][1] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_cleans_up_the_scratch_chunk_directory(
        self, seeded, initialised_db, five_second_wav, monkeypatch
    ):
        _meeting(seeded, audio_path=str(five_second_wav), duration_sec=5.0)
        ctx = _diarize_job_context(seeded, initialised_db)
        ctx.stage("diarizing")
        seen_dirs: list[Path] = []

        async def fake_diarize_file(ctx, path, *, model, duration_sec=None, progress_window=(0.0, 1.0)):
            seen_dirs.append(path.parent)
            return _chunk_payload(speaker_ids=["SPEAKER_00"], text_prefix="x"), 0

        monkeypatch.setattr("app.services.diarize.diarize_file", fake_diarize_file)

        await pipeline_mod._diarize_in_chunks(
            ctx, five_second_wav, model="m", duration_sec=5.0, chunk_seconds=2,
        )

        assert not seen_dirs[0].exists()

    @pytest.mark.asyncio
    async def test_cleans_up_even_when_a_chunk_fails(
        self, seeded, initialised_db, five_second_wav, monkeypatch
    ):
        _meeting(seeded, audio_path=str(five_second_wav), duration_sec=5.0)
        ctx = _diarize_job_context(seeded, initialised_db)
        ctx.stage("diarizing")
        seen_dirs: list[Path] = []

        async def boom(ctx, path, *, model, duration_sec=None, progress_window=(0.0, 1.0)):
            seen_dirs.append(path.parent)
            raise RuntimeError("diarizer exploded")

        monkeypatch.setattr("app.services.diarize.diarize_file", boom)

        with pytest.raises(RuntimeError, match="exploded"):
            await pipeline_mod._diarize_in_chunks(
                ctx, five_second_wav, model="m", duration_sec=5.0, chunk_seconds=2,
            )

        assert not seen_dirs[0].exists()


# --------------------------------------------------------------------------- #
# _diarize_stage -- the duration threshold that decides chunked vs. not
# --------------------------------------------------------------------------- #


class TestDiarizeStageChunkingDecision:
    @pytest.mark.asyncio
    async def test_a_long_recording_goes_through_chunking(self, seeded, initialised_db, monkeypatch):
        monkeypatch.setenv("MMN_DIARIZE_CHUNK_THRESHOLD_SEC", "100")
        monkeypatch.setenv("MMN_DIARIZE_FAKE", "false")
        from app.config import reset_settings_cache
        reset_settings_cache()

        _meeting(seeded, audio_path="/tmp/does-not-need-to-exist.wav", duration_sec=200.0)

        called_with = {}

        async def fake_chunked(ctx, path, *, model, duration_sec, chunk_seconds):
            called_with["chunk_seconds"] = chunk_seconds
            return {"segments": [], "speakers": [], "num_speakers": 0}, 0

        monkeypatch.setattr(pipeline_mod, "_diarize_in_chunks", fake_chunked)

        ctx = _diarize_job_context(seeded, initialised_db, payload={"meeting_id": 1})
        await pipeline_mod._diarize_stage(ctx, 1, force=True)

        assert "chunk_seconds" in called_with

    @pytest.mark.asyncio
    async def test_a_short_recording_skips_chunking(self, seeded, initialised_db, monkeypatch):
        monkeypatch.setenv("MMN_DIARIZE_CHUNK_THRESHOLD_SEC", "1000")
        monkeypatch.setenv("MMN_DIARIZE_FAKE", "false")
        from app.config import reset_settings_cache
        reset_settings_cache()

        _meeting(seeded, audio_path="/tmp/does-not-need-to-exist.wav", duration_sec=200.0)

        chunked_called = False

        async def fake_chunked(*a, **kw):
            nonlocal chunked_called
            chunked_called = True
            return {"segments": [], "speakers": [], "num_speakers": 0}, 0

        async def fake_diarize_file(ctx, path, *, model, duration_sec=None, progress_window=(0.0, 1.0)):
            return {"segments": [{"id": 0, "speaker": "SPEAKER_00", "start": 0, "end": 1, "text": "hi"}],
                    "speakers": [{"id": "SPEAKER_00"}], "num_speakers": 1}, 0

        monkeypatch.setattr(pipeline_mod, "_diarize_in_chunks", fake_chunked)
        monkeypatch.setattr("app.services.diarize.diarize_file", fake_diarize_file)

        ctx = _diarize_job_context(seeded, initialised_db, payload={"meeting_id": 1})
        await pipeline_mod._diarize_stage(ctx, 1, force=True)

        assert chunked_called is False

    @pytest.mark.asyncio
    async def test_fake_mode_never_chunks_even_past_the_threshold(
        self, seeded, initialised_db, monkeypatch
    ):
        # Fake mode replaces the whole request-to-a-model step -- there is no
        # real output budget to overrun, and every existing test that relies
        # on MMN_DIARIZE_FAKE must keep exercising the single-call path.
        monkeypatch.setenv("MMN_DIARIZE_CHUNK_THRESHOLD_SEC", "100")
        monkeypatch.setenv("MMN_DIARIZE_FAKE", "true")
        monkeypatch.setenv("MMN_DIARIZE_FAKE_DELAY_SEC", "0.01")
        from app.config import reset_settings_cache
        reset_settings_cache()

        _meeting(seeded, audio_path="/tmp/does-not-need-to-exist.wav", duration_sec=200.0)

        chunked_called = False

        async def fake_chunked(*a, **kw):
            nonlocal chunked_called
            chunked_called = True
            return {"segments": [], "speakers": [], "num_speakers": 0}, 0

        monkeypatch.setattr(pipeline_mod, "_diarize_in_chunks", fake_chunked)

        ctx = _diarize_job_context(seeded, initialised_db, payload={"meeting_id": 1})
        await pipeline_mod._diarize_stage(ctx, 1, force=True)

        assert chunked_called is False
