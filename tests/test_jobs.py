"""The job queue: progress arithmetic, cancellation, recovery and resume."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.db import get_conn, utcnow
from app.errors import JobCancelled
from app.jobs import queue as queue_mod
from app.jobs.queue import JobContext, JobQueue, create_job
from app.jobs.registry import INGEST_STAGES, stages_for


@pytest.fixture
def seeded(conn):
    """A user, thread and meeting to hang jobs off."""
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
    conn.execute(
        "INSERT INTO meetings (id, thread_id, owner_id, title, created_at, updated_at) "
        "VALUES (1, 1, 1, 'M', ?, ?)",
        (now, now),
    )
    return conn


def _job(conn, job_type="ingest", **kw):
    return create_job(conn, job_type=job_type, user_id=1, meeting_id=1, thread_id=1, **kw)


# --------------------------------------------------------------------------- #
# Progress arithmetic
# --------------------------------------------------------------------------- #


class TestProgress:
    def test_stage_weights_sum_to_one(self):
        for job_type in ("ingest", "diarize", "summarize", "match"):
            total = sum(s.weight for s in stages_for(job_type))
            assert total == pytest.approx(1.0), job_type

    def test_progress_accumulates_across_stages(self, seeded, initialised_db):
        job_id = _job(seeded)
        seeded.commit()
        ctx = JobContext(job_id, "ingest", {}, db_path=initialised_db)

        seen = []
        for stage in INGEST_STAGES:
            ctx.stage(stage.key)
            with get_conn(initialised_db) as c:
                seen.append(c.execute("SELECT progress FROM jobs WHERE id=?", (job_id,)).fetchone()[0])
            ctx.complete_stage()

        assert seen == sorted(seen), "progress must never go backwards"
        assert seen[0] == 0.0

    def test_completing_every_stage_reaches_one(self, seeded, initialised_db):
        job_id = _job(seeded)
        seeded.commit()
        ctx = JobContext(job_id, "ingest", {}, db_path=initialised_db)

        for stage in INGEST_STAGES:
            ctx.stage(stage.key)
            ctx.complete_stage()

        with get_conn(initialised_db) as c:
            final = c.execute("SELECT progress FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        assert final == pytest.approx(1.0)

    def test_skipped_weight_is_redistributed(self, seeded, initialised_db):
        """Skipping conversion must not cap the bar below 100%."""
        job_id = _job(seeded)
        seeded.commit()
        ctx = JobContext(job_id, "ingest", {}, db_path=initialised_db)

        for stage in INGEST_STAGES:
            if stage.key == "converting":
                ctx.skip(stage.key)
                continue
            ctx.stage(stage.key)
            ctx.complete_stage()

        with get_conn(initialised_db) as c:
            final = c.execute("SELECT progress FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        assert final == pytest.approx(1.0)

    def test_within_stage_progress_stays_inside_its_slice(self, seeded, initialised_db):
        job_id = _job(seeded)
        seeded.commit()
        ctx = JobContext(job_id, "ingest", {}, db_path=initialised_db)

        ctx.stage("received")
        ctx.complete_stage()
        ctx.stage("probing")
        before = ctx._progress_before("probing")
        ctx.stage_progress(0.5)

        with get_conn(initialised_db) as c:
            mid = c.execute("SELECT progress FROM jobs WHERE id=?", (job_id,)).fetchone()[0]

        assert before < mid < before + ctx._stage_weight("probing") + 1e-9

    def test_stage_progress_clamps_out_of_range_input(self, seeded, initialised_db):
        job_id = _job(seeded)
        seeded.commit()
        ctx = JobContext(job_id, "ingest", {}, db_path=initialised_db)
        ctx.stage("diarizing")
        ctx.stage_progress(5.0)

        with get_conn(initialised_db) as c:
            p = c.execute("SELECT progress FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        assert p <= 1.0


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


def test_stages_and_events_are_logged(seeded, initialised_db):
    job_id = _job(seeded)
    seeded.commit()
    ctx = JobContext(job_id, "ingest", {}, db_path=initialised_db)

    ctx.stage("received", "Upload received")
    ctx.event("something noteworthy")
    ctx.event("a warning", level="warn")

    with get_conn(initialised_db) as c:
        rows = c.execute(
            "SELECT * FROM job_events WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()

    messages = [r["message"] for r in rows]
    assert "Queued" in messages
    assert "Upload received" in messages
    assert "something noteworthy" in messages
    assert any(r["level"] == "warn" for r in rows)


def test_event_ids_are_monotonic_for_cursor_paging(seeded, initialised_db):
    job_id = _job(seeded)
    seeded.commit()
    ctx = JobContext(job_id, "ingest", {}, db_path=initialised_db)
    for i in range(5):
        ctx.event(f"event {i}")

    with get_conn(initialised_db) as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM job_events WHERE job_id=? ORDER BY id", (job_id,)
        )]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #


def test_cancellation_is_noticed_at_the_next_stage_boundary(seeded, initialised_db):
    job_id = _job(seeded)
    seeded.commit()
    ctx = JobContext(job_id, "ingest", {}, db_path=initialised_db)

    ctx.stage("received")
    with get_conn(initialised_db) as c:
        c.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))

    # Mid-stage work is not interrupted; the boundary is where it stops.
    assert ctx.is_cancelled() is True
    with pytest.raises(JobCancelled):
        ctx.stage("probing")


# --------------------------------------------------------------------------- #
# Running jobs end to end
# --------------------------------------------------------------------------- #


@pytest.fixture
async def running_queue(initialised_db):
    q = JobQueue(concurrency=1, db_path=initialised_db)
    queue_mod.set_queue(q)
    await q.start()
    yield q
    await q.stop()
    queue_mod.set_queue(None)


async def _wait_for(db_path, job_id, statuses, timeout=10.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        with get_conn(db_path) as c:
            row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row and row["status"] in statuses:
            return row
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached {statuses}; last={dict(row) if row else None}")


async def test_a_successful_job_records_its_result(seeded, initialised_db, running_queue):
    async def body(ctx):
        ctx.stage("done")
        ctx.complete_stage()
        return {"answer": 42}

    queue_mod.JOB_BODIES["testtype"] = body
    job_id = _job(seeded, job_type="testtype")
    seeded.commit()

    await running_queue.enqueue(job_id)
    row = await _wait_for(initialised_db, job_id, {"succeeded", "failed"})

    assert row["status"] == "succeeded"
    assert row["progress"] == 1.0
    assert json.loads(row["result_json"]) == {"answer": 42}
    assert row["finished_at"] is not None
    del queue_mod.JOB_BODIES["testtype"]


async def test_a_failing_stage_records_where_it_broke(seeded, initialised_db, running_queue):
    async def body(ctx):
        ctx.stage("diarizing")
        raise RuntimeError("service exploded")

    queue_mod.JOB_BODIES["testfail"] = body
    job_id = _job(seeded, job_type="testfail")
    seeded.commit()

    await running_queue.enqueue(job_id)
    row = await _wait_for(initialised_db, job_id, {"failed"})

    assert row["error"] == "service exploded"
    assert row["error_stage"] == "diarizing"
    assert "RuntimeError" in row["error_trace"]
    del queue_mod.JOB_BODIES["testfail"]


async def test_a_cancelled_job_ends_cancelled_not_failed(seeded, initialised_db, running_queue):
    started = asyncio.Event()

    async def body(ctx):
        ctx.stage("received")
        started.set()
        await asyncio.sleep(0.3)
        ctx.stage("probing")  # raises JobCancelled
        return {}

    queue_mod.JOB_BODIES["testcancel"] = body
    job_id = _job(seeded, job_type="testcancel")
    seeded.commit()

    await running_queue.enqueue(job_id)
    await asyncio.wait_for(started.wait(), timeout=5)

    with get_conn(initialised_db) as c:
        c.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))

    row = await _wait_for(initialised_db, job_id, {"cancelled", "failed", "succeeded"})
    assert row["status"] == "cancelled"
    del queue_mod.JOB_BODIES["testcancel"]


async def test_an_unregistered_job_type_fails_cleanly(seeded, initialised_db, running_queue):
    job_id = _job(seeded, job_type="nonexistent")
    seeded.commit()
    await running_queue.enqueue(job_id)
    row = await _wait_for(initialised_db, job_id, {"failed"})
    assert "No handler" in row["error"]


# --------------------------------------------------------------------------- #
# Recovery
# --------------------------------------------------------------------------- #


async def test_recover_marks_running_jobs_interrupted(seeded, initialised_db):
    job_id = _job(seeded)
    seeded.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
    seeded.commit()

    q = JobQueue(concurrency=0, db_path=initialised_db)
    stats = await q.recover()

    with get_conn(initialised_db) as c:
        row = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        events = [e["message"] for e in c.execute(
            "SELECT message FROM job_events WHERE job_id = ?", (job_id,)
        )]

    assert stats["interrupted"] == 1
    # Re-queued in the same pass, since attempts (0) is under max_attempts (1).
    assert row["status"] == "queued"
    assert any("Interrupted by a restart" in m for m in events)


async def test_recover_requeues_queued_jobs(seeded, initialised_db):
    job_id = _job(seeded)
    seeded.commit()

    q = JobQueue(concurrency=0, db_path=initialised_db)
    stats = await q.recover()
    assert stats["requeued"] == 1


async def test_recover_does_not_requeue_a_job_past_its_attempt_limit(seeded, initialised_db):
    job_id = _job(seeded)
    seeded.execute(
        "UPDATE jobs SET status = 'interrupted', attempts = 3, max_attempts = 3 WHERE id = ?",
        (job_id,),
    )
    seeded.commit()

    q = JobQueue(concurrency=0, db_path=initialised_db)
    stats = await q.recover()
    assert stats["requeued"] == 0

    with get_conn(initialised_db) as c:
        assert c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "interrupted"


async def test_recover_leaves_finished_jobs_alone(seeded, initialised_db):
    job_id = _job(seeded)
    seeded.execute("UPDATE jobs SET status = 'succeeded' WHERE id = ?", (job_id,))
    seeded.commit()

    q = JobQueue(concurrency=0, db_path=initialised_db)
    stats = await q.recover()
    assert stats == {"interrupted": 0, "requeued": 0}


async def test_resume_can_be_disabled(seeded, initialised_db, monkeypatch):
    from app.config import reset_settings_cache

    monkeypatch.setenv("MMN_JOBS_RESUME_ON_START", "false")
    reset_settings_cache()

    _job(seeded)
    seeded.commit()

    q = JobQueue(concurrency=0, db_path=initialised_db)
    stats = await q.recover()
    assert stats["requeued"] == 0
