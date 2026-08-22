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

from app.db import get_conn, utcnow
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


def _diar_turn(speaker: str, start: float, end: float) -> dict:
    """A diarization-only segment: real speaker turn, no text (see
    diarize.diarize_sync's expect_text=False)."""
    return {"id": 0, "speaker": speaker, "start": start, "end": end, "text": ""}


def _asr_seg(start: float, end: float, text: str) -> dict:
    """A transcription-only segment: real words, no speaker."""
    return {"id": 0, "start": start, "end": end, "text": text}


class TestCombineDiarizationAndTranscript:
    """Pure function, no I/O -- see meeting 24's actual pyannote+whisper test
    run for where the numbers (48 of 862 unattributed) came from."""

    def test_assigns_the_speaker_with_the_most_overlap(self):
        diar = {"segments": [_diar_turn("SPEAKER_00", 0, 5), _diar_turn("SPEAKER_01", 5, 10)],
                "speakers": [{"id": "SPEAKER_00"}, {"id": "SPEAKER_01"}]}
        asr = {"segments": [_asr_seg(0, 4, "hello"), _asr_seg(6, 9, "hi there")]}

        merged = pipeline_mod._combine_diarization_and_transcript(diar, asr)

        assert [s["speaker"] for s in merged["segments"]] == ["SPEAKER_00", "SPEAKER_01"]
        assert [s["text"] for s in merged["segments"]] == ["hello", "hi there"]

    def test_a_segment_spanning_a_speaker_change_goes_to_whoever_has_more_of_it(self):
        diar = {"segments": [_diar_turn("SPEAKER_00", 0, 3), _diar_turn("SPEAKER_01", 3, 10)],
                "speakers": [{"id": "SPEAKER_00"}, {"id": "SPEAKER_01"}]}
        # 0..8: 3s with SPEAKER_00, 5s with SPEAKER_01 -- SPEAKER_01 wins.
        asr = {"segments": [_asr_seg(0, 8, "a long segment crossing the change")]}

        merged = pipeline_mod._combine_diarization_and_transcript(diar, asr)
        assert merged["segments"][0]["speaker"] == "SPEAKER_01"

    def test_a_segment_with_no_overlapping_turn_at_all_is_dropped(self):
        """The exact shape of meeting 24's whisper hallucinations during
        pre-meeting silence: pyannote correctly saw nothing there at all."""
        diar = {"segments": [_diar_turn("SPEAKER_00", 120, 130)],
                "speakers": [{"id": "SPEAKER_00"}]}
        asr = {"segments": [_asr_seg(0, 1, "Thank you."), _asr_seg(120, 125, "real speech")]}

        merged = pipeline_mod._combine_diarization_and_transcript(diar, asr)

        assert len(merged["segments"]) == 1
        assert merged["segments"][0]["text"] == "real speech"

    def test_a_blank_transcribed_segment_is_dropped_before_alignment(self):
        diar = {"segments": [_diar_turn("SPEAKER_00", 0, 5)], "speakers": [{"id": "SPEAKER_00"}]}
        asr = {"segments": [_asr_seg(0, 5, "   ")]}

        merged = pipeline_mod._combine_diarization_and_transcript(diar, asr)
        assert merged["segments"] == []

    def test_output_is_shaped_like_a_normal_diarization_payload(self):
        """_persist_diarization (and everything downstream of it -- render,
        speaker-merge) must not need any diarize-only-awareness."""
        diar = {"segments": [_diar_turn("SPEAKER_00", 0, 5)],
                "speakers": [{"id": "SPEAKER_00", "label": "0"}], "duration": 5.0}
        asr = {"segments": [_asr_seg(0, 5, "hello")]}

        merged = pipeline_mod._combine_diarization_and_transcript(diar, asr)

        assert set(merged) >= {"task", "num_speakers", "segments", "speakers"}
        assert merged["num_speakers"] == 1
        assert merged["speakers"] == diar["speakers"]
        assert merged["duration"] == 5.0

    def test_segment_ids_are_sequential(self):
        diar = {"segments": [_diar_turn("SPEAKER_00", 0, 10)], "speakers": [{"id": "SPEAKER_00"}]}
        asr = {"segments": [_asr_seg(0, 3, "a"), _asr_seg(3, 6, "b"), _asr_seg(6, 9, "c")]}

        merged = pipeline_mod._combine_diarization_and_transcript(diar, asr)
        assert [s["id"] for s in merged["segments"]] == [0, 1, 2]


