"""
Cross-library author identity coordinator (v2.20.0 Phase 1).

Replaces the de-facto "an author exists per library" model with
"an author exists once, libraries reference them." The pain this
solves: when discovery (or an audit) resolves an `amazon_id` for
William D. Arand in `seshat_calibre-library.db`, the matching row
in `seshat_abs-audio-library.db` *also* needs that ID — currently
that requires a separate scan per library, which doubles Akamai
pressure and creates "double work" during ID audits.

Architecture
------------
Two new global tables (in `seshat.db`):

  - `persons` — one row per canonical author identity. Keyed by
    `normalized_name` (the existing matching function from
    `app/metadata/author_names.py`). Carries `canonical_name`,
    `bio`, `image_url`, optional `display_name_override`.

  - `author_links` — many-to-one map of per-library
    `authors.id` → `persons.id`. The "many" side is per-library
    rows; the "one" side is the canonical identity. References
    per-library `authors.id` as a plain INTEGER (no FK — can't
    FK across SQLite files); orphans are pruned by an app-level
    sweep.

Cross-DB SQLite reality
-----------------------
Each per-library DB is a separate SQLite file. aiosqlite has one
connection per file. There is no efficient SQL JOIN across files;
every cross-library query walks the per-library DBs in sequence.
Helpers below open a fresh per-library connection on demand and
close it afterwards. Callers that already have a connection in
hand can pass `db=` to avoid the re-open.

Public API
----------
- `get_or_create_person(library_slug, author_id) -> int`
- `person_id_for(library_slug, author_id) -> int | None`
- `linked_authors(person_id) -> list[(library_slug, author_id)]`
- `mirror_source_id(library_slug, author_id, source_name, value)`
- `migrate_to_cross_library_identity()` — one-time bootstrap
- `prune_orphan_links()` — sweep that drops `author_links` rows
   whose per-library author row no longer exists.

The migration is idempotent: re-running on a fully-linked system
is a no-op. The sentinel is `persons` row count vs author totals
across libraries — if a delta exists, we run; if not, we skip.

This module is intentionally INDEPENDENT of `app.discovery.lookup`
and `app.discovery.calibre_sync` to avoid circular imports — the
helpers do their own DB connection management via the two `get_db`
accessors (`app.database.get_db` for global, plus a direct
aiosqlite.connect() to the per-library file path).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiosqlite

from app.config import DATA_DIR
from app.database import get_db as get_global_db
from app.metadata.author_names import normalize_author_name


_log = logging.getLogger("seshat.discovery.author_identity")


# Source-ID columns we know about on the per-library `authors` table.
# Used by consolidation / low-confidence flagging logic that walks
# every column to detect collisions.
KNOWN_SOURCE_ID_COLUMNS: frozenset[str] = frozenset({
    "amazon_id",
    "goodreads_id",
    "hardcover_id",
    "kobo_id",
    "ibdb_id",
    "google_books_id",
    "openlibrary_id",
    "audible_id",
    "audiobookshelf_id",
    "fictiondb_id",
    "calibre_id",
})

# Subset of KNOWN_SOURCE_ID_COLUMNS that's safe to MIRROR across
# linked per-library rows. These IDs identify the same author on a
# globally-shared web source — an `amazon_id` for "William D. Arand"
# is the same string regardless of which Seshat library asked.
#
# `audiobookshelf_id` and `calibre_id` are EXCLUDED because they're
# library-local sync identifiers — the ABS library's internal author ID
# is meaningful only inside that ABS library, and mirroring it onto a
# Calibre row would write a nonsense value. Same for `calibre_id`.
MIRRORABLE_SOURCE_ID_COLUMNS: frozenset[str] = frozenset({
    "amazon_id",
    "goodreads_id",
    "hardcover_id",
    "kobo_id",
    "ibdb_id",
    "google_books_id",
    "openlibrary_id",
    "audible_id",
    "fictiondb_id",
})


@dataclass(frozen=True)
class Person:
    """Canonical author identity. One row in `persons`."""
    id: int
    canonical_name: str
    normalized_name: str
    display_name_override: Optional[str]
    bio: Optional[str]
    image_url: Optional[str]
    last_updated_at: float
    created_at: float

    @property
    def display_name(self) -> str:
        """Override beats canonical. Used by all public-facing UIs."""
        return self.display_name_override or self.canonical_name


@dataclass(frozen=True)
class AuthorLink:
    """One (library_slug, author_id) → person_id mapping."""
    id: int
    person_id: int
    library_slug: str
    author_id: int
    link_source: str        # 'auto' | 'manual'
    link_confidence: str    # 'high' | 'low'
    created_at: float


# ─── Per-library connection helper ─────────────────────────────


def _per_library_db_path(library_slug: str) -> Path:
    """Return the path to a per-library DB file. Doesn't validate
    existence — the caller decides whether absent means "skip" or
    "error." Mirrors the layout `app/discovery/database.py` uses."""
    return Path(DATA_DIR) / f"seshat_{library_slug}.db"


async def _open_per_library(library_slug: str) -> aiosqlite.Connection:
    """Open a per-library DB with the standard pragmas. Caller closes."""
    db = await aiosqlite.connect(str(_per_library_db_path(library_slug)))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA busy_timeout=30000")
    return db


# ─── Person lookups ────────────────────────────────────────────


async def get_person(
    person_id: int,
    *,
    db: Optional[aiosqlite.Connection] = None,
) -> Optional[Person]:
    """Fetch a person record by id. None if missing."""
    close_after = db is None
    if db is None:
        db = await get_global_db()
    try:
        row = await (await db.execute(
            "SELECT id, canonical_name, normalized_name, "
            "       display_name_override, bio, image_url, "
            "       last_updated_at, created_at "
            "FROM persons WHERE id = ?",
            (person_id,),
        )).fetchone()
        if not row:
            return None
        return Person(
            id=row["id"],
            canonical_name=row["canonical_name"],
            normalized_name=row["normalized_name"],
            display_name_override=row["display_name_override"],
            bio=row["bio"],
            image_url=row["image_url"],
            last_updated_at=row["last_updated_at"],
            created_at=row["created_at"],
        )
    finally:
        if close_after:
            await db.close()


async def person_id_for(
    library_slug: str,
    author_id: int,
    *,
    db: Optional[aiosqlite.Connection] = None,
) -> Optional[int]:
    """Map a per-library author row to its canonical person_id.

    Returns None if no `author_links` row exists for this pair —
    that means either the migration hasn't run for this row yet
    (e.g., it was inserted by Calibre sync between init and the
    sync-insert hook firing) or the row was deleted from the
    identity graph.
    """
    close_after = db is None
    if db is None:
        db = await get_global_db()
    try:
        row = await (await db.execute(
            "SELECT person_id FROM author_links "
            "WHERE library_slug = ? AND author_id = ?",
            (library_slug, author_id),
        )).fetchone()
        return row["person_id"] if row else None
    finally:
        if close_after:
            await db.close()


async def linked_authors(
    person_id: int,
    *,
    db: Optional[aiosqlite.Connection] = None,
) -> list[tuple[str, int]]:
    """Return every (library_slug, author_id) linked to a person."""
    close_after = db is None
    if db is None:
        db = await get_global_db()
    try:
        cur = await db.execute(
            "SELECT library_slug, author_id FROM author_links "
            "WHERE person_id = ?",
            (person_id,),
        )
        return [(r["library_slug"], r["author_id"]) for r in await cur.fetchall()]
    finally:
        if close_after:
            await db.close()


# ─── Creation / linking ────────────────────────────────────────


async def get_or_create_person(
    library_slug: str,
    author_id: int,
    *,
    name: Optional[str] = None,
    bio: Optional[str] = None,
    image_url: Optional[str] = None,
) -> int:
    """Return the person_id for this per-library author row,
    creating both the `persons` row and the `author_links` row
    if either is missing.

    Strategy:
      1. If `author_links` already has a row → return its person_id.
      2. Else read the author's display_name + normalized_name from
         the per-library DB (or use the `name`/`bio`/`image_url`
         override args if provided — sync-insert paths pass these
         to avoid a roundtrip to the row that was just inserted).
      3. Look up `persons` by normalized_name. If found, link to it.
      4. Else create a new `persons` row, then link.

    The `name` override is for hot-path callers that ALREADY have the
    author's name in hand (e.g., immediately after INSERT). All other
    callers leave it as None and we read from the per-library DB.
    """
    db = await get_global_db()
    try:
        # Step 1: already linked?
        row = await (await db.execute(
            "SELECT person_id FROM author_links "
            "WHERE library_slug = ? AND author_id = ?",
            (library_slug, author_id),
        )).fetchone()
        if row:
            return row["person_id"]

        # Step 2: fetch author info if not supplied.
        if name is None:
            per_lib = await _open_per_library(library_slug)
            try:
                arow = await (await per_lib.execute(
                    "SELECT name, bio, image_url FROM authors WHERE id = ?",
                    (author_id,),
                )).fetchone()
            finally:
                await per_lib.close()
            if not arow:
                raise ValueError(
                    f"author_id={author_id} not found in "
                    f"seshat_{library_slug}.db — cannot link"
                )
            name = arow["name"]
            if bio is None:
                bio = arow["bio"]
            if image_url is None:
                image_url = arow["image_url"]

        normalized = normalize_author_name(name)
        if not normalized:
            # Defensive — an empty normalized name would collide every
            # other empty-named author into one person row. We let it
            # through but stamp a sentinel so the row is traceable.
            normalized = f"__empty_{library_slug}_{author_id}"
            _log.warning(
                "get_or_create_person: empty normalized_name for "
                "%s author_id=%d name=%r — using sentinel %r",
                library_slug, author_id, name, normalized,
            )

        # Step 3: find existing person by normalized_name.
        prow = await (await db.execute(
            "SELECT id FROM persons WHERE normalized_name = ?",
            (normalized,),
        )).fetchone()
        if prow:
            person_id = prow["id"]
        else:
            # Step 4: create new person.
            cur = await db.execute(
                "INSERT INTO persons "
                "(canonical_name, normalized_name, bio, image_url) "
                "VALUES (?, ?, ?, ?)",
                (name, normalized, bio, image_url),
            )
            person_id = cur.lastrowid

        # Insert the link.
        await db.execute(
            "INSERT INTO author_links "
            "(person_id, library_slug, author_id, link_source) "
            "VALUES (?, ?, ?, 'auto')",
            (person_id, library_slug, author_id),
        )
        await db.commit()
        return person_id
    finally:
        await db.close()


# ─── Mirror source-ID across linked rows ───────────────────────


async def mirror_source_id(
    library_slug: str,
    author_id: int,
    source_name: str,
    value: Optional[str],
) -> int:
    """Propagate `authors.{source_name}_id = value` to every OTHER
    per-library `authors` row linked to the same person as
    `(library_slug, author_id)`.

    Returns the number of rows touched, **excluding the caller's own
    `(library_slug, author_id)` row** — the caller is expected to have
    already written its own slug's value before invoking the mirror;
    re-writing the same value from a second connection would deadlock
    against the caller's still-open write transaction (v2.20.1 fix —
    "database is locked" errors during author scans).

    Caller usage pattern: after an UPDATE writes `{source}_id` in the
    caller's library DB, call `mirror_source_id(slug, author_id,
    source_name + "_id" if not already suffixed, value)`. The caller
    does NOT need to commit before invoking the mirror — the mirror
    skips the caller's slug entirely, so the caller's transaction
    can stay open across the call.

    Defensive: `source_name` MUST be in `MIRRORABLE_SOURCE_ID_COLUMNS`.
    `audiobookshelf_id` and `calibre_id` are deliberately excluded
    (library-local sync identifiers — mirroring would write a
    nonsense value into a different library's row). Calling
    `mirror_source_id` with one of those raises ValueError, which is
    the right signal that the caller is asking for something incoherent.
    """
    # Accept both "amazon_id" and "amazon" — normalize to column form.
    column = source_name if source_name.endswith("_id") else f"{source_name}_id"
    if column not in MIRRORABLE_SOURCE_ID_COLUMNS:
        raise ValueError(
            f"mirror_source_id: refusing column {column!r} — "
            f"not in MIRRORABLE_SOURCE_ID_COLUMNS "
            f"({sorted(MIRRORABLE_SOURCE_ID_COLUMNS)})"
        )

    person_id = await person_id_for(library_slug, author_id)
    if person_id is None:
        # The caller's author row isn't in the identity graph yet.
        # Don't mirror — the caller's own write already applied, and
        # the row will get linked on the next sync-insert hook or
        # migration sweep. Returning 0 lets the caller log it if needed.
        _log.debug(
            "mirror_source_id: %s/%d not linked yet; nothing to mirror",
            library_slug, author_id,
        )
        return 0

    links = await linked_authors(person_id)
    touched = 0
    for slug, aid in links:
        # v2.20.1 — skip the caller's own slug. The caller already
        # wrote this value via its own connection's UPDATE before
        # invoking the mirror; opening a SECOND connection to the
        # same per-library DB to re-write the same value would
        # deadlock against the caller's still-open write transaction
        # (the caller hasn't committed yet — by design, since the
        # caller may have follow-up writes to bundle into the same
        # transaction). This was the source of v2.20.0's "database
        # is locked" DEBUG spam during author scans.
        if slug == library_slug:
            continue
        try:
            per_lib = await _open_per_library(slug)
        except Exception as exc:
            _log.warning(
                "mirror_source_id: cannot open seshat_%s.db: %s — skipping",
                slug, exc,
            )
            continue
        try:
            # Column is whitelisted above — safe to interpolate.
            await per_lib.execute(
                f"UPDATE authors SET {column} = ? WHERE id = ?",  # nosec B608
                (value, aid),
            )
            await per_lib.commit()
            touched += 1
        finally:
            await per_lib.close()

    if touched > 0:
        # Bump the person's last_updated_at so callers querying
        # "what changed recently" get a useful signal.
        gdb = await get_global_db()
        try:
            await gdb.execute(
                "UPDATE persons SET last_updated_at = strftime('%s', 'now') "
                "WHERE id = ?",
                (person_id,),
            )
            await gdb.commit()
        finally:
            await gdb.close()

    return touched


# ─── Orphan-link cleanup ───────────────────────────────────────


async def prune_orphan_links(
    *,
    known_library_slugs: Optional[list[str]] = None,
) -> int:
    """Drop `author_links` rows whose per-library row no longer exists.

    SQLite can't FK across files, so when an author is deleted from a
    per-library DB (e.g., Mark cleans up an ABS-narrator-as-author
    junk row), the global `author_links` row becomes an orphan. This
    sweep finds and drops them. Also drops `persons` rows that have
    zero remaining links (the person became unreferenced).

    `known_library_slugs` lets callers limit the sweep to specific
    libraries — useful when one library was just resynced and the rest
    don't need re-checking. None = sweep everything.

    Returns the count of orphan link rows dropped.
    """
    gdb = await get_global_db()
    dropped = 0
    try:
        cur = await gdb.execute(
            "SELECT id, library_slug, author_id FROM author_links"
        )
        link_rows = await cur.fetchall()

        slugs_to_check = (
            set(known_library_slugs) if known_library_slugs is not None
            else {r["library_slug"] for r in link_rows}
        )

        # Build a set of (slug, author_id) pairs that DO exist by
        # walking each library's authors table once.
        live_pairs: set[tuple[str, int]] = set()
        for slug in slugs_to_check:
            try:
                per_lib = await _open_per_library(slug)
            except Exception as exc:
                _log.warning(
                    "prune_orphan_links: cannot open seshat_%s.db: %s — "
                    "skipping (no rows in this slug will be considered live)",
                    slug, exc,
                )
                continue
            try:
                arows = await (await per_lib.execute(
                    "SELECT id FROM authors"
                )).fetchall()
                for ar in arows:
                    live_pairs.add((slug, ar["id"]))
            finally:
                await per_lib.close()

        # Find orphans + delete.
        orphan_ids: list[int] = []
        for r in link_rows:
            if r["library_slug"] not in slugs_to_check:
                # Outside the sweep window; leave alone.
                continue
            if (r["library_slug"], r["author_id"]) not in live_pairs:
                orphan_ids.append(r["id"])

        if orphan_ids:
            # Chunked DELETE to keep SQL parameter count sane.
            for i in range(0, len(orphan_ids), 400):
                chunk = orphan_ids[i:i + 400]
                ph = ",".join("?" * len(chunk))
                await gdb.execute(
                    f"DELETE FROM author_links WHERE id IN ({ph})",  # nosec B608
                    chunk,
                )
            # Drop persons rows that became unreferenced.
            await gdb.execute(
                "DELETE FROM persons WHERE id NOT IN ("
                "SELECT DISTINCT person_id FROM author_links)"
            )
            await gdb.commit()
            dropped = len(orphan_ids)
            _log.info(
                "prune_orphan_links: dropped %d orphan author_links rows "
                "(and any newly-unreferenced persons)", dropped,
            )
        return dropped
    finally:
        await gdb.close()


# ─── One-time bootstrap migration ──────────────────────────────


async def migrate_to_cross_library_identity(
    library_slugs: list[str],
    *,
    force: bool = False,
) -> dict:
    """Walk every per-library `authors` table and populate
    `persons` + `author_links` in seshat.db.

    Sentinel: skip if `persons` already has rows AND link count
    matches per-library author count (i.e., everyone is already
    linked). Override with `force=True`.

    Returns a summary dict for the caller to log.
    """
    gdb = await get_global_db()
    try:
        # Sentinel check.
        if not force:
            persons_count = (await (await gdb.execute(
                "SELECT COUNT(*) AS n FROM persons"
            )).fetchone())["n"]
            links_count = (await (await gdb.execute(
                "SELECT COUNT(*) AS n FROM author_links"
            )).fetchone())["n"]

            total_authors = 0
            for slug in library_slugs:
                try:
                    per_lib = await _open_per_library(slug)
                except Exception:
                    continue
                try:
                    has_authors = await (await per_lib.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='authors' LIMIT 1"
                    )).fetchone()
                    if not has_authors:
                        continue
                    n = (await (await per_lib.execute(
                        "SELECT COUNT(*) AS n FROM authors"
                    )).fetchone())["n"]
                    total_authors += n
                finally:
                    await per_lib.close()

            if links_count >= total_authors and persons_count > 0:
                _log.info(
                    "migrate_to_cross_library_identity: already linked "
                    "(persons=%d, links=%d, authors=%d) — skipping",
                    persons_count, links_count, total_authors,
                )
                return {
                    "skipped": True,
                    "persons": persons_count,
                    "links": links_count,
                    "authors": total_authors,
                }

        # ── Step 1: walk each library, link every author ─────
        created_persons = 0
        created_links = 0
        for slug in library_slugs:
            try:
                per_lib = await _open_per_library(slug)
            except Exception as exc:
                _log.warning(
                    "migrate: cannot open seshat_%s.db: %s — skipping",
                    slug, exc,
                )
                continue
            try:
                has_authors = await (await per_lib.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='authors' LIMIT 1"
                )).fetchone()
                if not has_authors:
                    _log.info("migrate: [%s] no `authors` table; skipping", slug)
                    continue
                arows = await (await per_lib.execute(
                    "SELECT id, name, normalized_name, bio, image_url "
                    "FROM authors"
                )).fetchall()
            finally:
                await per_lib.close()

            _log.info("migrate: [%s] linking %d authors", slug, len(arows))
            for ar in arows:
                name = ar["name"] or ""
                # Re-normalize defensively in case the per-library row's
                # stored `normalized_name` was computed with an older
                # version of the normalizer.
                normalized = normalize_author_name(name)
                if not normalized:
                    normalized = f"__empty_{slug}_{ar['id']}"

                # Skip if link already exists (idempotent).
                existing = await (await gdb.execute(
                    "SELECT person_id FROM author_links "
                    "WHERE library_slug = ? AND author_id = ?",
                    (slug, ar["id"]),
                )).fetchone()
                if existing:
                    continue

                # Find or create person.
                prow = await (await gdb.execute(
                    "SELECT id FROM persons WHERE normalized_name = ?",
                    (normalized,),
                )).fetchone()
                if prow:
                    person_id = prow["id"]
                else:
                    cur = await gdb.execute(
                        "INSERT INTO persons "
                        "(canonical_name, normalized_name, bio, image_url) "
                        "VALUES (?, ?, ?, ?)",
                        (name, normalized, ar["bio"], ar["image_url"]),
                    )
                    person_id = cur.lastrowid
                    created_persons += 1

                await gdb.execute(
                    "INSERT INTO author_links "
                    "(person_id, library_slug, author_id, link_source) "
                    "VALUES (?, ?, ?, 'auto')",
                    (person_id, slug, ar["id"]),
                )
                created_links += 1
            await gdb.commit()

        # ── Step 2: consolidation pass per person ────────────
        await _consolidate_persons(gdb, library_slugs)

        # ── Step 3: low-confidence flagging ──────────────────
        flagged = await _flag_low_confidence_links(gdb, library_slugs)

        # ── Step 4: pen_name_links migration ─────────────────
        pen_migrated = await _migrate_pen_name_links(gdb, library_slugs)

        _log.info(
            "migrate_to_cross_library_identity: created %d persons, "
            "%d author_links, flagged %d low-confidence, migrated %d "
            "pen_name_links rows",
            created_persons, created_links, flagged, pen_migrated,
        )
        return {
            "skipped": False,
            "created_persons": created_persons,
            "created_links": created_links,
            "low_confidence": flagged,
            "pen_name_migrated": pen_migrated,
        }
    finally:
        await gdb.close()


async def _consolidate_persons(
    gdb: aiosqlite.Connection,
    library_slugs: list[str],
) -> None:
    """For each person with multiple linked rows, pick the canonical
    display name + bio + image_url by tiebreak across the linked rows.

    Tiebreaks per Mark's spec (project_seshat_v220_plan.md):
      canonical_name : most source IDs populated → most books → lowest author_id
      bio            : longest non-empty bio
      image_url      : first non-empty + URL-pattern-valid image_url
    """
    # Find persons with > 1 linked row.
    rows = await (await gdb.execute(
        "SELECT person_id, COUNT(*) AS n FROM author_links "
        "GROUP BY person_id HAVING n > 1"
    )).fetchall()
    if not rows:
        return

    # Build a per-slug cache of (author_id → {name, bio, image_url,
    # source_id_count, book_count}) so we read each per-library DB
    # at most once during consolidation.
    per_lib_cache: dict[str, dict[int, dict]] = {}
    for slug in library_slugs:
        try:
            per_lib = await _open_per_library(slug)
        except Exception:
            continue
        try:
            has_authors = await (await per_lib.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='authors' LIMIT 1"
            )).fetchone()
            if not has_authors:
                continue
            # Count non-empty source IDs per author (excluding
            # calibre_id/audiobookshelf_id which are internal not "source")
            source_col_count_sql = " + ".join(
                f"(CASE WHEN COALESCE({c}, '') != '' THEN 1 ELSE 0 END)"
                for c in KNOWN_SOURCE_ID_COLUMNS
                if c not in ("calibre_id", "audiobookshelf_id")
            )
            cur = await per_lib.execute(
                f"SELECT a.id, a.name, a.bio, a.image_url, "  # nosec B608
                f"       ({source_col_count_sql}) AS source_id_count, "
                f"       (SELECT COUNT(*) FROM books WHERE author_id = a.id) "
                f"           AS book_count "
                f"FROM authors a"
            )
            cache = {}
            for r in await cur.fetchall():
                cache[r["id"]] = {
                    "name": r["name"],
                    "bio": r["bio"],
                    "image_url": r["image_url"],
                    "source_id_count": r["source_id_count"],
                    "book_count": r["book_count"],
                }
            per_lib_cache[slug] = cache
        finally:
            await per_lib.close()

    # Now for each multi-linked person, run the tiebreak.
    for prow in rows:
        person_id = prow["person_id"]
        link_rows = await (await gdb.execute(
            "SELECT library_slug, author_id FROM author_links "
            "WHERE person_id = ?",
            (person_id,),
        )).fetchall()
        candidates: list[tuple[int, int, int, str, str | None, str | None]] = []
        # (source_id_count, book_count, -author_id, name, bio, image_url)
        # We use -author_id so smaller author_id wins on ties (max() picks the
        # largest tuple; negating author_id flips it).
        for lr in link_rows:
            slug = lr["library_slug"]
            aid = lr["author_id"]
            info = per_lib_cache.get(slug, {}).get(aid)
            if not info:
                continue
            candidates.append((
                info["source_id_count"], info["book_count"], -aid,
                info["name"], info["bio"], info["image_url"],
            ))
        if not candidates:
            continue
        winner = max(candidates)
        _, _, _, win_name, _, _ = winner

        # bio: longest non-empty
        bios = [c[4] for c in candidates if c[4] and c[4].strip()]
        bio = max(bios, key=len) if bios else None

        # image_url: first non-empty + URL-shaped
        image_url = None
        for c in candidates:
            iu = c[5]
            if iu and isinstance(iu, str) and iu.startswith(("http://", "https://")):
                image_url = iu
                break

        await gdb.execute(
            "UPDATE persons SET canonical_name = ?, bio = ?, image_url = ? "
            "WHERE id = ?",
            (win_name, bio, image_url, person_id),
        )
    await gdb.commit()


async def _flag_low_confidence_links(
    gdb: aiosqlite.Connection,
    library_slugs: list[str],
) -> int:
    """Mark `author_links` rows as `low` confidence when their person
    has multiple linked rows across libraries that share ZERO source IDs.

    Rationale: two unrelated "John Smith"s in different libraries get
    auto-linked by normalized_name. If they actually ARE the same
    person, at least one source ID will agree (Amazon, Goodreads,
    Hardcover, etc.); if no source ID agrees, the collision is
    probably real and Mark should triage.

    Returns the count of links flagged.
    """
    # Per-library author rows + source IDs cached for collision check.
    per_lib_source_ids: dict[str, dict[int, set[str]]] = {}
    # Source ID columns we care about for confidence — exclude
    # calibre_id/audiobookshelf_id (internal sync identifiers, not
    # cross-library disambiguators).
    confidence_cols = [
        c for c in KNOWN_SOURCE_ID_COLUMNS
        if c not in ("calibre_id", "audiobookshelf_id")
    ]
    select_cols = ", ".join(confidence_cols)

    for slug in library_slugs:
        try:
            per_lib = await _open_per_library(slug)
        except Exception:
            continue
        try:
            has_authors = await (await per_lib.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='authors' LIMIT 1"
            )).fetchone()
            if not has_authors:
                continue
            cur = await per_lib.execute(
                f"SELECT id, {select_cols} FROM authors"  # nosec B608
            )
            cache: dict[int, set[str]] = {}
            for r in await cur.fetchall():
                ids = {
                    f"{c}:{r[c]}"
                    for c in confidence_cols
                    if r[c] and str(r[c]).strip()
                }
                cache[r["id"]] = ids
            per_lib_source_ids[slug] = cache
        finally:
            await per_lib.close()

    # Persons with multiple links from different libraries are the
    # only candidates for low-confidence flagging.
    #
    # Skip persons whose links are user-approved (`link_source='manual'`).
    # Approval via `POST /persons/{pid}/approve-links` flips link_source
    # to `manual`; we treat that as "Mark confirmed these really ARE the
    # same person" and exempt the person from future re-flagging. Without
    # this skip, `recompute-consolidation` would erase approvals every
    # time it ran (it resets confidence to `high` then re-applies the
    # disjoint-source-IDs test).
    rows = await (await gdb.execute(
        "SELECT person_id FROM author_links "
        "GROUP BY person_id "
        "HAVING COUNT(DISTINCT library_slug) > 1 "
        "  AND SUM(CASE WHEN link_source = 'manual' THEN 1 ELSE 0 END) = 0"
    )).fetchall()
    flagged = 0
    for prow in rows:
        person_id = prow["person_id"]
        link_rows = await (await gdb.execute(
            "SELECT library_slug, author_id FROM author_links "
            "WHERE person_id = ?",
            (person_id,),
        )).fetchall()
        # Union of all source IDs across all linked rows.
        all_ids: set[str] = set()
        per_link_ids: list[set[str]] = []
        for lr in link_rows:
            ids = per_lib_source_ids.get(lr["library_slug"], {}).get(
                lr["author_id"], set(),
            )
            all_ids |= ids
            per_link_ids.append(ids)
        # If every linked row has an empty source-ID set, OR no two
        # rows share an ID, the linkage is suspicious.
        all_disjoint = all(
            not (a & b)
            for i, a in enumerate(per_link_ids)
            for b in per_link_ids[i + 1:]
        )
        if all_disjoint:
            await gdb.execute(
                "UPDATE author_links SET link_confidence = 'low' "
                "WHERE person_id = ?",
                (person_id,),
            )
            flagged += len(link_rows)
    await gdb.commit()
    return flagged


async def _migrate_pen_name_links(
    gdb: aiosqlite.Connection,
    library_slugs: list[str],
) -> int:
    """Promote per-library `pen_name_links` rows into the global
    `pen_name_links_v2` table, mapping author_id endpoints through
    `author_links` to person_id endpoints.

    Per-library `pen_name_links` tables are LEFT IN PLACE for safety
    (read-fallback if something here misses). v2.21+ will drop them
    once the unified detail page reads exclusively from v2.

    Returns the count of v2 rows inserted.
    """
    inserted = 0
    for slug in library_slugs:
        try:
            per_lib = await _open_per_library(slug)
        except Exception:
            continue
        try:
            has_pnl = await (await per_lib.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='pen_name_links' LIMIT 1"
            )).fetchone()
            if not has_pnl:
                continue
            pn_rows = await (await per_lib.execute(
                "SELECT canonical_author_id, alias_author_id, link_type "
                "FROM pen_name_links"
            )).fetchall()
        finally:
            await per_lib.close()

        for pr in pn_rows:
            # Resolve both endpoints to person_id via author_links.
            crow = await (await gdb.execute(
                "SELECT person_id FROM author_links "
                "WHERE library_slug = ? AND author_id = ?",
                (slug, pr["canonical_author_id"]),
            )).fetchone()
            arow = await (await gdb.execute(
                "SELECT person_id FROM author_links "
                "WHERE library_slug = ? AND author_id = ?",
                (slug, pr["alias_author_id"]),
            )).fetchone()
            if not crow or not arow:
                _log.debug(
                    "_migrate_pen_name_links: [%s] canonical=%d alias=%d "
                    "could not resolve both endpoints — skipping",
                    slug, pr["canonical_author_id"], pr["alias_author_id"],
                )
                continue
            if crow["person_id"] == arow["person_id"]:
                # Both pen-name rows ended up under the same person.
                # That means the names normalized identically — likely
                # a typo-fix case, not a true pen name. Skip.
                continue
            try:
                await gdb.execute(
                    "INSERT INTO pen_name_links_v2 "
                    "(canonical_person_id, alias_person_id, link_type) "
                    "VALUES (?, ?, ?)",
                    (
                        crow["person_id"],
                        arow["person_id"],
                        pr["link_type"] or "pen_name",
                    ),
                )
                inserted += 1
            except aiosqlite.IntegrityError:
                # UNIQUE collision — already migrated (e.g., the same
                # pen-name pair existed in both libraries). Idempotent.
                continue
        await gdb.commit()
    return inserted
