"""
v2.16.0 Data Hygiene Command Center action.

A single user-triggered run fans six chained jobs across every
configured library:

  1. Empty author + series cleanup
  2. Hardcover -> goodreads_id / openlibrary_id / google_books_id
     backfill (depends on the v2.16.0 Gap 1 fix; reuses Hardcover's
     `book_mappings` table on a batched per-book query)
  3. Phase-2 author goodreads_id backfill (reverse-lookup from any
     book carrying a resolvable identifier — reuses
     `backfill_missing_author_ids`)
  4. Book deduplication pass (identifier-keyed + same-series-position
     — reuses `_dedupe_same_series_position` plus an explicit
     identifier-grouping sweep that calls `merge_books` per pair)
  5. Series consolidation (intra-author canonical-form merge —
     reuses `_dedupe_intra_author_series`)
  6. ABS author name-match cross-stamp (cheap cross-library copy of
     `goodreads_id` / `hardcover_id` / `openlibrary_id` /
     `google_books_id` from enriched ebook authors to ABS authors of
     the same normalized name)

Universal rules applied across every job:

  - **Skip hidden items**. Cleanup + dedup jobs filter `hidden = 0`
    in their working sets. Identifier-class writes (stamping a
    discovered goodreads_id onto a row) ignore hidden state because
    the columns are scaffolding, not user-curated content — same
    rule the live scan layer follows.
  - **Idempotent**. Re-running back-to-back is a near-no-op. Each
    job's "fixes" counter drops to 0 once steady state is reached.
  - **Preserve `authors_allowed` by name**. The empty-cleanup job
    refuses to delete any author whose normalized name appears in
    the global allow-list, even if their books were all removed —
    that allow-list is the user's authorial-allowlist of record.

Coordinator surface:

  - `run_all(...)` — the chained entry point. Drives
    `state._hygiene_progress` per-step and returns a stats dict.
  - `POST /api/discovery/hygiene/run` (in
    `app/discovery/routers/hygiene.py`) spawns it as a background
    task and returns immediately; the existing scan-status banner
    polls `/discovery/scan-status` for progress.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from app import state
from app.config import load_settings
from app.discovery import cross_library
from app.discovery.database import (
    _dedupe_intra_author_series,
    _dedupe_same_series_position,
    cleanup_empty_series,
    get_active_library,
    get_db as get_library_db,
    set_active_library,
)
from app.metadata.author_names import normalize_author_name

logger = logging.getLogger("seshat.discovery.hygiene")


JOB_NAMES = (
    "Empty author + series cleanup",
    "Hardcover ID backfill",
    "Phase-2 author goodreads_id backfill",
    "Book deduplication",
    "Series consolidation",
    "ABS author cross-stamp",
)
TOTAL_JOBS = len(JOB_NAMES)


def _zero_stats() -> dict[str, Any]:
    return {
        "deleted_authors": 0,
        "deleted_series": 0,
        "books_backfilled": 0,
        "authors_resolved": 0,
        "books_merged": 0,
        "series_merged": 0,
        "abs_authors_stamped": 0,
        "errors": [],
    }


def _set_phase(job_idx: int, library: str = "", total: int = 0, current: int = 0) -> None:
    """Reset `state._hygiene_progress` for the start of a job's work
    against one library.

    Keeps the cumulative `current_job_idx` / total layout fixed and
    bumps `current` / `total` per library so the dashboard banner
    smoothly tracks intra-job progress.
    """
    state._hygiene_progress["current_job_idx"] = job_idx
    state._hygiene_progress["total_jobs"] = TOTAL_JOBS
    state._hygiene_progress["current_job_name"] = JOB_NAMES[job_idx]
    state._hygiene_progress["current_library"] = library
    state._hygiene_progress["current"] = current
    state._hygiene_progress["total"] = total


async def _load_allowed_norms() -> frozenset[str]:
    """Return the global `authors_allowed` normalized name set.

    Wrapper around `load_normalized_sets` that opens / closes the
    pipeline DB so the Hygiene coordinator stays self-contained and
    doesn't keep a connection across job boundaries (each job opens
    its own per-library DB as it runs).
    """
    from app.database import get_db as get_pipeline_db
    from app.storage.authors import load_normalized_sets

    db = await get_pipeline_db()
    try:
        allowed, _ignored = await load_normalized_sets(db)
    finally:
        await db.close()
    return allowed


async def _load_cross_library_book_names(libs: list[dict]) -> frozenset[str]:
    """Return the union of normalized author names that have ≥1 book
    in any library.

    Used by `job_empty_cleanup` as a cross-library preservation rule:
    the v2.12.1 dual-row pattern creates mirror author rows in every
    library so cross-format scans see each author from either side.
    A naive "zero books in THIS library AND not allowlisted" delete
    rule (v2.16.0's first cut) silently destroyed those mirrors —
    V. E. Schwab / J. J. Bookerson / 91 others in UAT 2026-05-17.

    The fix: pre-compute the set of names that have books somewhere,
    pass it to every per-library cleanup, and refuse to delete any
    author whose normalized name is in either the cross-library set
    or the global allowlist.

    Returns an EMPTY frozenset on empty input so the no-libraries
    test path still works (the empty-cleanup default is no
    cross-library protection, matching v2.16.0 semantics).
    """
    names: set[str] = set()
    for lib in libs:
        slug = lib.get("slug")
        if not slug:
            continue
        try:
            db = await get_library_db(slug)
        except Exception:
            logger.warning(
                "hygiene: cross-library names: could not open %s — skipping",
                slug,
            )
            continue
        try:
            cur = await db.execute(
                "SELECT DISTINCT a.normalized_name "
                "FROM authors a "
                "JOIN books b ON b.author_id = a.id "
                "WHERE a.normalized_name IS NOT NULL "
                "  AND a.normalized_name != ''"
            )
            rows = await cur.fetchall()
            for r in rows:
                v = r[0]
                if v:
                    names.add(str(v))
        finally:
            await db.close()
    return frozenset(names)


# ─── Job 1 — Empty author + series cleanup ──────────────────────────

async def job_empty_cleanup(
    slug: str,
    stats: dict[str, Any],
    *,
    cross_library_book_names: frozenset[str] = frozenset(),
) -> None:
    """Delete authors with 0 books and series with 0 books in the
    library named by `slug`. Preserves two cohorts:

      1. **`authors_allowed` by name** — the global allowlist is the
         user's authorial-allowlist of record.
      2. **Cross-library mirror rows** — authors who have ≥1 book in
         ANY other library. The v2.12.1 dual-row pattern requires
         these mirrors for cross-format scans to surface audiobooks
         alongside ebooks (and vice versa); deleting them silently
         is a v2.16.0 regression caught during UAT.

    Series preservation: there's no series-allowlist concept; any
    series with zero member books is fair game.

    Order matters — series cleanup runs FIRST so an author whose
    only book pointed at a now-defunct series doesn't get
    misidentified as empty during the author pass.

    `cross_library_book_names` is built once by the coordinator
    (`_load_cross_library_book_names`) and passed in so every
    per-library invocation sees the same set. The default empty
    frozenset preserves the v2.16.0 single-library test behavior
    (callers that don't supply it get the same delete-anything-not-
    allowlisted semantics as before).
    """
    db = await get_library_db(slug)
    try:
        # Empty series first. cleanup_empty_series returns an int row
        # count and handles its own commit.
        deleted_series = await cleanup_empty_series(db) or 0
        stats["deleted_series"] += deleted_series

        # Author cleanup — count books per author, skip allowlisted
        # names, skip cross-library mirrors, delete the rest.
        allowed_norms = await _load_allowed_norms()

        cur = await db.execute(
            "SELECT a.id, a.name FROM authors a "
            "LEFT JOIN books b ON b.author_id = a.id "
            "GROUP BY a.id HAVING COUNT(b.id) = 0"
        )
        candidates = await cur.fetchall()
        deletable: list[int] = []
        kept_allowlist = 0
        kept_cross_library = 0
        for r in candidates:
            norm = normalize_author_name(r["name"] or "")
            if norm and norm in allowed_norms:
                kept_allowlist += 1
                continue
            if norm and norm in cross_library_book_names:
                kept_cross_library += 1
                continue
            deletable.append(int(r["id"]))

        if deletable:
            # Series rows referenced by these authors are already
            # orphaned (no books), so cascade isn't required — but
            # we delete in a single transaction to keep the slug's
            # row count consistent for any concurrent reader.
            chunk = 500
            for i in range(0, len(deletable), chunk):
                batch = deletable[i : i + chunk]
                placeholders = ",".join("?" * len(batch))
                await db.execute(
                    f"DELETE FROM authors WHERE id IN ({placeholders})",
                    batch,
                )
            await db.commit()
            stats["deleted_authors"] += len(deletable)

        logger.info(
            "hygiene[%s] empty-cleanup: deleted_authors=%d deleted_series=%d "
            "kept_by_allowlist=%d kept_by_cross_library=%d",
            slug, len(deletable), deleted_series,
            kept_allowlist, kept_cross_library,
        )
    except Exception as e:
        msg = f"empty-cleanup ({slug}): {type(e).__name__}: {e}"
        logger.exception(msg)
        stats["errors"].append(msg)
    finally:
        await db.close()


# ─── Job 2 — Hardcover identifier backfill ──────────────────────────

async def _fetch_hardcover_book_mappings(
    src, book_ids: list[int]
) -> dict[int, dict[str, str]]:
    """Batched GraphQL: pull `book_mappings` for `book_ids`.

    Returns `{book_id: {"goodreads": ..., "openlibrary": ...,
    "google": ...}}` with only the platforms Hardcover actually has
    a mapping for. Missing platforms are absent from the inner dict
    rather than mapped to None — caller treats absence as "don't
    overwrite".

    OL values are stripped of the `/books/` / `/works/` prefix so
    the stored value matches what `openlibrary.py` itself writes
    (bare `OL...` form).
    """
    if not book_ids:
        return {}
    # 50 ids/batch keeps the GraphQL request size sane for very
    # large libraries; Hardcover's default per-query budget is
    # generous enough that we could push 100, but 50 lets the
    # batch quota stretch across more authors per session if the
    # operator runs Hygiene shortly after a Calibre sync.
    BATCH = 50
    out: dict[int, dict[str, str]] = {}
    # Platform names in Hardcover's `book_mappings.platform.name` are
    # LOWERCASE (`goodreads`, `openlibrary`, `google`) — confirmed by
    # UAT 2026-05-17 against the live API. The TitleCase form used in
    # v2.16.0/v2.16.1 matched zero rows (`_in` is case-sensitive),
    # producing `candidates=5300 updated=0` against Mark's library.
    # The extraction loop in `hardcover.py` already case-folds before
    # comparison, so the filter is the only place case-sensitivity bit.
    query = """
    query HygieneBookMappings($ids: [Int!]) {
      books(where: {id: {_in: $ids}}) {
        id
        book_mappings(where: {platform: {name: {_in: ["goodreads", "openlibrary", "google"]}}}) {
          external_id
          platform { name }
        }
      }
    }
    """
    for i in range(0, len(book_ids), BATCH):
        batch = book_ids[i : i + BATCH]
        try:
            data = await src._query(query, {"ids": batch})
        except Exception as e:
            logger.warning("hygiene: hardcover batch error: %s", e)
            continue
        for book in (data.get("books") or []):
            try:
                bid = int(book.get("id"))
            except (TypeError, ValueError):
                continue
            mappings: dict[str, str] = {}
            for m in (book.get("book_mappings") or []):
                if not isinstance(m, dict):
                    continue
                ext = m.get("external_id")
                if not ext:
                    continue
                pname = (m.get("platform") or {}).get("name", "")
                pkey = str(pname).strip().lower()
                if pkey == "goodreads":
                    mappings["goodreads"] = str(ext).strip()
                elif pkey == "openlibrary":
                    raw = str(ext).strip()
                    mappings["openlibrary"] = (
                        raw.rsplit("/", 1)[-1] if "/" in raw else raw
                    )
                elif pkey == "google":
                    mappings["google"] = str(ext).strip()
            if mappings:
                out[bid] = mappings
    return out


async def job_hardcover_id_backfill(slug: str, stats: dict[str, Any]) -> None:
    """For each book in `slug` carrying `hardcover_id` but missing
    `goodreads_id` (or OL / GB), batch-query Hardcover's
    `book_mappings` table and COALESCE-fill the per-source ID
    columns.

    Ignores `hidden = 0` per the universal rule: identifier writes
    are scaffolding, safe on hidden rows.

    No-op when Hardcover isn't configured (no API key). Reuses
    HardcoverSource's 1s-rate-limit + retry / soft-fail behavior.
    """
    settings = load_settings()
    api_key = (settings.get("hardcover_api_key") or "").strip()
    if not api_key:
        try:
            from app.secrets import get_secret
            api_key = (await get_secret("hardcover_api_key") or "").strip()
        except Exception:
            api_key = ""
    if not api_key:
        logger.info("hygiene[%s] hardcover-backfill: no API key — skipping", slug)
        return

    from app.discovery.sources.hardcover import HardcoverSource

    db = await get_library_db(slug)
    try:
        cur = await db.execute(
            "SELECT id, hardcover_id, goodreads_id, openlibrary_id, google_books_id "
            "FROM books "
            "WHERE hardcover_id IS NOT NULL AND hardcover_id != '' "
            "AND ("
            "  goodreads_id IS NULL OR goodreads_id = '' "
            "  OR openlibrary_id IS NULL OR openlibrary_id = '' "
            "  OR google_books_id IS NULL OR google_books_id = ''"
            ")"
        )
        rows = await cur.fetchall()
        if not rows:
            logger.info(
                "hygiene[%s] hardcover-backfill: no candidates", slug
            )
            return

        # Parse hardcover_id -> int. Skip rows where the id can't
        # parse (legacy data corruption) so a bad row doesn't poison
        # the batch.
        candidates: list[tuple[int, int]] = []  # (book_row_id, hardcover_int_id)
        for r in rows:
            try:
                hid = int(str(r["hardcover_id"]).strip())
            except (TypeError, ValueError):
                continue
            candidates.append((int(r["id"]), hid))

        _set_phase(1, library=slug, total=len(candidates), current=0)

        src = HardcoverSource(api_key=api_key)
        try:
            hcover_ids = [c[1] for c in candidates]
            mappings = await _fetch_hardcover_book_mappings(src, hcover_ids)
        finally:
            await src.close()

        # Index by hardcover_id for the per-row UPDATE pass.
        per_book: dict[int, dict[str, str]] = mappings
        updated = 0
        for book_row_id, hid in candidates:
            m = per_book.get(hid)
            state._hygiene_progress["current"] += 1
            if not m:
                continue
            sets: list[str] = []
            vals: list[Any] = []
            if m.get("goodreads"):
                sets.append("goodreads_id = COALESCE(goodreads_id, ?)")
                vals.append(m["goodreads"])
            if m.get("openlibrary"):
                sets.append("openlibrary_id = COALESCE(openlibrary_id, ?)")
                vals.append(m["openlibrary"])
            if m.get("google"):
                sets.append("google_books_id = COALESCE(google_books_id, ?)")
                vals.append(m["google"])
            if not sets:
                continue
            vals.append(book_row_id)
            await db.execute(
                f"UPDATE books SET {', '.join(sets)} WHERE id = ?", vals
            )
            updated += 1

        if updated:
            await db.commit()
        stats["books_backfilled"] += updated
        logger.info(
            "hygiene[%s] hardcover-backfill: candidates=%d updated=%d",
            slug, len(candidates), updated,
        )
    except Exception as e:
        msg = f"hardcover-backfill ({slug}): {type(e).__name__}: {e}"
        logger.exception(msg)
        stats["errors"].append(msg)
    finally:
        await db.close()


# ─── Job 3 — Phase-2 author goodreads_id backfill ───────────────────

async def job_author_id_backfill(slug: str, stats: dict[str, Any]) -> None:
    """Re-use `backfill_missing_author_ids`. It opens its own DB via
    the active-library accessor, so we set + restore the active
    library here.

    The existing sweep handles its own logging, rate-limiting, and
    soft-block detection — we just observe the stats it returns and
    fold them into the Hygiene rollup.
    """
    from app.discovery.goodreads_author_backfill import (
        backfill_missing_author_ids,
    )
    try:
        result = await backfill_missing_author_ids(limit=None)
        stats["authors_resolved"] += int(result.get("resolved", 0))
        logger.info(
            "hygiene[%s] author-id-backfill: considered=%d resolved=%d "
            "missed=%d soft_blocked=%d",
            slug,
            int(result.get("considered", 0)),
            int(result.get("resolved", 0)),
            int(result.get("missed", 0)),
            int(result.get("skipped_soft_blocked", 0)),
        )
    except Exception as e:
        msg = f"author-id-backfill ({slug}): {type(e).__name__}: {e}"
        logger.exception(msg)
        stats["errors"].append(msg)


# ─── Job 4 — Book dedup ─────────────────────────────────────────────

async def _dedupe_by_identifier(
    db, col: str, stats: dict[str, Any], slug: str
) -> int:
    """Merge book rows that share a non-null value in `col`.

    For each duplicate group, pick the lowest-id row as the winner
    and use the local field-resolution merge (a streamlined version
    of `book_merge.merge_books` that only handles the in-library
    case — we don't touch the pipeline DB / book_grab_links here
    because the Hygiene-time hits should be rare and pipeline
    redirects on a stale pre-merge book_id self-heal on the next
    grab-link write).

    Hidden books are excluded from the comparison set: a hidden row
    sharing a goodreads_id with an active row was explicitly hidden
    by the user; merging silently would surface the unwanted row's
    metadata under the kept id. Same reason MAM / source scans
    skip hidden during fuzzy match.
    """
    cur = await db.execute(
        f"SELECT {col}, COUNT(*) AS c FROM books "
        f"WHERE {col} IS NOT NULL AND {col} != '' AND hidden = 0 "
        f"GROUP BY {col} HAVING c > 1"
    )
    groups = await cur.fetchall()
    merged = 0
    for grp in groups:
        ident = grp[col]
        cur2 = await db.execute(
            f"SELECT id, owned, title FROM books WHERE {col} = ? "
            f"AND hidden = 0 ORDER BY owned DESC, id ASC",
            (ident,),
        )
        members = await cur2.fetchall()
        if len(members) < 2:
            continue
        winner_id = int(members[0]["id"])
        loser_ids = [int(m["id"]) for m in members[1:]]
        # Local fold: copy identity columns COALESCE-style from the
        # losers onto the winner, then delete the loser rows. We
        # stay inside the per-library DB transaction, matching the
        # pattern `_dedupe_same_series_position` uses one section
        # over.
        IDENT_COLS = (
            "isbn", "hardcover_id", "goodreads_id", "fictiondb_id",
            "kobo_id", "amazon_id", "google_books_id", "ibdb_id",
            "openlibrary_id", "audible_id", "audiobookshelf_id",
            "hardcover_slug", "kobo_slug", "asin",
            "mam_torrent_id", "mam_url", "mam_status", "mam_formats",
            "mam_category",
        )
        for loser_id in loser_ids:
            # COALESCE-fill the winner from the loser for each
            # identity column. We do it column-by-column so a
            # constraint failure on one column doesn't roll back
            # the whole batch.
            for c in IDENT_COLS:
                try:
                    await db.execute(
                        f"UPDATE books SET {c} = COALESCE({c}, "
                        f"  (SELECT {c} FROM books WHERE id = ?)) "
                        f"WHERE id = ?",
                        (loser_id, winner_id),
                    )
                except Exception as e:
                    logger.debug(
                        "hygiene[%s] dedup col=%s loser=%d: %s",
                        slug, c, loser_id, e,
                    )
            # Drop the loser. CASCADE clears any
            # book_series_suggestions rows; work_links in the
            # pipeline DB reconcile on next works-matcher run.
            await db.execute("DELETE FROM books WHERE id = ?", (loser_id,))
            merged += 1
            logger.info(
                "hygiene[%s] dedup-by-%s: merged loser id=%d -> winner id=%d "
                "(value=%r)",
                slug, col, loser_id, winner_id, ident,
            )
    if merged:
        await db.commit()
    return merged


async def job_book_dedup(slug: str, stats: dict[str, Any]) -> None:
    """Two-pass book dedup.

    Pass A — identifier-keyed merge. Any two books sharing a non-
    null `goodreads_id` / `hardcover_id` / `isbn` / etc. are the
    same book (Hardcover-stamped Goodreads ids from Job 2 are what
    make this newly productive). Conservative; identifier matches
    are extremely high-precision.

    Pass B — `_dedupe_same_series_position`. Catches the
    "Remnant II" vs "Remnant Book 2" case where two rows share
    `(series_id, series_index)` even though titles don't fuzzy-
    match. Existing helper, runs at init_db too.
    """
    db = await get_library_db(slug)
    try:
        # Pass A — identifier-keyed. Order matters: stronger
        # identifiers first so the winning row keeps the most
        # canonical id slot.
        for col in (
            "goodreads_id", "hardcover_id", "isbn",
            "amazon_id", "audible_id", "asin",
        ):
            stats["books_merged"] += await _dedupe_by_identifier(
                db, col, stats, slug,
            )

        # Pass B — same-series-position.
        deleted = await _dedupe_same_series_position(db) or 0
        stats["books_merged"] += deleted
        logger.info(
            "hygiene[%s] book-dedup: total merged=%d (last-pass same-position=%d)",
            slug, stats["books_merged"], deleted,
        )
    except Exception as e:
        msg = f"book-dedup ({slug}): {type(e).__name__}: {e}"
        logger.exception(msg)
        stats["errors"].append(msg)
    finally:
        await db.close()


# ─── Job 5 — Series consolidation ───────────────────────────────────

async def job_series_consolidate(slug: str, stats: dict[str, Any]) -> None:
    """Intra-author canonical-form series merge. Re-uses the
    existing helper that also runs at `init_db` time — the Hygiene
    surface is the on-demand version of the same operation, useful
    when post-Job-4 ID stamping produced new mergeable rows.
    """
    db = await get_library_db(slug)
    try:
        collapsed = await _dedupe_intra_author_series(db) or 0
        stats["series_merged"] += collapsed
        # And re-run empty-series cleanup in case Pass A + B
        # orphaned anything.
        empty = await cleanup_empty_series(db) or 0
        stats["deleted_series"] += empty
        logger.info(
            "hygiene[%s] series-consolidate: collapsed=%d post-empty=%d",
            slug, collapsed, empty,
        )
    except Exception as e:
        msg = f"series-consolidate ({slug}): {type(e).__name__}: {e}"
        logger.exception(msg)
        stats["errors"].append(msg)
    finally:
        await db.close()


# ─── Job 6 — ABS author name-match cross-stamp ──────────────────────

async def job_abs_author_cross_stamp(stats: dict[str, Any]) -> None:
    """For every author in an audiobook-library DB missing a
    Goodreads / Hardcover / OpenLibrary / Google identifier, look
    up an ebook-library author with the same normalized name and
    COALESCE-fill the missing columns.

    Cheap-and-safe scope: only name-equality. Real ABS author
    enrichment (cross-DB Goodreads resolution for ABS-only authors
    whose ebook side has no match either) is deferred to v2.17.x —
    needs its own design pass.

    Operates across libraries via `cross_library`'s registry, not
    just the active one.
    """
    libs = cross_library.libraries_for("all")
    abs_libs = [
        l for l in libs
        if (l.get("content_type") or "ebook") == "audiobook" and l.get("slug")
    ]
    ebook_libs = [
        l for l in libs
        if (l.get("content_type") or "ebook") == "ebook" and l.get("slug")
    ]
    if not abs_libs or not ebook_libs:
        logger.info(
            "hygiene: abs-cross-stamp: need both ebook + audiobook "
            "libraries (have ebook=%d, abs=%d) — skipping",
            len(ebook_libs), len(abs_libs),
        )
        return

    # Build a normalized-name -> ids map from every ebook library.
    # When two ebook libraries hold the same author, last-write-wins
    # for the lookup table — both rows have the same person's IDs
    # anyway, so either is correct.
    XID_COLS = (
        "goodreads_id", "hardcover_id", "openlibrary_id", "google_books_id",
    )
    ebook_map: dict[str, dict[str, str]] = {}
    for lib in ebook_libs:
        slug = lib["slug"]
        db = await get_library_db(slug)
        try:
            cur = await db.execute(
                "SELECT name, " + ", ".join(XID_COLS) + " FROM authors"
            )
            rows = await cur.fetchall()
        finally:
            await db.close()
        for r in rows:
            norm = normalize_author_name(r["name"] or "")
            if not norm:
                continue
            existing = ebook_map.setdefault(norm, {})
            for col in XID_COLS:
                v = r[col]
                if v and not existing.get(col):
                    existing[col] = v

    if not ebook_map:
        return

    # Stamp ABS authors with missing ids.
    stamped = 0
    for lib in abs_libs:
        slug = lib["slug"]
        db = await get_library_db(slug)
        try:
            cur = await db.execute(
                "SELECT id, name, " + ", ".join(XID_COLS) + " FROM authors"
            )
            rows = await cur.fetchall()
            for r in rows:
                norm = normalize_author_name(r["name"] or "")
                if not norm:
                    continue
                ebook_ids = ebook_map.get(norm)
                if not ebook_ids:
                    continue
                sets: list[str] = []
                vals: list[Any] = []
                for col in XID_COLS:
                    cur_val = r[col]
                    new_val = ebook_ids.get(col)
                    if new_val and not cur_val:
                        sets.append(f"{col} = ?")
                        vals.append(new_val)
                if not sets:
                    continue
                vals.append(int(r["id"]))
                await db.execute(
                    f"UPDATE authors SET {', '.join(sets)} WHERE id = ?",
                    vals,
                )
                stamped += 1
            if stamped:
                await db.commit()
        except Exception as e:
            msg = f"abs-cross-stamp ({slug}): {type(e).__name__}: {e}"
            logger.exception(msg)
            stats["errors"].append(msg)
        finally:
            await db.close()
    stats["abs_authors_stamped"] += stamped
    logger.info(
        "hygiene: abs-cross-stamp: stamped=%d author(s) across %d "
        "audiobook library/libraries",
        stamped, len(abs_libs),
    )


# ─── Coordinator ────────────────────────────────────────────────────

async def run_all() -> dict[str, Any]:
    """Run the full 6-job Hygiene chain across every configured
    library. Returns the rollup stats dict.

    Drives `state._hygiene_progress` per-step so the dashboard
    banner has a `1 of 6: <job name> — <library>` display path.

    Hygiene_progress mutations are point-in-time only — the
    coordinator does NOT block on a flag (other than its own task
    handle) so the Source Scan / MAM Scan / Library Sync paths can
    still acquire their own DB write locks while Hygiene runs.
    `aiosqlite`'s 30s busy_timeout handles writer-vs-writer
    contention on the per-library DBs.
    """
    started = time.time()
    stats = _zero_stats()
    libs = cross_library.libraries_for("all")
    libs = [l for l in libs if l.get("slug")]
    state._hygiene_progress.update({
        "running": True,
        "current_job_idx": 0,
        "total_jobs": TOTAL_JOBS,
        "current_job_name": JOB_NAMES[0],
        "current_library": "",
        "current": 0,
        "total": 0,
        "status": "running",
        "type": "hygiene",
        "jobs": [],
    })
    original_active = get_active_library()
    try:
        # Job 1 — per-library empty cleanup.
        # Build the cross-library "has books somewhere" name set once
        # so every per-library invocation sees the same view. Without
        # this, mirror author rows from the v2.12.1 dual-row pattern
        # (Calibre author with no audiobook books in ABS, ABS author
        # with no ebook books in Calibre) get deleted because each
        # library sees them as locally empty. UAT 2026-05-17 caught
        # this against 93 ABS mirror rows that would have been wiped.
        _set_phase(0)
        cross_lib_names = await _load_cross_library_book_names(libs)
        logger.info(
            "hygiene: cross-library names: %d author(s) with books "
            "somewhere — will be preserved by empty-cleanup even when "
            "their per-library count is zero",
            len(cross_lib_names),
        )
        for lib in libs:
            slug = lib["slug"]
            _set_phase(0, library=slug)
            await job_empty_cleanup(
                slug, stats,
                cross_library_book_names=cross_lib_names,
            )
        state._hygiene_progress["jobs"].append({
            "name": JOB_NAMES[0],
            "deleted_authors": stats["deleted_authors"],
            "deleted_series": stats["deleted_series"],
        })

        # Job 2 — Hardcover identifier backfill (per-library).
        for lib in libs:
            slug = lib["slug"]
            _set_phase(1, library=slug)
            await job_hardcover_id_backfill(slug, stats)
        state._hygiene_progress["jobs"].append({
            "name": JOB_NAMES[1],
            "books_backfilled": stats["books_backfilled"],
        })

        # Job 3 — Phase-2 author goodreads_id backfill. The existing
        # function reads `get_active_library`, so set it per loop.
        for lib in libs:
            slug = lib["slug"]
            _set_phase(2, library=slug)
            set_active_library(slug)
            await job_author_id_backfill(slug, stats)
        state._hygiene_progress["jobs"].append({
            "name": JOB_NAMES[2],
            "authors_resolved": stats["authors_resolved"],
        })

        # Job 4 — Book dedup.
        for lib in libs:
            slug = lib["slug"]
            _set_phase(3, library=slug)
            await job_book_dedup(slug, stats)
        state._hygiene_progress["jobs"].append({
            "name": JOB_NAMES[3],
            "books_merged": stats["books_merged"],
        })

        # Job 5 — Series consolidation.
        for lib in libs:
            slug = lib["slug"]
            _set_phase(4, library=slug)
            await job_series_consolidate(slug, stats)
        state._hygiene_progress["jobs"].append({
            "name": JOB_NAMES[4],
            "series_merged": stats["series_merged"],
        })

        # Job 6 — ABS cross-stamp (cross-library, runs once).
        _set_phase(5, library="(cross-library)")
        await job_abs_author_cross_stamp(stats)
        state._hygiene_progress["jobs"].append({
            "name": JOB_NAMES[5],
            "abs_authors_stamped": stats["abs_authors_stamped"],
        })

        state._hygiene_progress.update({
            "running": False,
            "status": "complete" if not stats["errors"] else "complete (with errors)",
            "completed_at": time.time(),
        })

        # User-facing toast.
        try:
            from app.orchestrator.sse_publishers import publish_toast
            summary = (
                f"Hygiene complete: "
                f"-{stats['deleted_authors']} empty authors, "
                f"-{stats['deleted_series']} empty series, "
                f"+{stats['books_backfilled']} book IDs, "
                f"+{stats['authors_resolved']} author IDs, "
                f"~{stats['books_merged']} books merged, "
                f"~{stats['series_merged']} series merged, "
                f"+{stats['abs_authors_stamped']} ABS stamps"
            )
            await publish_toast(
                "warning" if stats["errors"] else "success", summary,
            )
        except Exception:
            logger.debug("hygiene toast failed", exc_info=True)
    except Exception as e:
        logger.exception("hygiene: coordinator crash")
        stats["errors"].append(f"coordinator: {type(e).__name__}: {e}")
        state._hygiene_progress.update({
            "running": False,
            "status": f"error: {e}",
        })
    finally:
        if original_active and original_active != get_active_library():
            set_active_library(original_active)

    stats["elapsed_sec"] = time.time() - started
    logger.info("hygiene: run_all complete: %s", stats)
    return stats


async def is_running() -> bool:
    """Bool view used by the HTTP entry point to refuse overlap."""
    t = state._hygiene_task
    return bool(t and not t.done())
