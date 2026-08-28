"""Account-wide email backfill: how far along it is, and a way to finish it.

Hydration is lazy by design -- it fills a thread in as you open it. The cost of
that is invisibility: an account with two hundred attached emails across threads
nobody has revisited stays mostly un-backfilled, and there is nothing in the app
that says so. This is the page that says so, plus a button.

**Still not a queued job.** Each POST does one bounded unit of work and reports
what is left, and the SPA calls it again. That keeps the whole thing built out of
the same checkpointed pieces the lazy path uses -- ``body_fetched_at`` and the
summary predicate *are* the resume state, so navigating away mid-backfill loses
nothing and pressing the button again continues from where it stopped. A job
would add restart survival this already has, and put a long-running item in the
progress dock next to the diarizations people are actually waiting on.

Per-user, not admin: these are the caller's own threads and their own connected
accounts, and the work is charged to their own provider quota and LLM spend.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends

from app.deps import CurrentUser, active_user, get_db
from app.logging_config import get_logger
from app.services import email_bodies as email_bodies_svc

router = APIRouter(prefix="/api/email-backfill", tags=["email-backfill"])
log = get_logger("email_backfill")


@router.get("/stats")
def backfill_stats(
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Counts across every email on every thread this user owns."""
    return email_bodies_svc.account_stats(conn, user.id)


@router.post("/bodies")
async def backfill_bodies(
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Fetch one batch of bodies, from whichever thread has the most outstanding.

    Most-outstanding-first so the progress bar moves fastest at the start and a
    single large thread cannot be starved behind a queue of small ones.

    No LLM call happens here -- see ``/summaries`` below, which is separate
    because it costs money per message.
    """
    target = email_bodies_svc.next_thread_needing_bodies(conn, user.id)
    if target is None:
        return {"done": True, "thread_id": None, "thread_title": None}

    result = await email_bodies_svc.hydrate_thread_emails(
        None, thread_id=target["thread_id"], user_id=user.id
    )
    with_stats = {
        "done": False,
        "thread_id": target["thread_id"],
        "thread_title": target["title"],
        **result,
    }
    log.info(
        "user %s backfilled %s/%s bodies on thread %s",
        user.username, result["fetched"], result["requested"], target["thread_id"],
    )
    return with_stats


@router.post("/summaries")
async def backfill_summaries(
    user: CurrentUser = Depends(active_user),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Summarise one batch of stored bodies. One LLM call per message.

    Deliberately a separate button from the bodies pass, and deliberately not
    reached by it: the SPA shows the outstanding count first, so nobody starts a
    two-hundred-call run without seeing the number.
    """
    target = email_bodies_svc.next_thread_needing_summaries(conn, user.id)
    if target is None:
        return {"done": True, "thread_id": None, "thread_title": None}

    result = await email_bodies_svc.summarise_thread_emails(
        None, thread_id=target["thread_id"]
    )
    # A batch that requested work and summarised none is a failing LLM. Say so,
    # or the client loops forever on rows that stay eligible by design.
    stalled = result["requested"] > 0 and result["summarised"] == 0
    log.info(
        "user %s summarised %s/%s on thread %s%s",
        user.username, result["summarised"], result["requested"],
        target["thread_id"], " (stalled)" if stalled else "",
    )
    return {
        "done": False,
        "stalled": stalled,
        "thread_id": target["thread_id"],
        "thread_title": target["title"],
        **result,
    }
