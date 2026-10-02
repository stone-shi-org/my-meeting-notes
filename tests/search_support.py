"""Shared seeding + a deterministic fake embedding model for the MMN-15 tests.

The fake maps each word to a handful of "concept" axes, so two texts that
share no word but share a concept ("budget" / "spending") still score high --
which is exactly what a keyword search cannot do and the semantic arm must.
"""

from __future__ import annotations

import json
import re

import httpx

from app.db import utcnow

EMBED_BASE = "https://embed.test/v1"
EMBED_URL = f"{EMBED_BASE}/embeddings"

CONCEPTS: dict[str, tuple[str, ...]] = {
    "money": ("budget", "spending", "spend", "cost", "costs", "dollars", "money", "price", "invoice"),
    "people": ("hire", "hiring", "recruit", "recruiting", "candidate", "headcount", "interview"),
    "travel": ("flight", "trip", "travel", "hotel", "airport", "visa"),
    "food": ("lunch", "dinner", "pizza", "catering", "menu"),
}
AXES = list(CONCEPTS) + ["other"]


def fake_vector(text: str) -> list[float]:
    vec = [0.0] * len(AXES)
    for word in re.findall(r"\w+", text.lower()):
        for i, words in enumerate(CONCEPTS.values()):
            if word in words:
                vec[i] += 1.0
                break
        else:
            vec[-1] += 0.05
    if not any(vec):
        vec[-1] = 1.0
    return vec


def embed_side_effect(calls: list[list[str]] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        inputs = body["input"]
        if calls is not None:
            calls.append(list(inputs))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": fake_vector(t)} for i, t in enumerate(inputs)
                ]
            },
        )

    return handler


def enable_embeddings(monkeypatch, *, model: str = "fake-embed-1", min_score: float = 0.5) -> None:
    from app.config import reset_settings_cache

    monkeypatch.setenv("MMN_EMBEDDING_ENABLED", "true")
    monkeypatch.setenv("MMN_EMBEDDING_BASE_URL", EMBED_BASE)
    monkeypatch.setenv("MMN_EMBEDDING_MODEL", model)
    monkeypatch.setenv("MMN_EMBEDDING_MIN_SCORE", str(min_score))
    reset_settings_cache()


# --------------------------------------------------------------------------- #
# Seeding straight into the db (service-level tests)
# --------------------------------------------------------------------------- #


def make_user(conn, username: str) -> int:
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, password_salt, created_at, updated_at) "
        "VALUES (?, 'x', 'x', ?, ?)",
        (username, now, now),
    )
    return cur.lastrowid


def diarization_payload(*turns: tuple[str, str]) -> dict:
    """``turns`` are (speaker_id, text); each turn lasts 10 seconds."""
    speakers = sorted({s for s, _ in turns})
    return {
        "task": "transcribe",
        "duration": 10.0 * len(turns),
        "num_speakers": len(speakers),
        "speakers": [{"id": s, "label": s, "total_speech_duration": 10.0} for s in speakers],
        "segments": [
            {"id": i, "speaker": s, "label": s, "start": 10.0 * i, "end": 10.0 * (i + 1), "text": t}
            for i, (s, t) in enumerate(turns)
        ],
    }


def add_transcript(conn, meeting_id: int, *turns: tuple[str, str]) -> int:
    """Insert a diarization and make it active, the way pipeline does."""
    from app.services import search_index

    payload = diarization_payload(*turns)
    cur = conn.execute(
        "INSERT INTO diarizations (meeting_id, provider_url, model, raw_json, created_at) "
        "VALUES (?, 'http://x', 'm', ?, ?)",
        (meeting_id, json.dumps(payload), utcnow()),
    )
    diar_id = cur.lastrowid
    for order, sp in enumerate(payload["speakers"]):
        conn.execute(
            "INSERT INTO speaker_map (meeting_id, speaker_id, label, sort_order, source, updated_at) "
            "VALUES (?, ?, ?, ?, 'diarizer', ?) ON CONFLICT(meeting_id, speaker_id) DO NOTHING",
            (meeting_id, sp["id"], sp["label"], order, utcnow()),
        )
    conn.execute(
        "UPDATE meetings SET active_diarization_id = ?, updated_at = ? WHERE id = ?",
        (diar_id, utcnow(), meeting_id),
    )
    search_index.mark_meeting(conn, meeting_id)
    return diar_id


def add_summary(conn, meeting_id: int, *, tldr: str, summary_md: str = "",
                decisions=(), topics=(), questions=(), actions=()) -> int:
    from app.services import search_index

    version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM summaries WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()[0]
    conn.execute("UPDATE summaries SET is_current = 0 WHERE meeting_id = ?", (meeting_id,))
    cur = conn.execute(
        "INSERT INTO summaries (meeting_id, version, is_current, model, prompt_name, prompt_sha256, "
        "prompt_text, tldr, summary_md, key_decisions_json, topics_json, open_questions_json, "
        "created_at) VALUES (?, ?, 1, 'm', 'p', 'sha', 'text', ?, ?, ?, ?, ?, ?)",
        (
            meeting_id, version, tldr, summary_md,
            json.dumps([{"text": d} for d in decisions]),
            json.dumps(list(topics)), json.dumps(list(questions)), utcnow(),
        ),
    )
    summary_id = cur.lastrowid
    for idx, text in enumerate(actions):
        conn.execute(
            "INSERT INTO action_items (summary_id, meeting_id, idx, text, owner_label, created_at) "
            "VALUES (?, ?, ?, ?, 'Priya', ?)",
            (summary_id, meeting_id, idx, text, utcnow()),
        )
    conn.execute(
        "UPDATE meetings SET active_summary_id = ?, updated_at = ? WHERE id = ?",
        (summary_id, utcnow(), meeting_id),
    )
    search_index.mark_meeting(conn, meeting_id)
    return summary_id


def add_email(conn, thread_id: int, user_id: int, *, message_id: str, subject: str,
              snippet: str = "", body: str | None = None, ai_summary: str | None = None,
              sender: str = "priya@acme.com") -> int:
    from app.services import matching

    matching.attach_email(
        conn, thread_id=thread_id, meeting_id=None, user_id=user_id,
        email={"message_id": message_id, "subject": subject, "snippet": snippet,
               "sender": sender, "date": "2026-09-01T10:00:00+00:00"},
    )
    row_id = conn.execute(
        "SELECT id FROM thread_emails WHERE thread_id = ? AND message_id = ?",
        (thread_id, message_id),
    ).fetchone()[0]
    if body is not None or ai_summary is not None:
        conn.execute(
            "UPDATE thread_emails SET body = ?, ai_summary = ?, body_fetched_at = ? WHERE id = ?",
            (body, ai_summary, utcnow(), row_id),
        )
    return row_id


def add_event(conn, thread_id: int, user_id: int, *, uid: str, summary: str,
              description: str = "", location: str = "") -> int:
    from app.services import matching

    matching.attach_event(
        conn, thread_id=thread_id, meeting_id=None, user_id=user_id,
        event={"uid": uid, "summary": summary, "description": description,
               "location": location, "start": "2026-09-02T09:00:00+00:00"},
    )
    return conn.execute(
        "SELECT id FROM thread_calendar_events WHERE thread_id = ? AND uid = ?", (thread_id, uid)
    ).fetchone()[0]


def kinds_and_labels(result: dict) -> list[tuple[str, str]]:
    return [(h["kind"], h["title"]) for h in result["hits"]]
