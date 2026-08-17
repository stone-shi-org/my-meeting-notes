"""Audio probing and conversion via ffmpeg.

The diarization service wants 16 kHz mono signed-16-bit PCM. Anything already in
that form is used as-is rather than re-encoded, which saves minutes and a disk
copy on the common case of an already-converted wav.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.errors import AudioError
from app.logging_config import get_logger

log = get_logger("audio")

TARGET_SAMPLE_RATE = 16000
TARGET_CHANNELS = 1
TARGET_CODEC = "pcm_s16le"

FFMPEG_TIMEOUT = 3600
FFPROBE_TIMEOUT = 60

# Extensions we will accept on upload. ffmpeg handles far more, but an
# allow-list keeps someone from posting a 2 GB .mkv by accident.
ALLOWED_EXTENSIONS = {
    ".wav", ".mp3", ".m4a", ".mp4", ".aac", ".flac", ".ogg", ".oga",
    ".opus", ".wma", ".webm", ".qta", ".mov", ".caf", ".aiff", ".aif",
}


@dataclass
class AudioInfo:
    duration_sec: float | None
    sample_rate: int | None
    channels: int | None
    codec_name: str | None
    format_name: str | None

    @property
    def is_conformant(self) -> bool:
        return (
            self.codec_name == TARGET_CODEC
            and self.sample_rate == TARGET_SAMPLE_RATE
            and self.channels == TARGET_CHANNELS
        )


def _require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise AudioError(
            f"{name} is not installed. The container image must include ffmpeg."
        )
    return path


def probe(path: Path) -> AudioInfo:
    """Read stream metadata with ffprobe."""
    binary = _require_binary("ffprobe")
    cmd = [
        binary,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-select_streams", "a:0",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioError(f"ffprobe timed out on {path.name}") from exc

    if result.returncode != 0:
        raise AudioError(f"Could not read audio: {result.stderr.strip()[:400]}")

    return parse_probe_output(result.stdout)


def parse_probe_output(stdout: str) -> AudioInfo:
    """Split out from probe() so it can be tested without a subprocess."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AudioError("ffprobe returned malformed JSON") from exc

    streams = data.get("streams") or []
    if not streams:
        raise AudioError("File contains no audio stream")

    stream = streams[0]
    fmt = data.get("format") or {}

    def _float(value) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _int(value) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    # Duration lives on the format for most containers and on the stream for
    # some; prefer whichever is present.
    duration = _float(fmt.get("duration")) or _float(stream.get("duration"))

    return AudioInfo(
        duration_sec=duration,
        sample_rate=_int(stream.get("sample_rate")),
        channels=_int(stream.get("channels")),
        codec_name=stream.get("codec_name"),
        format_name=fmt.get("format_name"),
    )


def needs_conversion(info: AudioInfo) -> bool:
    return not info.is_conformant


def build_ffmpeg_command(src: Path, dest: Path) -> list[str]:
    """The exact conversion the diarizer expects. Kept separate so it's testable."""
    return [
        _require_binary("ffmpeg"),
        "-nostdin",
        "-y",
        "-i", str(src),
        "-ac", str(TARGET_CHANNELS),
        "-ar", str(TARGET_SAMPLE_RATE),
        "-c:a", TARGET_CODEC,
        str(dest),
    ]


def build_ffmpeg_command_stereo(src: Path, dest: Path) -> list[str]:
    """Same target sample rate/codec as build_ffmpeg_command, but keeps both
    channels instead of downmixing to mono.

    Used only for channel-separated (mic + room) recordings: the two channels
    carry two different speakers there, and downmixing to mono the way every
    other recording does would sum them back into one signal -- exactly the
    separation the whole feature exists to preserve.
    """
    return [
        _require_binary("ffmpeg"),
        "-nostdin",
        "-y",
        "-i", str(src),
        "-ac", "2",
        "-ar", str(TARGET_SAMPLE_RATE),
        "-c:a", TARGET_CODEC,
        str(dest),
    ]


def build_ffmpeg_command_multichannel(src: Path, dest: Path, channels: int) -> list[str]:
    """Same as build_ffmpeg_command_stereo, generalized past exactly 2
    channels -- for an uploaded file that already has one speaker per
    channel (see split_channels, meeting_audio_channels).
    """
    return [
        _require_binary("ffmpeg"),
        "-nostdin",
        "-y",
        "-i", str(src),
        "-ac", str(channels),
        "-ar", str(TARGET_SAMPLE_RATE),
        "-c:a", TARGET_CODEC,
        str(dest),
    ]


def _run_ffmpeg(cmd: list[str], src_name: str, *outputs: Path) -> None:
    """Shared subprocess plumbing for every ffmpeg call in this module.

    One error-handling path so a stereo split fails exactly the same way a
    mono conversion always has -- named timeout, stderr tail, "no output" --
    rather than three subtly different messages for the same three failures.
    """
    for dest in outputs:
        dest.parent.mkdir(parents=True, exist_ok=True)

    log.info("running ffmpeg on %s -> %s", src_name, ", ".join(o.name for o in outputs))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioError(f"ffmpeg timed out converting {src_name}") from exc

    if result.returncode != 0:
        # ffmpeg puts everything on stderr; the tail is the useful part.
        tail = result.stderr.strip().splitlines()[-5:]
        raise AudioError("ffmpeg failed: " + " | ".join(tail))

    for dest in outputs:
        if not dest.exists() or dest.stat().st_size == 0:
            raise AudioError("ffmpeg produced no output")


def convert_to_wav16k_mono(src: Path, dest: Path) -> Path:
    """Transcode to 16 kHz mono PCM. Blocking -- call via asyncio.to_thread."""
    cmd = build_ffmpeg_command(src, dest)
    _run_ffmpeg(cmd, src.name, dest)
    return dest


