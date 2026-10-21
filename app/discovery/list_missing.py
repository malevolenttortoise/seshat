"""The per-author "list missing books" switch (D6 / F8 / F18, ADR-0029).

Off: scans skip the author (`roster.scan_skip_reason`), and the author's
missing books already listed are hidden with reason `author_off` — a book
another of its contributors still lists stays (C6: "still lists" = that
contributor is scan-eligible with the switch on). On again: those books
show at once, and the Goodreads candidates set aside for the author go
back to the queue. MAM announces for the author still pass the filter: the
switch is about the missing-books list, not grabbing. First use: Tom
Clancy (434 missing, owns none).
"""
from __future__ import annotations

import logging
from typing import Optional

from app.discovery import filters
from app.discovery.database import get_db, recompute_all_series_author_mode

logger = logging.getLogger("seshat.discovery.list_missing")


async def _listed_by_others(db, roster, book_id: int, author_id: int) -> bool:
    rows = await (await db.execute(
        "SELECT a.id, a.name FROM book_authors ba JOIN authors a ON a.id = ba.author_id "
        "WHERE ba.book_id = ? AND ba.author_id != ?",
        (book_id, author_id),
    )).fetchall()
    return any(roster.is_scan_eligible(r[1] or "", r[0]) for r in rows)


async def _books_hidden_if_off(db, roster, author_id: int) -> list[int]:
    # A book Mark showed (`shown`) stays shown: "Show" is for good (F19).
    rows = await (await db.execute(
        "SELECT b.id FROM books b JOIN book_authors ba ON ba.book_id = b.id "
        "WHERE ba.author_id = ? AND b.owned = 0 AND b.hidden = 0 "
        "AND COALESCE(b.hidden_reason, '') != ?",
        (author_id, filters.SHOWN),
    )).fetchall()
    return [r[0] for r in rows if not await _listed_by_others(db, roster, r[0], author_id)]


async def status(author_id: int, slug: Optional[str] = None) -> dict:
    """The switch, and how many missing books turning it off would hide
    (F18's "hides the {n} already listed")."""
    from app.discovery.roster import load_roster
    db = await get_db(slug)
    try:
        row = await (await db.execute(
            "SELECT list_missing FROM authors WHERE id = ?", (author_id,),
        )).fetchone()
        if row is None:
            return {"found": False}
        on = bool(row[0])
        if on:
            roster = await load_roster(db, slug)
            n = len(await _books_hidden_if_off(db, roster, author_id))
        else:
            n = (await (await db.execute(
                "SELECT COUNT(*) FROM books b JOIN book_authors ba ON ba.book_id = b.id "
                "WHERE ba.author_id = ? AND b.hidden_reason = ?",
                (author_id, filters.AUTHOR_OFF),
            )).fetchone())[0]
        return {"found": True, "on": on, "missing": n}
    finally:
        await db.close()


async def set_list_missing(author_id: int, on: bool, slug: Optional[str] = None) -> dict:
    """Turn the switch on or off; returns {on, hidden | shown}."""
    from app.discovery import roster as roster_mod
    db = await get_db(slug)
    try:
        row = await (await db.execute(
            "SELECT name, list_missing FROM authors WHERE id = ?", (author_id,),
        )).fetchone()
        if row is None:
            return {"found": False}
        await db.execute(
            "UPDATE authors SET list_missing = ? WHERE id = ?", (1 if on else 0, author_id),
        )
        await db.commit()
        roster_mod.invalidate(slug)
        roster = await roster_mod.load_roster(db, slug, force=True)
        if on:
            ids = [r[0] for r in await (await db.execute(
                "SELECT b.id FROM books b JOIN book_authors ba ON ba.book_id = b.id "
                "WHERE ba.author_id = ? AND b.hidden_reason = ?",
                (author_id, filters.AUTHOR_OFF),
            )).fetchall()]
            if ids:
                ph = ",".join("?" * len(ids))
                await db.execute(
                    f"UPDATE books SET hidden = 0, hidden_reason = NULL WHERE id IN ({ph})", ids,
                )
            result = {"found": True, "on": True, "shown": len(ids)}
        else:
            ids = await _books_hidden_if_off(db, roster, author_id)
            if ids:
                ph = ",".join("?" * len(ids))
                await db.execute(
                    f"UPDATE books SET hidden = 1, hidden_reason = ? WHERE id IN ({ph})",
                    [filters.AUTHOR_OFF, *ids],
                )
            result = {"found": True, "on": False, "hidden": len(ids)}
        if ids:
            await recompute_all_series_author_mode(db, context="list missing books")
        await db.commit()
    finally:
        await db.close()
    if on:
        await _requeue_goodreads_candidates(author_id, slug)
    logger.info(
        "list missing books %s for %r: %s", "on" if on else "off", row[0],
        {k: v for k, v in result.items() if k in ("hidden", "shown")},
    )
    return result


async def _requeue_goodreads_candidates(author_id: int, slug: Optional[str]) -> None:
    """Candidates the worker set aside while the switch was off go back."""
    try:
        from app.discovery import metadata_cache
        from app.discovery.database import get_active_library
        from app.discovery.roster import SKIP_LIST_MISSING_OFF
        lib = slug or get_active_library()
        db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
        try:
            ct = metadata_cache.candidates_table(metadata_cache.SOURCE_GOODREADS)
            at = metadata_cache.candidate_authors_table(metadata_cache.SOURCE_GOODREADS)
            await db.execute(
                f"UPDATE {ct} SET state = 'pending', reason = NULL "
                f"WHERE state = 'skipped' AND reason = ? AND library_slug = ? AND author_id IN "
                f"(SELECT author_id FROM {at} WHERE seshat_author_id = ? AND library_slug = ?)",
                (SKIP_LIST_MISSING_OFF, lib, author_id, lib),
            )
            await db.commit()
        finally:
            await db.close()
    except Exception:
        logger.debug("list missing books: Goodreads candidates not requeued", exc_info=True)
