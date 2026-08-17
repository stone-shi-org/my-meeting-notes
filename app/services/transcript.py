"""Transcript rendering.

Speaker renames are applied *here*, at render time, by joining the immutable
diarization payload against the speaker_map table. Nothing in this module (or
anywhere else) writes to ``diarizations.raw_json``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from app.errors import NotFoundError, ValidationError
from app.services.diarize import strip_language_tag

# Whole-segment markers the diarizer emits for non-speech. A segment that merely
# contains a bracketed aside is still speech and is never filtered.
NON_SPEECH_WORDS = {
    "environmental sounds",
    "silence",
    "music",
    "noise",
    "background noise",
    "inaudible",
    "applause",
    "laughter",
}

_BRACKETED = re.compile(r"^\[([^\]]+)\]$")

FORMATS = ("json", "text", "md", "vtt")


def is_non_speech(text: str) -> bool:
    match = _BRACKETED.match((text or "").strip())
    return bool(match) and match.group(1).strip().lower() in NON_SPEECH_WORDS


def fmt_clock(seconds: float) -> str:
    total = int(seconds)
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_vtt(seconds: float) -> str:
    """WebVTT cue timestamp. Hours are mandatory in the spec."""
    ms = int(round(max(0.0, seconds) * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{milli:03d}"


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def load_diarization(conn: sqlite3.Connection, meeting_id: int) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT d.* FROM diarizations d
          JOIN meetings m ON m.active_diarization_id = d.id
         WHERE m.id = ?
        """,
        (meeting_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("This meeting has no transcript yet")
    return row


def load_speaker_map(conn: sqlite3.Connection, meeting_id: int) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM speaker_map WHERE meeting_id = ? AND source != 'user_hint'",
        (meeting_id,),
    ).fetchall()
    return {r["speaker_id"]: r for r in rows}


def display_name_for(speaker_id: str, mapping: dict[str, sqlite3.Row]) -> str:
    """The user's name for a speaker, falling back to the raw diarization id."""
    row = mapping.get(speaker_id)
    if row is not None and row["display_name"]:
        return row["display_name"]
    return speaker_id


def canonical_speaker_id(speaker_id: str, mapping: dict[str, sqlite3.Row]) -> str:
    """Resolve a merge target. One hop only -- the router reparents any
    existing followers whenever a merge target itself gets merged, so a
    chain never has to be walked here."""
    row = mapping.get(speaker_id)
    if row is not None and row["merged_into"]:
        return row["merged_into"]
    return speaker_id


def is_me_for(speaker_id: str, mapping: dict[str, sqlite3.Row]) -> bool:
    row = mapping.get(speaker_id)
    return bool(row is not None and row["is_me"])


def label_with_me(name: str, is_me: bool) -> str:
    return f"{name} (me)" if is_me else name


def me_speaker_id(conn: sqlite3.Connection, meeting_id: int) -> str | None:
    """The one speaker_id marked as the logged-in user for this meeting, if any."""
    row = conn.execute(
        "SELECT speaker_id FROM speaker_map WHERE meeting_id = ? AND is_me = 1 "
        "AND source != 'user_hint' LIMIT 1",
        (meeting_id,),
    ).fetchone()
    return row["speaker_id"] if row else None


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def build_transcript(
    payload: dict,
    mapping: dict[str, sqlite3.Row],
    *,
    apply_names: bool = True,
    include_nonspeech: bool = True,
) -> dict:
    """Normalise the raw payload into what the SPA and the LLM both consume."""
    raw_segments = payload.get("segments") or []
    raw_speakers = payload.get("speakers") or []

    segments = []
    for seg in raw_segments:
        # Applied at render time, same as speaker renames above -- the
        # stored raw_json stays exactly what the model returned; only what
        # gets shown/exported is cleaned. See strip_language_tag's doc
        # comment for why this is a substring strip, not the whole-segment
        # is_non_speech check just below.
        text = strip_language_tag(seg.get("text") or "")
        non_speech = is_non_speech(text)
        if non_speech and not include_nonspeech:
            continue

        raw_speaker_id = seg.get("speaker") or ""
        speaker_id = canonical_speaker_id(raw_speaker_id, mapping)
        segments.append(
            {
                "id": seg.get("id"),
                "speaker": speaker_id,
                "speaker_name": (
                    display_name_for(speaker_id, mapping) if apply_names else speaker_id
                ),
                "label": seg.get("label"),
                "start": seg.get("start"),
                "end": seg.get("end"),
                "text": text,
                "non_speech": non_speech,
                "is_me": is_me_for(speaker_id, mapping),
            }
        )

    speakers = []
    for sp in raw_speakers:
        sid = sp.get("id") or ""
        # Each entry reports its own identity, not the merge target's --
        # is_me/hidden are transferred on merge (see the router), so a
        # merged-away row must not appear to inherit them from the target it
        # no longer speaks for. The frontend resolves the target's name for
        # "merged into ..." by looking up its entry in this same list.
        row = mapping.get(sid)
        merged_into = row["merged_into"] if (row is not None and row["merged_into"]) else None
        speakers.append(
            {
                "id": sid,
                "label": sp.get("label"),
                "display_name": (row["display_name"] if row is not None else None),
                "color": (row["color"] if row is not None else None),
                "total_speech_duration": sp.get("total_speech_duration"),
                "segment_count": sp.get("segment_count"),
                "hidden": bool(row["hidden"]) if row is not None else False,
                "is_me": bool(row["is_me"]) if row is not None else False,
                "merged_into": merged_into,
            }
        )

    return {
        "task": payload.get("task"),
        "duration": payload.get("duration"),
        "num_speakers": payload.get("num_speakers"),
        "speakers": speakers,
        "segments": segments,
    }


