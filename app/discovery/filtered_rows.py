"""Filtered rows in the library DBs (ADR-0029): the settings that let a
family through, and "Show" / "Hide" on a row.

The merge stores a filtered book's family in `books.hidden_reason` and hides
it when its family's setting says so (`filters.hides`). Turning "Hide comics
and manga" or "Hide box sets and omnibuses" off shows that family's rows at
once, and on again hides them again: no re-scan. A row Mark showed carries
`shown` and is left alone; a book he hid himself carries no reason.
"""
from __future__ import annotations

import logging
from typing import Iterable

from app.discovery import filters

logger = logging.getLogger("seshat.discovery.filtered_rows")

# SQL for the hide / show endpoints: Mark's own hide is his (no reason); a
# Show on a row Seshat hid sticks (`shown`), so neither a later scan nor the
# family's setting hides it again.
HIDE_SET = "hidden = 1, hidden_reason = NULL"
SHOW_SET = "hidden = 0, hidden_reason = CASE WHEN hidden_reason IS NULL THEN NULL ELSE 'shown' END"


def _slugs() -> list[str]:
    from app import state
    return [lib["slug"] for lib in (state._discovered_libraries or []) if lib.get("slug")]


async def apply_family_settings(settings: dict, families: Iterable[str] | None = None) -> dict[str, int]:
    """Hide or show every unowned row of each settings-driven family as its
    setting now says, in every library. Returns rows changed per library."""
    from app.discovery.database import get_db, recompute_all_series_author_mode

    families = [f for f in (families or filters.SETTING_FOR) if f in filters.SETTING_FOR]
    changed: dict[str, int] = {}
    for slug in _slugs():
        db = await get_db(slug)
        try:
            n = 0
            for family in families:
                hide = 1 if filters.hides(family, settings) else 0
                cur = await db.execute(
                    "UPDATE books SET hidden = ? "
                    "WHERE hidden_reason = ? AND owned = 0 AND hidden != ?",
                    (hide, family, hide),
                )
                n += cur.rowcount or 0
            if n:
                await recompute_all_series_author_mode(db, context="filter setting")
            await db.commit()
            changed[slug] = n
            if n:
                logger.info("filters[%s]: %d row(s) %s by their setting", slug, n,
                            "re-hidden or shown")
        finally:
            await db.close()
    return changed


__all__ = ["HIDE_SET", "SHOW_SET", "apply_family_settings"]