def convert_to_wav16k_multichannel(src: Path, dest: Path, channels: int) -> Path:
    """Transcode to 16 kHz N-channel PCM, preserving channel identity.

    Blocking -- call via asyncio.to_thread. Used only for an uploaded
    "speaker by channel" file (channel_map == 'multi_channel'); see
    build_ffmpeg_command_multichannel and split_channels.
    """
    cmd = build_ffmpeg_command_multichannel(src, dest, channels)
    _run_ffmpeg(cmd, src.name, dest)
    return dest


def convert_to_wav16k_stereo(src: Path, dest: Path) -> Path:
    """Transcode to 16 kHz stereo PCM, preserving channel 0/1 identity.

    Blocking -- call via asyncio.to_thread. Used only for channel-separated
    (mic + room) recordings; see build_ffmpeg_command_stereo.
    """
    cmd = build_ffmpeg_command_stereo(src, dest)
    _run_ffmpeg(cmd, src.name, dest)
    return dest


def split_stereo_channels(src: Path, left_dest: Path, right_dest: Path) -> None:
    """Split a 2-channel wav into two mono wavs. Blocking -- call via asyncio.to_thread.

    channel 0 -> left_dest, channel 1 -> right_dest.

    ``channelsplit`` rather than ``-map_channel``: -map_channel on a source
    that turns out to have only one channel silently produces a mono file
    with no error, which would make a channel-separated recording collapse
    back to one speaker without anyone noticing. channelsplit fails loudly on
    a mismatched layout instead.

    Deliberately not implemented in terms of the more general split_channels
    below: this is the recorder's tested, production mic_room path, and it
    stays on its own exact implementation rather than sharing code with a
    brand-new function.
    """
    binary = _require_binary("ffmpeg")
    cmd = [
        binary, "-nostdin", "-y",
        "-i", str(src),
        "-filter_complex", "[0:a]channelsplit=channel_layout=stereo[left][right]",
        "-map", "[left]", str(left_dest),
        "-map", "[right]", str(right_dest),
    ]
    _run_ffmpeg(cmd, src.name, left_dest, right_dest)


def split_channels(src: Path, dests: list[Path]) -> None:
    """Split an N-channel wav into N mono wavs, dests[i] <- source channel i.

    Blocking -- call via asyncio.to_thread. For an uploaded "speaker by
    channel" file (see meeting_audio_channels) -- N independent speakers,
    not a surround mix, so there is no named channel_layout (stereo, 5.1,
    ...) to hand channelsplit for an arbitrary N. ``pan=mono|c0=cI`` instead
    picks channel I by raw index regardless of what layout the source
    claims, and -- like channelsplit -- fails at filter-graph setup rather
    than silently emitting fewer/shorter channels if the source doesn't
    actually have that many.
    """
    binary = _require_binary("ffmpeg")
    filters = ";".join(f"[0:a]pan=mono|c0=c{i}[ch{i}]" for i in range(len(dests)))
    cmd = [binary, "-nostdin", "-y", "-i", str(src), "-filter_complex", filters]
    for i, dest in enumerate(dests):
        cmd += ["-map", f"[ch{i}]", str(dest)]
    _run_ffmpeg(cmd, src.name, *dests)


def pad_and_merge_channels(
    sources: list[tuple[Path, float]], dest_channels: list[Path], merged_dest: Path
) -> None:
    """Align N independently-recorded mono files onto one shared timeline.

    Blocking -- call via asyncio.to_thread. Unlike split_channels' input
    (one file, already sample-aligned by a single recording clock), an
    uploaded "speaker by file" set was never aligned by anything -- each
    ``(path, start_offset_sec)`` pair may start and end at a different wall-
    clock moment. ``adelay`` covers the leading gap; ``apad=whole_dur=...``
    extends every source to the run's overall longest total length (offset
    included) so the amerge below -- which needs equal-length inputs --
    never silently truncates whichever source is shortest.

    Writes each padded, aligned mono file to dest_channels[i] (these feed
    the diarize stage directly; nothing needs to split them back out of the
    merged file) and separately amerges all of them into merged_dest, kept
    only for playback -- the existing single-file audio_path convention.
    """
    if len(sources) != len(dest_channels):
        raise ValueError("sources and dest_channels must be the same length")

    binary = _require_binary("ffmpeg")

    # The longest source, once its own leading silence is counted, sets the
    # common length every channel pads to.
    run_length = 0.0
    for path, offset in sources:
        info = probe(path)
        run_length = max(run_length, max(0.0, offset) + (info.duration_sec or 0.0))

    for (path, offset), dest in zip(sources, dest_channels):
        delay_ms = max(0.0, offset) * 1000
        cmd = [
            binary, "-nostdin", "-y",
            "-i", str(path),
            "-af", f"adelay={delay_ms}:all=1,apad=whole_dur={run_length}",
            "-ac", str(TARGET_CHANNELS),
            "-ar", str(TARGET_SAMPLE_RATE),
            "-c:a", TARGET_CODEC,
            str(dest),
        ]
        _run_ffmpeg(cmd, path.name, dest)

    merge_cmd = [binary, "-nostdin", "-y"]
    for dest in dest_channels:
        merge_cmd += ["-i", str(dest)]
    merge_cmd += [
        "-filter_complex", f"amerge=inputs={len(dest_channels)}",
        "-ac", str(len(dest_channels)),
        "-ar", str(TARGET_SAMPLE_RATE),
        "-c:a", TARGET_CODEC,
        str(merged_dest),
    ]
    _run_ffmpeg(merge_cmd, "merged channels", merged_dest)


def is_allowed_extension(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS
