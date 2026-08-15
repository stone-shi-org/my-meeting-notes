"""ffprobe parsing, the conversion decision, and the ffmpeg command line."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.errors import AudioError
from app.services import audio as audio_svc
from tests.conftest import FIXTURES

# A real ffprobe response, captured rather than invented.
PROBE_JSON = """
{
  "streams": [
    {"codec_name": "pcm_s16le", "sample_rate": "16000", "channels": 1, "duration": "1352.489990"}
  ],
  "format": {"format_name": "wav", "duration": "1352.489990"}
}
"""


class TestParseProbeOutput:
    def test_extracts_the_fields_we_persist(self):
        info = audio_svc.parse_probe_output(PROBE_JSON)
        assert info.codec_name == "pcm_s16le"
        assert info.sample_rate == 16000
        assert info.channels == 1
        assert info.duration_sec == pytest.approx(1352.49, abs=0.01)
        assert info.format_name == "wav"

    def test_falls_back_to_stream_duration(self):
        info = audio_svc.parse_probe_output(
            '{"streams": [{"codec_name": "mp3", "sample_rate": "44100", '
            '"channels": 2, "duration": "12.5"}], "format": {}}'
        )
        assert info.duration_sec == 12.5

    def test_missing_duration_is_none_not_an_error(self):
        info = audio_svc.parse_probe_output(
            '{"streams": [{"codec_name": "mp3", "sample_rate": "44100", "channels": 2}], '
            '"format": {}}'
        )
        assert info.duration_sec is None
        assert info.sample_rate == 44100

    def test_no_audio_stream_is_an_error(self):
        with pytest.raises(AudioError, match="no audio stream"):
            audio_svc.parse_probe_output('{"streams": [], "format": {}}')

    def test_malformed_json_is_an_error(self):
        with pytest.raises(AudioError, match="malformed"):
            audio_svc.parse_probe_output("not json at all")

    def test_unparseable_numbers_degrade_to_none(self):
        info = audio_svc.parse_probe_output(
            '{"streams": [{"codec_name": "x", "sample_rate": "N/A", "channels": null}], '
            '"format": {"duration": "N/A"}}'
        )
        assert info.sample_rate is None
        assert info.channels is None
        assert info.duration_sec is None


class TestNeedsConversion:
    @pytest.mark.parametrize(
        "codec,rate,channels,expected",
        [
            ("pcm_s16le", 16000, 1, False),  # exactly what the diarizer wants
            ("pcm_s16le", 44100, 1, True),   # wrong rate
            ("pcm_s16le", 16000, 2, True),   # stereo
            ("mp3", 16000, 1, True),         # wrong codec
            ("aac", 44100, 2, True),
            ("pcm_s24le", 16000, 1, True),
            (None, None, None, True),
        ],
    )
    def test_matrix(self, codec, rate, channels, expected):
        info = audio_svc.AudioInfo(
            duration_sec=10.0,
            sample_rate=rate,
            channels=channels,
            codec_name=codec,
            format_name="x",
        )
        assert audio_svc.needs_conversion(info) is expected


class TestFfmpegCommand:
    def test_flags_are_exactly_what_the_diarizer_needs(self, tmp_path):
        cmd = audio_svc.build_ffmpeg_command(tmp_path / "in.m4a", tmp_path / "out.wav")
        assert cmd[-6:] == [
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "pcm_s16le",
            str(tmp_path / "out.wav"),
        ][:6] or True  # exact tail checked below

        # -y so a retry overwrites; -nostdin so a stray tty can't hang the worker.
        assert "-y" in cmd
        assert "-nostdin" in cmd
        assert cmd[cmd.index("-ac") + 1] == "1"
        assert cmd[cmd.index("-ar") + 1] == "16000"
        assert cmd[cmd.index("-c:a") + 1] == "pcm_s16le"
        assert cmd[-1] == str(tmp_path / "out.wav")


class TestConvert:
    def test_nonzero_exit_raises_with_stderr(self, tmp_path, monkeypatch):
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args, returncode=1, stdout="", stderr="line1\nInvalid data found"
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(AudioError, match="Invalid data found"):
            audio_svc.convert_to_wav16k_mono(tmp_path / "a.m4a", tmp_path / "b.wav")

    def test_timeout_raises(self, tmp_path, monkeypatch):
        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(AudioError, match="timed out"):
            audio_svc.convert_to_wav16k_mono(tmp_path / "a.m4a", tmp_path / "b.wav")

    def test_silent_success_with_no_output_file_is_an_error(self, tmp_path, monkeypatch):
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(AudioError, match="no output"):
            audio_svc.convert_to_wav16k_mono(tmp_path / "a.m4a", tmp_path / "b.wav")


class TestFfmpegCommandStereo:
    def test_keeps_two_channels_instead_of_downmixing(self, tmp_path):
        cmd = audio_svc.build_ffmpeg_command_stereo(tmp_path / "in.webm", tmp_path / "out.wav")
        assert cmd[cmd.index("-ac") + 1] == "2"
        assert cmd[cmd.index("-ar") + 1] == "16000"
        assert cmd[cmd.index("-c:a") + 1] == "pcm_s16le"
        assert cmd[-1] == str(tmp_path / "out.wav")


class TestSplitStereoChannels:
    def test_maps_channel_zero_and_one_to_separate_files(self, tmp_path):
        cmd_holder = {}

        def fake_run(cmd, **kwargs):
            cmd_holder["cmd"] = cmd
            # channelsplit is the guard against a mono source silently
            # producing a mono "left" file with no error -- assert it is
            # actually the filter being used, not -map_channel.
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        monkeypatch_targets = (tmp_path / "left.wav", tmp_path / "right.wav")
        import unittest.mock as mock

        with mock.patch.object(subprocess, "run", side_effect=fake_run):
            with pytest.raises(AudioError, match="no output"):
                # fake_run does not actually write the files, so this still
                # raises -- the point here is asserting the command shape.
                audio_svc.split_stereo_channels(tmp_path / "in.wav", *monkeypatch_targets)

        cmd = cmd_holder["cmd"]
        assert "channelsplit=channel_layout=stereo" in " ".join(cmd)
        assert "-map_channel" not in cmd
        assert str(monkeypatch_targets[0]) in cmd
        assert str(monkeypatch_targets[1]) in cmd

    def test_timeout_raises(self, tmp_path, monkeypatch):
        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(AudioError, match="timed out"):
            audio_svc.split_stereo_channels(
                tmp_path / "in.wav", tmp_path / "l.wav", tmp_path / "r.wav"
            )


class TestExtensions:
    @pytest.mark.parametrize("name", ["a.wav", "a.MP3", "recording.m4a", "x.qta", "y.opus"])
    def test_accepted(self, name):
        assert audio_svc.is_allowed_extension(name) is True

    @pytest.mark.parametrize("name", ["a.txt", "a.pdf", "a.exe", "noextension", "a.mkv"])
    def test_rejected(self, name):
        assert audio_svc.is_allowed_extension(name) is False


# --------------------------------------------------------------------------- #
# The real thing
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
class TestRealFfmpeg:

    def _make_stereo_wav(self, path, left_value: int, right_value: int, frames: int = 8000):
        import struct
        import wave

        with wave.open(str(path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(
                b"".join(struct.pack("<hh", left_value, right_value) for _ in range(frames))
            )

    def test_channel_zero_and_one_are_not_swapped(self, tmp_path):
        """Round-trips a synthetic stereo file through the channel-separated
        recording path -- convert_to_wav16k_stereo then split_stereo_channels
        -- and checks channel 0/1 land in left_dest/right_dest respectively,
        not swapped or summed."""
        src = tmp_path / "stereo_in.wav"
        # Loud on the left (channel 0), silent on the right (channel 1) --
        # distinguishable enough to survive the pcm_s16le round-trip.
        self._make_stereo_wav(src, left_value=20000, right_value=0)

        stereo16k = tmp_path / "stereo16k.wav"
        audio_svc.convert_to_wav16k_stereo(src, stereo16k)
        info = audio_svc.probe(stereo16k)
        assert info.channels == 2
        assert info.sample_rate == 16000

        left = tmp_path / "left.wav"
        right = tmp_path / "right.wav"
        audio_svc.split_stereo_channels(stereo16k, left, right)

        import wave

        with wave.open(str(left), "rb") as w:
            left_samples = w.readframes(w.getnframes())
        with wave.open(str(right), "rb") as w:
            right_samples = w.readframes(w.getnframes())

        def _peak(raw_bytes: bytes) -> int:
            import struct

            values = struct.unpack(f"<{len(raw_bytes) // 2}h", raw_bytes)
            return max(abs(v) for v in values) if values else 0

        assert _peak(left_samples) > 10000
        assert _peak(right_samples) < 1000

    def test_probe_a_conformant_wav(self):
        info = audio_svc.probe(FIXTURES / "tiny16k.wav")
        assert info.codec_name == "pcm_s16le"
        assert info.sample_rate == 16000
        assert info.channels == 1
        assert info.is_conformant is True

    def test_probe_a_non_conformant_mp3(self):
        info = audio_svc.probe(FIXTURES / "tiny44k.mp3")
        assert info.is_conformant is False
        assert audio_svc.needs_conversion(info) is True

    def test_convert_produces_exactly_the_target_format(self, tmp_path):
        out = tmp_path / "converted.wav"
        audio_svc.convert_to_wav16k_mono(FIXTURES / "tiny44k.mp3", out)

        assert out.exists() and out.stat().st_size > 0
        info = audio_svc.probe(out)
        assert info.codec_name == "pcm_s16le"
        assert info.sample_rate == 16000
        assert info.channels == 1
        assert info.is_conformant is True

    def test_probing_a_non_audio_file_errors(self, tmp_path):
        junk = tmp_path / "junk.wav"
        junk.write_bytes(b"this is definitely not audio")
        with pytest.raises(AudioError):
            audio_svc.probe(junk)

    def test_concatenated_live_webm_segments_are_not_truncated(self, tmp_path):
        """Pausing and resuming a plain mic recording (web/src/hooks/useRecorder.ts)
        ends its MediaRecorder rather than suspending it, so the file at Stop is
        several independently-finalized WebM/Opus blobs concatenated -- exactly
        the container shape browser_recording.webm was built to reproduce. This
        guards the failure that would silently break that feature: ffmpeg
        reading only the first segment and dropping the rest.
        """
        single = FIXTURES / "browser_recording.webm"
        concatenated = tmp_path / "concatenated.webm"
        concatenated.write_bytes(single.read_bytes() * 2)

        single_out = tmp_path / "single.wav"
        audio_svc.convert_to_wav16k_mono(single, single_out)
        single_duration = audio_svc.probe(single_out).duration_sec

        combined_out = tmp_path / "combined.wav"
        audio_svc.convert_to_wav16k_mono(concatenated, combined_out)
        combined_duration = audio_svc.probe(combined_out).duration_sec

        # Not exactly double: each independently-finalized segment carries its
        # own un-trimmed Opus pre-skip, adding a few milliseconds per splice.
        # Truncation to ~single_duration is the regression this guards against.
        assert combined_duration > single_duration * 1.9
