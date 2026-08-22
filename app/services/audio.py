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


def is_allowed_extension(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


def build_chunk_command(src: Path, out_dir: Path, chunk_seconds: int) -> list[str]:
    """Cut into fixed-length pieces via ffmpeg's segment muxer. ``-c copy`` on
    an already-16kHz-mono-PCM wav is a sample-accurate cut, not a re-encode --
    fast, and lossless at the boundary. Kept separate so it's testable."""
    pattern = str(out_dir / "chunk_%04d.wav")
    return [
        _require_binary("ffmpeg"),
        "-nostdin",
        "-y",
        "-i", str(src),
        "-f", "segment",
        "-segment_time", str(chunk_seconds),
        "-reset_timestamps", "1",
        "-c", "copy",
        pattern,
    ]


def split_into_chunks(src: Path, out_dir: Path, chunk_seconds: int) -> list[Path]:
    """Split a long recording into pieces small enough to stay under a
    diarization backend's own output budget (see diarize.py's
    looks_like_embedded_turns_dump). Blocking -- call via asyncio.to_thread.

    The chunk count isn't predicted up front and checked against what ffmpeg
    actually produced -- a real file's duration can differ by a hair from
    whatever was probed and stored earlier, which would make an exact
    prediction an occasional false failure for no real problem. Instead this
    just globs whatever the segment muxer wrote and insists there's at least
    one, non-empty.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = build_chunk_command(src, out_dir, chunk_seconds)

    log.info("splitting %s into %ds chunks", src.name, chunk_seconds)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioError(f"ffmpeg timed out splitting {src.name}") from exc

    if result.returncode != 0:
        tail = result.stderr.strip().splitlines()[-5:]
        raise AudioError("ffmpeg failed to split audio: " + " | ".join(tail))

    chunks = sorted(out_dir.glob("chunk_*.wav"))
    if not chunks or any(c.stat().st_size == 0 for c in chunks):
        raise AudioError("ffmpeg produced no usable chunks")
    return chunks
