"""Transcript rendering against the real captured diarization payload."""

from __future__ import annotations

import pytest

from app.errors import ValidationError
from app.services import transcript as ts


class FakeRow(dict):
    """Stands in for a sqlite3.Row in the pure-rendering tests."""

    def __getitem__(self, key):
        return self.get(key)


def mapping(**names) -> dict:
    return {
        sid: FakeRow(speaker_id=sid, display_name=name, color=None)
        for sid, name in names.items()
    }


class TestNonSpeech:
    def test_matches_the_marker_the_service_emits(self):
        assert ts.is_non_speech("[Environmental Sounds]") is True

    @pytest.mark.parametrize(
        "text", ["[Silence]", "[MUSIC]", "  [laughter]  ", "[Inaudible]"]
    )
    def test_matches_the_other_known_markers(self, text):
        assert ts.is_non_speech(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "Yes, this is Stan speaking.",
            "",
            "So then [laughter] we shipped it",  # bracketed aside inside speech
            "[Q3 Planning]",  # bracketed, but not a known marker
        ],
    )
    def test_leaves_speech_alone(self, text):
        assert ts.is_non_speech(text) is False


class TestClockFormatting:
    @pytest.mark.parametrize(
        "seconds,expected",
        [(0, "0:00"), (41.9, "0:41"), (1334, "22:14"), (3600, "1:00:00"), (3787, "1:03:07")],
    )
    def test_fmt_clock(self, seconds, expected):
        assert ts.fmt_clock(seconds) == expected

    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "00:00:00.000"),
            (1.659999966621399, "00:00:01.660"),
            (1352.489990234375, "00:22:32.490"),
            (3787.5, "01:03:07.500"),
        ],
    )
    def test_fmt_vtt_always_includes_hours(self, seconds, expected):
        assert ts.fmt_vtt(seconds) == expected


class TestBuildTranscript:
    def test_unmapped_speakers_keep_their_raw_ids(self, sample_diarization):
        out = ts.build_transcript(sample_diarization, {})
        assert out["segments"][0]["speaker_name"] == "SPEAKER_00"
        assert out["segments"][1]["speaker_name"] == "SPEAKER_01"

    def test_mapped_speakers_get_their_names(self, sample_diarization):
        out = ts.build_transcript(
            sample_diarization, mapping(SPEAKER_00="Stan", SPEAKER_01="Donna")
        )
        names = {s["speaker_name"] for s in out["segments"]}
        assert names == {"Stan", "Donna"}
        # The underlying id is preserved for colour assignment and the LLM.
        assert out["segments"][0]["speaker"] == "SPEAKER_00"

    def test_a_partial_mapping_leaves_the_rest_raw(self, sample_diarization):
        out = ts.build_transcript(sample_diarization, mapping(SPEAKER_00="Stan"))
        names = {s["speaker_name"] for s in out["segments"]}
        assert names == {"Stan", "SPEAKER_01"}

    def test_apply_names_false_ignores_the_mapping(self, sample_diarization):
        out = ts.build_transcript(
            sample_diarization, mapping(SPEAKER_00="Stan"), apply_names=False
        )
        assert all(s["speaker_name"].startswith("SPEAKER_") for s in out["segments"])

    def test_non_speech_is_flagged_but_kept_by_default(self, sample_diarization):
        out = ts.build_transcript(sample_diarization, {})
        assert len(out["segments"]) == 79
        # The sample carries three: [Music] twice and a trailing
        # [Environmental Sounds].
        assert sum(s["non_speech"] for s in out["segments"]) == 3
        assert out["segments"][-1]["non_speech"] is True
        assert out["segments"][0]["non_speech"] is False

    def test_non_speech_can_be_filtered_out(self, sample_diarization):
        out = ts.build_transcript(sample_diarization, {}, include_nonspeech=False)
        assert len(out["segments"]) == 76
        assert all(s["non_speech"] is False for s in out["segments"])

    def test_top_level_metadata_survives(self, sample_diarization):
        out = ts.build_transcript(sample_diarization, {})
        assert out["num_speakers"] == 2
        assert out["duration"] == pytest.approx(1352.49, abs=0.01)
        assert len(out["speakers"]) == 2

    def test_speaker_entries_carry_talk_time(self, sample_diarization):
        out = ts.build_transcript(sample_diarization, mapping(SPEAKER_00="Stan"))
        first = out["speakers"][0]
        assert first["id"] == "SPEAKER_00"
        assert first["display_name"] == "Stan"
        assert first["total_speech_duration"] > 0
        assert first["segment_count"] > 0

    def test_an_empty_payload_does_not_explode(self):
        out = ts.build_transcript({}, {})
        assert out["segments"] == []
        assert out["speakers"] == []

    def test_ordinary_payload_has_no_chunk_boundaries(self, sample_diarization):
        out = ts.build_transcript(sample_diarization, {})
        assert out["chunk_boundaries"] == []

    def test_a_chunked_payload_passes_its_boundaries_through_verbatim(self, sample_diarization):
        chunked = {**sample_diarization, "chunk_boundaries": [0.0, 1500.0, 3000.0]}
        out = ts.build_transcript(chunked, {})
        assert out["chunk_boundaries"] == [0.0, 1500.0, 3000.0]


