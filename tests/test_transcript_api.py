"""Transcript, audio and speaker endpoints end to end."""

from __future__ import annotations

import json
import time

import pytest

from app.db import get_conn
from tests.conftest import FIXTURES


@pytest.fixture(autouse=True)
def fake_diarization(monkeypatch):
    monkeypatch.setenv("MMN_DIARIZE_FAKE", "true")
    monkeypatch.setenv("MMN_DIARIZE_FAKE_DELAY_SEC", "0.05")
    monkeypatch.setenv("MMN_JOB_CONCURRENCY", "1")
    from app.config import reset_settings_cache

    reset_settings_cache()


@pytest.fixture
def meeting(user_client):
    """An uploaded, fully-ingested meeting."""
    with (FIXTURES / "tiny16k.wav").open("rb") as fh:
        resp = user_client.post(
            "/api/meetings/upload",
            data={"title": "Standup", "new_thread_title": "Atlas"},
            files={"file": ("tiny16k.wav", fh, "audio/wav")},
        )
    body = resp.json()

    deadline = time.time() + 20
    while time.time() < deadline:
        job = user_client.get(f"/api/jobs/{body['job_id']}").json()
        if job["status"] in {"succeeded", "failed"}:
            assert job["status"] == "succeeded", job.get("error")
            break
        time.sleep(0.1)
    return body


# --------------------------------------------------------------------------- #
# Raw payload
# --------------------------------------------------------------------------- #


def test_raw_diarization_is_served_verbatim(user_client, meeting, sample_diarization):
    resp = user_client.get(f"/api/meetings/{meeting['meeting_id']}/diarization")
    assert resp.status_code == 200
    assert resp.json() == sample_diarization


def test_transcript_404s_before_diarization(user_client):
    m = user_client.post(
        "/api/meetings", json={"new_thread_title": "T", "title": "No audio"}
    ).json()
    assert user_client.get(f"/api/meetings/{m['id']}/transcript").status_code == 404


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def test_transcript_json_shape(user_client, meeting):
    body = user_client.get(f"/api/meetings/{meeting['meeting_id']}/transcript").json()
    assert body["num_speakers"] == 2
    assert len(body["segments"]) == 79
    assert body["segments"][0]["speaker"] == "SPEAKER_00"
    assert body["segments"][0]["speaker_name"] == "SPEAKER_00"


def test_transcript_json_carries_speaker_stats(user_client, meeting):
    """The legend's talk-time share comes from this call and nowhere else."""
    body = user_client.get(f"/api/meetings/{meeting['meeting_id']}/transcript").json()
    speakers = body["speakers"]

    assert [s["id"] for s in speakers] == ["SPEAKER_00", "SPEAKER_01"]  # loudest first
    assert [s["duration_human"] for s in speakers] == ["11:49", "10:38"]
    assert sum(s["share"] for s in speakers) == pytest.approx(1.0)
    assert all(s["share"] > 0 for s in speakers)


@pytest.mark.parametrize(
    "fmt,expected_type,marker",
    [
        ("text", "text/plain", "[0:00] SPEAKER_00:"),
        ("md", "text/markdown", "# Transcript"),
        ("vtt", "text/vtt", "WEBVTT"),
    ],
)
def test_alternate_formats(user_client, meeting, fmt, expected_type, marker):
    resp = user_client.get(
        f"/api/meetings/{meeting['meeting_id']}/transcript", params={"format": fmt}
    )
    assert resp.status_code == 200
    assert expected_type in resp.headers["content-type"]
    assert marker in resp.text


def test_unknown_format_is_rejected(user_client, meeting):
    resp = user_client.get(
        f"/api/meetings/{meeting['meeting_id']}/transcript", params={"format": "docx"}
    )
    assert resp.status_code == 422


def test_non_speech_can_be_excluded(user_client, meeting):
    mid = meeting["meeting_id"]
    with_marker = user_client.get(f"/api/meetings/{mid}/transcript").json()
    without = user_client.get(
        f"/api/meetings/{mid}/transcript", params={"include_nonspeech": False}
    ).json()

    assert len(with_marker["segments"]) == 79
    assert len(without["segments"]) == 76  # two [Music] plus [Environmental Sounds]
    assert any(s["non_speech"] for s in with_marker["segments"])
    assert not any(s["non_speech"] for s in without["segments"])


# --------------------------------------------------------------------------- #
# Speaker renaming -- the raw payload must survive untouched
# --------------------------------------------------------------------------- #


