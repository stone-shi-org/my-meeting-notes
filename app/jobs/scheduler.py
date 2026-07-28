"""The timer behind the periodic re-match.

One asyncio task, started and stopped by the FastAPI lifespan alongside the job
queue's watchdog. It wakes on a fixed tick, asks which threads are due, and
sweeps them one at a time.

Why not the job queue: a sweep is invisible maintenance, and every run through
``jobs`` writes a job row plus a handful of ``job_events`` that would appear in
the progress dock the SPA polls. Multiply that by every thread every half hour
and the user's own uploads are buried in machine noise. The queue also exists to
make expensive work survive a restart; a sweep that dies is simply due again on
the next tick.

Threads are swept **sequentially**. The point of a background sweep is to be
unnoticeable, and a burst of N concurrent searches is exactly what makes a
provider start rate-limiting the interactive match the user is waiting on.

One process is assumed, as it is for the job queue: ``run.py`` starts a single
uvicorn worker. Two processes would each run this loop, and since a thread is
stamped after its sweep rather than claimed before it, both could sweep the same
thread at once. Adding workers means giving this a lease first.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from app.config import effective, get_settings
from app.db import get_conn
from app.logging_config import get_logger
from app.services import followups as followups_svc

log = get_logger("scheduler")


def due_threads(conn: sqlite3.Connection, *, now: datetime) -> list[sqlite3.Row]:
    """Watched threads whose turn it is, oldest-swept first.

    Excluded: archived threads, threads belonging to a deactivated user, and
    threads nobody has touched in ``auto_match_idle_days``. The last one is what
    keeps the sweep from growing without bound -- a thread from last year has no
    follow-ups coming, and searching for them costs the same as searching for
    real ones.

    Ordering by ``auto_match_at`` with NULLs first means a newly created thread
    is looked at on the next tick, and no thread can be starved by the per-cycle
    cap: whatever was skipped is the oldest next time round.
    """
    interval = int(effective(conn, "auto_match_interval_minutes"))
    idle_days = int(effective(conn, "auto_match_idle_days"))
    limit = int(effective(conn, "auto_match_max_threads_per_cycle"))

    due_before = (now - timedelta(minutes=max(1, interval))).isoformat()
    idle_before = (now - timedelta(days=max(1, idle_days))).isoformat()

    return conn.execute(
        """
        SELECT t.id, t.owner_id
          FROM threads t
          JOIN users u ON u.id = t.owner_id
         WHERE t.archived = 0
           AND u.is_active = 1
           AND t.updated_at >= ?
           AND (t.auto_match_at IS NULL OR t.auto_match_at < ?)
         ORDER BY t.auto_match_at IS NOT NULL, t.auto_match_at, t.id
         LIMIT ?
        """,
        (idle_before, due_before, limit),
    ).fetchall()


class AutoMatchScheduler:
    def __init__(self, db_path=None):
        self.db_path = db_path
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="mmn-auto-match")

    async def stop(self) -> None:
        self._stopping.set()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - shutdown must not raise
            log.exception("auto-match loop raised on shutdown")

    def _conn_factory(self):
        return get_conn(self.db_path)

    async def run_due(self, *, now: datetime | None = None) -> dict:
        """One cycle. Public so a test -- and the operator -- can trigger it.

        Returns a summary; never raises. A cycle that blew up would take the
        whole loop task with it and silently stop every future sweep.
        """
        moment = now or datetime.now(timezone.utc)

        with self._conn_factory() as conn:
            if not effective(conn, "auto_match_enabled"):
                return {"enabled": False, "swept": 0, "attached": 0}
            candidates = due_threads(conn, now=moment)

        swept = attached = 0
        for row in candidates:
            if self._stopping.is_set():
                break
            try:
                result = await followups_svc.sweep_thread(
                    self._conn_factory,
                    thread_id=row["id"],
                    user_id=row["owner_id"],
                    db_path=self.db_path,
                    now=moment,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                log.exception("sweep of thread %s crashed", row["id"])
                continue
            swept += 1
            attached += result["attached_events"] + result["attached_emails"]

        if attached:
            log.info("auto-match cycle: swept %d thread(s), attached %d item(s)",
                     swept, attached)
        return {"enabled": True, "swept": swept, "attached": attached}

    async def _loop(self) -> None:
        tick = max(5, get_settings().auto_match_tick_seconds)
        while not self._stopping.is_set():
            try:
                await asyncio.sleep(tick)
            except asyncio.CancelledError:
                return
            try:
                await self.run_due()
            except asyncio.CancelledError:
                return
            except Exception:  # pragma: no cover - defensive
                log.exception("auto-match cycle failed")


_scheduler: AutoMatchScheduler | None = None


def get_scheduler() -> AutoMatchScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AutoMatchScheduler()
    return _scheduler


def set_scheduler(scheduler: AutoMatchScheduler | None) -> None:
    global _scheduler
    _scheduler = scheduler
