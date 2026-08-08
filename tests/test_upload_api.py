"""Upload endpoint and the ingest pipeline, with diarization faked."""

from __future__ import annotations

import time

import pytest

from app.db import get_conn
from tests.conftest import FIXTURES


@pytest.fixture(autouse=True)
def fake_diarization(monkeypatch):
    """Replay the captured diarizer response instead of hitting the GPU box."""
    monkeypatch.setenv("MMN_DIARIZE_FAKE", "true")
    monkeypatch.setenv("MMN_DIARIZE_FAKE_DELAY_SEC", "0.05")
    monkeypatch.setenv("MMN_JOB_CONCURRENCY", "1")
    from app.config import reset_settings_cache

    reset_settings_cache()


def upload(client, path=None, **fields):
    path = path or (FIXTURES / "tiny16k.wav")
    form = {"title": "Standup", "new_thread_title": "Atlas", **fields}
    with path.open("rb") as fh:
        return client.post(
            "/api/meetings/upload",
            data=form,
            files={"file": (path.name, fh, "audio/wav")},
        )


def wait_for_job(client, job_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish: {job}")


# --------------------------------------------------------------------------- #
# Upload validation
# --------------------------------------------------------------------------- #


def test_upload_returns_202_with_ids(user_client):
    resp = upload(user_client)
    assert resp.status_code == 202
    body = resp.json()
    assert body["meeting_id"] > 0
    assert body["thread_id"] > 0
    assert body["job_id"]
    assert body["bytes"] > 0


def test_upload_creates_the_thread_when_asked(user_client):
    body = upload(user_client, new_thread_title="Brand New").json()
    thread = user_client.get(f"/api/threads/{body['thread_id']}").json()
    assert thread["title"] == "Brand New"


def test_upload_into_an_existing_thread(user_client):
    t = user_client.post("/api/threads", json={"title": "Existing"}).json()
    body = upload(user_client, thread_id=t["id"], new_thread_title=None).json()
    assert body["thread_id"] == t["id"]


def test_upload_rejects_a_disallowed_extension(user_client, tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("hello")
    resp = upload(user_client, path=bad)
    assert resp.status_code == 400
    assert "Unsupported file type" in resp.json()["error"]["message"]


def test_upload_rejects_an_empty_file(user_client, tmp_path):
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    resp = upload(user_client, path=empty)
    assert resp.status_code == 400
    assert "empty" in resp.json()["error"]["message"].lower()


def test_upload_rejects_a_file_over_the_limit(user_client, tmp_path, monkeypatch):
    from app.config import reset_settings_cache

    monkeypatch.setenv("MMN_MAX_UPLOAD_MB", "1")
    reset_settings_cache()

    big = tmp_path / "big.wav"
    big.write_bytes(b"\0" * (2 * 1024 * 1024))
    resp = upload(user_client, path=big)
    assert resp.status_code == 400
    assert "exceeds" in resp.json()["error"]["message"]


def test_a_rejected_upload_leaves_no_orphan_meeting(user_client, tmp_path, monkeypatch):
    from app.config import reset_settings_cache

    monkeypatch.setenv("MMN_MAX_UPLOAD_MB", "1")
    reset_settings_cache()

    before = user_client.get("/api/meetings").json()["total"]

    big = tmp_path / "big.wav"
    big.write_bytes(b"\0" * (2 * 1024 * 1024))
    upload(user_client, path=big)

    assert user_client.get("/api/meetings").json()["total"] == before


def test_a_rejected_upload_leaves_no_orphan_thread(
    user_client, isolated_settings, tmp_path, monkeypatch
):
    from app.config import reset_settings_cache

    monkeypatch.setenv("MMN_MAX_UPLOAD_MB", "1")
    reset_settings_cache()
    with get_conn(isolated_settings.db_path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]

    big = tmp_path / "big.wav"
    big.write_bytes(b"\0" * (2 * 1024 * 1024))
    upload(user_client, path=big, new_thread_title="Must not survive")

    with get_conn(isolated_settings.db_path) as conn:
        after = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    assert after == before


def test_upload_is_fully_staged_before_the_database_write(
    user_client, monkeypatch
):
    """The large file copy must not run inside SQLite's writer transaction."""
    from app.routers import meetings as meetings_router

    staged = False
    original_stream = meetings_router._stream_to_disk
    original_resolve = meetings_router.resolve_thread

    async def observed_stream(file, dest):
        nonlocal staged
        written = await original_stream(file, dest)
        staged = True
        return written

    def observed_resolve(*args, **kwargs):
        assert staged, "thread/meeting writes started before upload staging finished"
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(meetings_router, "_stream_to_disk", observed_stream)
    monkeypatch.setattr(meetings_router, "resolve_thread", observed_resolve)

    assert upload(user_client).status_code == 202


def test_upload_needs_a_thread(user_client):
    resp = upload(user_client, new_thread_title=None)
    assert resp.status_code == 400


def test_upload_requires_authentication(client):
    resp = upload(client)
    assert resp.status_code == 401


def test_upload_stores_the_original_on_disk(user_client, isolated_settings):
    body = upload(user_client).json()
    meeting_dir = isolated_settings.audio_dir / str(body["meeting_id"])
    assert meeting_dir.is_dir()
    assert (meeting_dir / "original.wav").exists()


# --------------------------------------------------------------------------- #
# The pipeline
# --------------------------------------------------------------------------- #


def test_ingest_runs_every_stage_and_marks_the_meeting_ready(user_client):
    body = upload(user_client).json()
    job = wait_for_job(user_client, body["job_id"])

    assert job["status"] == "succeeded", job.get("error")
    assert job["progress"] == 1.0
    assert job["result"]["diarization_id"] is not None

    meeting = user_client.get(f"/api/meetings/{body['meeting_id']}").json()
    assert meeting["status"] == "ready"
    assert meeting["has_transcript"] is True


def test_conversion_is_skipped_for_an_already_conformant_wav(user_client):
    body = upload(user_client)  # tiny16k.wav is already 16k mono pcm_s16le
    job_id = body.json()["job_id"]
    wait_for_job(user_client, job_id)

    events = user_client.get(f"/api/jobs/{job_id}/events").json()["events"]
    messages = " ".join(e["message"] for e in events)
    assert "no conversion needed" in messages.lower()

    meeting = user_client.get(f"/api/meetings/{body.json()['meeting_id']}").json()
    assert meeting["audio_converted"] is False


def test_a_non_conformant_upload_is_converted(user_client, isolated_settings):
    body = upload(user_client, path=FIXTURES / "tiny44k.mp3").json()
    wait_for_job(user_client, body["job_id"])

    meeting = user_client.get(f"/api/meetings/{body['meeting_id']}").json()
    assert meeting["audio_converted"] is True
    assert meeting["audio_sample_rate"] == 16000
    assert meeting["audio_channels"] == 1

    wav = isolated_settings.audio_dir / str(body["meeting_id"]) / "audio16k.wav"
    assert wav.exists()


def test_probe_records_duration_on_the_meeting(user_client):
    body = upload(user_client).json()
    wait_for_job(user_client, body["job_id"])

    meeting = user_client.get(f"/api/meetings/{body['meeting_id']}").json()
    assert meeting["audio_duration_sec"] == pytest.approx(0.5, abs=0.2)


class TestAudioOntoAnExistingMeeting:
    """A meeting can exist before its recording does.

    Creating one from an upcoming calendar event is exactly that case, and
    before this route existed such a meeting could never get a transcript: the
    only upload endpoint always created a *new* meeting, stranding the attached
    event and the speaker hints on the old one.
    """

    def add_audio(self, client, meeting_id, path=None, **fields):
        path = path or (FIXTURES / "tiny16k.wav")
        with path.open("rb") as fh:
            return client.post(
                f"/api/meetings/{meeting_id}/audio",
                data=fields,
                files={"file": (path.name, fh, "audio/wav")},
            )

    def blank_meeting(self, client):
        return client.post(
            "/api/meetings",
            json={"new_thread_title": "Atlas", "title": "Kickoff", "meeting_at": None},
        ).json()

    def test_a_meeting_with_no_audio_can_be_given_some(self, user_client):
        meeting = self.blank_meeting(user_client)
        assert meeting["has_audio"] is False

        resp = self.add_audio(user_client, meeting["id"])
        assert resp.status_code == 202, resp.text
        assert resp.json()["meeting_id"] == meeting["id"], "the same meeting, not a new one"

        job = wait_for_job(user_client, resp.json()["job_id"])
        assert job["status"] == "succeeded", job.get("error")

        after = user_client.get(f"/api/meetings/{meeting['id']}").json()
        assert after["has_audio"] is True
        assert after["has_transcript"] is True

    def test_it_stays_on_its_thread_and_keeps_its_speaker_hints(self, user_client):
        """The reason this is not "delete it and upload again"."""
        meeting = self.blank_meeting(user_client)
        thread_id = meeting["thread_id"]

        self.add_audio(user_client, meeting["id"], speaker_names="Ada, Grace")
        after = user_client.get(f"/api/meetings/{meeting['id']}").json()

        assert after["thread_id"] == thread_id
        assert after["speaker_count"] == 2

    def test_only_one_meeting_exists_afterwards(self, user_client):
        meeting = self.blank_meeting(user_client)
        self.add_audio(user_client, meeting["id"])

        listing = user_client.get(
            "/api/meetings", params={"thread_id": meeting["thread_id"]}
        ).json()
        assert listing["total"] == 1

    def test_a_failed_attempt_can_be_replaced(self, user_client):
        """Otherwise a bad first upload is another dead end."""
        meeting = self.blank_meeting(user_client)
        bad = self.add_audio(user_client, meeting["id"], path=FIXTURES / "icloud_recurring.ics")
        assert bad.status_code == 400

        good = self.add_audio(user_client, meeting["id"])
        assert good.status_code == 202

    def test_it_refuses_to_overwrite_a_transcript(self, user_client):
        meeting = self.blank_meeting(user_client)
        first = self.add_audio(user_client, meeting["id"])
        wait_for_job(user_client, first.json()["job_id"])

        second = self.add_audio(user_client, meeting["id"])
        assert second.status_code == 409
        assert "transcript" in second.json()["error"]["message"].lower()

    def test_the_rejected_extension_check_still_applies(self, user_client, tmp_path):
        meeting = self.blank_meeting(user_client)
        junk = tmp_path / "notes.txt"
        junk.write_text("not audio")
        assert self.add_audio(user_client, meeting["id"], path=junk).status_code == 400

    def test_an_empty_file_is_rejected_without_touching_the_meeting(
        self, user_client, tmp_path
    ):
        meeting = self.blank_meeting(user_client)
        empty = tmp_path / "empty.wav"
        empty.write_bytes(b"")

        assert self.add_audio(user_client, meeting["id"], path=empty).status_code == 400
        # The meeting predates the request, so a bad upload must not delete it.
        assert user_client.get(f"/api/meetings/{meeting['id']}").status_code == 200

    def test_a_bad_replacement_preserves_the_previous_failed_audio(
        self, user_client, isolated_settings, tmp_path
    ):
        meeting = self.blank_meeting(user_client)
        meeting_dir = isolated_settings.audio_dir / str(meeting["id"])
        meeting_dir.mkdir(parents=True)
        original = meeting_dir / "original.wav"
        original.write_bytes(b"only surviving recording")
        with get_conn(isolated_settings.db_path) as conn:
            conn.execute(
                "UPDATE meetings SET status = 'failed', original_path = ?, "
                "original_filename = 'original.wav', original_bytes = ? WHERE id = ?",
                (str(original), original.stat().st_size, meeting["id"]),
            )

        empty = tmp_path / "empty.wav"
        empty.write_bytes(b"")
        assert self.add_audio(user_client, meeting["id"], path=empty).status_code == 400

        assert original.read_bytes() == b"only surviving recording"
        with get_conn(isolated_settings.db_path) as conn:
            row = conn.execute(
                "SELECT original_path, original_bytes FROM meetings WHERE id = ?",
                (meeting["id"],),
            ).fetchone()
        assert row["original_path"] == str(original)
        assert row["original_bytes"] == len(b"only surviving recording")

    def test_someone_elses_meeting_is_404(self, user_client, other_user_client):
        meeting = self.blank_meeting(user_client)
        assert self.add_audio(other_user_client, meeting["id"]).status_code == 404


class TestDeletingABlankMeeting:
    def test_a_meeting_with_no_audio_can_be_deleted(self, user_client):
        meeting = user_client.post(
            "/api/meetings", json={"new_thread_title": "Atlas", "title": "Oops"}
        ).json()

        assert user_client.delete(f"/api/meetings/{meeting['id']}").status_code == 200
        assert user_client.get(f"/api/meetings/{meeting['id']}").status_code == 404

    def test_the_thread_survives_its_last_meeting(self, user_client):
        """Deleting the meeting must not take the thread's attached email and
        calendar with it."""
        meeting = user_client.post(
            "/api/meetings", json={"new_thread_title": "Atlas", "title": "Oops"}
        ).json()
        user_client.delete(f"/api/meetings/{meeting['id']}")

        thread = user_client.get(f"/api/threads/{meeting['thread_id']}").json()
        assert thread["meeting_count"] == 0


class TestBrowserRecording:
    """A clip from the in-page recorder, which is a WebM with no header duration.

    MediaRecorder writes WebM as a stream and a stream does not know how long it
    will be, so ffprobe reports nothing for the source file. The fixture is a
    real one built the same way (`ffmpeg -f webm -live 1`).
    """

    def test_the_source_really_has_no_duration(self):
        """Guards the premise: if this ever starts reporting one, the backfill
        below is testing nothing."""
        from app.services import audio as audio_svc

        assert audio_svc.probe(FIXTURES / "browser_recording.webm").duration_sec is None

    def test_it_uploads_and_processes(self, user_client):
        body = upload(
            user_client, path=FIXTURES / "browser_recording.webm", title="Recorded here"
        )
        assert body.status_code == 202

        job = wait_for_job(user_client, body.json()["job_id"])
        assert job["status"] == "succeeded", job.get("error")

    def test_the_length_is_recovered_from_the_converted_wav(self, user_client):
        """Or the player, the meeting card and the diarizer's progress estimate
        all sit on a NULL for every recording made in the browser."""
        body = upload(user_client, path=FIXTURES / "browser_recording.webm").json()
        wait_for_job(user_client, body["job_id"])

        meeting = user_client.get(f"/api/meetings/{body['meeting_id']}").json()
        assert meeting["audio_converted"] is True
        assert meeting["audio_duration_sec"] == pytest.approx(1.0, abs=0.2)


def test_diarization_is_stored_verbatim_and_on_disk(
    user_client, isolated_settings, sample_diarization
):
    import json

    body = upload(user_client).json()
    wait_for_job(user_client, body["job_id"])

    with get_conn(isolated_settings.db_path) as conn:
        row = conn.execute(
            "SELECT * FROM diarizations WHERE meeting_id = ?", (body["meeting_id"],)
        ).fetchone()

    assert json.loads(row["raw_json"]) == sample_diarization
    assert row["segment_count"] == len(sample_diarization["segments"])
    assert row["num_speakers"] == sample_diarization["num_speakers"]

    on_disk = isolated_settings.audio_dir / str(body["meeting_id"]) / "diarization.json"
    assert json.loads(on_disk.read_text()) == sample_diarization


def test_speaker_map_is_seeded_with_names_left_blank(
    user_client, isolated_settings, sample_diarization
):
    body = upload(user_client).json()
    wait_for_job(user_client, body["job_id"])

    with get_conn(isolated_settings.db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM speaker_map WHERE meeting_id = ? AND source != 'user_hint' "
            "ORDER BY sort_order",
            (body["meeting_id"],),
        ).fetchall()

    assert [r["speaker_id"] for r in rows] == [s["id"] for s in sample_diarization["speakers"]]
    # NULL until a human names them; the UI shows the raw SPEAKER_nn meanwhile.
    assert all(r["display_name"] is None for r in rows)


def test_speaker_name_hints_are_parked(user_client, isolated_settings):
    body = upload(user_client, speaker_names="Alice, Bob , Priya").json()

    with get_conn(isolated_settings.db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM speaker_map WHERE meeting_id = ? AND source = 'user_hint' "
            "ORDER BY sort_order",
            (body["meeting_id"],),
        ).fetchall()

    assert [r["display_name"] for r in rows] == ["Alice", "Bob", "Priya"]


def test_progress_events_are_recorded_in_order(user_client):
    body = upload(user_client).json()
    wait_for_job(user_client, body["job_id"])

    events = user_client.get(f"/api/jobs/{body['job_id']}/events").json()["events"]
    assert len(events) > 3
    ids = [e["id"] for e in events]
    assert ids == sorted(ids)

    progresses = [e["progress"] for e in events if e["progress"] is not None]
    assert progresses == sorted(progresses)


# --------------------------------------------------------------------------- #
# Jobs API
# --------------------------------------------------------------------------- #


def test_event_cursor_paging(user_client):
    body = upload(user_client).json()
    wait_for_job(user_client, body["job_id"])

    first = user_client.get(f"/api/jobs/{body['job_id']}/events", params={"limit": 2}).json()
    assert len(first["events"]) == 2

    second = user_client.get(
        f"/api/jobs/{body['job_id']}/events",
        params={"after_id": first["next_after_id"], "limit": 2},
    ).json()

    assert all(e["id"] > first["next_after_id"] for e in second["events"])


def test_a_job_carries_its_stage_definitions_for_the_stepper(user_client):
    body = upload(user_client).json()
    job = user_client.get(f"/api/jobs/{body['job_id']}").json()
    keys = [s["key"] for s in job["stages"]]
    assert keys == ["received", "probing", "converting", "diarizing", "persisting", "summarizing", "done"]


def test_active_jobs_filter(user_client):
    upload(user_client)
    resp = user_client.get("/api/jobs", params={"status": "active"})
    assert resp.status_code == 200


def test_a_users_job_is_invisible_to_another_user(user_client, other_user_client):
    body = upload(user_client).json()
    assert other_user_client.get(f"/api/jobs/{body['job_id']}").status_code == 404
    assert other_user_client.get(f"/api/jobs/{body['job_id']}/events").status_code == 404


def test_admin_can_see_any_job(admin_client, user_client):
    body = upload(user_client).json()
    assert admin_client.get(f"/api/jobs/{body['job_id']}").status_code == 200


def test_cannot_cancel_a_finished_job(user_client):
    body = upload(user_client).json()
    wait_for_job(user_client, body["job_id"])
    resp = user_client.post(f"/api/jobs/{body['job_id']}/cancel")
    assert resp.status_code == 409


def test_cannot_retry_a_successful_job(user_client):
    body = upload(user_client).json()
    wait_for_job(user_client, body["job_id"])
    resp = user_client.post(f"/api/jobs/{body['job_id']}/retry")
    assert resp.status_code == 409


def test_deleting_a_meeting_removes_its_audio_directory(user_client, isolated_settings):
    body = upload(user_client).json()
    wait_for_job(user_client, body["job_id"])

    meeting_dir = isolated_settings.audio_dir / str(body["meeting_id"])
    assert meeting_dir.is_dir()

    resp = user_client.delete(f"/api/meetings/{body['meeting_id']}")
    assert resp.json()["purged_audio"] is True
    assert not meeting_dir.exists()


def test_resume_skips_diarization_that_already_completed(user_client, isolated_settings):
    """The checkpoint that stops a restart re-spending minutes on the GPU."""
    body = upload(user_client).json()
    wait_for_job(user_client, body["job_id"])

    with get_conn(isolated_settings.db_path) as conn:
        before = conn.execute(
            "SELECT COUNT(*) FROM diarizations WHERE meeting_id = ?", (body["meeting_id"],)
        ).fetchone()[0]

    # Re-run the same ingest job, as recovery would after a crash.
    resp = user_client.post(f"/api/jobs/{body['job_id']}/retry")
    if resp.status_code == 409:
        with get_conn(isolated_settings.db_path) as conn:
            conn.execute(
                "UPDATE jobs SET status='interrupted', finished_at=NULL WHERE id=?",
                (body["job_id"],),
            )
        resp = user_client.post(f"/api/jobs/{body['job_id']}/retry")
    assert resp.status_code == 200

    job = wait_for_job(user_client, body["job_id"])
    assert job["status"] == "succeeded"

    with get_conn(isolated_settings.db_path) as conn:
        after = conn.execute(
            "SELECT COUNT(*) FROM diarizations WHERE meeting_id = ?", (body["meeting_id"],)
        ).fetchone()[0]

    assert after == before, "diarization should have been skipped, not repeated"

    events = user_client.get(f"/api/jobs/{body['job_id']}/events").json()["events"]
    assert any("already exists" in e["message"].lower() for e in events)