def test_renaming_a_speaker_changes_the_rendered_transcript(user_client, meeting):
    mid = meeting["meeting_id"]

    resp = user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[
            {"speaker_id": "SPEAKER_00", "display_name": "Stan"},
            {"speaker_id": "SPEAKER_01", "display_name": "Donna"},
        ],
    )
    assert resp.status_code == 200

    transcript = user_client.get(f"/api/meetings/{mid}/transcript").json()
    names = {s["speaker_name"] for s in transcript["segments"]}
    assert names == {"Stan", "Donna"}


def test_renaming_leaves_the_raw_json_byte_identical(
    user_client, meeting, isolated_settings
):
    """The guarantee that makes renames safe and reversible."""
    mid = meeting["meeting_id"]

    with get_conn(isolated_settings.db_path) as conn:
        before = conn.execute(
            "SELECT raw_json FROM diarizations WHERE meeting_id = ?", (mid,)
        ).fetchone()[0]
    on_disk_before = (
        isolated_settings.audio_dir / str(mid) / "diarization.json"
    ).read_bytes()

    user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "display_name": "Stan"}],
    )

    with get_conn(isolated_settings.db_path) as conn:
        after = conn.execute(
            "SELECT raw_json FROM diarizations WHERE meeting_id = ?", (mid,)
        ).fetchone()[0]
    on_disk_after = (
        isolated_settings.audio_dir / str(mid) / "diarization.json"
    ).read_bytes()

    assert after == before
    assert on_disk_after == on_disk_before

    # Names live only in speaker_map; the payload never grows a rendered field.
    payload = json.loads(after)
    assert all("speaker_name" not in seg for seg in payload["segments"])
    assert all("display_name" not in sp for sp in payload["speakers"])


def test_clearing_a_name_falls_back_to_the_raw_id(user_client, meeting):
    mid = meeting["meeting_id"]
    user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "display_name": "Stan"}],
    )
    user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "display_name": ""}],
    )

    transcript = user_client.get(f"/api/meetings/{mid}/transcript").json()
    assert transcript["segments"][0]["speaker_name"] == "SPEAKER_00"


def test_apply_names_false_shows_the_raw_ids(user_client, meeting):
    mid = meeting["meeting_id"]
    user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "display_name": "Stan"}],
    )

    body = user_client.get(
        f"/api/meetings/{mid}/transcript", params={"apply_names": False}
    ).json()
    assert body["segments"][0]["speaker_name"] == "SPEAKER_00"


def test_renaming_is_idempotent(user_client, meeting):
    mid = meeting["meeting_id"]
    for _ in range(3):
        resp = user_client.put(
            f"/api/meetings/{mid}/speakers",
            json=[{"speaker_id": "SPEAKER_00", "display_name": "Stan"}],
        )
        assert resp.status_code == 200

    body = user_client.get(f"/api/meetings/{mid}/speakers").json()
    matching = [s for s in body["speakers"] if s["id"] == "SPEAKER_00"]
    assert len(matching) == 1
    assert matching[0]["display_name"] == "Stan"


def test_empty_update_list_is_rejected(user_client, meeting):
    resp = user_client.put(f"/api/meetings/{meeting['meeting_id']}/speakers", json=[])
    assert resp.status_code == 400


def test_speakers_endpoint_reports_talk_time(user_client, meeting):
    body = user_client.get(f"/api/meetings/{meeting['meeting_id']}/speakers").json()
    assert len(body["speakers"]) == 2
    assert sum(s["share"] for s in body["speakers"]) == pytest.approx(1.0)
    assert body["speakers"][0]["duration_human"]


def test_upload_name_hints_are_offered_back(user_client):
    with (FIXTURES / "tiny16k.wav").open("rb") as fh:
        body = user_client.post(
            "/api/meetings/upload",
            data={
                "title": "Standup",
                "new_thread_title": "Atlas",
                "speaker_names": "Stan, Donna",
            },
            files={"file": ("tiny16k.wav", fh, "audio/wav")},
        ).json()

    hints = user_client.get(f"/api/meetings/{body['meeting_id']}/speakers").json()
    assert hints["name_hints"] == ["Stan", "Donna"]


# --------------------------------------------------------------------------- #
# Hide / show
# --------------------------------------------------------------------------- #


def test_hiding_a_speaker_does_not_change_the_transcript(user_client, meeting):
    """Hidden is display-only: the API's segment list is untouched."""
    mid = meeting["meeting_id"]
    before = user_client.get(f"/api/meetings/{mid}/transcript").json()

    resp = user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "hidden": True}],
    )
    assert resp.status_code == 200

    after = user_client.get(f"/api/meetings/{mid}/transcript").json()
    assert after["segments"] == before["segments"]

    speakers = user_client.get(f"/api/meetings/{mid}/speakers").json()["speakers"]
    hidden = {s["id"]: s["hidden"] for s in speakers}
    assert hidden == {"SPEAKER_00": True, "SPEAKER_01": False}


