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


def convert_to_wav16k_mono(src: Path, dest: Path) -> Path:
    """Transcode to 16 kHz mono PCM. Blocking -- call via asyncio.to_thread."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_ffmpeg_command(src, dest)

    log.info("converting %s -> %s", src.name, dest.name)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioError(f"ffmpeg timed out converting {src.name}") from exc

    if result.returncode != 0:
        # ffmpeg puts everything on stderr; the tail is the useful part.
        tail = result.stderr.strip().splitlines()[-5:]
        raise AudioError("ffmpeg failed: " + " | ".join(tail))

    if not dest.exists() or dest.stat().st_size == 0:
        raise AudioError("ffmpeg produced no output")

    return dest


def is_allowed_extension(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS
