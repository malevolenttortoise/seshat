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
    #
    # Resolve the path LAZILY off `app.config.DATA_DIR` rather than importing
    # `app.database.APP_DB_PATH`: that constant is bound at import time, so a
    # test that monkeypatches DATA_DIR would still be pointed at the real
    # `seshat.db` — silently reading the developer's live allow list into a
    # unit test, or creating a stray empty DB at the production path.
    # Opened read-only (`mode=ro`) so a missing file errors instead of being
    # created.
    allowed: frozenset[str] = frozenset()
    try:
        import aiosqlite
        from app import config as _config
        from app.storage.authors import load_normalized_sets

        db_path = _config.DATA_DIR / "seshat.db"
        if not db_path.exists():
            # No pipeline DB (fresh install, or a unit test with a tmp
            # DATA_DIR). Owned-only admission is the correct, safe answer.
            logger.debug("roster: no pipeline DB at %s; owned-only", db_path)
            raise FileNotFoundError(db_path)
        pdb = await aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            pdb.row_factory = aiosqlite.Row
            allowed, _ignored = await load_normalized_sets(pdb)
        finally:
            await pdb.close()
    except FileNotFoundError:
        pass
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


async def scan_eligible_authors(
    db, cutoff: float, slug: Optional[str] = None,
) -> list:
    """Authors due for a source scan AND in the roster, oldest-first.

    The single source of truth for "who gets scanned" — both the scan loop
    (`lookup.run_full_lookup`) and the pre-flight due-count in the scan
    router call this, so the count the operator sees can never disagree
    with what the loop actually visits.

    Two filters compose:

      - **due + non-orphan** (SQL): ``last_lookup_at`` older than `cutoff`,
        and the author has at least one `book_authors` row. Orphan authors
        have nothing to merge into, so scanning them is wasted budget.
      - **roster** (Python): the allow list lives in a different database
        than `authors`, so this half can't be expressed in the same query.

    The roster filter is what stops a pre-ADR-0021 install from continuing
    to cascade: junk rows already on disk stay on disk, but go inert.
    """
    roster = await load_roster(db, slug)
    rows = await (await db.execute(
        "SELECT id, name FROM authors "
        "WHERE COALESCE(last_lookup_at,0) < ? "
        "AND id IN (SELECT DISTINCT author_id FROM book_authors) "
        "ORDER BY COALESCE(last_lookup_at,0) ASC",
        (cutoff,),
    )).fetchall()
    eligible = [r for r in rows if roster.is_scan_eligible(r["name"], r["id"])]
    if len(eligible) != len(rows):
        logger.info(
            "roster: %d of %d due authors are scan-eligible (%d non-roster "
            "skipped)", len(eligible), len(rows), len(rows) - len(eligible),
        )
    return eligible


# A library author is ALREADY a roster member via the owned half, so
# surfacing them is a convenience (it lets the operator make the allow list
# genuinely complete), never a correctness requirement. That makes it safe
# to bound: an install with a large library and an empty allow list would
# otherwise get one review row per author on first sync — the same flood
# ADR-0021 refused to create for scan skips, and exactly what CLAUDE.md's
# "auto-adopt features need a grandfather timestamp" rule warns about.
# Surface a slice per sync instead; later syncs drain the rest.
_MAX_SURFACED_PER_SYNC = 250


async def surface_non_roster_library_authors(slug: str) -> int:
    """Offer library authors that aren't allow-listed for operator review.

    ADR-0021 keeps library sync's bylines intact — Calibre/ABS are
    authoritative over their own contributor lists, and gating there would
    orphan owned books for no pollution prevented. But an author who is in
    the library and NOT on the allow list is a real gap: they're tracked
    only because they own something, and MAM announces for them still fall
    through the filter.

    So they're pushed into the existing `authors_tentative_review` list,
    where the operator can promote them to allowed (or ignored) from the
    Authors page. Returns the number newly surfaced.

    Skips anything already on the allow list, the ignore list, or already
    pending review — a rejected author must not keep reappearing.
    """
    from app.discovery.database import get_db as _get_library_db

    ldb = await _get_library_db(slug)
    try:
        rows = await (await ldb.execute(
            "SELECT name FROM authors "
            "WHERE (calibre_id IS NOT NULL OR audiobookshelf_id IS NOT NULL) "
            "AND name IS NOT NULL AND TRIM(name) != ''"
        )).fetchall()
    finally:
        await ldb.close()
    if not rows:
        return 0

    import aiosqlite
    from app import config as _config

    db_path = _config.DATA_DIR / "seshat.db"
    if not db_path.exists():
        return 0

    pdb = await aiosqlite.connect(str(db_path))
    try:
        pdb.row_factory = aiosqlite.Row
        known: set[str] = set()
        for table in ("authors_allowed", "authors_ignored",
                      "authors_tentative_review"):
            cur = await pdb.execute(f"SELECT normalized FROM {table}")  # nosec B608
            known.update(str(r[0]) for r in await cur.fetchall() if r[0])

        surfaced = 0
        truncated = False
        for r in rows:
            name = (r["name"] or "").strip()
            norm = normalize_author(name)
            if not norm or norm in known:
                continue
            if surfaced >= _MAX_SURFACED_PER_SYNC:
                truncated = True
                break
            await pdb.execute(
                "INSERT OR IGNORE INTO authors_tentative_review "
                "(name, normalized, source) VALUES (?,?,?)",
                (name, norm, "library_sync"),
            )
            known.add(norm)
            surfaced += 1
        if surfaced:
            await pdb.commit()
    finally:
        await pdb.close()

    if surfaced:
        logger.info(
            "roster[%s]: surfaced %d library author(s) for allow-list review%s",
            slug, surfaced,
            " (capped — more will follow next sync)" if truncated else "",
        )
    return surfaced


def invalidate(slug: Optional[str] = None) -> None:
    """Drop cached roster(s). Call after mutating the allow list."""
    if slug is None:
        _CACHE.clear()
    else:
        _CACHE.pop(slug, None)