class TestRendering:
    @pytest.fixture
    def transcript(self, sample_diarization):
        return ts.build_transcript(
            sample_diarization, mapping(SPEAKER_00="Stan", SPEAKER_01="Donna")
        )

    def test_text_has_a_timestamped_line_per_segment(self, transcript):
        out = ts.render_text(transcript)
        lines = out.splitlines()
        assert len(lines) == 79
        assert lines[0].startswith("[0:00] Stan:")
        assert "this is Stan speaking" in lines[0]

    def test_markdown_groups_consecutive_turns_by_speaker(self, transcript):
        out = ts.render_markdown(transcript)
        assert out.startswith("# Transcript")
        assert "**Stan**" in out
        assert "**Donna**" in out
        # Header only when the speaker changes, not once per segment.
        assert out.count("**Stan**") < 79

    def test_vtt_is_well_formed(self, transcript):
        out = ts.render_vtt(transcript)
        lines = out.splitlines()
        assert lines[0] == "WEBVTT"
        assert lines[2] == "1"
        assert "-->" in lines[3]
        assert lines[4].startswith("<v Stan>")

    def test_vtt_timestamps_are_hh_mm_ss_mmm_past_twenty_minutes(self, transcript):
        out = ts.render_vtt(transcript)
        assert "00:22:" in out  # the sample runs to 22:32

    def test_json_format_returns_the_dict(self, transcript):
        assert ts.render(transcript, "json") is transcript

    def test_unknown_format_is_rejected(self, transcript):
        with pytest.raises(ValidationError, match="Unknown format"):
            ts.render(transcript, "docx")


class TestFingerprint:
    def test_is_stable_for_identical_input(self, sample_diarization):
        a = ts.build_transcript(sample_diarization, {})
        b = ts.build_transcript(sample_diarization, {})
        assert ts.transcript_sha256(a) == ts.transcript_sha256(b)

    def test_changes_when_a_speaker_is_renamed(self, sample_diarization):
        """A rename changes what the LLM sees, so the summary is stale."""
        raw = ts.build_transcript(sample_diarization, {})
        named = ts.build_transcript(sample_diarization, mapping(SPEAKER_00="Stan"))
        assert ts.transcript_sha256(raw) != ts.transcript_sha256(named)


class TestSpeakerStats:
    def test_shares_sum_to_one_and_sort_descending(self, sample_diarization):
        transcript = ts.build_transcript(sample_diarization, {})
        stats = ts.speaker_stats(transcript)

        assert sum(s["share"] for s in stats) == pytest.approx(1.0)
        assert stats[0]["total_speech_duration"] >= stats[1]["total_speech_duration"]
        assert stats[0]["duration_human"]

    def test_no_speakers_does_not_divide_by_zero(self):
        assert ts.speaker_stats({"speakers": [], "segments": []}) == []
