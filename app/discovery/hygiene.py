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
    "Orphan author retrolink",
    "Cross-library person backfill",
    "Consolidate persons by shared source ID",
    "Prune orphan author links",
    # v3.x (ADR-0016 slice 05) — image-URL health check. Retires Job 8's
    # `/books/`-path substring workaround in favor of an honest HEAD
    # verify + substring blacklist. Local-clear-only (does not fan
    # NULLs through siblings; the next scan re-establishes coherence
    # via mirror_image_url's rank-aware overwrite).
    "Image URL health check",
    "Soft-delete retention sweep",
)
TOTAL_JOBS = len(JOB_NAMES)


def _zero_stats() -> dict[str, Any]:
    return {
        "deleted_authors": 0,
        "deleted_series": 0,
        "deleted_author_links": 0,
        "books_backfilled": 0,
        "authors_resolved": 0,
        "books_merged": 0,
        "series_merged": 0,
        "abs_authors_stamped": 0,
        "orphan_authors_retrolinked": 0,
        "person_ids_mirrored": 0,
        "person_id_conflicts_resolved": 0,
        "person_bios_backfilled": 0,
        # v3.x (ADR-0016 slice 05) — image-URL health check stats.
        # Replaces the v2.22.0 `broken_image_urls_cleared` Job 8 stat,
        # which was the John-Birmingham `/books/`-path substring
        # workaround. Job 11 splits the cause into two buckets so the
        # operator can see HEAD-verified-dead vs substring-blacklisted.
        "image_urls_head_failed": 0,
        "image_urls_blacklisted_path": 0,
        "low_confidence_flagged": 0,
        "orphan_links_pruned": 0,
        # v3.x (ADR-0015 slice 05) — consolidate persons by shared source ID.
        "persons_merged_by_source_id": 0,
        "persons_merge_ambiguous": 0,
        # v2.27.0 Phase 5b Phase 6 — soft-delete retention sweeper.
        "soft_deletes_purged": 0,
        "soft_deletes_kept": 0,
        "soft_deletes_malformed": 0,
        "soft_deletes_errors": 0,
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
            # v3.0.0 Phase 9 (ADR-0012): contributor-aware — an author
            # "has books" if they're a contributor (book_authors), not just
            # the dropped books.author_id primary.
            cur = await db.execute(
                "SELECT DISTINCT a.normalized_name "
                "FROM authors a "
                "JOIN book_authors bpa ON bpa.author_id = a.id "
                "JOIN books b ON b.id = bpa.book_id "
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

        # Orphan-cleanup pass for `book_authors`: rows whose `book_id`
        # no longer exists in `books`. These accumulate when books
        # were deleted with PRAGMA foreign_keys=OFF (the FK was added
        # in v3.0.0 with ON DELETE CASCADE on book_id, but historical
        # deletes happened with FKs disabled, leaving the join rows
        # behind). Two consequences if we don't clean them up:
        #   1. The empty-author HAVING-COUNT query below still sees
        #      these orphans via the LEFT JOIN to authors, so it
        #      undercounts genuinely-empty authors when an orphan
        #      row references the author through a dangling book.
        #      In fact it OVERCOUNTS — the LEFT JOIN to books
        #      gives NULL for the orphan, COUNT(b.id) still =0, so
        #      such authors fall INTO the "empty" batch.
        #   2. The DELETE FROM authors at the bottom of this job
        #      then trips `book_authors.author_id` FK because the
        #      orphan row still pins the author. Observed live in
        #      hygiene on 2026-06-02 for abs-audio-library.
        cur = await db.execute(
            "DELETE FROM book_authors "
            "WHERE book_id NOT IN (SELECT id FROM books)"
        )
        orphan_ba_count = cur.rowcount or 0
        if orphan_ba_count:
            await db.commit()
            logger.info(
                "hygiene[%s] book_authors orphan-cleanup: deleted=%d "
                "(rows referencing book_id no longer in books)",
                slug, orphan_ba_count,
            )

        # Author cleanup — count books per author, skip allowlisted
        # names, skip cross-library mirrors, delete the rest.
        allowed_norms = await _load_allowed_norms()

        # v3.0.0 Phase 9 (ADR-0012): contributor-aware — an author with any
        # book_authors link is NOT an orphan (protects pure co-authors).
        cur = await db.execute(
            "SELECT a.id, a.name FROM authors a "
            "LEFT JOIN book_authors bpa ON bpa.author_id = a.id "
            "LEFT JOIN books b ON b.id = bpa.book_id "
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

        deleted_links = 0
        if deletable:
            # Series rows referenced by these authors are already
            # orphaned (no books), so cascade isn't required — but
            # we delete in a single transaction to keep the slug's
            # row count consistent for any concurrent reader.
            # Cascade-delete the global author_links rows first.
            # `author_links` lives in the global DB and can't have a
            # cross-file FK to the per-library `authors`, so we
            # delete it here manually. Pre-v2.22.0 this was silently
            # left behind, producing the orphaned-link churn pattern
            # documented in the ABS-sync arc.
            try:
                from app.database import get_db as get_global_db
                gdb = await get_global_db()
                try:
                    chunk = 500
                    for i in range(0, len(deletable), chunk):
                        batch = deletable[i : i + chunk]
                        placeholders = ",".join("?" * len(batch))
                        cur = await gdb.execute(
                            f"DELETE FROM author_links "
                            f"WHERE library_slug = ? "
                            f"AND author_id IN ({placeholders})",  # nosec B608
                            [slug, *batch],
                        )
                        deleted_links += cur.rowcount or 0
                    await gdb.commit()
                finally:
                    await gdb.close()
            except Exception as e:
                # Don't block the per-library cleanup on a global
                # DB hiccup; Phase G's orphan sweep is the safety net.
                logger.warning(
                    "hygiene[%s] author_links cascade failed (non-fatal): "
                    "%s: %s", slug, type(e).__name__, e,
                )
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
            stats.setdefault("deleted_author_links", 0)
            stats["deleted_author_links"] += deleted_links

        logger.info(
            "hygiene[%s] empty-cleanup: deleted_authors=%d deleted_series=%d "
            "deleted_author_links=%d kept_by_allowlist=%d "
            "kept_by_cross_library=%d",
            slug, len(deletable), deleted_series,
            deleted_links,
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

    v2.16.3 — pass `limit=200` so a first-run against a library
    with hundreds of audiobook-only authors (645 ABS Phase-2
    candidates on Mark's library — UAT 2026-05-17) doesn't take
    ~70 minutes at Goodreads' 5s + jitter rate-limit. The limit is
    shared across Phase-1 + Phase-2 by `backfill_missing_author_ids`,
    so Phase-1 (small, anchor-book-driven) runs first and Phase-2
    inherits the remaining budget. Hygiene is idempotent — a
    second run picks up the next batch of candidates that weren't
    reached. With ~30s per HTTP-bound author and a mix of fast
    resolver-chain-dry skips, this caps the chain at ~10-15 min
    per library wall-time even on first-run.
    """
    from app.discovery.goodreads_author_backfill import (
        backfill_missing_author_ids,
    )
    try:
        result = await backfill_missing_author_ids(limit=200)
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


# ─── Job 7 — Cross-library person backfill (v2.22.0) ────────────────
#
# Person-aware version of job 6. Walks every multi-link person and
# mirrors missing source IDs across linked sibling author rows. For
# the rare 2+ unique-value conflict, applies a "calibre wins"
# (ebook-side wins) policy and audit-logs the displaced value via
# `author_id_audit_log` so it's recoverable.
#
# Distinct from job 6 in that it uses the persons / author_links graph
# instead of normalized-name equality — manually-linked persons whose
# library rows have name variance still get their IDs mirrored, and
# accidentally-name-collided persons don't get cross-contaminated.
#
# Job 6 is retained for safety (handles authors not yet linked to a
# person), but after Job 7's orphan-retrolinking sibling runs (Job 8)
# Job 6 will no-op for the steady state.

async def job_cross_library_person_backfill(stats: dict[str, Any]) -> None:
    """Mirror NULL source IDs across linked persons; resolve conflicts
    via ebook-wins; clear the broken John Birmingham image_url
    (v3.x backlog item — proper author-image fix lives there).
    """
    from app.discovery.author_identity import (
        MIRRORABLE_SOURCE_ID_COLUMNS, _open_per_library,
    )
    from app.database import get_db as get_global_db

    # Build slug → content_type map for conflict resolution.
    slug_to_type: dict[str, str] = {}
    for lib in cross_library.libraries_for("all"):
        s = lib.get("slug")
        if s:
            slug_to_type[s] = lib.get("content_type") or "ebook"

    def _pick_winner(values: dict[str, str]) -> str:
        """Given {slug: value}, return the winning slug.

        Policy: ebook content_type wins; tiebreak alphabetical slug.
        Falls back to first slug alphabetically if no ebook lib has
        a value (e.g. two audiobook libraries).
        """
        ebook = sorted(s for s in values if slug_to_type.get(s) == "ebook")
        if ebook:
            return ebook[0]
        return sorted(values)[0]

    # Collect multi-link persons + their links.
    by_person: dict[int, list[tuple[str, int]]] = {}
    gdb = await get_global_db()
    try:
        cur = await gdb.execute(
            "SELECT person_id, library_slug, author_id FROM author_links "
            "WHERE person_id IN (SELECT person_id FROM author_links "
            "                    GROUP BY person_id HAVING COUNT(*) > 1)"
        )
        rows = await cur.fetchall()
        for r in rows:
            by_person.setdefault(r["person_id"], []).append(
                (r["library_slug"], r["author_id"])
            )
    finally:
        await gdb.close()

    sortable_cols = sorted(MIRRORABLE_SOURCE_ID_COLUMNS)
    mirrored = 0
    conflicts_resolved = 0
    audit_writes: list[tuple] = []

    for person_id, links in by_person.items():
        # Group aids by slug.
        slug_to_aids: dict[str, list[int]] = {}
        for s, aid in links:
            slug_to_aids.setdefault(s, []).append(aid)

        # Fetch current values per slug: {slug: {col: value}}.
        per_slug_vals: dict[str, dict[str, Optional[str]]] = {}
        for slug, aids in slug_to_aids.items():
            try:
                per_lib = await _open_per_library(slug)
            except Exception as e:
                logger.warning(
                    "person-backfill: cannot open %s: %s (skipping person %d)",
                    slug, e, person_id,
                )
                continue
            try:
                # Defensive — test fixtures and brand-new libraries
                # may not have the `authors` table yet.
                has_authors = await (await per_lib.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='authors' LIMIT 1"
                )).fetchone()
                if not has_authors:
                    continue
                cols_str = ", ".join(sortable_cols)
                ph = ",".join("?" * len(aids))
                cur = await per_lib.execute(
                    f"SELECT id, {cols_str} FROM authors "  # nosec B608
                    f"WHERE id IN ({ph})",
                    aids,
                )
                fetched = await cur.fetchall()
                slug_vals: dict[str, Optional[str]] = {}
                for r in fetched:
                    for col in sortable_cols:
                        v = r[col]
                        if v and not slug_vals.get(col):
                            slug_vals[col] = v
                per_slug_vals[slug] = slug_vals
            finally:
                await per_lib.close()

        # Per-column write decisions.
        for col in sortable_cols:
            slug_values: dict[str, str] = {}
            for slug, cols in per_slug_vals.items():
                v = cols.get(col)
                if v:
                    slug_values[slug] = v
            if not slug_values:
                continue
            unique = set(slug_values.values())
            if len(unique) == 1:
                value = next(iter(unique))
                for slug, aids in slug_to_aids.items():
                    if slug in slug_values:
                        continue
                    try:
                        per_lib = await _open_per_library(slug)
                    except Exception:
                        continue
                    try:
                        for aid in aids:
                            await per_lib.execute(
                                f"UPDATE authors SET {col} = ? "  # nosec B608
                                f"WHERE id = ? AND "
                                f"({col} IS NULL OR {col} = '')",
                                (value, aid),
                            )
                        await per_lib.commit()
                        mirrored += 1
                    finally:
                        await per_lib.close()
            else:
                # Conflict — apply ebook-wins.
                winner = _pick_winner(slug_values)
                winning_value = slug_values[winner]
                for slug, value in slug_values.items():
                    if slug == winner or value == winning_value:
                        continue
                    audit_writes.append(
                        (person_id, col, value, winning_value)
                    )
                    aids = slug_to_aids.get(slug, [])
                    try:
                        per_lib = await _open_per_library(slug)
                    except Exception:
                        continue
                    try:
                        for aid in aids:
                            await per_lib.execute(
                                f"UPDATE authors SET {col} = ? "  # nosec B608
                                f"WHERE id = ?",
                                (winning_value, aid),
                            )
                        await per_lib.commit()
                        conflicts_resolved += 1
                        logger.warning(
                            "person-backfill: conflict person_id=%d col=%s "
                            "winner=%s winning_value=%s loser=%s "
                            "loser_value=%s",
                            person_id, col, winner, winning_value,
                            slug, value,
                        )
                    finally:
                        await per_lib.close()

    # Flush audit-log writes (batched).
    if audit_writes:
        gdb = await get_global_db()
        try:
            await gdb.executemany(
                "INSERT INTO author_id_audit_log "
                "(person_id, source_name, old_value, new_value) "
                "VALUES (?, ?, ?, ?)",
                audit_writes,
            )
            await gdb.commit()
        finally:
            await gdb.close()

    # v2.22.0 Phase D — backfill persons.bio from any non-null
    # sibling bio. The Author Detail endpoint returns `persons.bio`
    # directly, so populating it makes the bio visible across the
    # cross-library identity surface. Picks the longest non-empty
    # bio as canonical when multiple siblings have one (consistent
    # with `_consolidate_persons`' existing tiebreak heuristic).
    bios_backfilled = 0
    from app.database import get_db as get_global_db_for_bio
    for person_id, links in by_person.items():
        # Skip if persons.bio is already populated.
        gdb = await get_global_db_for_bio()
        try:
            cur = await gdb.execute(
                "SELECT bio FROM persons WHERE id = ?", (person_id,),
            )
            row = await cur.fetchone()
            if row and row["bio"] and row["bio"].strip():
                continue
        finally:
            await gdb.close()
        # Collect per-library bios.
        bios: list[str] = []
        slug_to_aids2: dict[str, list[int]] = {}
        for s, aid in links:
            slug_to_aids2.setdefault(s, []).append(aid)
        for slug, aids in slug_to_aids2.items():
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
                ph = ",".join("?" * len(aids))
                cur = await per_lib.execute(
                    f"SELECT bio FROM authors WHERE id IN ({ph})",  # nosec B608
                    aids,
                )
                for r in await cur.fetchall():
                    if r["bio"] and r["bio"].strip():
                        bios.append(r["bio"])
            finally:
                await per_lib.close()
        if not bios:
            continue
        canonical_bio = max(bios, key=len)
        gdb = await get_global_db_for_bio()
        try:
            await gdb.execute(
                "UPDATE persons SET bio = ?, "
                "    last_updated_at = strftime('%s', 'now') "
                "WHERE id = ?",
                (canonical_bio, person_id),
            )
            await gdb.commit()
            bios_backfilled += 1
        finally:
            await gdb.close()

    # v3.x (ADR-0016 slice 05) — the John-Birmingham `/books/`-path
    # image-clear workaround that used to live here has been retired
    # in favor of an honest HEAD-verify in the new Job 11 "Image URL
    # health check". Job 8 keeps its named purpose (cross-library
    # source-ID mirror + bio backfill + link_confidence recompute);
    # image-URL hygiene is no longer a side-job here.

    stats["person_ids_mirrored"] = (
        stats.get("person_ids_mirrored", 0) + mirrored
    )
    stats["person_id_conflicts_resolved"] = (
        stats.get("person_id_conflicts_resolved", 0) + conflicts_resolved
    )
    stats["person_bios_backfilled"] = (
        stats.get("person_bios_backfilled", 0) + bios_backfilled
    )

    # v2.22.1 — recompute `link_confidence` flags after Job 8's
    # source-ID mirror. The pre-existing `_flag_low_confidence_links`
    # heuristic flags persons whose siblings share no source ID;
    # Job 8 may have just propagated IDs to all of them, so any
    # stale 'low' flags from the v2.20.0 migration become bookkeeping
    # debt unless we recompute. Without this, the Author Triage page
    # surfaces dozens of false-positive low-confidence cards even
    # though the underlying identity is fine. Matches the logic
    # exposed by `POST /persons/recompute-consolidation` but runs
    # automatically as part of hygiene.
    from app.discovery.author_identity import _flag_low_confidence_links
    from app.database import get_db as get_global_db_for_flag
    library_slugs = [s for s in slug_to_type.keys() if s]
    flagged_low = 0
    if library_slugs:
        gdb = await get_global_db_for_flag()
        try:
            # Reset all to 'high' then re-flag so manually-fixed links
            # get a fresh assessment.
            await gdb.execute(
                "UPDATE author_links SET link_confidence = 'high'"
            )
            await gdb.commit()
            flagged_low = await _flag_low_confidence_links(
                gdb, library_slugs,
            )
        finally:
            await gdb.close()
    stats["low_confidence_flagged"] = (
        stats.get("low_confidence_flagged", 0) + flagged_low
    )

    logger.info(
        "hygiene: person-backfill: mirrored=%d conflicts_resolved=%d "
        "person_bios_backfilled=%d low_confidence_flagged=%d",
        mirrored, conflicts_resolved, bios_backfilled, flagged_low,
    )


# ─── Job 8 — Orphan author retrolinking (v2.22.0) ──────────────────
#
# For every author row that lacks an `author_links` entry, call
# `get_or_create_person` so it gets joined to the cross-library
# identity graph. This catches:
#
#   1. Stub rows created by `author_mirror.backfill_dual_author_rows`
#      before v2.22.0 (which inserted without calling
#      `get_or_create_person`).
#   2. Authors created by `mirror_new_author_to_other_type_libs`
#      (the live mirror path) that no subsequent ABS/Calibre sync
#      has touched, leaving them unlinked.
#
# Idempotent — `get_or_create_person` is a no-op when the link
# already exists.

async def job_orphan_author_retrolink(stats: dict[str, Any]) -> None:
    """Walk every library's authors and ensure each has an
    author_links row. Uses `get_or_create_person`, which now (v2.22.0)
    fuzzy-matches when the exact normalized form misses, so existing
    person rows are reused where possible."""
    from app.discovery import author_identity
    from app.database import get_db as get_global_db

    retrolinked = 0
    libs = cross_library.libraries_for("all")
    for lib in libs:
        slug = lib.get("slug")
        if not slug:
            continue
        # Find authors without a link in this library.
        gdb = await get_global_db()
        try:
            cur = await gdb.execute(
                "SELECT author_id FROM author_links WHERE library_slug = ?",
                (slug,),
            )
            linked = {r["author_id"] for r in await cur.fetchall()}
        finally:
            await gdb.close()

        db = await get_library_db(slug)
        try:
            has_authors = await (await db.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='authors' LIMIT 1"
            )).fetchone()
            if not has_authors:
                continue
            cur = await db.execute("SELECT id, name FROM authors")
            authors = await cur.fetchall()
        finally:
            await db.close()

        for a in authors:
            if a["id"] in linked:
                continue
            try:
                await author_identity.get_or_create_person(
                    slug, a["id"], name=a["name"],
                )
                retrolinked += 1
            except Exception as e:
                logger.warning(
                    "orphan-retrolink: %s/%d (%r): %s: %s",
                    slug, a["id"], a["name"], type(e).__name__, e,
                )

    stats["orphan_authors_retrolinked"] = (
        stats.get("orphan_authors_retrolinked", 0) + retrolinked
    )
    logger.info(
        "hygiene: orphan-retrolink: %d author(s) joined to persons graph",
        retrolinked,
    )


# ─── Job 9 — Prune orphan author_links (v2.22.0) ───────────────────
#
# Wraps the existing `author_identity.prune_orphan_links` so it runs
# on every hygiene pass instead of waiting for an explicit caller.
# Safety net for the rare case where an author row gets deleted via
# a path that bypassed Job 1's cascade.

async def job_consolidate_persons_by_source_id(
    stats: dict[str, Any],
) -> None:
    """v3.x (ADR-0015 slice 05) — Find groups of distinct persons that
    share a ``(source, source_id)`` via their linked per-library
    author rows, and merge them into one.

    Closes the v2.20.0 split-person gap on prod day-one. Slice 03's
    ID-rung consolidates persons *as new scans run*; this job
    back-applies the same logic across existing data, so persons that
    were anchored to the same source ID before slice 03 landed get
    merged immediately rather than waiting for a future scan.

    Distinct from Job 8 (``job_cross_library_person_backfill``), which
    only *mirrors* a source-ID value across rows already linked to
    the same person; that path never merges separate persons. This
    job does the merging.

    Algorithm:
      1. Walk every library; read every per-library author row's
         populated source-ID columns.
      2. Map (source, value) → set of person_ids (via author_links).
      3. For each (source, value) where the set has 2+ persons, merge
         them into the lowest person_id (deterministic, stable
         re-runs).
      4. Merge mechanic: ``UPDATE author_links SET person_id=winner``
         for all losing-person links, then ``DELETE FROM persons
         WHERE id=loser``. Audit-log via the ``person_merges`` table
         (one row per merged pair, with the source/source_id that
         anchored the merge).
      5. Idempotent: once every group has collapsed onto one person,
         re-running finds no 2+ groups → zero merges.

    Greedy merging by source ID is the simple, defensible policy for
    a backfill. The slice-03 ambiguity case (one row's IDs anchoring
    two persons) materializes naturally here: the FIRST source/value
    we hit picks the merge winner; subsequent source/values that
    would have anchored a different winner now see all participants
    on the same person and are no-ops. The result converges to the
    same consistent state as slice 03's runtime behavior.

    Stats:
      - ``persons_merged_by_source_id`` — count of merges performed.
    """
    from app.database import get_db as get_global_db
    from app.discovery.author_identity import (
        MIRRORABLE_SOURCE_ID_COLUMNS, _open_per_library,
    )

    merged_count = 0
    libs = cross_library.libraries_for("all")
    slugs = [l["slug"] for l in libs if l.get("slug")]

    try:
        gdb = await get_global_db()
        try:
            # Build a (slug, author_id) → person_id map once.
            cur = await gdb.execute(
                "SELECT library_slug, author_id, person_id FROM author_links"
            )
            link_map: dict[tuple[str, int], int] = {
                (r["library_slug"], r["author_id"]): r["person_id"]
                for r in await cur.fetchall()
            }
        finally:
            await gdb.close()

        # Build (source, value) → set of person_ids across all libraries.
        id_cols = sorted(MIRRORABLE_SOURCE_ID_COLUMNS)
        cols_sql = ", ".join(["id"] + id_cols)
        groups: dict[tuple[str, str], set[int]] = {}
        for slug in slugs:
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
                    f"SELECT {cols_sql} FROM authors"  # nosec B608
                )
                for r in await cur.fetchall():
                    pid = link_map.get((slug, r["id"]))
                    if pid is None:
                        continue
                    for col in id_cols:
                        v = r[col]
                        if v and str(v).strip():
                            groups.setdefault(
                                (col[:-3], str(v).strip()),
                                set(),
                            ).add(pid)
            finally:
                await per_lib.close()

        # For each multi-person group, merge into the lowest person_id.
        if not any(len(s) > 1 for s in groups.values()):
            logger.info(
                "hygiene: consolidate-persons-by-source-id: no "
                "multi-person groups; nothing to merge (idempotent no-op)"
            )
            stats["persons_merged_by_source_id"] = (
                stats.get("persons_merged_by_source_id", 0)
            )
            return

        # Redirect map: a person that's been folded into another
        # records loser→winner here. When processing a later group,
        # translate every pid through the redirect (transitively) so
        # we never try to point an author_link at a deleted person
        # (FK violation) or merge a person into itself.
        redirect: dict[int, int] = {}

        def resolve(pid: int) -> int:
            while pid in redirect:
                pid = redirect[pid]
            return pid

        gdb = await get_global_db()
        try:
            for (source, value), pids in groups.items():
                # Translate through earlier merges in this run.
                live = {resolve(p) for p in pids}
                if len(live) < 2:
                    continue
                winner = min(live)
                losers = sorted(live - {winner})
                for loser in losers:
                    # Capture the loser's canonical_name for audit.
                    row = await (await gdb.execute(
                        "SELECT canonical_name FROM persons WHERE id = ?",
                        (loser,),
                    )).fetchone()
                    if not row:
                        # Defensive: should be unreachable given the
                        # redirect map above, but if the persons row
                        # vanished some other way, skip rather than
                        # FK-violate.
                        continue
                    loser_name = row["canonical_name"]
                    cur = await gdb.execute(
                        "UPDATE author_links SET person_id = ? "
                        "WHERE person_id = ?",
                        (winner, loser),
                    )
                    moved = cur.rowcount
                    await gdb.execute(
                        "DELETE FROM persons WHERE id = ?",
                        (loser,),
                    )
                    await gdb.execute(
                        "INSERT INTO person_merges "
                        "(winner_person_id, loser_person_id, reason, "
                        " source, source_id, moved_links, "
                        " loser_canonical_name) "
                        "VALUES (?, ?, 'consolidate_by_source_id', "
                        "        ?, ?, ?, ?)",
                        (winner, loser, source, value, moved,
                         loser_name),
                    )
                    redirect[loser] = winner
                    merged_count += 1
                    logger.info(
                        "hygiene: consolidate-persons-by-source-id: "
                        "merged loser_person_id=%d (%r) into "
                        "winner_person_id=%d via %s=%s (moved %d link(s))",
                        loser, loser_name, winner, source, value, moved,
                    )
            await gdb.commit()
        finally:
            await gdb.close()

        stats["persons_merged_by_source_id"] = (
            stats.get("persons_merged_by_source_id", 0) + merged_count
        )
    except Exception as e:
        msg = (
            f"consolidate-persons-by-source-id: {type(e).__name__}: {e}"
        )
        logger.exception(msg)
        stats["errors"].append(msg)


async def job_prune_orphan_links(stats: dict[str, Any]) -> None:
    from app.discovery import author_identity
    try:
        dropped = await author_identity.prune_orphan_links()
        stats["orphan_links_pruned"] = (
            stats.get("orphan_links_pruned", 0) + dropped
        )
        logger.info("hygiene: prune-orphan-links: dropped=%d", dropped)
    except Exception as e:
        msg = f"prune-orphan-links: {type(e).__name__}: {e}"
        logger.exception(msg)
        stats["errors"].append(msg)


# ─── Job 11 — Image URL health check (v3.x ADR-0016 slice 05) ─────


# Substring blacklist of URL patterns that are NEVER author photos.
# `/books/` — Goodreads book-cover-as-author-photo (the long-standing
#   John-Birmingham failure mode that Job 8 used to substring-clear).
# `nophoto` — Goodreads placeholder URLs for authors without a photo
#   (e.g. `nophoto/user/u_50x66-...`). The discovery-side
#   `_extract_author_photo` filter would have caught these at write
#   time post-slice-04, but legacy rows + future drift make defense-
#   in-depth cheap.
_IMAGE_URL_BLACKLIST_PATTERNS = ("/books/", "nophoto")


async def job_image_url_health_check(stats: dict[str, Any]) -> None:
    """v3.x (ADR-0016 slice 05) — verify every populated `authors.image_url`
    across every per-library DB. Two clears:

      1. Substring blacklist (`/books/`, `nophoto`) — clears the
         historical book-cover-as-author-photo URLs (the failure mode
         retired from Job 8 here) + any nophoto placeholders that
         slipped past upstream filters.
      2. HEAD-verify the remainder. Non-200 → NULL the row's image
         (ADR-0016 §6).

    **Local-clear-only** (ADR-0016 §6 D8(i)): clears the per-library
    row in place; does NOT fan a NULL through linked siblings + the
    persons row. The next scan re-establishes coherence via
    `mirror_image_url`'s rank-aware overwrite. Forcing a NULL fan-out
    would also clear siblings whose row might still return a 200 on
    its own URL (different per-library rows can carry different
    captures pre-mirror lockstep, and an operator-edited per-library
    override would also live here).

    HTTP cost: ~1500 authors × HEAD against CDNs (Goodreads `images.gr-
    assets.com`, Amazon `m.media-amazon.com`, etc.) per run. Only
    fires on operator Hygiene click; concurrent ceiling kept modest
    (8) so a hygiene run completes in seconds, not minutes.

    Errors during the HEAD itself (timeout, connection refused, etc.)
    are treated as non-200 per the locked design — they NULL the row.
    A subsequent scan repopulates the URL from any source so this is
    self-correcting; the cost of leaving a definitely-broken URL
    visible is higher than the cost of a brief placeholder while the
    next scan refills.
    """
    import asyncio
    import httpx
    from app.discovery.author_identity import _open_per_library

    head_failed = 0
    blacklisted = 0
    libraries = cross_library.libraries_for("all")

    # One AsyncClient covers all libraries; HTTP/2 + connection pooling
    # cuts HEAD overhead substantially vs per-request clients.
    timeout = httpx.Timeout(connect=5.0, read=5.0, write=5.0, pool=5.0)
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=8)

    async with httpx.AsyncClient(
        timeout=timeout, limits=limits, follow_redirects=True,
    ) as client:
        for lib in libraries:
            slug = lib.get("slug")
            if not slug:
                continue
            try:
                per_lib = await _open_per_library(slug)
            except Exception:
                continue
            try:
                # Guard against pre-v2.20.0 DBs that lack the table.
                has_authors = await (await per_lib.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='authors' LIMIT 1"
                )).fetchone()
                if not has_authors:
                    continue

                # 1) Substring blacklist clear (one SQL pass per pattern;
                # rowcount sums for stats).
                lib_blacklisted = 0
                for pattern in _IMAGE_URL_BLACKLIST_PATTERNS:
                    cur = await per_lib.execute(
                        "UPDATE authors SET image_url = NULL, "
                        "                   image_url_source = NULL "
                        "WHERE image_url LIKE ?",
                        (f"%{pattern}%",),
                    )
                    lib_blacklisted += cur.rowcount or 0
                if lib_blacklisted:
                    await per_lib.commit()
                blacklisted += lib_blacklisted

                # 2) HEAD-verify the remaining populated rows.
                rows = await (await per_lib.execute(
                    "SELECT id, image_url FROM authors "
                    "WHERE image_url IS NOT NULL"
                )).fetchall()

                async def _verify(row):
                    url = row["image_url"]
                    try:
                        r = await client.head(url)
                        return row["id"], r.status_code == 200
                    except Exception:
                        return row["id"], False

                # Parallelize within the per-library batch under the
                # client's max_connections ceiling. Each HEAD is small
                # + the connection pool reuses sockets.
                if rows:
                    results = await asyncio.gather(
                        *[_verify(r) for r in rows],
                        return_exceptions=False,
                    )
                    lib_failed = 0
                    for aid, ok in results:
                        if not ok:
                            await per_lib.execute(
                                "UPDATE authors SET image_url = NULL, "
                                "                   image_url_source = NULL "
                                "WHERE id = ?",
                                (aid,),
                            )
                            lib_failed += 1
                    if lib_failed:
                        await per_lib.commit()
                    head_failed += lib_failed
            finally:
                await per_lib.close()

    stats["image_urls_blacklisted_path"] = (
        stats.get("image_urls_blacklisted_path", 0) + blacklisted
    )
    stats["image_urls_head_failed"] = (
        stats.get("image_urls_head_failed", 0) + head_failed
    )
    logger.info(
        "hygiene: image-url-health: blacklisted=%d head_failed=%d",
        blacklisted, head_failed,
    )


# ─── Coordinator ────────────────────────────────────────────────────

async def run_all() -> dict[str, Any]:
    """Run the full Hygiene chain (12 jobs at v3.x ADR-0016 slice 05)
    across every configured library. Returns the rollup stats dict.

    Drives `state._hygiene_progress` per-step so the dashboard
    banner has a `N of TOTAL_JOBS: <job name> — <library>` path.

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

        # Job 7 — Orphan author retrolinking (cross-library). Runs
        # before Job 8 so newly-linked orphans participate in the
        # subsequent source-ID mirror pass.
        _set_phase(6, library="(cross-library)")
        await job_orphan_author_retrolink(stats)
        state._hygiene_progress["jobs"].append({
            "name": JOB_NAMES[6],
            "orphan_authors_retrolinked": stats["orphan_authors_retrolinked"],
        })

        # Job 8 — Cross-library person backfill (v2.22.0). Person-aware
        # source-ID mirror across linked siblings, with ebook-wins
        # conflict resolution + author_id_audit_log writes for any
        # displaced values.
        _set_phase(7, library="(cross-library)")
        await job_cross_library_person_backfill(stats)
        state._hygiene_progress["jobs"].append({
            "name": JOB_NAMES[7],
            "person_ids_mirrored": stats["person_ids_mirrored"],
            "person_id_conflicts_resolved": stats["person_id_conflicts_resolved"],
        })

        # Job 9 — Consolidate persons by shared source ID
        # (v3.x ADR-0015 slice 05). Runs after Job 8's mirror so the
        # population of {source}_id columns is at its most complete;
        # then this job merges persons sharing a (source, source_id).
        _set_phase(8, library="(cross-library)")
        await job_consolidate_persons_by_source_id(stats)
        # v3.6.2 — explicit job-completion log so operators see Jobs
        # 9/11/12 in the log stream. Pre-v3.6.2, only Job 10 emitted
        # a finish-line marker (`prune-orphan-links: dropped=%d`),
        # leaving the other cross-library jobs invisible.
        logger.info(
            "hygiene: %s complete: persons_merged_by_source_id=%d",
            JOB_NAMES[8], stats["persons_merged_by_source_id"],
        )
        state._hygiene_progress["jobs"].append({
            "name": JOB_NAMES[8],
            "persons_merged_by_source_id": stats["persons_merged_by_source_id"],
        })

        # Job 10 — Prune orphan author_links (safety net for the rare
        # case where an author row gets deleted via a path that
        # bypassed Job 1's cascade).
        _set_phase(9, library="(cross-library)")
        await job_prune_orphan_links(stats)
        state._hygiene_progress["jobs"].append({
            "name": JOB_NAMES[9],
            "orphan_links_pruned": stats["orphan_links_pruned"],
        })

        # Job 11 — Image URL health check (v3.x ADR-0016 slice 05).
        # Substring blacklist (`/books/`, `nophoto`) clears the legacy
        # John-Birmingham-shape rows that used to live in Job 8's
        # workaround; HEAD-verifies remaining populated rows; clears
        # non-200 responses. Local-clear-only (ADR-0016 §6 D8(i)).
        _set_phase(10, library="(cross-library)")
        await job_image_url_health_check(stats)
        logger.info(
            "hygiene: %s complete: blacklisted_path=%d head_failed=%d",
            JOB_NAMES[10],
            stats["image_urls_blacklisted_path"],
            stats["image_urls_head_failed"],
        )
        state._hygiene_progress["jobs"].append({
            "name": JOB_NAMES[10],
            "image_urls_blacklisted_path": stats["image_urls_blacklisted_path"],
            "image_urls_head_failed":      stats["image_urls_head_failed"],
        })

        # Job 12 — Soft-delete retention sweep (v2.27.0 Phase 5b
        # Phase 6). Filesystem-only; walks every library's
        # .seshat-replaced/<ts>/ subdirs and purges anything older
        # than `active_replacement_soft_delete_retention_days`
        # (default 30). Idempotent — once steady state is reached,
        # this is a near-no-op on every subsequent run.
        _set_phase(11, library="(cross-library)")
        try:
            from app.orchestrator.active_replacement import (
                purge_expired_soft_deletes,
            )
            sweep = purge_expired_soft_deletes(libraries=libs)
            stats["soft_deletes_purged"]    = sweep["purged"]
            stats["soft_deletes_kept"]      = sweep["kept"]
            stats["soft_deletes_malformed"] = sweep["malformed"]
            stats["soft_deletes_errors"]    = sweep["errors"]
        except Exception as e:
            logger.exception("hygiene: soft-delete sweep crashed")
            stats["errors"].append(
                f"soft_delete_sweep: {type(e).__name__}: {e}"
            )
        logger.info(
            "hygiene: %s complete: purged=%d kept=%d malformed=%d errors=%d",
            JOB_NAMES[11],
            stats["soft_deletes_purged"],
            stats["soft_deletes_kept"],
            stats["soft_deletes_malformed"],
            stats["soft_deletes_errors"],
        )
        state._hygiene_progress["jobs"].append({
            "name": JOB_NAMES[11],
            "soft_deletes_purged":    stats["soft_deletes_purged"],
            "soft_deletes_kept":      stats["soft_deletes_kept"],
            "soft_deletes_malformed": stats["soft_deletes_malformed"],
            "soft_deletes_errors":    stats["soft_deletes_errors"],
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
                f"+{stats['abs_authors_stamped']} ABS stamps, "
                f"+{stats.get('orphan_authors_retrolinked', 0)} retrolinked, "
                f"+{stats.get('person_ids_mirrored', 0)} IDs mirrored, "
                f"-{stats.get('orphan_links_pruned', 0)} orphan links, "
                f"-{stats.get('soft_deletes_purged', 0)} expired soft-deletes"
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