def test_hiding_one_speaker_does_not_touch_another(user_client, meeting):
    mid = meeting["meeting_id"]
    user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "display_name": "Stan"}],
    )
    user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "hidden": True}],
    )

    speakers = user_client.get(f"/api/meetings/{mid}/speakers").json()["speakers"]
    stan = next(s for s in speakers if s["id"] == "SPEAKER_00")
    assert stan["hidden"] is True
    assert stan["display_name"] == "Stan"  # a hidden-only patch must not clear the name


# --------------------------------------------------------------------------- #
# "Me"
# --------------------------------------------------------------------------- #


def test_marking_me_is_exclusive(user_client, meeting):
    mid = meeting["meeting_id"]
    user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "is_me": True}],
    )
    speakers = user_client.get(f"/api/meetings/{mid}/speakers").json()["speakers"]
    assert {s["id"]: s["is_me"] for s in speakers} == {
        "SPEAKER_00": True,
        "SPEAKER_01": False,
    }

    user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_01", "is_me": True}],
    )
    speakers = user_client.get(f"/api/meetings/{mid}/speakers").json()["speakers"]
    assert {s["id"]: s["is_me"] for s in speakers} == {
        "SPEAKER_00": False,
        "SPEAKER_01": True,
    }


def test_me_marker_reaches_the_rendered_transcript(user_client, meeting):
    mid = meeting["meeting_id"]
    user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "is_me": True}],
    )

    text = user_client.get(
        f"/api/meetings/{mid}/transcript", params={"format": "text"}
    ).text
    assert "SPEAKER_00 (me):" in text
    assert "SPEAKER_01 (me):" not in text
    assert "SPEAKER_01:" in text


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #


def test_merging_canonicalises_segments_and_speakers(user_client, meeting):
    mid = meeting["meeting_id"]
    user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_01", "display_name": "Stan"}],
    )
    resp = user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "merge_into": "SPEAKER_01"}],
    )
    assert resp.status_code == 200

    transcript = user_client.get(f"/api/meetings/{mid}/transcript").json()
    assert {s["speaker"] for s in transcript["segments"]} == {"SPEAKER_01"}
    assert {s["speaker_name"] for s in transcript["segments"]} == {"Stan"}

    speakers = {s["id"]: s for s in transcript["speakers"]}
    assert speakers["SPEAKER_00"]["merged_into"] == "SPEAKER_01"
    assert speakers["SPEAKER_01"]["merged_into"] is None
    # The merged-away speaker's own share must not double-count against the target.
    assert speakers["SPEAKER_00"]["share"] == 0.0
    assert speakers["SPEAKER_01"]["share"] == pytest.approx(1.0)


def test_cannot_merge_into_self(user_client, meeting):
    mid = meeting["meeting_id"]
    resp = user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "merge_into": "SPEAKER_00"}],
    )
    assert resp.status_code == 400


def test_cannot_merge_into_an_unknown_speaker(user_client, meeting):
    mid = meeting["meeting_id"]
    resp = user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "merge_into": "SPEAKER_99"}],
    )
    assert resp.status_code == 400


def test_cannot_merge_into_an_already_merged_speaker(user_client, meeting):
    mid = meeting["meeting_id"]
    user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "merge_into": "SPEAKER_01"}],
    )
    resp = user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_01", "merge_into": "SPEAKER_00"}],
    )
    assert resp.status_code == 400


def test_a_follower_is_reparented_when_its_target_is_merged_again(
    user_client, meeting, isolated_settings
):
    """C -> B, then B -> A must leave C -> A, never a two-hop chain."""
    mid = meeting["meeting_id"]
    # The fixture only produces two speakers; exercise the three-way chain by
    # inserting a third id straight into speaker_map (the router only ever
    # needs the row -- it doesn't require a matching segment to exist).
    with get_conn(isolated_settings.db_path) as conn:
        conn.execute(
            "INSERT INTO speaker_map (meeting_id, speaker_id, source, updated_at) "
            "VALUES (?, 'SPEAKER_02', 'diarizer', datetime('now'))",
            (mid,),
        )
        conn.commit()

    user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_02", "merge_into": "SPEAKER_01"}],
    )
    resp = user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_01", "merge_into": "SPEAKER_00"}],
    )
    assert resp.status_code == 200

    with get_conn(isolated_settings.db_path) as conn:
        row = conn.execute(
            "SELECT merged_into FROM speaker_map WHERE meeting_id = ? AND speaker_id = 'SPEAKER_02'",
            (mid,),
        ).fetchone()
    assert row["merged_into"] == "SPEAKER_00"


