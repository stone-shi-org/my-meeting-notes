"""The timer behind unattended email backfill.

Same shape as ``jobs/scheduler.py``'s ``AutoMatchScheduler``, and for the same
reasons: one asyncio task, started and stopped by the FastAPI lifespan, woken
on a fixed tick, asking which *users* are due and sweeping them one at a time.

Why not the job queue: hydration and summarisation are already checkpointed
against ``body_fetched_at`` and the summary predicate (see
``services/email_bodies.py``), which *are* the resume state -- a job would add
restart survival this already has for free, and put invisible maintenance in
the progress dock next to the diarizations people are actually waiting on.

Users are swept **sequentially**, for the same reason threads are in
``AutoMatchScheduler``: a burst of concurrent hydration/summary calls is
exactly what would start rate-limiting the interactive path -- someone else's
manual Settings click, or a thread being opened right now.

One process is assumed, as it is for the job queue and the auto-match sweep:
since a user is stamped after their sweep rather than claimed before it, two
workers could double-sweep the same account.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from app.config import effective, get_settings
from app.db import get_conn
from app.logging_config import get_logger
from app.services import email_bodies as email_bodies_svc

log = get_logger("email_backfill_scheduler")


def due_users(conn: sqlite3.Connection, *, now: datetime) -> list[sqlite3.Row]:
    """Active users whose turn it is, oldest-swept first.

    Excludes deactivated users, same as ``due_threads`` excludes threads owned
    by one. Ordering by ``auto_backfill_at`` with NULLs first means a brand
    new account is swept on the next tick, and no account can be starved by
    the per-cycle cap: whatever was skipped is oldest next time round.
    """
    interval = int(effective(conn, "auto_backfill_interval_minutes"))
    limit = int(effective(conn, "auto_backfill_max_users_per_cycle"))
    due_before = (now - timedelta(minutes=max(1, interval))).isoformat()

    return conn.execute(
        """
        SELECT id FROM users
         WHERE is_active = 1
           AND (auto_backfill_at IS NULL OR auto_backfill_at < ?)
         ORDER BY auto_backfill_at IS NOT NULL, auto_backfill_at, id
         LIMIT ?
        """,
        (due_before, limit),
    ).fetchall()


async def _sweep_user(
    conn_factory,
    *,
    user_id: int,
    db_path,
    max_rounds: int,
) -> dict:
    """Drain as much of one user's backfill backlog as the round cap allows.

    Alternates a bodies batch and a summaries batch -- the exact same two
    calls ``routers/email_backfill.py`` already exposes to the manual button
    -- until both report nothing left, a batch makes no progress, a summary
    batch stalls (the LLM is down: the same interlock ``rank_sync`` returning
    ``None`` gives the follow-up sweep), or ``max_rounds`` is reached.

    Never raises: this is unattended work, and one user's provider or LLM
    being down must not stop the cycle for everyone swept after them.
    """
    bodies_fetched = summaries_done = 0
    bodies_exhausted = summaries_exhausted = False
    stalled = False

    try:
        for _ in range(max_rounds):
            if bodies_exhausted and summaries_exhausted:
                break

            if not bodies_exhausted:
                with conn_factory() as conn:
                    target = email_bodies_svc.next_thread_needing_bodies(conn, user_id)
                if target is None:
                    bodies_exhausted = True
                else:
                    result = await email_bodies_svc.hydrate_thread_emails(
                        db_path, thread_id=target["thread_id"], user_id=user_id
                    )
                    bodies_fetched += result["fetched"]
                    if result["fetched"] == 0:
                        # No progress on this batch -- the account cannot
                        # supply any more right now. Stop asking rather than
                        # spinning through the same "unavailable" rows.
                        bodies_exhausted = True

            if not summaries_exhausted:
                with conn_factory() as conn:
                    target = email_bodies_svc.next_thread_needing_summaries(conn, user_id)
                if target is None:
                    summaries_exhausted = True
                else:
                    result = await email_bodies_svc.summarise_thread_emails(
                        db_path, thread_id=target["thread_id"]
                    )
                    summaries_done += result["summarised"]
                    if result["requested"] > 0 and result["summarised"] == 0:
                        stalled = True
                        summaries_exhausted = True
                    elif result["summarised"] == 0:
                        summaries_exhausted = True
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - defensive, mirrors sweep_thread's crash path
        log.exception("backfill sweep of user %s crashed", user_id)

    return {
        "bodies_fetched": bodies_fetched,
        "summaries_done": summaries_done,
        "stalled": stalled,
    }


class AutoBackfillScheduler:
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
        self._task = asyncio.create_task(self._loop(), name="mmn-auto-backfill")

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
            log.exception("auto-backfill loop raised on shutdown")

    def _conn_factory(self):
        return get_conn(self.db_path)

    async def run_due(self, *, now: datetime | None = None) -> dict:
        """One cycle. Public so a test -- and the operator -- can trigger it.

        Returns a summary; never raises. A cycle that blew up would take the
        whole loop task with it and silently stop every future sweep.
        """
        moment = now or datetime.now(timezone.utc)

        with self._conn_factory() as conn:
            if not effective(conn, "auto_backfill_enabled"):
                return {
                    "enabled": False, "swept": 0,
                    "bodies_fetched": 0, "summaries_done": 0, "stalled_users": 0,
                }
            candidates = due_users(conn, now=moment)
            max_rounds = int(effective(conn, "auto_backfill_max_rounds_per_user"))

        swept = bodies_fetched = summaries_done = stalled_users = 0
        for row in candidates:
            if self._stopping.is_set():
                break
            try:
                result = await _sweep_user(
                    self._conn_factory,
                    user_id=row["id"],
                    db_path=self.db_path,
                    max_rounds=max_rounds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                log.exception("backfill sweep of user %s crashed", row["id"])
                result = {"bodies_fetched": 0, "summaries_done": 0, "stalled": False}

            # Stamped even on failure -- a broken account must not turn into a
            # request storm re-tried on every tick. Same rule as
            # threads.auto_match_at.
            with self._conn_factory() as conn:
                conn.execute(
                    "UPDATE users SET auto_backfill_at = ? WHERE id = ?",
                    (moment.isoformat(), row["id"]),
                )

            swept += 1
            bodies_fetched += result["bodies_fetched"]
            summaries_done += result["summaries_done"]
            if result["stalled"]:
                stalled_users += 1

        if bodies_fetched or summaries_done:
            log.info(
                "auto-backfill cycle: swept %d user(s), fetched %d body/bodies, "
                "summarised %d",
                swept, bodies_fetched, summaries_done,
            )
        return {
            "enabled": True,
            "swept": swept,
            "bodies_fetched": bodies_fetched,
            "summaries_done": summaries_done,
            "stalled_users": stalled_users,
        }

    async def _loop(self) -> None:
        tick = max(5, get_settings().auto_backfill_tick_seconds)
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
                log.exception("auto-backfill cycle failed")


_backfill_scheduler: AutoBackfillScheduler | None = None


def get_backfill_scheduler() -> AutoBackfillScheduler:
    global _backfill_scheduler
    if _backfill_scheduler is None:
        _backfill_scheduler = AutoBackfillScheduler()
    return _backfill_scheduler


def set_backfill_scheduler(scheduler: AutoBackfillScheduler | None) -> None:
    global _backfill_scheduler
    _backfill_scheduler = scheduler
