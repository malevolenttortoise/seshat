"""Dual author-row pattern (v2.12.1 #2).

When an author exists in one library type (ebook OR audiobook), a
stub row is auto-created in every library of the OTHER type. The
stub has zero books — empty author pages display "No X yet for this
author" when filtered to the stub-only type.

Why: pre-v2.12.1 the cross-library Scan Audiobooks / Scan Ebooks
buttons no-op'd ("No matching authors in target libraries") when
the author existed only in one library type. The locked design from
v2.12.0 UAT 2026-05-14: every author should have a row in BOTH
types so the cross-library scan can always run discovery, even when
no books are owned yet in the target type. Stub rows are
discovery-only — they don't claim ownership of any books and don't
carry app-specific identity columns (no calibre_id / audiobookshelf_id).

Three entry points:

  1. `mirror_new_author_to_other_type_libs(name, source_content_type)`
     called from calibre_sync + audiobookshelf_sync immediately after
     they insert a new author row. Idempotent — checks for existing
     name match per target lib before inserting.

  2. `backfill_dual_author_rows()` — one-shot startup task that runs
     on the first boot after v2.12.1. Iterates every author in every
     library, mirrors any missing stubs. Guarded by a settings flag
     so it doesn't repeat.

  3. Tests cover idempotency + the per-library-discovery iteration.
"""
from __future__ import annotations

import logging
from typing import Optional

from app import state
from app.discovery.database import get_db


logger = logging.getLogger("seshat.discovery.author_mirror")


async def mirror_new_author_to_other_type_libs(
    name: str,
    source_content_type: str,
    *,
    sort_name: Optional[str] = None,
    normalized_name: Optional[str] = None,
) -> int:
    """Create a stub author row in every library whose content_type
    differs from `source_content_type`.

    Returns the count of libraries that received a new stub
    (libraries that already had a matching row are skipped, so the
    return value is purely informational).

    `name` is the display name; `sort_name` and `normalized_name`
    follow if provided (caller knows the canonical forms for the
    source library — pass them through for consistency).

    Idempotent: queries each target lib for an existing `name` match
    before inserting. Cross-library author identity is by canonical
    name (the same heuristic the merge layer uses).
    """
    if not name or not name.strip():
        return 0
    if source_content_type not in ("ebook", "audiobook"):
        # Unknown content type — refuse to mirror rather than fan
        # into every other type (could over-mirror if future content
        # types land).
        return 0
    target_libs = [
        lib for lib in state._discovered_libraries
        if (lib.get("content_type") or "ebook") != source_content_type
    ]
    if not target_libs:
        return 0
    inserted = 0
    for lib in target_libs:
        slug = lib.get("slug")
        if not slug:
            continue
        try:
            db = await get_db(slug)
        except Exception as e:
            logger.warning(
                "author_mirror: cannot open lib %s for stub insert: %s",
                slug, e,
            )
            continue
        try:
            existing = await (await db.execute(
                "SELECT id FROM authors WHERE name = ?", (name,),
            )).fetchone()
            if existing:
                continue
            await db.execute(
                "INSERT INTO authors (name, sort_name, normalized_name) "
                "VALUES (?, ?, ?)",
                (name, sort_name or name, normalized_name),
            )
            await db.commit()
            inserted += 1
            logger.debug(
                "author_mirror: stubbed '%s' into %s (source_type=%s)",
                name, slug, source_content_type,
            )
        finally:
            await db.close()
    if inserted > 0:
        logger.info(
            "author_mirror: '%s' stubbed into %d %s librar%s",
            name, inserted,
            "ebook" if source_content_type == "audiobook" else "audiobook",
            "y" if inserted == 1 else "ies",
        )
    return inserted


async def backfill_dual_author_rows() -> dict:
    """One-shot backfill pass. Iterates every author in every library
    and ensures the OTHER content type's libraries have matching stub
    rows.

    Returns a summary dict for logging:
      {checked: N, stubs_inserted: M, by_library: {slug: count, ...}}

    Idempotent. Safe to re-run; subsequent calls find existing rows
    and skip them. The startup hook guards against unnecessary re-runs
    via a settings flag (`v2_12_1_dual_row_backfill_done`).
    """
    summary = {"checked": 0, "stubs_inserted": 0, "by_library": {}}
    libs_by_type: dict[str, list[dict]] = {"ebook": [], "audiobook": []}
    for lib in state._discovered_libraries:
        ct = lib.get("content_type") or "ebook"
        libs_by_type.setdefault(ct, []).append(lib)
    if not (libs_by_type.get("ebook") and libs_by_type.get("audiobook")):
        # User only has one content type configured — nothing to mirror.
        logger.info(
            "author_mirror.backfill: skipping; user has libraries of only "
            "%s content type(s)", list(libs_by_type.keys()),
        )
        return summary

    # For each source content type, collect every author name, then
    # ensure each opposite-type lib has a row. The two loops are
    # independent (ebook → mirror to audiobook libs; audiobook →
    # mirror to ebook libs).
    for source_ct in ("ebook", "audiobook"):
        target_ct = "audiobook" if source_ct == "ebook" else "ebook"
        # Collect names from every source-type lib.
        source_names: set[str] = set()
        for lib in libs_by_type.get(source_ct, []):
            slug = lib.get("slug")
            if not slug:
                continue
            try:
                db = await get_db(slug)
            except Exception as e:
                logger.warning(
                    "author_mirror.backfill: cannot open %s: %s", slug, e,
                )
                continue
            try:
                rows = await (await db.execute(
                    "SELECT name FROM authors",
                )).fetchall()
                source_names.update(r[0] for r in rows if r[0])
                summary["checked"] += len(rows)
            finally:
                await db.close()
        # Insert stubs into every target-type lib for any name that
        # isn't already there.
        for lib in libs_by_type.get(target_ct, []):
            slug = lib.get("slug")
            if not slug:
                continue
            try:
                db = await get_db(slug)
            except Exception as e:
                logger.warning(
                    "author_mirror.backfill: cannot open %s: %s", slug, e,
                )
                continue
            try:
                existing_rows = await (await db.execute(
                    "SELECT name FROM authors",
                )).fetchall()
                existing = {r[0] for r in existing_rows if r[0]}
                missing = source_names - existing
                if not missing:
                    continue
                # Bulk-insert the missing stubs. Populate
                # normalized_name so future ABS/Calibre syncs can
                # consolidate onto these rows via the normalized
                # fallback path (v2.22.0 — pre-fix, stubs from this
                # path had NULL normalized_name and produced the
                # orphan-author churn pattern).
                from app.metadata.author_names import normalize_author_name
                inserted_here = 0
                for name in missing:
                    norm = normalize_author_name(name)
                    await db.execute(
                        "INSERT INTO authors (name, sort_name, normalized_name) "
                        "VALUES (?, ?, ?)",
                        (name, name, norm),
                    )
                    inserted_here += 1
                await db.commit()
                summary["stubs_inserted"] += inserted_here
                summary["by_library"][slug] = (
                    summary["by_library"].get(slug, 0) + inserted_here
                )
                logger.info(
                    "author_mirror.backfill: stubbed %d author(s) into %s "
                    "(source_type=%s)", inserted_here, slug, source_ct,
                )
            finally:
                await db.close()
    return summary