class TestDiarizeAndTranscribe:
    @pytest.mark.asyncio
    async def test_calls_both_services_and_combines_their_output(
        self, seeded, initialised_db, monkeypatch
    ):
        _meeting(seeded, audio_path="/tmp/does-not-need-to-exist.wav", duration_sec=5.0)
        ctx = _diarize_job_context(seeded, initialised_db)
        ctx.stage("diarizing")

        diar_calls = []
        asr_calls = []

        async def fake_diarize_file(ctx, path, *, model, duration_sec=None,
                                     progress_window=(0.0, 1.0), expect_text=True):
            diar_calls.append({"model": model, "expect_text": expect_text, "window": progress_window})
            return {"segments": [_diar_turn("SPEAKER_00", 0, 5)],
                    "speakers": [{"id": "SPEAKER_00"}]}, 111

        async def fake_transcribe_file(ctx, path, *, model, duration_sec=None,
                                        progress_window=(0.0, 1.0)):
            asr_calls.append({"model": model, "window": progress_window})
            return {"segments": [_asr_seg(0, 5, "hello")]}, 222

        monkeypatch.setattr("app.services.diarize.diarize_file", fake_diarize_file)
        monkeypatch.setattr("app.services.transcribe.transcribe_file", fake_transcribe_file)

        payload, request_ms = await pipeline_mod._diarize_and_transcribe(
            ctx, Path("/tmp/does-not-need-to-exist.wav"),
            diar_model="pyannote/speaker-diarization-community-1",
            transcribe_model="whisper-large-turbo-q8_0",
            duration_sec=5.0,
        )

        assert diar_calls == [{
            "model": "pyannote/speaker-diarization-community-1",
            "expect_text": False,
            "window": (0.0, 0.5),
        }]
        assert asr_calls == [{"model": "whisper-large-turbo-q8_0", "window": (0.5, 1.0)}]
        assert request_ms == 333
        assert payload["segments"][0]["text"] == "hello"
        assert payload["segments"][0]["speaker"] == "SPEAKER_00"


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

    @pytest.mark.asyncio
    async def test_diarize_only_bypasses_chunking_even_past_the_threshold(
        self, seeded, initialised_db, monkeypatch
    ):
        # A long recording that would otherwise chunk must not, once
        # diarize_only is on -- confirmed on meeting 24's full ~59 minute
        # recording, both services handled it in one request each.
        monkeypatch.setenv("MMN_DIARIZE_CHUNK_THRESHOLD_SEC", "100")
        monkeypatch.setenv("MMN_DIARIZE_FAKE", "false")
        monkeypatch.setenv("MMN_DIARIZE_ONLY", "true")
        monkeypatch.setenv("MMN_TRANSCRIBE_MODEL", "whisper-large-turbo-q8_0")
        from app.config import reset_settings_cache
        reset_settings_cache()

        _meeting(seeded, audio_path="/tmp/does-not-need-to-exist.wav", duration_sec=200.0)

        chunked_called = False
        combo_called_with = {}

        async def fake_chunked(*a, **kw):
            nonlocal chunked_called
            chunked_called = True
            return {"segments": [], "speakers": [], "num_speakers": 0}, 0

        async def fake_combo(ctx, path, *, diar_model, transcribe_model, duration_sec):
            combo_called_with.update(
                diar_model=diar_model, transcribe_model=transcribe_model, duration_sec=duration_sec
            )
            return {"segments": [], "speakers": [], "num_speakers": 0}, 0

        monkeypatch.setattr(pipeline_mod, "_diarize_in_chunks", fake_chunked)
        monkeypatch.setattr(pipeline_mod, "_diarize_and_transcribe", fake_combo)

        ctx = _diarize_job_context(seeded, initialised_db, payload={"meeting_id": 1})
        await pipeline_mod._diarize_stage(ctx, 1, force=True)

        assert chunked_called is False
        assert combo_called_with["transcribe_model"] == "whisper-large-turbo-q8_0"
        assert combo_called_with["duration_sec"] == 200.0

    @pytest.mark.asyncio
    async def test_fake_mode_wins_over_diarize_only_too(
        self, seeded, initialised_db, monkeypatch
    ):
        # Same reasoning as fake-mode-vs-chunking: fake mode replaces the
        # whole request-to-a-model step, so every existing fake-diarization
        # test must keep exercising the single-call path regardless of this
        # setting's default.
        monkeypatch.setenv("MMN_DIARIZE_FAKE", "true")
        monkeypatch.setenv("MMN_DIARIZE_FAKE_DELAY_SEC", "0.01")
        monkeypatch.setenv("MMN_DIARIZE_ONLY", "true")
        from app.config import reset_settings_cache
        reset_settings_cache()

        _meeting(seeded, audio_path="/tmp/does-not-need-to-exist.wav", duration_sec=200.0)

        combo_called = False

        async def fake_combo(*a, **kw):
            nonlocal combo_called
            combo_called = True
            return {"segments": [], "speakers": [], "num_speakers": 0}, 0

        monkeypatch.setattr(pipeline_mod, "_diarize_and_transcribe", fake_combo)

        ctx = _diarize_job_context(seeded, initialised_db, payload={"meeting_id": 1})
        await pipeline_mod._diarize_stage(ctx, 1, force=True)

        assert combo_called is False

    @pytest.mark.asyncio
    async def test_diarize_only_and_plain_runs_are_checkpointed_separately(
        self, seeded, initialised_db, monkeypatch
    ):
        """Flipping "Diarization only" on or off must not make a stale
        same-model diarization from the other mode look reusable -- they are
        materially different results."""
        monkeypatch.setenv("MMN_DIARIZE_FAKE", "false")
        monkeypatch.setenv("MMN_DIARIZATION_MODEL", "vibevoice-cpp-asr")
        monkeypatch.setenv("MMN_TRANSCRIBE_MODEL", "whisper-large-turbo-q8_0")
        from app.config import reset_settings_cache
        reset_settings_cache()

        _meeting(seeded, audio_path="/tmp/does-not-need-to-exist.wav", duration_sec=10.0)

        async def fake_diarize_file(ctx, path, *, model, duration_sec=None,
                                     progress_window=(0.0, 1.0), expect_text=True):
            return {"segments": [{"id": 0, "speaker": "SPEAKER_00", "start": 0, "end": 1,
                                   "text": "" if not expect_text else "hi"}],
                    "speakers": [{"id": "SPEAKER_00"}], "num_speakers": 1}, 0

        async def fake_transcribe_file(ctx, path, *, model, duration_sec=None,
                                        progress_window=(0.0, 1.0)):
            return {"segments": [{"id": 0, "start": 0, "end": 1, "text": "hi"}]}, 0

        monkeypatch.setattr("app.services.diarize.diarize_file", fake_diarize_file)
        monkeypatch.setattr("app.services.transcribe.transcribe_file", fake_transcribe_file)

        # First a plain run (diarize_only off, force=True so nothing skips).
        monkeypatch.setenv("MMN_DIARIZE_ONLY", "false")
        reset_settings_cache()
        ctx1 = _diarize_job_context(seeded, initialised_db, payload={"meeting_id": 1})
        plain_id = await pipeline_mod._diarize_stage(ctx1, 1, force=True)

        # Then a diarize_only run, without forcing -- it must NOT see the
        # plain run's row as "already exists for this model" and skip.
        monkeypatch.setenv("MMN_DIARIZE_ONLY", "true")
        reset_settings_cache()
        ctx2 = _diarize_job_context(seeded, initialised_db, payload={"meeting_id": 1})
        combo_id = await pipeline_mod._diarize_stage(ctx2, 1, force=False)

        assert combo_id != plain_id

        with get_conn(initialised_db) as c:
            models = {
                r["id"]: r["model"]
                for r in c.execute("SELECT id, model FROM diarizations WHERE meeting_id = 1")
            }
        assert models[plain_id] == "vibevoice-cpp-asr"
        assert models[combo_id] == "vibevoice-cpp-asr+whisper-large-turbo-q8_0"
