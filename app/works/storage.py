"""
Work-links storage — CRUD helpers for the cross-library link table.

The caller either passes an open DB connection (test path) or lets the
helper open a fresh one (production path). Writes are always followed
by a commit on helper-opened connections; injected connections are
left for the caller to commit/close.

Why stringy `library_slug` + `book_id` instead of a foreign key?
The linked rows live in per-library discovery DBs (`seshat_{slug}.db`),
not in `seshat.db`. SQLite can't FK across files. The `reconcile`
helper handles orphan cleanup by scanning link rows against the
live per-library DBs during sync.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

import aiosqlite


@dataclass(frozen=True)
class WorkLink:
    """One (library, book) → work_id membership."""
    id: int
    work_id: str
    library_slug: str
    book_id: int
    content_type: str
    link_source: str  # "auto" | "manual"
    created_at: float


async def _open() -> aiosqlite.Connection:
    from app.database import get_db
    return await get_db()


def generate_work_id() -> str:
    """Return a new random work_id (UUID4, hex-only, no dashes).

    We use random UUIDs instead of deriving from (author, title) hash so
    a cosmetic metadata cleanup doesn't accidentally unify two works
    that were deliberately kept separate (e.g. two unrelated books that
    happen to share a normalized key).
    """
    return uuid.uuid4().hex


# ─── Queries ──────────────────────────────────────────────────

async def get_link(
    library_slug: str, book_id: int, *, db: Optional[aiosqlite.Connection] = None
) -> Optional[WorkLink]:
    close_after = db is None
    if db is None:
        db = await _open()
    try:
        row = await (await db.execute(
            "SELECT id, work_id, library_slug, book_id, content_type, "
            "link_source, created_at FROM work_links "
            "WHERE library_slug = ? AND book_id = ?",
            (library_slug, book_id),
        )).fetchone()
        return _row_to_link(row) if row else None
    finally:
        if close_after:
            await db.close()


async def get_siblings_for_books(
    library_slug: str,
    book_ids: list[int],
    *,
    db: Optional[aiosqlite.Connection] = None,
) -> dict[int, list[WorkLink]]:
    """Bulk lookup: for each book_id in the given library, return its
    cross-library work siblings (other work_links with the same work_id
    excluding self).

    Returned dict only contains entries for books that actually have a
    sibling; books with no work row or no cross-library twin are
    omitted. Callers treat a missing key as "no pairing" and render no
    badge. Two round-trips — one to resolve book→work_id, one to pull
    all siblings for those work_ids — both parameterized so hundreds of
    books resolve in two statements, not N.
    """
    if not book_ids:
        return {}
    close_after = db is None
    if db is None:
        db = await _open()
    try:
        ph = ",".join("?" * len(book_ids))
        rows = await (await db.execute(
            f"SELECT id, work_id, library_slug, book_id, content_type, "
            f"link_source, created_at FROM work_links "
            f"WHERE library_slug = ? AND book_id IN ({ph})",
            [library_slug, *book_ids],
        )).fetchall()
        # book_id → work_id map for the requested library
        self_links = [_row_to_link(r) for r in rows]
        if not self_links:
            return {}
        work_ids = list({sl.work_id for sl in self_links})
        wph = ",".join("?" * len(work_ids))
        sibling_rows = await (await db.execute(
            f"SELECT id, work_id, library_slug, book_id, content_type, "
            f"link_source, created_at FROM work_links "
            f"WHERE work_id IN ({wph})",
            work_ids,
        )).fetchall()
        all_members: dict[str, list[WorkLink]] = {}
        for r in sibling_rows:
            wl = _row_to_link(r)
            all_members.setdefault(wl.work_id, []).append(wl)
        result: dict[int, list[WorkLink]] = {}
        for sl in self_links:
            siblings = [
                m for m in all_members.get(sl.work_id, [])
                if not (m.library_slug == sl.library_slug and m.book_id == sl.book_id)
            ]
            if siblings:
                result[sl.book_id] = siblings
        return result
    finally:
        if close_after:
            await db.close()


async def get_work_members(
    work_id: str, *, db: Optional[aiosqlite.Connection] = None
) -> list[WorkLink]:
    close_after = db is None
    if db is None:
        db = await _open()
    try:
        rows = await (await db.execute(
            "SELECT id, work_id, library_slug, book_id, content_type, "
            "link_source, created_at FROM work_links WHERE work_id = ? "
            "ORDER BY content_type, library_slug",
            (work_id,),
        )).fetchall()
        return [_row_to_link(r) for r in rows]
    finally:
        if close_after:
            await db.close()


async def list_works(
    *,
    library_slug: Optional[str] = None,
    content_type: Optional[str] = None,
    db: Optional[aiosqlite.Connection] = None,
) -> list[str]:
    """Return distinct work_ids, optionally filtered.

    For Phase 5, callers fan this out into per-work member queries
    (`get_work_members`). A later optimization could pre-aggregate
    a materialized view for the Works index page, but the current
    row count (~hundreds) is nowhere near the level that matters.
    """
    close_after = db is None
    if db is None:
        db = await _open()
    try:
        where: list[str] = []
        params: list = []
        if library_slug:
            where.append("library_slug = ?")
            params.append(library_slug)
        if content_type:
            where.append("content_type = ?")
            params.append(content_type)
        sql = "SELECT DISTINCT work_id FROM work_links"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY work_id"
        rows = await (await db.execute(sql, params)).fetchall()
        return [r["work_id"] for r in rows]
    finally:
        if close_after:
            await db.close()


# ─── Mutations ────────────────────────────────────────────────

async def create_link(
    *,
    work_id: str,
    library_slug: str,
    book_id: int,
    content_type: str,
    link_source: str = "auto",
    db: Optional[aiosqlite.Connection] = None,
) -> None:
    """Insert a single membership row.

    INSERT OR IGNORE means re-running the matcher is a no-op when a
    row already exists. Use `update_work_id` to re-home a book to a
    different work.
    """
    close_after = db is None
    if db is None:
        db = await _open()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO work_links "
            "(work_id, library_slug, book_id, content_type, link_source) "
            "VALUES (?, ?, ?, ?, ?)",
            (work_id, library_slug, book_id, content_type, link_source),
        )
        if close_after:
            await db.commit()
    finally:
        if close_after:
            await db.close()


async def merge_books_into_work(
    *,
    work_id: str,
    members: list[dict],
    link_source: str = "auto",
    db: Optional[aiosqlite.Connection] = None,
) -> int:
    """Bulk-insert a set of (library_slug, book_id, content_type) members.

    Returns the number of rows actually inserted — duplicates skipped
    by the UNIQUE(library_slug, book_id) constraint don't count.
    """
    close_after = db is None
    if db is None:
        db = await _open()
    inserted = 0
    try:
        for m in members:
            cur = await db.execute(
                "INSERT OR IGNORE INTO work_links "
                "(work_id, library_slug, book_id, content_type, link_source) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    work_id, m["library_slug"], m["book_id"],
                    m["content_type"], link_source,
                ),
            )
            inserted += cur.rowcount or 0
        if close_after:
            await db.commit()
    finally:
        if close_after:
            await db.close()
    return inserted


async def unlink_book(
    library_slug: str,
    book_id: int,
    *,
    db: Optional[aiosqlite.Connection] = None,
) -> bool:
    """Remove a single (library, book) from its work. Returns True if a row went."""
    close_after = db is None
    if db is None:
        db = await _open()
    try:
        cur = await db.execute(
            "DELETE FROM work_links WHERE library_slug = ? AND book_id = ?",
            (library_slug, book_id),
        )
        if close_after:
            await db.commit()
        return (cur.rowcount or 0) > 0
    finally:
        if close_after:
            await db.close()


async def delete_work(
    work_id: str, *, db: Optional[aiosqlite.Connection] = None
) -> int:
    """Drop every membership row for `work_id`. Returns rows deleted."""
    close_after = db is None
    if db is None:
        db = await _open()
    try:
        cur = await db.execute(
            "DELETE FROM work_links WHERE work_id = ?", (work_id,),
        )
        if close_after:
            await db.commit()
        return cur.rowcount or 0
    finally:
        if close_after:
            await db.close()


async def reconcile_library(
    library_slug: str,
    live_book_ids: list[int],
    *,
    db: Optional[aiosqlite.Connection] = None,
) -> int:
    """Drop link rows for books no longer present in the library.

    Called at the end of each sync with the id list the sync just
    upserted. Any `work_links` row for this library whose `book_id`
    isn't in that set is stale (the book was pruned from the source
    library) and gets dropped. Returns rows deleted.

    Safety: a zero-length `live_book_ids` skips the reconcile entirely
    because it most likely means a transient read error — same safety
    net calibre_sync / audiobookshelf_sync apply to their own prune
    pass.
    """
    if not live_book_ids:
        return 0
    close_after = db is None
    if db is None:
        db = await _open()
    try:
        placeholders = ",".join("?" * len(live_book_ids))
        cur = await db.execute(
            f"DELETE FROM work_links WHERE library_slug = ? "
            f"AND book_id NOT IN ({placeholders})",
            [library_slug, *live_book_ids],
        )
        if close_after:
            await db.commit()
        return cur.rowcount or 0
    finally:
        if close_after:
            await db.close()


# ─── Helpers ──────────────────────────────────────────────────

def _row_to_link(row) -> WorkLink:
    return WorkLink(
        id=row["id"],
        work_id=row["work_id"],
        library_slug=row["library_slug"],
        book_id=row["book_id"],
        content_type=row["content_type"],
        link_source=row["link_source"],
        created_at=row["created_at"],
    )
