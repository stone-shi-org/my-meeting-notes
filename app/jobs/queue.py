"""In-process background job queue.

An asyncio.Queue plus a small pool of worker tasks, started and stopped by the
FastAPI lifespan. Blocking work (ffmpeg, the diarization POST, sqlite writes)
goes through ``asyncio.to_thread`` so the event loop stays free -- which matters
because the SPA polls job progress every two seconds for minutes at a time.

No broker. Redis would mean a second container and a second failure mode for a
single-box LAN app, and restart survival is bought instead by ``recover()``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from app.config import get_settings
from app.db import get_conn, utcnow
from app.errors import JobCancelled
from app.jobs.registry import Stage, stages_for
from app.logging_config import get_logger

log = get_logger("jobs")

JobBody = Callable[["JobContext"], Awaitable[dict | None]]

# Registered by app.services.pipeline at import time to avoid a circular import.
JOB_BODIES: dict[str, JobBody] = {}


def register_job(job_type: str) -> Callable[[JobBody], JobBody]:
    def decorator(fn: JobBody) -> JobBody:
        JOB_BODIES[job_type] = fn
        return fn

    return decorator


class JobContext:
    """Handed to a job body. Owns progress reporting and cancellation."""

    def __init__(self, job_id: str, job_type: str, payload: dict, db_path=None):
        self.job_id = job_id
        self.job_type = job_type
        self.payload = payload
        self.db_path = db_path
        self._stages: tuple[Stage, ...] = stages_for(job_type)
        self._skipped: set[str] = set()
        self._completed: set[str] = set()
        self._current: str | None = None

    # -- progress ---------------------------------------------------------- #

    def _weight_scale(self) -> float:
        """Redistribute skipped weight so the bar still reaches 1.0."""
        total = sum(s.weight for s in self._stages)
        skipped = sum(s.weight for s in self._stages if s.key in self._skipped)
        remaining = total - skipped
        return (total / remaining) if remaining > 0 else 1.0

    def _progress_before(self, stage_key: str) -> float:
        scale = self._weight_scale()
        acc = 0.0
        for s in self._stages:
            if s.key == stage_key:
                break
            if s.key not in self._skipped:
                acc += s.weight * scale
        return min(acc, 1.0)

    def _stage_weight(self, stage_key: str) -> float:
        for s in self._stages:
            if s.key == stage_key:
                return s.weight * self._weight_scale()
        return 0.0

    def skip(self, stage_key: str, message: str = "") -> None:
        self._skipped.add(stage_key)
        self.event(message or f"Skipped {stage_key}", stage=stage_key, level="info")

    def stage(self, stage_key: str, message: str = "") -> None:
        """Enter a stage. Raises JobCancelled if a cancel was requested."""
        self.check_cancelled()
        self._current = stage_key
        progress = self._progress_before(stage_key)
        with get_conn(self.db_path) as conn:
            conn.execute(
                "UPDATE jobs SET stage = ?, progress = ?, updated_at = ?, "
                "heartbeat_at = ? WHERE id = ?",
                (stage_key, progress, utcnow(), utcnow(), self.job_id),
            )
            self._append_event(conn, message or stage_key, stage_key, "info", progress)

    def stage_progress(self, fraction: float, message: str | None = None) -> None:
        """Report progress *within* the current stage, 0..1."""
        if self._current is None:
            return
        fraction = max(0.0, min(1.0, fraction))
        overall = self._progress_before(self._current) + self._stage_weight(self._current) * fraction
        with get_conn(self.db_path) as conn:
            conn.execute(
                "UPDATE jobs SET progress = ?, updated_at = ?, heartbeat_at = ? WHERE id = ?",
                (min(overall, 1.0), utcnow(), utcnow(), self.job_id),
            )
            if message:
                self._append_event(conn, message, self._current, "info", overall)

    def complete_stage(self, stage_key: str | None = None) -> None:
        key = stage_key or self._current
        if key is None:
            return
        self._completed.add(key)
        progress = self._progress_before(key) + self._stage_weight(key)
        with get_conn(self.db_path) as conn:
            conn.execute(
                "UPDATE jobs SET progress = ?, updated_at = ?, heartbeat_at = ? WHERE id = ?",
                (min(progress, 1.0), utcnow(), utcnow(), self.job_id),
            )

    def event(self, message: str, *, stage: str | None = None, level: str = "info") -> None:
        with get_conn(self.db_path) as conn:
            self._append_event(conn, message, stage or self._current, level, None)

    def _append_event(
        self,
        conn: sqlite3.Connection,
        message: str,
        stage: str | None,
        level: str,
        progress: float | None,
    ) -> None:
        conn.execute(
            "INSERT INTO job_events (job_id, ts, stage, level, message, progress) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self.job_id, utcnow(), stage, level, message, progress),
        )

    def heartbeat(self) -> None:
        with get_conn(self.db_path) as conn:
            conn.execute(
                "UPDATE jobs SET heartbeat_at = ? WHERE id = ?", (utcnow(), self.job_id)
            )

    # -- cancellation ------------------------------------------------------ #

    def is_cancelled(self) -> bool:
        with get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?", (self.job_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def check_cancelled(self) -> None:
        if self.is_cancelled():
            raise JobCancelled(f"job {self.job_id} cancelled")


# --------------------------------------------------------------------------- #
# Persistence helpers
# --------------------------------------------------------------------------- #


def create_job(
    conn: sqlite3.Connection,
    *,
    job_type: str,
    user_id: int,
    meeting_id: int | None = None,
    thread_id: int | None = None,
    payload: dict | None = None,
    max_attempts: int = 1,
) -> str:
    job_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO jobs (id, type, status, stage, progress, meeting_id, thread_id,
                          user_id, payload_json, max_attempts, created_at, updated_at)
        VALUES (?, ?, 'queued', NULL, 0, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            job_type,
            meeting_id,
            thread_id,
            user_id,
            json.dumps(payload or {}),
            max_attempts,
            utcnow(),
            utcnow(),
        ),
    )
    conn.execute(
        "INSERT INTO job_events (job_id, ts, stage, level, message, progress) "
        "VALUES (?, ?, NULL, 'info', 'Queued', 0)",
        (job_id, utcnow()),
    )
    return job_id


def get_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def row_to_job(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "type": row["type"],
        "status": row["status"],
        "stage": row["stage"],
        "progress": row["progress"],
        "meeting_id": row["meeting_id"],
        "thread_id": row["thread_id"],
        "payload": json.loads(row["payload_json"] or "{}"),
        "result": json.loads(row["result_json"] or "null"),
        "error": row["error"],
        "error_stage": row["error_stage"],
        "attempts": row["attempts"],
        "max_attempts": row["max_attempts"],
        "cancel_requested": bool(row["cancel_requested"]),
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "heartbeat_at": row["heartbeat_at"],
        "stages": [
            {"key": s.key, "label": s.label, "weight": s.weight} for s in stages_for(row["type"])
        ],
    }


# --------------------------------------------------------------------------- #
# The queue
# --------------------------------------------------------------------------- #


class JobQueue:
    def __init__(self, concurrency: int | None = None, db_path=None):
        settings = get_settings()
        self.concurrency = (
            concurrency if concurrency is not None else settings.job_concurrency
        )
        self.db_path = db_path
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._watchdog: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    async def start(self) -> None:
        if self.concurrency <= 0:
            log.info("job worker pool disabled (concurrency=0)")
            return
        self._stopping.clear()
        for n in range(self.concurrency):
            self._workers.append(asyncio.create_task(self._worker(n), name=f"mmn-worker-{n}"))
        self._watchdog = asyncio.create_task(self._watchdog_loop(), name="mmn-watchdog")
        log.info("started %d job worker(s)", self.concurrency)

    async def stop(self) -> None:
        self._stopping.set()
        settings = get_settings()

        for task in [*self._workers, self._watchdog]:
            if task is not None:
                task.cancel()

        if self._workers:
            await asyncio.wait(
                self._workers, timeout=settings.job_shutdown_grace_sec
            )
        self._workers.clear()
        self._watchdog = None

        # Anything still marked running died with the process.
        with get_conn(self.db_path) as conn:
            conn.execute(
                "UPDATE jobs SET status = 'interrupted', error = 'process stopped', "
                "finished_at = ?, updated_at = ? WHERE status = 'running'",
                (utcnow(), utcnow()),
            )

    async def enqueue(self, job_id: str) -> None:
        await self._queue.put(job_id)

    def enqueue_nowait(self, job_id: str) -> None:
        self._queue.put_nowait(job_id)

    async def recover(self) -> dict:
        """Reconcile the jobs table with reality after a restart.

        Anything left 'running' died mid-flight. Resume is checkpoint-based:
        each stage re-checks whether its output already exists, so re-running a
        job skips the expensive work it already finished.
        """
        settings = get_settings()
        with get_conn(self.db_path) as conn:
            orphaned = conn.execute(
                "SELECT id FROM jobs WHERE status = 'running'"
            ).fetchall()
            for row in orphaned:
                conn.execute(
                    "UPDATE jobs SET status = 'interrupted', error = 'process restarted', "
                    "finished_at = ?, updated_at = ? WHERE id = ?",
                    (utcnow(), utcnow(), row["id"]),
                )
                conn.execute(
                    "INSERT INTO job_events (job_id, ts, level, message) "
                    "VALUES (?, ?, 'warn', 'Interrupted by a restart')",
                    (row["id"], utcnow()),
                )

            requeue: list[str] = []
            if settings.jobs_resume_on_start:
                rows = conn.execute(
                    "SELECT id FROM jobs WHERE status = 'queued' "
                    "OR (status = 'interrupted' AND attempts < max_attempts) "
                    "ORDER BY created_at"
                ).fetchall()
                requeue = [r["id"] for r in rows]

        for job_id in requeue:
            with get_conn(self.db_path) as conn:
                conn.execute(
                    "UPDATE jobs SET status = 'queued', updated_at = ? WHERE id = ?",
                    (utcnow(), job_id),
                )
            self.enqueue_nowait(job_id)

        if orphaned or requeue:
            log.info(
                "recovery: %d interrupted, %d re-queued", len(orphaned), len(requeue)
            )
        return {"interrupted": len(orphaned), "requeued": len(requeue)}

    # -- internals --------------------------------------------------------- #

    async def _worker(self, n: int) -> None:
        while not self._stopping.is_set():
            try:
                job_id = await self._queue.get()
            except asyncio.CancelledError:
                return
            try:
                await self._run_job(job_id)
            except asyncio.CancelledError:
                return
            except Exception:  # pragma: no cover - defensive
                log.exception("worker %d crashed handling job %s", n, job_id)
            finally:
                self._queue.task_done()

    async def _run_job(self, job_id: str) -> None:
        with get_conn(self.db_path) as conn:
            row = get_job(conn, job_id)
            if row is None:
                log.warning("job %s vanished before it ran", job_id)
                return
            if row["status"] not in {"queued", "interrupted"}:
                return
            if row["cancel_requested"]:
                conn.execute(
                    "UPDATE jobs SET status = 'cancelled', finished_at = ?, "
                    "updated_at = ? WHERE id = ?",
                    (utcnow(), utcnow(), job_id),
                )
                return

            conn.execute(
                "UPDATE jobs SET status = 'running', started_at = COALESCE(started_at, ?), "
                "attempts = attempts + 1, updated_at = ?, heartbeat_at = ? WHERE id = ?",
                (utcnow(), utcnow(), utcnow(), job_id),
            )
            job_type = row["type"]
            payload = json.loads(row["payload_json"] or "{}")

        body = JOB_BODIES.get(job_type)
        ctx = JobContext(job_id, job_type, payload, db_path=self.db_path)

        if body is None:
            self._finish_failed(job_id, None, f"No handler registered for {job_type!r}")
            return

        try:
            result = await body(ctx)
        except JobCancelled:
            with get_conn(self.db_path) as conn:
                conn.execute(
                    "UPDATE jobs SET status = 'cancelled', finished_at = ?, "
                    "updated_at = ? WHERE id = ?",
                    (utcnow(), utcnow(), job_id),
                )
                conn.execute(
                    "INSERT INTO job_events (job_id, ts, level, message) "
                    "VALUES (?, ?, 'warn', 'Cancelled')",
                    (job_id, utcnow()),
                )
            log.info("job %s cancelled", job_id)
        except Exception as exc:
            self._finish_failed(job_id, ctx._current, str(exc), traceback.format_exc())
            log.exception("job %s failed in stage %s", job_id, ctx._current)
        else:
            with get_conn(self.db_path) as conn:
                conn.execute(
                    "UPDATE jobs SET status = 'succeeded', progress = 1.0, "
                    "result_json = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(result or {}), utcnow(), utcnow(), job_id),
                )
                conn.execute(
                    "INSERT INTO job_events (job_id, ts, level, message, progress) "
                    "VALUES (?, ?, 'info', 'Done', 1.0)",
                    (job_id, utcnow()),
                )
            log.info("job %s succeeded", job_id)

    def _finish_failed(
        self, job_id: str, stage: str | None, message: str, trace: str | None = None
    ) -> None:
        with get_conn(self.db_path) as conn:
            conn.execute(
                "UPDATE jobs SET status = 'failed', error = ?, error_stage = ?, "
                "error_trace = ?, finished_at = ?, updated_at = ? WHERE id = ?",
                (message, stage, (trace or "")[:8000], utcnow(), utcnow(), job_id),
            )
            conn.execute(
                "INSERT INTO job_events (job_id, ts, stage, level, message) "
                "VALUES (?, ?, ?, 'error', ?)",
                (job_id, utcnow(), stage, message),
            )

    async def _watchdog_loop(self) -> None:
        """Fail jobs whose heartbeat has gone stale so a wedged socket can't
        hold a worker slot forever."""
        settings = get_settings()
        while not self._stopping.is_set():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return
            try:
                cutoff = (
                    datetime.now(timezone.utc)
                    - timedelta(seconds=settings.job_stale_seconds)
                ).isoformat()
                with get_conn(self.db_path) as conn:
                    stale = conn.execute(
                        "SELECT id FROM jobs WHERE status = 'running' "
                        "AND COALESCE(heartbeat_at, started_at) < ?",
                        (cutoff,),
                    ).fetchall()
                    for row in stale:
                        conn.execute(
                            "UPDATE jobs SET status = 'failed', error = 'stalled: no "
                            "heartbeat', finished_at = ?, updated_at = ? WHERE id = ?",
                            (utcnow(), utcnow(), row["id"]),
                        )
                if stale:
                    log.warning("watchdog failed %d stalled job(s)", len(stale))
            except Exception:  # pragma: no cover - defensive
                log.exception("watchdog iteration failed")


# --------------------------------------------------------------------------- #
# Module-level singleton
# --------------------------------------------------------------------------- #

_queue: JobQueue | None = None


def get_queue() -> JobQueue:
    global _queue
    if _queue is None:
        _queue = JobQueue()
    return _queue


def set_queue(queue: JobQueue | None) -> None:
    global _queue
    _queue = queue