def render_text(transcript: dict) -> str:
    lines = []
    for seg in transcript["segments"]:
        stamp = fmt_clock(seg["start"] or 0)
        name = label_with_me(seg["speaker_name"], seg["is_me"])
        lines.append(f"[{stamp}] {name}: {seg['text'].strip()}")
    return "\n".join(lines)


def render_markdown(transcript: dict) -> str:
    lines = ["# Transcript", ""]
    if transcript.get("duration"):
        lines.append(
            f"_{fmt_clock(transcript['duration'])} · "
            f"{transcript.get('num_speakers', '?')} speakers_"
        )
        lines.append("")

    previous = None
    for seg in transcript["segments"]:
        name = label_with_me(seg["speaker_name"], seg["is_me"])
        stamp = fmt_clock(seg["start"] or 0)
        if name != previous:
            lines.append(f"**{name}** `{stamp}`")
            previous = name
        lines.append("")
        lines.append(seg["text"].strip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_vtt(transcript: dict) -> str:
    lines = ["WEBVTT", ""]
    for i, seg in enumerate(transcript["segments"], start=1):
        name = label_with_me(seg["speaker_name"], seg["is_me"])
        lines.append(str(i))
        lines.append(f"{fmt_vtt(seg['start'] or 0)} --> {fmt_vtt(seg['end'] or 0)}")
        lines.append(f"<v {name}>{seg['text'].strip()}")
        lines.append("")
    return "\n".join(lines)


def render(transcript: dict, fmt: str) -> str | dict:
    if fmt == "json":
        return transcript
    if fmt == "text":
        return render_text(transcript)
    if fmt == "md":
        return render_markdown(transcript)
    if fmt == "vtt":
        return render_vtt(transcript)
    raise ValidationError(f"Unknown format {fmt!r}. Expected one of {', '.join(FORMATS)}")


def transcript_sha256(transcript: dict) -> str:
    """Stable fingerprint used to tell whether a summary is still current."""
    body = "\n".join(
        f"{s['speaker_name']}|{s['start']}|{s['text']}" for s in transcript["segments"]
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def speaker_stats(transcript: dict) -> list[dict]:
    """Talk time per speaker, for the legend and the share bar.

    A merged-away speaker keeps its own row (so the legend can offer
    "Unmerge") but is excluded from the total: its speech now renders under
    the target's identity, and counting both would double the total.
    """
    canonical = [sp for sp in transcript["speakers"] if not sp.get("merged_into")]
    total = sum(sp.get("total_speech_duration") or 0 for sp in canonical) or 1
    stats = []
    for sp in transcript["speakers"]:
        duration = sp.get("total_speech_duration") or 0
        merged = bool(sp.get("merged_into"))
        stats.append(
            {
                **sp,
                "share": 0.0 if merged else duration / total,
                "duration_human": fmt_clock(duration),
            }
        )
    return sorted(
        stats,
        key=lambda s: (bool(s.get("merged_into")), -(s["total_speech_duration"] or 0)),
    )


def get_transcript(
    conn: sqlite3.Connection,
    meeting_id: int,
    *,
    apply_names: bool = True,
    include_nonspeech: bool = True,
) -> dict:
    diarization = load_diarization(conn, meeting_id)
    payload = json.loads(diarization["raw_json"])
    mapping = load_speaker_map(conn, meeting_id)
    return build_transcript(
        payload,
        mapping,
        apply_names=apply_names,
        include_nonspeech=include_nonspeech,
    )
