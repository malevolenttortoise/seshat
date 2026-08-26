"""The roster — which authors Seshat actively tracks in a library.

See [ADR-0021](../../docs/adr/0021-roster-gates-discovery-author-creation.md).

An author is **in the roster** for a library when either:

  - their normalized name is on the global ``authors_allowed`` allow list, or
  - they own >=1 book in that library, in **any** contributor position
    (per *co-authored ownership*, ADR-0008 — a co-author counts, not only
    a primary).

The roster gates two operations with the same predicate:

  1. **Discovery author creation** — a contributor found by a source scan is
     linked only if it is in the roster. An allow-listed name may be MINTED;
     a non-allow-listed name may only be LINKED to an author row that already
     owns a book. Anything else is skipped entirely.
  2. **Scan eligibility** — only roster members are scanned, so a non-roster
     row that already exists (from a pre-ADR-0021 install) goes inert instead
     of continuing to cascade.

Why this exists: before ADR-0021, ``_link_discovered_contributors`` minted an
author row for every author-role contributor a source reported, and a minted
row instantly satisfied the scan-due query (``has any book_authors row``). One
Hardcover anthology therefore promoted 20+ strangers to scan targets, each of
which then minted the next generation. On the reference install that took
``calibre-library`` from 896 to 7,214 authors in ~36 hours.

⚠️ **Normalization.** Allow-list membership is tested with
``app.filter.normalize.normalize_author`` — the normalizer the allow list
itself is keyed on — NOT ``app.metadata.author_names.normalize_author_name``,
which is what ``authors.normalized_name`` uses. The two genuinely disagree
("J.R.R. Tolkien" -> ``j r r tolkien`` vs ``jrr tolkien``), and using the
wrong one silently rejects allow-listed authors.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from app.filter.normalize import normalize_author

logger = logging.getLogger("seshat.discovery.roster")

# A bulk scan calls into the roster once per source per author (~3x per
# author), so an uncached load would run two queries thousands of times per
# run. Cache per library slug behind a short TTL: cheap during a scan, and
# self-refreshing so an allow-list edit made mid-scan is picked up within a
# minute without any explicit invalidation contract. `invalidate()` exists
# for the call sites that DO know they changed something (see
# `app.state.refresh_filter_authors`).
_TTL_S = 60.0
_CACHE: dict[str, tuple[float, "Roster"]] = {}


@dataclass(frozen=True)
class Roster:
    """Snapshot of a library's roster. Immutable; rebuilt on TTL expiry."""

    #: Allow-list names, normalized with `filter.normalize.normalize_author`.
    allowed_names: frozenset[str]
    #: Per-library `authors.id` values owning >=1 book in ANY position.
    owned_author_ids: frozenset[int]

    def may_mint(self, name: str) -> bool:
        """True when a source-reported name may CREATE an author row.

        Only the allow-list half confers mint permission: an allow-listed
        name with no author row is an author the operator explicitly asked
        to track (177 such entries existed on the reference install). The
        owned half can never mint — by definition an owner already has a row.
        """
        norm = normalize_author(name or "")
        return bool(norm) and norm in self.allowed_names

    def admits(self, name: str, author_id: Optional[int]) -> bool:
        """True when a resolved contributor may be LINKED to a book.

        Allow-listed by name, or resolved to a row that owns a book here.
        """
        if self.may_mint(name):
            return True
        return author_id is not None and author_id in self.owned_author_ids

    def is_scan_eligible(self, name: str, author_id: Optional[int]) -> bool:
        """Alias of `admits` for the scan-eligibility call sites.

        Same predicate by design (ADR-0021: "one predicate, two call sites");
        named separately so the intent reads correctly at the scan gate.
        """
        return self.admits(name, author_id)

    @property
    def is_empty(self) -> bool:
        return not self.allowed_names and not self.owned_author_ids


async def load_roster(
    db, slug: Optional[str] = None, *, force: bool = False,
) -> Roster:
    """Build (or return a cached) `Roster` for the active library.

    `db` is the per-library discovery connection — it supplies the owned
    half. The allow list lives in the global pipeline DB (`seshat.db`), so
    that half is read through its own short-lived connection, mirroring the
    `from app.database import get_db as _get_pipeline_db` pattern already
    used elsewhere in `lookup.py`.

    On a **fresh install with an empty allow list** the owned half still
    admits every library author, so scanning behaves exactly as it did
    pre-ADR-0021. The gate only ever excludes authors who are neither owned
    nor explicitly asked for.
    """
    if slug is None:
        from app.discovery.database import get_active_library
        slug = get_active_library() or "_unknown"

    now = time.monotonic()
    if not force:
        hit = _CACHE.get(slug)
        if hit and (now - hit[0]) < _TTL_S:
            return hit[1]

    # --- allow-list half (global pipeline DB) ---
    allowed: frozenset[str] = frozenset()
    try:
        from app.database import get_db as _get_pipeline_db
        from app.storage.authors import load_normalized_sets
        pdb = await _get_pipeline_db()
        try:
            allowed, _ignored = await load_normalized_sets(pdb)
        finally:
            await pdb.close()
    except Exception:
        # Never break a scan on the roster read. An empty allow list is
        # SAFE-BY-CONSTRUCTION here: the owned half still admits every
        # library author, so a transient failure degrades to pre-ADR-0021
        # linking behavior rather than silently dropping every contributor.
        logger.warning(
            "roster: allow-list read failed for slug=%s; "
            "falling back to owned-only admission", slug, exc_info=True,
        )

    # --- owned half (per-library discovery DB) ---
    rows = await (await db.execute(
        "SELECT DISTINCT ba.author_id FROM book_authors ba "
        "JOIN books b ON b.id = ba.book_id "
        "WHERE b.owned = 1 AND ba.author_id IS NOT NULL"
    )).fetchall()
    owned = frozenset(int(r[0]) for r in rows)

    roster = Roster(allowed_names=allowed, owned_author_ids=owned)
    _CACHE[slug] = (now, roster)
    logger.debug(
        "roster[%s]: %d allow-listed names, %d owned author ids",
        slug, len(allowed), len(owned),
    )
    return roster


def invalidate(slug: Optional[str] = None) -> None:
    """Drop cached roster(s). Call after mutating the allow list."""
    if slug is None:
        _CACHE.clear()
    else:
        _CACHE.pop(slug, None)