def test_is_me_transfers_to_the_merge_target(user_client, meeting):
    mid = meeting["meeting_id"]
    user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "is_me": True}],
    )
    user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "merge_into": "SPEAKER_01"}],
    )

    speakers = user_client.get(f"/api/meetings/{mid}/speakers").json()["speakers"]
    assert {s["id"]: s["is_me"] for s in speakers} == {
        "SPEAKER_00": False,
        "SPEAKER_01": True,
    }


def test_unmerging_restores_independence(user_client, meeting):
    mid = meeting["meeting_id"]
    user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "merge_into": "SPEAKER_01"}],
    )
    resp = user_client.put(
        f"/api/meetings/{mid}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "merge_into": ""}],
    )
    assert resp.status_code == 200

    transcript = user_client.get(f"/api/meetings/{mid}/transcript").json()
    assert {s["speaker"] for s in transcript["segments"]} == {"SPEAKER_00", "SPEAKER_01"}
    speakers = {s["id"]: s for s in transcript["speakers"]}
    assert speakers["SPEAKER_00"]["merged_into"] is None


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #


def test_audio_is_served_with_range_support(user_client, meeting):
    """No Range means no seeking, which kills the whole player interaction."""
    resp = user_client.get(f"/api/meetings/{meeting['meeting_id']}/audio")
    assert resp.status_code == 200
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-type"] == "audio/wav"


def test_a_range_request_returns_206(user_client, meeting):
    resp = user_client.get(
        f"/api/meetings/{meeting['meeting_id']}/audio",
        headers={"Range": "bytes=0-1023"},
    )
    assert resp.status_code == 206
    assert len(resp.content) == 1024


def test_audio_404s_when_there_is_none(user_client):
    m = user_client.post(
        "/api/meetings", json={"new_thread_title": "T", "title": "Silent"}
    ).json()
    assert user_client.get(f"/api/meetings/{m['id']}/audio").status_code == 404


# --------------------------------------------------------------------------- #
# Ownership
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path", ["transcript", "diarization", "audio", "speakers"]
)
def test_transcript_routes_respect_ownership(user_client, other_user_client, meeting, path):
    resp = other_user_client.get(f"/api/meetings/{meeting['meeting_id']}/{path}")
    assert resp.status_code == 404


def test_another_user_cannot_rename_speakers(user_client, other_user_client, meeting):
    resp = other_user_client.put(
        f"/api/meetings/{meeting['meeting_id']}/speakers",
        json=[{"speaker_id": "SPEAKER_00", "display_name": "hijacked"}],
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Re-diarize
# --------------------------------------------------------------------------- #


def test_rediarize_queues_a_job(user_client, meeting):
    resp = user_client.post(f"/api/meetings/{meeting['meeting_id']}/rediarize")
    assert resp.status_code == 202
    assert resp.json()["job_id"]


def test_rediarize_needs_audio(user_client):
    m = user_client.post(
        "/api/meetings", json={"new_thread_title": "T", "title": "Silent"}
    ).json()
    assert user_client.post(f"/api/meetings/{m['id']}/rediarize").status_code == 400


def _diarization_count(db_path, meeting_id) -> int:
    with get_conn(db_path) as c:
        return c.execute(
            "SELECT COUNT(*) FROM diarizations WHERE meeting_id = ?", (meeting_id,)
        ).fetchone()[0]


def _wait_for_job(user_client, job_id: str) -> dict:
    deadline = time.time() + 20
    while time.time() < deadline:
        job = user_client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish in time")


def test_rediarize_defaults_to_forcing_a_new_run(user_client, meeting, db_path):
    """The whole point of this button is "redo it" -- without force=True the
    ingest-style checkpoint sees a diarization already exists for the default
    model and skips the request entirely, silently returning the same old
    transcript (the bug this route shipped with)."""
    meeting_id = meeting["meeting_id"]
    assert _diarization_count(db_path, meeting_id) == 1

    resp = user_client.post(f"/api/meetings/{meeting_id}/rediarize")
    assert resp.status_code == 202
    job = _wait_for_job(user_client, resp.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")

    assert _diarization_count(db_path, meeting_id) == 2


def test_rediarize_force_false_respects_the_checkpoint(user_client, meeting, db_path):
    """The opt-out: a caller that explicitly wants the resumability behaviour
    (skip if a previous attempt already got this far) instead of a redo."""
    meeting_id = meeting["meeting_id"]

    resp = user_client.post(f"/api/meetings/{meeting_id}/rediarize?force=false")
    assert resp.status_code == 202
    job = _wait_for_job(user_client, resp.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")

    assert _diarization_count(db_path, meeting_id) == 1
