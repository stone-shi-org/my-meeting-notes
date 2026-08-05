"""Thread groups: the folders the home screen lays threads out in.

A group is a name and an owner, nothing more. Membership lives on the thread
(``threads.group_id``), so there is exactly one place to change when a card is
dragged, and "Ungrouped" costs no row: it is ``group_id IS NULL``, which every
thread written before this feature already satisfies.
"""

from __future__ import annotations

import sqlite3

from app.db import utcnow
from app.errors import ConflictError, NotFoundError

# Threads in the group regardless of the home screen's search/archived filters:
# it is the placeholder the heading shows before that section's own (filtered)
# query lands, and the number the delete confirmation quotes.
GROUP_COUNTS_SQL = """
    (SELECT COUNT(*) FROM threads t WHERE t.group_id = g.id) AS thread_count
"""


def row_to_group(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "owner_id": row["owner_id"],
        "name": row["name"],
        "thread_count": row["thread_count"] if "thread_count" in row.keys() else 0,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_groups(
    conn: sqlite3.Connection, *, scope_sql: str, scope_params: list
) -> list[sqlite3.Row]:
    """Every group the caller can see, ordered the way the home screen shows them.

    By name, because that is the only order the user controls -- there is no
    drag-to-reorder for groups, so creation order would look arbitrary the
    moment a second group exists.
    """
    return conn.execute(
        f"""
        SELECT g.*, {GROUP_COUNTS_SQL}
          FROM thread_groups g
         WHERE {scope_sql.replace("owner_id", "g.owner_id")}
         ORDER BY g.name COLLATE NOCASE, g.id
        """,
        list(scope_params),
    ).fetchall()


def get_group(conn: sqlite3.Connection, group_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT g.*, {GROUP_COUNTS_SQL} FROM thread_groups g WHERE g.id = ?",
        (group_id,),
    ).fetchone()


def require_group(conn: sqlite3.Connection, group_id: int) -> sqlite3.Row:
    row = get_group(conn, group_id)
    if row is None:
        raise NotFoundError("Group not found")
    return row


def create_group(conn: sqlite3.Connection, *, owner_id: int, name: str) -> sqlite3.Row:
    now = utcnow()
    try:
        cur = conn.execute(
            "INSERT INTO thread_groups (owner_id, name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (owner_id, name, now, now),
        )
    except sqlite3.IntegrityError as exc:
        raise _duplicate(name) from exc
    return require_group(conn, cur.lastrowid)  # type: ignore[arg-type]


def rename_group(conn: sqlite3.Connection, *, group_id: int, name: str) -> sqlite3.Row:
    try:
        conn.execute(
            "UPDATE thread_groups SET name = ?, updated_at = ? WHERE id = ?",
            (name, utcnow(), group_id),
        )
    except sqlite3.IntegrityError as exc:
        raise _duplicate(name) from exc
    return require_group(conn, group_id)


def delete_group(conn: sqlite3.Connection, group_id: int) -> int:
    """Remove the folder. Its threads fall back to Ungrouped, they are not deleted.

    The ``ON DELETE SET NULL`` on ``threads.group_id`` does that -- and only
    because ``db.connect`` turns foreign keys on, which is off by default in
    SQLite. Returns how many threads were released, for the log line.
    """
    released = conn.execute(
        "SELECT COUNT(*) FROM threads WHERE group_id = ?", (group_id,)
    ).fetchone()[0]
    conn.execute("DELETE FROM thread_groups WHERE id = ?", (group_id,))
    return released


def set_thread_group(
    conn: sqlite3.Connection, *, thread_id: int, group_id: int | None
) -> None:
    """Move one thread into a group, or out of all of them when ``group_id`` is None.

    ``threads.updated_at`` is deliberately not bumped: filing a thread is not
    activity on it, and the home screen's default sort is last activity, so
    touching it would send every card you tidied to the top of the list.
    """
    conn.execute(
        "UPDATE threads SET group_id = ? WHERE id = ?", (group_id, thread_id)
    )


def _duplicate(name: str) -> ConflictError:
    return ConflictError(f"You already have a group called “{name}”")
