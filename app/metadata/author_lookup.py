"""
Cross-library author lookup helpers for the metadata enricher.

The enricher runs library-agnostic — by the time `enrich()` is
called, the completed download hasn't yet been linked to a specific
library's `books` row (acquisition linkback happens later). So when
we want the author's stored `goodreads_id` to anchor GoodreadsSource's
T4/T5 resolver tiers, we have to walk every discovered library's
authors table and take the first non-empty match.

Author identity is global (the same person has the same Goodreads ID
in every library), so picking the first hit is correct. If a library
happens to hold a wrong ID, the enricher's downstream `score_match()`
gate will reject the resulting bogus MetaRecord on confidence anyway.

The input `name` is whatever the enricher held in `metadata.author` —
often a multi-author comma/and/&-joined blob like "Alex Toxic, Nadya
Lee". The `authors` table stores each name as its own row, so we
split the blob via `app.filter.gate.split_authors` and try each name
in primary-first order, returning the first hit. Primary-first
matters because the resolver's T4/T5 tiers fuzzy-match against the
anchor author's bibliography — co-author bibliographies are a less
reliable place to find a book where someone else is the headline
author.
"""
from __future__ import annotations

import logging

_log = logging.getLogger("seshat.metadata.author_lookup")


async def get_goodreads_id_for_author(name: str) -> str:
    """Return the stored `authors.goodreads_id` for `name`, or "".

    Splits multi-author blobs and walks every discovered library
    looking for any individual name (primary author tried first).
    Returns the first non-empty goodreads_id found. Empty string
    when no name matches any library, the matched author has no
    stored goodreads_id, or the discovery state hasn't initialized
    (test mode).
    """
    return await _get_author_column("goodreads_id", name, library_slug="")


async def get_amazon_id_for_author(name: str, library_slug: str = "") -> str:
    """Return the stored `authors.amazon_id` for `name`, or "".

    Mirrors :func:`get_goodreads_id_for_author` but reads
    ``authors.amazon_id``. When ``library_slug`` is non-empty the
    walk is restricted to that library (the enricher passes the
    grab's destination library so the cache-first Amazon path
    queries against the library that actually owns the cached rows).
    Empty ``library_slug`` walks every discovered library — useful
    in tests and any future caller without an active library context.
    """
    return await _get_author_column("amazon_id", name, library_slug=library_slug)


async def _get_author_column(
    column: str, name: str, *, library_slug: str = "",
) -> str:
    """Internal helper — look up ``authors.<column>`` for ``name``.

    Library selection:
      - ``library_slug=""`` walks every discovered library and returns
        the first non-empty match (identifier identity is global per
        the module docstring above).
      - Non-empty ``library_slug`` restricts the walk to that single
        library. Returns "" if the library has the author but no
        stored value, or if the library doesn't have the author at all.
    """
    if not name or not name.strip():
        return ""

    # Defer state import so test code paths that don't need this
    # can avoid pulling the global library state.
    try:
        from app import state
        from app.discovery.database import get_db as get_library_db
        from app.filter.gate import split_authors
        from app.metadata.author_names import normalize_author_name
    except Exception:
        return ""

    all_libraries = list(state._discovered_libraries or [])
    if not all_libraries:
        return ""
    if library_slug:
        libraries = [lib for lib in all_libraries if (lib or {}).get("slug") == library_slug]
    else:
        libraries = all_libraries
    if not libraries:
        return ""

    individual_names = split_authors(name)
    if not individual_names:
        return ""

    sql = f"SELECT {column} FROM authors WHERE normalized_name = ?"
    for individual_name in individual_names:
        # Match on `authors.normalized_name`, not `name`. MAM announces
        # often drop punctuation ("St Arkham") while Calibre keeps it
        # ("St. Arkham"); a strict `WHERE name = ?` misses the row and
        # the enricher proceeds without an author identifier anchor.
        norm_target = normalize_author_name(individual_name)
        if not norm_target:
            continue
        for lib in libraries:
            slug = (lib or {}).get("slug")
            if not slug:
                continue
            try:
                db = await get_library_db(slug)
            except Exception:
                continue
            try:
                row = await (await db.execute(sql, (norm_target,))).fetchone()
                if row and row[0]:
                    return str(row[0])
            except Exception as e:
                _log.debug(
                    "author_lookup: %s %s lookup failed for %r: %s",
                    slug, column, individual_name, e,
                )
            finally:
                try:
                    await db.close()
                except Exception:
                    pass
    return ""


def get_library_slug_for_content_type(content_type: str) -> str:
    """Return the slug of the first discovered library matching
    ``content_type`` ("ebook" or "audiobook"), or "" when none match.

    Used by the orchestrator to derive a destination library at
    enrichment time. Today's setups are one-library-per-content-type
    so this is deterministic; future multi-library-per-content-type
    work will need richer routing.
    """
    if not content_type:
        return ""
    try:
        from app import state
    except Exception:
        return ""
    for lib in state._discovered_libraries or []:
        if (lib or {}).get("content_type") == content_type:
            slug = (lib or {}).get("slug") or ""
            if slug:
                return str(slug)
    return ""
