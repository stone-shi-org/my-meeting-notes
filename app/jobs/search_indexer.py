"""The background half of search indexing (MMN-15).

One always-started ``asyncio.Task``, the same shape as ``AutoBackfillScheduler``.
Each cycle it:

1. runs :func:`search_index.reconcile` if it is due (at startup, then every
   ``search_reconcile_interval_minutes``) -- the safety net that finds any
   scope a write path changed without marking it;
2. drains ``search_dirty`` -- re-rendering each scope in its own short
   ``BEGIN IMMEDIATE`` transaction inside ``asyncio.to_thread``;
3. if ``embedding_enabled``, runs one bounded :func:`search_embed.embed_pass`.

Write paths nudge it through ``search_index.set_waker`` so a new note is
searchable within about a second rather than at the next idle tick. The nudge
can arrive before the writer has committed, so the loop waits ``SETTLE_SEC``
after waking before it reads the queue.

Why not the job queue: the same reasons as email hydration. Indexing is
invisible maintenance that would bury people's uploads in the progress dock,
and restart survival is free -- ``search_dirty`` plus the reconcile *are* the
resume state.

An embedding failure backs off exponentially (capped at ``MAX_BACKOFF_SEC``)
without stopping keyword indexing; semantic search meanwhile fails open to
keyword results at query time.
"""

from __future__ import annotations

import asyncio
import time

from app.config import get_settings
from app.db import connect
from app.errors import EmbeddingError
from app.logging_config import get_logger
from app.services import search_embed, search_index

log = get_logger("search_indexer")

IDLE_SEC = 5.0
SETTLE_SEC = 0.5
DRAIN_BATCH = 200
MAX_BACKOFF_SEC = 600.0

_indexer: "SearchIndexer | None" = None


def set_indexer(indexer: "SearchIndexer | None") -> None:
    global _indexer
    _indexer = indexer


def nudge_indexer() -> None:
    if _indexer is not None:
        _indexer.nudge()


class SearchIndexer:
    def __init__(self, db_path=None):
        self.db_path = db_path
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_reconcile: float | None = None
        self._embed_backoff_until = 0.0
        self._embed_backoff = 0.0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._loop = asyncio.get_running_loop()
        self._stopping.clear()
        search_index.set_waker(self.nudge)
        self._task = asyncio.create_task(self._run(), name="mmn-search-indexer")

    async def stop(self) -> None:
        search_index.set_waker(None)
        self._stopping.set()
        self._wake.set()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - shutdown must not raise
            log.exception("search indexer raised on shutdown")

    def nudge(self) -> None:
        """Thread-safe: called from request threads and to_thread workers."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._wake.set)
        except RuntimeError:  # pragma: no cover - loop shutting down
            pass

    # ------------------------------------------------------------------ #

    def _reconcile_and_drain(self, *, reconcile: bool) -> int:
        conn = connect(self.db_path)
        try:
            if reconcile:
                search_index.reconcile(conn)
                conn.commit()
            return search_index.drain_dirty(conn, limit=DRAIN_BATCH)
        finally:
            conn.close()

    async def run_once(self) -> dict:
        """One cycle. Public so a test or an operator can drive it. Never raises."""
        interval = max(1, int(get_settings().search_reconcile_interval_minutes)) * 60
        now = time.monotonic()
        due = self._last_reconcile is None or now - self._last_reconcile >= interval
        indexed = embedded_scopes = 0
        try:
            indexed = await asyncio.to_thread(self._reconcile_and_drain, reconcile=due)
            if due:
                self._last_reconcile = now
        except Exception:
            log.exception("search indexing cycle failed")

        if time.monotonic() >= self._embed_backoff_until:
            try:
                result = await search_embed.embed_pass(self.db_path)
                embedded_scopes = result["scopes"]
                self._embed_backoff = 0.0
            except EmbeddingError as exc:
                self._embed_backoff = min(MAX_BACKOFF_SEC, max(5.0, self._embed_backoff * 2))
                self._embed_backoff_until = time.monotonic() + self._embed_backoff
                log.warning(
                    "search embedding failed, retrying in %.0fs: %s", self._embed_backoff, exc.message
                )
            except Exception:
                log.exception("search embedding pass crashed")
        return {"indexed": indexed, "embedded_scopes": embedded_scopes}

    async def _run(self) -> None:
        while not self._stopping.is_set():
            result = await self.run_once()
            if result["indexed"] or result["embedded_scopes"]:
                await asyncio.sleep(0)  # more may be waiting; go again
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=IDLE_SEC)
            except asyncio.TimeoutError:
                continue
            if self._stopping.is_set():
                break
            await asyncio.sleep(SETTLE_SEC)
