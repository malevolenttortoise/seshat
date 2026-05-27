"""
Author endpoints — list, detail, scan triggers, and reset operations.

The author scan triggers in this file all funnel through
`_spawn_lookup_task`, which manages the single-author / single-author-
full-rescan / bulk-authors paths as background asyncio tasks tracked
through `state._lookup_task` + `state._lookup_progress`. This is what
makes the Dashboard widget's "Stop" button work uniformly regardless
of where the scan was kicked off from.

Endpoints:
  GET  /api/authors                       — paginated list with filters
  GET  /api/authors/{aid}                 — detail with series & standalone
  POST /api/authors/{aid}/lookup          — single-author source scan
  POST /api/authors/{aid}/full-rescan     — single-author full re-scan
  POST /api/authors/clear-scan-data       — wipe source/MAM data per author set
  POST /api/authors/scan-sources          — bulk source scan
  POST /api/authors/scan-mam              — bulk MAM scan
  POST /api/sources/reset                 — global source-scan reset
"""
import asyncio
import logging
from typing import Any, Optional
from fastapi import APIRouter, Body, HTTPException, Query

from app import state
from app.config import load_settings
from app.database import get_db as get_global_db
from app.discovery.author_identity import (
    MIRRORABLE_SOURCE_ID_COLUMNS,
    get_person,
    linked_authors,
    mirror_source_id,
    person_id_for,
)
from app.metadata.source_url_parsers import (
    canonical_author_url,
    known_sources,
    parse_source_id,
)
from app.discovery.database import get_db, get_active_library, HF, cleanup_empty_series
from app.discovery.routers.series import _recompute_series_author
from app.discovery.lookup import lookup_author
from app.discovery.cross_library import (
    libraries_for,
    run_across_libraries,
    sort_and_paginate,
    sort_key_for,
)

logger = logging.getLogger("seshat.discovery")

router = APIRouter(prefix="/api/discovery", tags=["authors"])


def _build_authors_sql(search, has_missing, book_type, include_orphans, sort, sort_dir):
    q = (
        f"SELECT a.*, "
        f"COUNT(DISTINCT CASE WHEN {HF} AND COALESCE(b.is_omnibus,0)=0 THEN b.id END) as total_books, "
        f"SUM(CASE WHEN b.owned=1 AND {HF} AND COALESCE(b.is_omnibus,0)=0 THEN 1 ELSE 0 END) as owned_count, "
        f"SUM(CASE WHEN b.owned=0 AND {HF} AND COALESCE(b.is_omnibus,0)=0 THEN 1 ELSE 0 END) as missing_count, "
        f"SUM(CASE WHEN b.is_new=1 AND b.owned=0 AND {HF} AND COALESCE(b.is_omnibus,0)=0 THEN 1 ELSE 0 END) as new_count, "
        f"COUNT(DISTINCT b.series_id) as series_count, "
        f"(SELECT COUNT(*) FROM pen_name_links pl "
        f" WHERE pl.canonical_author_id=a.id OR pl.alias_author_id=a.id) as link_count "
        # v3.0.0 Phase 4 — per-author counts route through book_authors
        # (ADR-0008) so a co-authored book counts for EACH of its authors,
        # not just the primary. `book_authors` PK is (book_id,author_id),
        # so a book links to a given author at most once → the existing
        # COUNT(DISTINCT b.id) / SUM(CASE…) aggregates don't fan out.
        # (Whole-library totals live elsewhere and stay on distinct
        # `books` rows — never summed from these per-author counts.)
        f"FROM authors a "
        f"LEFT JOIN book_authors ba ON a.id=ba.author_id "
        f"LEFT JOIN books b ON b.id=ba.book_id"
    )
    p: list = []; c: list[str] = []
    if search:
        c.append("a.name LIKE ?"); p.append(f"%{search}%")
    if book_type == "series":
        c.append("b.series_id IS NOT NULL")
    elif book_type == "standalone":
        c.append("b.series_id IS NULL")
    if c:
        q += " WHERE " + " AND ".join(c)
    q += " GROUP BY a.id"
    having = []
    if not include_orphans:
        having.append("total_books > 0")
    if has_missing:
        having.append("missing_count > 0")
    if having:
        q += " HAVING " + " AND ".join(having)
    d = "DESC" if sort_dir == "desc" else "ASC"
    q += {
        "missing": f" ORDER BY missing_count {d}, a.sort_name ASC",
        "new": f" ORDER BY new_count {d}, a.sort_name ASC",
        "total": f" ORDER BY total_books {d}, a.sort_name ASC",
        # v2.17.0 Feat D — Owned sort. Parity with the "Missing" /
        # "Total" sorts; ties broken by sort_name ASC for stable ordering.
        "owned": f" ORDER BY owned_count {d}, a.sort_name ASC",
    }.get(sort, f" ORDER BY a.sort_name {d}")
    return q, p


@router.get("/authors")
async def get_authors(search: str = Query(None), sort: str = Query("name"), sort_dir: str = Query("asc"), has_missing: bool = Query(None), book_type: str = Query(None), include_orphans: bool = Query(False), content_type: str = Query(None)):
    """List authors.

    `content_type` selects active-library (omitted) vs. cross-library
    aggregation ("ebook" / "audiobook" / "all"). In cross-library
    mode, authors with the same normalized name across libraries get
    their per-library stats merged so a user with Calibre + ABS sees
    one "Pierce Brown" row with owned/missing counts summed — not one
    row per library.

    By default, "orphan" authors with zero linked book rows are hidden.
    `?include_orphans=true` shows everything.
    """
    if content_type:
        sql, params = _build_authors_sql(
            search, has_missing, book_type, include_orphans, sort, sort_dir,
        )

        async def q(db):
            rows = await (await db.execute(sql, params)).fetchall()
            return [dict(r) for r in rows]

        # v2.17.1 — always fetch across EVERY library (passing "all"
        # to run_across_libraries) so the merged counts are global
        # regardless of which format tab the user clicked. The
        # content_type the user requested gets applied AFTER merge as
        # a list filter on `content_types`. Pre-fix the Audiobooks
        # tab only queried audiobook libraries, so a cross-format
        # author like Emrys Ambrosius (5 ebooks + 1 audiobook) was
        # shown with just the audiobook counts (1/0) instead of the
        # global 1/5. Same issue mirror-image on Ebooks tab.
        rows = await run_across_libraries("all", q)
        # v2.20.0 Phase 4 — dedupe by person_id when present, falling
        # back to normalized_name for rows that aren't (yet) linked.
        # This is more accurate than name-only matching across
        # libraries: punctuation drift (C.W. vs Charles W.) merges
        # correctly via author_links, and name collisions across
        # libraries (two different "John Smith"s, marked low_confidence
        # at migration time) stay split — which the legacy name-match
        # path silently smooshed together.
        from app.works.normalize import normalize_author
        # Pre-fetch every author_link for the libraries we're about
        # to merge so the per-row person_id lookup is a single dict
        # hit rather than a roundtrip per row.
        gdb_local = await get_global_db()
        try:
            link_rows = await (await gdb_local.execute(
                "SELECT library_slug, author_id, person_id FROM author_links"
            )).fetchall()
            person_id_lookup: dict[tuple[str, int], int] = {
                (lr["library_slug"], lr["author_id"]): lr["person_id"]
                for lr in link_rows
            }
        finally:
            await gdb_local.close()
        merged: dict[str, dict] = {}
        for r in rows:
            slug = r.get("library_slug")
            aid = r.get("id")
            person_id = (
                person_id_lookup.get((slug, aid))
                if slug and aid is not None else None
            )
            if person_id is not None:
                key = f"pid:{person_id}"
            else:
                key = f"norm:{normalize_author(r.get('name', ''))}"
            if not key or key == "norm:":
                continue
            if key in merged:
                base = merged[key]
                for counter in ("total_books", "owned_count", "missing_count",
                                "new_count", "series_count"):
                    base[counter] = (base.get(counter) or 0) + (r.get(counter) or 0)
                # Track which libraries + per-library ids the author
                # appears in — frontend uses these to navigate into
                # the right library's author-detail page.
                base["library_slugs"].append(r["library_slug"])
                base["author_ids_by_slug"][r["library_slug"]] = r.get("id")
                # v2.17.0 Bug A — parallel content-type list lets the
                # Authors tile render a format badge (📖/🎧/📖🎧)
                # without re-fetching `_discovered_libraries` from the
                # frontend.
                ct = r.get("content_type")
                if ct and ct not in base["content_types"]:
                    base["content_types"].append(ct)
            else:
                merged[key] = {
                    **r,
                    "library_slugs": [r["library_slug"]],
                    "author_ids_by_slug": {r["library_slug"]: r.get("id")},
                    "content_types": (
                        [r["content_type"]] if r.get("content_type") else []
                    ),
                }
        # v2.17.0 — sort_dir now applies uniformly across all sort
        # keys (was previously baked-in DESC for count sorts via a
        # negative-key trick). Parity with the DiscBooksPage sort
        # behavior + a frontend invert button gives the user explicit
        # control.
        #
        # v2.17.2 — name sort now keys on LAST NAME (matching the
        # frontend's `getLetterKey`/`getLastName` logic for the
        # alphabet sidebar). `sort_name` from Calibre is inconsistent:
        # most rows store "Last, First", but pseudonyms and some
        # imports stay "First Last" verbatim (Emrys Ambrosius,
        # Aaron Renfroe, etc.). The alphabetical SQL sort placed
        # those under their FIRST initial — so Emrys landed under
        # "E", invisible on page 1 of the All-sort even though the
        # sidebar correctly puts them at "A" via last-word
        # extraction. Now both layers agree.
        def _last_name_key(x):
            name = (x.get("name") or x.get("sort_name") or "").strip()
            parts = name.split()
            last = parts[-1] if len(parts) > 1 else (parts[0] if parts else "")
            return (last.lower(), name.lower())
        def _key_count(field: str):
            def k(x):
                return (
                    x.get(field) or 0,
                    *_last_name_key(x),
                )
            return k
        sort_fn = {
            "missing": _key_count("missing_count"),
            "new": _key_count("new_count"),
            "total": _key_count("total_books"),
            "owned": _key_count("owned_count"),
        }.get(sort, _last_name_key)
        reverse = sort_dir == "desc"
        authors = sorted(merged.values(), key=sort_fn, reverse=reverse)
        # v2.17.1 — post-merge content_type filter. The frontend's
        # "Audiobooks" / "Ebooks" tabs scope which authors to show,
        # but each author's counts are already global from the merge
        # above. "all" passes everything through.
        if content_type and content_type != "all":
            authors = [
                a for a in authors
                if content_type in (a.get("content_types") or [])
            ]
        return {"authors": authors}

    db = await get_db()
    try:
        sql, p = _build_authors_sql(
            search, has_missing, book_type, include_orphans, sort, sort_dir,
        )
        return {"authors": [dict(r) for r in await (await db.execute(sql, p)).fetchall()]}
    finally:
        await db.close()


async def _author_detail_for_slug(slug: str, aid: int) -> Optional[dict]:
    """Fetch the full author detail (author + series + standalone) from a specific library.

    Returns None when the author id isn't in that library. Used by the
    cross-library fan-out below so the detail page can show both
    ebook and audiobook sections of a merged author.

    Books returned under `standalone_books` are stamped with
    `library_slug` + `content_type` so the frontend's
    `coverSrcFor` picks the per-library cover endpoint. Without this
    the cover-src fell back to the active-library path and served
    unrelated books' covers.
    """
    content_type = next(
        (l.get("content_type", "ebook") for l in state._discovered_libraries
         if l.get("slug") == slug),
        "ebook",
    )
    db = await get_db(slug)
    try:
        r = await (await db.execute("SELECT * FROM authors WHERE id=?", (aid,))).fetchone()
        if not r:
            return None
        a = dict(r)
        # The HAVING filter drops series where every book by this
        # author is hidden — without it the tile still renders as
        # "(0/0)" after a user hides the last visible book in a
        # source-scan-populated series. The count uses
        # `author_visible_count` (omnibus included) so a series whose
        # only book by this author is an omnibus still renders; the
        # IS section surfaces the omnibus under "Omnibus / Collections".
        # `author_book_count` (omnibus EXCLUDED) is what the count
        # badge displays so progress reflects actual entries, not
        # collections.
        # v3.0.0 Phase 4 — author-scoped counts read from book_authors
        # (ADR-0008). `bca` is scoped to THIS author (bca.author_id=aid),
        # so `bca.author_id IS NOT NULL` means "aid is a contributor of
        # this book" — a co-authored book now counts for every one of
        # its authors, not just the primary. At most one `bca` row per
        # book (the (book_id,author_id) PK), so no GROUP fan-out.
        # `book_count` (whole-series total, any author) stays on the
        # books row; `multi_author` (a series-taxonomy hint owned by
        # Phase 6) stays on the legacy author_id for now.
        a["series"] = [dict(s) for s in await (await db.execute(
            f"""SELECT s.*,
                COUNT(DISTINCT CASE WHEN {HF} AND COALESCE(b.is_omnibus,0)=0 THEN b.id END) as book_count,
                COUNT(DISTINCT CASE WHEN bca.author_id IS NOT NULL AND {HF} AND COALESCE(b.is_omnibus,0)=0 THEN b.id END) as author_book_count,
                COUNT(DISTINCT CASE WHEN bca.author_id IS NOT NULL AND {HF} THEN b.id END) as author_visible_count,
                COUNT(DISTINCT CASE WHEN bca.author_id IS NOT NULL AND {HF} AND COALESCE(b.is_omnibus,0)=1 THEN b.id END) as author_omnibus_count,
                SUM(CASE WHEN b.owned=1 AND bca.author_id IS NOT NULL AND {HF} AND COALESCE(b.is_omnibus,0)=0 THEN 1 ELSE 0 END) as owned_count,
                SUM(CASE WHEN b.owned=0 AND bca.author_id IS NOT NULL AND {HF} AND COALESCE(b.is_omnibus,0)=0 THEN 1 ELSE 0 END) as missing_count,
                CASE WHEN COUNT(DISTINCT b.author_id) > 1 THEN 1 ELSE 0 END as multi_author
            FROM series s
            JOIN books b ON s.id=b.series_id
            LEFT JOIN book_authors bca ON bca.book_id=b.id AND bca.author_id=?
            WHERE s.id IN (SELECT DISTINCT b2.series_id FROM books b2
                           JOIN book_authors ba2 ON ba2.book_id=b2.id
                           WHERE ba2.author_id=? AND b2.series_id IS NOT NULL)
            GROUP BY s.id
            HAVING author_visible_count > 0
            ORDER BY s.name""",
            (aid, aid)
        )).fetchall()]
        # v3.0.0 Phase 4 — standalone list joins book_authors scoped to
        # aid (INNER → only books where aid is a contributor). The
        # `author_name` display still resolves the primary via
        # b.author_id; per-book multi-author display is Phase 7.
        standalone = [
            {**dict(b), "library_slug": slug, "content_type": content_type}
            for b in await (await db.execute(
                f"SELECT b.*, a2.name as author_name FROM books b "
                f"JOIN book_authors bca ON bca.book_id=b.id AND bca.author_id=? "
                f"JOIN authors a2 ON b.author_id=a2.id "
                f"WHERE b.series_id IS NULL AND {HF} ORDER BY b.pub_date ASC, b.title ASC",
                (aid,)
            )).fetchall()
        ]
        # Cross-format sibling info so the UI can render "also
        # available as audiobook" badges on ebook cards and vice
        # versa. Series books get stamped by the series endpoint
        # on its own fetch; here we only need to cover the
        # standalone list.
        from app.works.storage import get_siblings_for_books
        ids = [int(b["id"]) for b in standalone if b.get("id") is not None]
        if slug and ids:
            sib_map = await get_siblings_for_books(slug, ids)
            for b in standalone:
                s = sib_map.get(int(b["id"]))
                if s:
                    b["work_id"] = s[0].work_id
                    b["work_siblings"] = [
                        {"library_slug": w.library_slug, "book_id": w.book_id,
                         "content_type": w.content_type}
                        for w in s
                    ]
        a["standalone_books"] = standalone
        return a
    finally:
        await db.close()


@router.get("/authors/{aid}")
async def get_author(aid: int, include_cross_library: bool = False, slug: Optional[str] = None):
    """Return an author's detail (series + standalone + stats).

    `slug=X` overrides which library the `aid` belongs to. Without it
    we fall back to the active library. This matters when the user
    clicks a merged author row whose `id` came from a non-active
    library — e.g. Troy Denning's id 5 in ABS is Jack Bryce's id 5
    in Calibre, so the frontend MUST pass the source slug or we
    resolve the wrong person.

    `include_cross_library=1` additionally looks up the author in every
    OTHER discovered library by normalized name and returns those
    library's detail under `cross_library` keyed by slug. The frontend
    uses this to render Ebook / Audiobook tabs on the merged authors
    detail view. Single-library installs or unmatched names return
    an empty `cross_library` dict — callers should treat its presence
    as the signal to show tabs, not absence.
    """
    primary_slug = slug or get_active_library()
    a = await _author_detail_for_slug(primary_slug, aid)
    if a is None:
        raise HTTPException(404)

    if include_cross_library:
        cross: dict[str, Any] = {}
        # v2.20.0 — prefer author_links lookup over the lossy
        # normalized_name match. author_links is the migration-
        # populated identity graph; a single hop resolves every linked
        # per-library row regardless of whether the names normalize
        # identically (C.W. Lamb vs Charles W. Lamb, etc). Fall back to
        # normalized_name matching when the author isn't (yet) linked
        # — covers freshly-synced rows that the next migration sweep
        # will pick up.
        person_id = await person_id_for(primary_slug, aid)
        matched_slugs: set[str] = set()
        if person_id is not None:
            for slug, other_aid in await linked_authors(person_id):
                if slug == primary_slug:
                    continue
                detail = await _author_detail_for_slug(slug, other_aid)
                if detail is None:
                    continue
                lib_meta = next(
                    (l for l in state._discovered_libraries
                     if l.get("slug") == slug),
                    {},
                )
                cross[slug] = {
                    "library_name": lib_meta.get("display_name")
                        or lib_meta.get("name") or slug,
                    "content_type": lib_meta.get("content_type", "ebook"),
                    "app_type": lib_meta.get("app_type", ""),
                    "author": detail,
                }
                matched_slugs.add(slug)

        # Defensive fallback: walk libraries the identity graph didn't
        # cover via normalized_name. Same lossy match the pre-v2.20
        # path used; only kicks in for the edge case described above.
        from app.works.normalize import normalize_author
        target_norm = normalize_author(a["name"])
        if target_norm:
            for lib in state._discovered_libraries:
                if lib["slug"] == primary_slug or lib["slug"] in matched_slugs:
                    continue
                other_db = await get_db(lib["slug"])
                try:
                    rows = await (await other_db.execute(
                        "SELECT id, name FROM authors"
                    )).fetchall()
                finally:
                    await other_db.close()
                match_id = None
                for row in rows:
                    if normalize_author(row["name"]) == target_norm:
                        match_id = row["id"]
                        break
                if match_id is None:
                    continue
                detail = await _author_detail_for_slug(lib["slug"], match_id)
                if detail is None:
                    continue
                cross[lib["slug"]] = {
                    "library_name": lib.get("display_name") or lib.get("name") or lib["slug"],
                    "content_type": lib.get("content_type", "ebook"),
                    "app_type": lib.get("app_type", ""),
                    "author": detail,
                }
        a["cross_library"] = cross
        a["active_library_slug"] = primary_slug
        a["active_content_type"] = next(
            (l.get("content_type", "ebook") for l in state._discovered_libraries
             if l["slug"] == primary_slug),
            "ebook",
        )

        # v2.17.0 Bug B — `global_stats` sums owned / missing / series
        # across the primary library AND every cross_library entry,
        # so the author-detail page header can render unified counts
        # (rather than per-library counts that mislead the user when
        # an audiobook-only author has Calibre-side ebook discoveries
        # waiting). Frontend reads `a.global_stats` for the top-of-page
        # tile; per-tab views keep showing their per-library numbers.
        primary_stats = _stats_for_author_block(a)
        global_owned = primary_stats["owned"]
        global_total = primary_stats["total"]
        all_series_names: set[str] = set(primary_stats["series_names"])
        for slug_key, payload in cross.items():
            other = payload.get("author") or {}
            s = _stats_for_author_block(other)
            global_owned += s["owned"]
            global_total += s["total"]
            all_series_names |= s["series_names"]
        a["global_stats"] = {
            "owned": global_owned,
            "missing": max(0, global_total - global_owned),
            "total": global_total,
            "series_count": len(all_series_names),
        }

    # v2.20.0 — additively expose the canonical person_id. Frontend can
    # use it to switch over to /discovery/persons/{person_id} for a
    # cleaner cross-library view without bespoke routing logic. None
    # when the author row isn't (yet) linked to a person — e.g. a row
    # inserted between init_db() and the next migration sweep.
    a["person_id"] = await person_id_for(primary_slug, aid)
    return a


def _stats_for_author_block(block: dict[str, Any]) -> dict[str, Any]:
    """Sum owned/total and collect unique series names for an
    `_author_detail_for_slug`-shaped block. Shared between the legacy
    `cross_library` aggregation and the new `/persons/{person_id}`
    endpoint so both produce identical totals."""
    sa = block.get("standalone_books") or []
    ser = block.get("series") or []
    sa_owned = sum(1 for b in sa if (b.get("owned") or 0) == 1)
    sa_total = len(sa)
    ser_owned = sum(int(s.get("owned_count") or 0) for s in ser)
    ser_total = sum(
        int(s.get("author_book_count") or s.get("book_count") or 0)
        for s in ser
    )
    return {
        "owned": sa_owned + ser_owned,
        "total": sa_total + ser_total,
        "series_names": {
            str(s.get("name") or "") for s in ser if s.get("name")
        },
    }


# NOTE: this static-prefix route is defined BEFORE the parameterized
# `/persons/{person_id}` route below — FastAPI routes are matched in
# registration order, and the int-typed `{person_id}` path otherwise
# 422s on a request like `/persons/search` (string can't convert).
@router.get("/persons/search")
async def search_persons(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(20, ge=1, le=100),
):
    """v2.20.0 Phase 4 — cross-library person search.

    Returns one result per `persons` row, with the libraries the
    person appears in collated from `author_links`. The query is
    normalized through the Phase 1 canonical normalizer so typing
    `D. L. Bacon` matches `D.L. Bacon`'s person row.

    Match strategy (in priority order):
      1. `persons.normalized_name LIKE %normalized_q%` — punctuation-
         tolerant match (the primary path for cross-library dedup).
      2. `persons.canonical_name LIKE %q%` COLLATE NOCASE — defensive
         fallback for cases where the user typed a literal string the
         normalizer would mangle.
      3. `persons.display_name_override LIKE %q%` COLLATE NOCASE —
         catches manual overrides that diverge from the canonical.

    Results sorted by exact-prefix-match first, then alphabetical.
    """
    from app.metadata.author_names import normalize_author_name

    normalized = normalize_author_name(q)
    needle_norm = f"%{normalized}%" if normalized else ""
    needle_raw = f"%{q.strip()}%"
    prefix_raw = f"{q.strip()}%"

    gdb = await get_global_db()
    try:
        # Single SQL covers the three match strategies via OR. The
        # ranking ORDER BY puts exact-prefix matches first.
        params: list = [needle_raw, needle_raw]
        clauses: list[str] = [
            "canonical_name LIKE ? COLLATE NOCASE",
            "display_name_override LIKE ? COLLATE NOCASE",
        ]
        if needle_norm:
            params.append(needle_norm)
            clauses.append("normalized_name LIKE ?")
        sql = (
            "SELECT id, canonical_name, normalized_name, "
            "       display_name_override "
            f"FROM persons WHERE ({' OR '.join(clauses)}) "  # nosec B608
            "ORDER BY "
            "  CASE WHEN canonical_name LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END, "
            "  canonical_name COLLATE NOCASE "
            "LIMIT ?"
        )
        params.append(prefix_raw)
        params.append(limit)
        person_rows = await (await gdb.execute(sql, params)).fetchall()

        # Collate author_links per person so the response can show
        # library_slugs / content_types badges per result.
        results = []
        for p_row in person_rows:
            link_rows = await (await gdb.execute(
                "SELECT library_slug, author_id "
                "FROM author_links WHERE person_id = ?",
                (p_row["id"],),
            )).fetchall()
            library_slugs = [r["library_slug"] for r in link_rows]
            author_ids_by_slug = {
                r["library_slug"]: r["author_id"] for r in link_rows
            }
            content_types = []
            for slug in library_slugs:
                lib = next(
                    (l for l in state._discovered_libraries
                     if l.get("slug") == slug),
                    None,
                )
                if lib:
                    ct = lib.get("content_type", "ebook")
                    if ct not in content_types:
                        content_types.append(ct)
            results.append({
                "person_id": p_row["id"],
                "canonical_name": p_row["canonical_name"],
                "display_name": (
                    p_row["display_name_override"]
                    or p_row["canonical_name"]
                ),
                "normalized_name": p_row["normalized_name"],
                "library_slugs": library_slugs,
                "author_ids_by_slug": author_ids_by_slug,
                "content_types": content_types,
            })
        return {"q": q, "persons": results}
    finally:
        await gdb.close()


@router.get("/persons/triage")
async def get_triage_state():
    """v2.20.0 Phase 5 — surface link issues for manual triage.

    Returns three buckets:
      - `low_confidence`: persons whose author_links the migration
        flagged as risky (multiple linked rows from different
        libraries with no shared source IDs — classic "John Smith"
        collision pattern).
      - `unlinked_authors`: per-library author rows with NO row in
        `author_links`. These appear when a sync hook missed the
        `get_or_create_person` call (defensive — shouldn't happen
        post-Phase-1 + Phase-2 wiring) or when the migration ran
        before the row existed.
      - `normalized_collisions`: persons that share a normalized_name
        with at least one other person. The migration's UNIQUE
        constraint prevents this for new persons, but legacy rows
        from before the unique index landed could collide.

    The Database Manager UI displays these as triage targets.
    """
    gdb = await get_global_db()
    try:
        # Low-confidence persons.
        low_rows = await (await gdb.execute(
            "SELECT DISTINCT al.person_id, p.canonical_name, "
            "       p.display_name_override, p.normalized_name "
            "FROM author_links al "
            "JOIN persons p ON p.id = al.person_id "
            "WHERE al.link_confidence = 'low' "
            "ORDER BY p.canonical_name COLLATE NOCASE"
        )).fetchall()
        # Two-pass to resolve per-library author names without N+1:
        # collect (slug, author_id) tuples across every low-confidence
        # person, then bulk-fetch names by slug. Mark wants the actual
        # per-library names visible on the Triage page so he can
        # eyeball mismatches like "Marc J Gregson" vs "Marc J. Gregson"
        # at a glance.
        low_confidence_link_rows: dict[int, list] = {}
        wanted_by_slug: dict[str, set[int]] = {}
        for r in low_rows:
            link_rows = await (await gdb.execute(
                "SELECT library_slug, author_id, link_confidence "
                "FROM author_links WHERE person_id = ?",
                (r["person_id"],),
            )).fetchall()
            low_confidence_link_rows[r["person_id"]] = list(link_rows)
            for lr in link_rows:
                wanted_by_slug.setdefault(lr["library_slug"], set()).add(
                    lr["author_id"]
                )
    finally:
        # Close BEFORE opening per-library connections to avoid
        # holding the global write-lock across nested I/O.
        await gdb.close()

    # Bulk-fetch (slug, author_id) → name for every link surface above.
    from app.discovery.author_identity import _open_per_library
    name_by_pair: dict[tuple[str, int], str] = {}
    for slug, aids in wanted_by_slug.items():
        if not aids:
            continue
        try:
            per_lib = await _open_per_library(slug)
        except Exception:
            continue
        try:
            aid_list = sorted(aids)
            ph = ",".join("?" * len(aid_list))
            cur = await per_lib.execute(
                f"SELECT id, name FROM authors WHERE id IN ({ph})",  # nosec B608
                aid_list,
            )
            for ar in await cur.fetchall():
                name_by_pair[(slug, ar["id"])] = ar["name"]
        except Exception:
            pass
        finally:
            await per_lib.close()

    gdb = await get_global_db()
    try:
        low_confidence = []
        for r in low_rows:
            link_rows = low_confidence_link_rows.get(r["person_id"], [])
            low_confidence.append({
                "person_id": r["person_id"],
                "canonical_name": r["canonical_name"],
                "display_name": (
                    r["display_name_override"] or r["canonical_name"]
                ),
                "normalized_name": r["normalized_name"],
                "links": [
                    {
                        "library_slug": lr["library_slug"],
                        "author_id": lr["author_id"],
                        "link_confidence": lr["link_confidence"],
                        "author_name": name_by_pair.get(
                            (lr["library_slug"], lr["author_id"])
                        ),
                    }
                    for lr in link_rows
                ],
            })

        # Normalized-name collisions (persons sharing a normalized_name).
        coll_rows = await (await gdb.execute(
            "SELECT normalized_name, COUNT(*) AS n FROM persons "
            "GROUP BY normalized_name HAVING n > 1"
        )).fetchall()
        normalized_collisions = []
        for r in coll_rows:
            persons = await (await gdb.execute(
                "SELECT id, canonical_name, display_name_override "
                "FROM persons WHERE normalized_name = ?",
                (r["normalized_name"],),
            )).fetchall()
            normalized_collisions.append({
                "normalized_name": r["normalized_name"],
                "persons": [
                    {
                        "person_id": p["id"],
                        "canonical_name": p["canonical_name"],
                        "display_name": (
                            p["display_name_override"] or p["canonical_name"]
                        ),
                    }
                    for p in persons
                ],
            })
    finally:
        await gdb.close()

    # Unlinked per-library authors — walk every library's authors
    # table, collect rows whose (library_slug, author_id) isn't in
    # author_links. Expensive but bounded by the library size.
    unlinked_authors: list[dict] = []
    from app.discovery.author_identity import _open_per_library
    gdb = await get_global_db()
    try:
        link_set: set[tuple[str, int]] = set()
        cur = await gdb.execute("SELECT library_slug, author_id FROM author_links")
        for r in await cur.fetchall():
            link_set.add((r["library_slug"], r["author_id"]))
    finally:
        await gdb.close()

    for lib in state._discovered_libraries:
        slug = lib.get("slug")
        if not slug:
            continue
        try:
            ldb = await _open_per_library(slug)
        except Exception:
            continue
        try:
            has_authors = await (await ldb.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='authors' LIMIT 1"
            )).fetchone()
            if not has_authors:
                continue
            rows = await (await ldb.execute(
                "SELECT id, name, normalized_name FROM authors"
            )).fetchall()
        finally:
            await ldb.close()
        for ar in rows:
            if (slug, ar["id"]) in link_set:
                continue
            unlinked_authors.append({
                "library_slug": slug,
                "author_id": ar["id"],
                "name": ar["name"],
                "normalized_name": ar["normalized_name"],
            })

    return {
        "low_confidence": low_confidence,
        "unlinked_authors": unlinked_authors,
        "normalized_collisions": normalized_collisions,
    }


@router.post("/persons/{person_id}/unlink-author")
async def unlink_author_from_person(person_id: int, data: dict = Body(...)):
    """v2.20.0 Phase 5 — detach one (library_slug, author_id) from a
    person. Creates a new persons row for the orphaned author so its
    identity is preserved.

    Body: `{library_slug, author_id}`. The pair must currently be
    linked to `person_id` or we 400.
    """
    library_slug = data.get("library_slug")
    author_id = data.get("author_id")
    if not library_slug or author_id is None:
        raise HTTPException(400, "library_slug and author_id are required")

    if await get_person(person_id) is None:
        raise HTTPException(404, f"Person {person_id} not found")

    from app.discovery.author_identity import _open_per_library
    from app.metadata.author_names import normalize_author_name

    gdb = await get_global_db()
    try:
        existing = await (await gdb.execute(
            "SELECT id FROM author_links "
            "WHERE person_id = ? AND library_slug = ? AND author_id = ?",
            (person_id, library_slug, author_id),
        )).fetchone()
        if not existing:
            raise HTTPException(
                400,
                f"Author {library_slug}/{author_id} is not linked to "
                f"person {person_id}",
            )
        # Read author's display name from the per-library DB to seed
        # the new persons row.
        per_lib = await _open_per_library(library_slug)
        try:
            arow = await (await per_lib.execute(
                "SELECT name, bio, image_url FROM authors WHERE id = ?",
                (author_id,),
            )).fetchone()
        finally:
            await per_lib.close()
        if not arow:
            raise HTTPException(
                404,
                f"Author {library_slug}/{author_id} not found in per-library DB",
            )
        name = arow["name"]
        normalized = normalize_author_name(name) or (
            f"__split_{library_slug}_{author_id}"
        )
        # If the normalizer collides with the existing person's
        # normalized_name, attach a slug+id sentinel so we don't break
        # the UNIQUE(normalized_name) constraint on persons.
        existing_with_norm = await (await gdb.execute(
            "SELECT id FROM persons WHERE normalized_name = ?",
            (normalized,),
        )).fetchone()
        if existing_with_norm:
            normalized = f"{normalized}__split_{library_slug}_{author_id}"
        cur = await gdb.execute(
            "INSERT INTO persons "
            "(canonical_name, normalized_name, bio, image_url) "
            "VALUES (?, ?, ?, ?)",
            (name, normalized, arow["bio"], arow["image_url"]),
        )
        new_person_id = cur.lastrowid
        # Move the link.
        await gdb.execute(
            "UPDATE author_links SET person_id = ?, link_source = 'manual' "
            "WHERE id = ?",
            (new_person_id, existing["id"]),
        )
        # If the old person now has no links, drop it.
        remaining = await (await gdb.execute(
            "SELECT COUNT(*) AS n FROM author_links WHERE person_id = ?",
            (person_id,),
        )).fetchone()
        old_person_dropped = remaining["n"] == 0
        if old_person_dropped:
            await gdb.execute("DELETE FROM persons WHERE id = ?", (person_id,))
        await gdb.commit()
        return {
            "status": "ok",
            "new_person_id": new_person_id,
            "old_person_dropped": old_person_dropped,
        }
    finally:
        await gdb.close()


@router.post("/persons/{person_id}/link-author")
async def link_author_to_person(person_id: int, data: dict = Body(...)):
    """v2.20.0 Phase 5 — manually attach a (library_slug, author_id)
    to a person. If the pair is currently linked to a DIFFERENT
    person, that link is moved (and the source person is dropped if
    it becomes orphan).

    Body: `{library_slug, author_id}`.
    """
    library_slug = data.get("library_slug")
    author_id = data.get("author_id")
    if not library_slug or author_id is None:
        raise HTTPException(400, "library_slug and author_id are required")

    if await get_person(person_id) is None:
        raise HTTPException(404, f"Person {person_id} not found")

    # Verify the per-library row exists.
    from app.discovery.author_identity import _open_per_library
    per_lib = await _open_per_library(library_slug)
    try:
        arow = await (await per_lib.execute(
            "SELECT id FROM authors WHERE id = ?", (author_id,),
        )).fetchone()
    finally:
        await per_lib.close()
    if not arow:
        raise HTTPException(
            404,
            f"Author {library_slug}/{author_id} not found in per-library DB",
        )

    gdb = await get_global_db()
    try:
        existing = await (await gdb.execute(
            "SELECT id, person_id FROM author_links "
            "WHERE library_slug = ? AND author_id = ?",
            (library_slug, author_id),
        )).fetchone()
        old_person_dropped = False
        if existing:
            if existing["person_id"] == person_id:
                return {"status": "already_linked"}
            old_person_id = existing["person_id"]
            await gdb.execute(
                "UPDATE author_links SET person_id = ?, link_source = 'manual' "
                "WHERE id = ?",
                (person_id, existing["id"]),
            )
            # Drop old person if orphaned.
            remaining = await (await gdb.execute(
                "SELECT COUNT(*) AS n FROM author_links "
                "WHERE person_id = ?",
                (old_person_id,),
            )).fetchone()
            if remaining["n"] == 0:
                await gdb.execute(
                    "DELETE FROM persons WHERE id = ?", (old_person_id,),
                )
                old_person_dropped = True
        else:
            await gdb.execute(
                "INSERT INTO author_links "
                "(person_id, library_slug, author_id, link_source) "
                "VALUES (?, ?, ?, 'manual')",
                (person_id, library_slug, author_id),
            )
        await gdb.commit()
        return {"status": "ok", "old_person_dropped": old_person_dropped}
    finally:
        await gdb.close()


@router.post("/persons/{person_id}/approve-links")
async def approve_person_links(person_id: int):
    """v2.20.0 Phase 5 — confirm that a flagged-low-confidence person's
    links really do all belong to the same human.

    Flips every `author_links` row for this person to
    `link_confidence='high'` + `link_source='manual'`. The `manual`
    source tag is what `_flag_low_confidence_links` checks to skip
    re-flagging on subsequent `recompute-consolidation` runs, so the
    approval survives — no need to re-approve after every recompute.

    Returns the count of links flipped. 404 if the person doesn't exist.
    """
    if await get_person(person_id) is None:
        raise HTTPException(404, f"Person {person_id} not found")
    gdb = await get_global_db()
    try:
        cur = await gdb.execute(
            "UPDATE author_links "
            "SET link_confidence = 'high', link_source = 'manual' "
            "WHERE person_id = ?",
            (person_id,),
        )
        await gdb.commit()
        approved = cur.rowcount or 0
        logger.info(
            f"Approved {approved} link(s) for person {person_id} "
            f"(now exempt from low-confidence re-flagging)"
        )
        return {"status": "ok", "approved": approved}
    finally:
        await gdb.close()


@router.post("/persons/recompute-consolidation")
async def recompute_consolidation():
    """v2.20.0 Phase 5 — re-run the Phase 1 consolidation pass
    (tiebreak canonical_name/bio/image_url) and low-confidence flag
    pass on every multi-linked person. Useful after Mark fixes a
    batch of normalized_name mismatches via manual link/unlink.
    """
    from app.discovery.author_identity import (
        _consolidate_persons,
        _flag_low_confidence_links,
    )
    library_slugs = [
        l["slug"] for l in state._discovered_libraries if l.get("slug")
    ]
    gdb = await get_global_db()
    try:
        await _consolidate_persons(gdb, library_slugs)
        # Reset link_confidence to 'high' before re-flagging so links
        # the user manually fixed get a fresh assessment.
        await gdb.execute(
            "UPDATE author_links SET link_confidence = 'high'"
        )
        await gdb.commit()
        flagged = await _flag_low_confidence_links(gdb, library_slugs)
        return {"status": "ok", "flagged": flagged}
    finally:
        await gdb.close()


@router.get("/persons/source-ids")
async def list_persons_source_ids():
    """v2.22.0 — Per-person source ID overview for the Persons Manager
    page. Returns the multi-link persons in the identity graph with
    each `MIRRORABLE_SOURCE_ID_COLUMN`'s union value across linked
    siblings, plus a `divergent` set listing columns where siblings
    disagree (the JFW-style conflict case — calibre-vs-abs IDs that
    differ, surfaced for manual reconciliation even though Job 8 of
    Hygiene auto-resolves these).

    Single-link persons are excluded; nothing to manage across
    libraries for them.

    Route ORDER: declared before `GET /persons/{person_id}` because
    FastAPI walks routes in registration order and the parameterized
    variant would otherwise greedy-match `source-ids` as a person_id
    string and fail validation (v2.22.4 fix).
    """
    from app.discovery.author_identity import _open_per_library
    gdb = await get_global_db()
    try:
        cur = await gdb.execute(
            "SELECT al.person_id, p.canonical_name, p.display_name_override, "
            "       p.normalized_name, al.library_slug, al.author_id "
            "FROM author_links al "
            "JOIN persons p ON p.id = al.person_id "
            "WHERE al.person_id IN ("
            "  SELECT person_id FROM author_links "
            "  GROUP BY person_id HAVING COUNT(*) > 1"
            ") "
            "ORDER BY p.canonical_name COLLATE NOCASE, al.library_slug"
        )
        rows = await cur.fetchall()
    finally:
        await gdb.close()

    # Group links by person_id.
    by_person: dict[int, dict] = {}
    wanted_by_slug: dict[str, set[int]] = {}
    for r in rows:
        pid = r["person_id"]
        if pid not in by_person:
            by_person[pid] = {
                "person_id": pid,
                "canonical_name": r["canonical_name"],
                "display_name": (
                    r["display_name_override"] or r["canonical_name"]
                ),
                "normalized_name": r["normalized_name"],
                "links": [],
            }
        by_person[pid]["links"].append({
            "library_slug": r["library_slug"],
            "author_id": r["author_id"],
        })
        wanted_by_slug.setdefault(r["library_slug"], set()).add(r["author_id"])

    # Bulk-fetch the mirrorable source IDs per (slug, author_id).
    sortable_cols = sorted(MIRRORABLE_SOURCE_ID_COLUMNS)
    cols_str = ", ".join(sortable_cols)
    ids_by_pair: dict[tuple[str, int], dict[str, Optional[str]]] = {}
    for slug, aids in wanted_by_slug.items():
        if not aids:
            continue
        try:
            per_lib = await _open_per_library(slug)
        except Exception:
            continue
        try:
            aid_list = sorted(aids)
            ph = ",".join("?" * len(aid_list))
            cur = await per_lib.execute(
                f"SELECT id, name, {cols_str} FROM authors "  # nosec B608
                f"WHERE id IN ({ph})",
                aid_list,
            )
            for ar in await cur.fetchall():
                ids_by_pair[(slug, ar["id"])] = {
                    "name": ar["name"],
                    **{c: ar[c] for c in sortable_cols},
                }
        finally:
            await per_lib.close()

    # Materialize per-person union + divergence.
    out: list[dict] = []
    for pid, p in by_person.items():
        union: dict[str, Optional[str]] = {c: None for c in sortable_cols}
        per_slug_values: dict[str, dict[str, Optional[str]]] = {}
        for link in p["links"]:
            key = (link["library_slug"], link["author_id"])
            cols = ids_by_pair.get(key)
            if not cols:
                continue
            link["author_name"] = cols.get("name")
            per_slug_values[link["library_slug"]] = cols
            for c in sortable_cols:
                v = cols.get(c)
                if v and not union[c]:
                    union[c] = v
        divergent: list[str] = []
        for c in sortable_cols:
            seen: set[str] = set()
            for cols in per_slug_values.values():
                v = cols.get(c)
                if v:
                    seen.add(v)
            if len(seen) > 1:
                divergent.append(c)
        out.append({
            **p,
            "source_ids": union,
            "divergent": divergent,
        })
    return {"persons": out}


@router.get("/persons/{person_id}")
async def get_person_detail(person_id: int):
    """Unified cross-library author detail (v2.20.0 Phase 2).

    Returns one merged view of an author across every library they
    appear in. The shape mirrors the existing per-library
    `/authors/{aid}` block, but instead of one author row + a
    `cross_library` dict keyed by slug, we return:

      - canonical identity from the `persons` row
      - `source_ids`: union of per-library `authors.{source}_id`
         columns (post-Phase-1 mirror, these are identical across
         linked rows; the union is defensive)
      - `libraries`: one entry per `author_links` row, each carrying
         the per-library author detail block (series, standalone_books)
      - `pen_names`: from `pen_name_links_v2`, both directions
      - `global_stats`: owned/missing/total summed across libraries,
         distinct series_names counted once
      - `low_confidence`: any linked row flagged at migration time

    Frontend `/author/{person_id}` consumes this directly; no per-
    library slug routing needed.
    """
    p = await get_person(person_id)
    if p is None:
        raise HTTPException(404, f"Person {person_id} not found")

    links = await linked_authors(person_id)
    if not links:
        # Shouldn't happen post-migration (every person has ≥ 1 link),
        # but defensive — return the canonical identity with an empty
        # libraries list rather than 500-ing the page.
        return {
            "person_id": p.id,
            "canonical_name": p.canonical_name,
            "display_name": p.display_name,
            "normalized_name": p.normalized_name,
            "display_name_override": p.display_name_override,
            "bio": p.bio,
            "image_url": p.image_url,
            "source_ids": {},
            "libraries": [],
            "pen_names": [],
            "global_stats": {"owned": 0, "missing": 0, "total": 0, "series_count": 0},
            "low_confidence": False,
        }

    # Build per-library blocks. Each entry reuses
    # `_author_detail_for_slug` so the standalone_books cover-src logic,
    # series-aggregate stats, and work_siblings are identical to the
    # per-library detail page.
    libraries_out: list[dict[str, Any]] = []
    union_source_ids: dict[str, Optional[str]] = {}
    global_owned = 0
    global_total = 0
    all_series_names: set[str] = set()

    for slug, aid in links:
        detail = await _author_detail_for_slug(slug, aid)
        if detail is None:
            # Orphan link — the per-library author row was deleted. The
            # `prune_orphan_links` sweep is responsible for cleaning
            # these up; we just skip them here so the endpoint stays
            # functional in the meantime.
            continue
        lib_meta = next(
            (l for l in state._discovered_libraries if l.get("slug") == slug),
            {},
        )
        libraries_out.append({
            "library_slug": slug,
            "library_name": lib_meta.get("display_name")
                or lib_meta.get("name") or slug,
            "content_type": lib_meta.get("content_type", "ebook"),
            "app_type": lib_meta.get("app_type", ""),
            "author_id": aid,
            "author": detail,
        })
        # Union source IDs — first non-empty wins (Phase 1 mirror
        # ensures they agree, but a row inserted between mirror passes
        # may still be lagging). Limited to MIRRORABLE_SOURCE_ID_COLUMNS
        # so the response only surfaces external-web sources — the
        # library-local `audiobookshelf_id` / `calibre_id` are sync
        # identifiers, not user-facing source IDs, and Phase 3's badge
        # UI only renders the web sources.
        for col in MIRRORABLE_SOURCE_ID_COLUMNS:
            v = detail.get(col)
            if v and union_source_ids.get(col[:-3]) is None:
                # Strip the "_id" suffix so the JSON key is
                # "amazon" not "amazon_id".
                union_source_ids[col[:-3]] = str(v)
        # Stats.
        s = _stats_for_author_block(detail)
        global_owned += s["owned"]
        global_total += s["total"]
        all_series_names |= s["series_names"]

    # Pen names from `pen_name_links_v2`. We return BOTH directions so
    # the UI can render "Also known as ..." (this person is canonical
    # of) and "Pen name for ..." (this person is an alias of) without
    # client-side reshape.
    gdb = await get_global_db()
    try:
        pn_rows = await (await gdb.execute(
            "SELECT pl.id, pl.link_type, "
            "       pl.canonical_person_id, pl.alias_person_id, "
            "       cp.canonical_name AS canonical_name, "
            "       cp.display_name_override AS canonical_override, "
            "       ap.canonical_name AS alias_name, "
            "       ap.display_name_override AS alias_override "
            "FROM pen_name_links_v2 pl "
            "JOIN persons cp ON pl.canonical_person_id = cp.id "
            "JOIN persons ap ON pl.alias_person_id = ap.id "
            "WHERE pl.canonical_person_id = ? OR pl.alias_person_id = ?",
            (person_id, person_id),
        )).fetchall()
        pen_names = []
        for r in pn_rows:
            if r["canonical_person_id"] == person_id:
                # This person is canonical; the other end is alias.
                pen_names.append({
                    "link_id": r["id"],
                    "person_id": r["alias_person_id"],
                    "canonical_name": r["alias_name"],
                    "display_name": r["alias_override"] or r["alias_name"],
                    "link_type": r["link_type"],
                    "direction": "alias_of_this",
                })
            else:
                pen_names.append({
                    "link_id": r["id"],
                    "person_id": r["canonical_person_id"],
                    "canonical_name": r["canonical_name"],
                    "display_name": r["canonical_override"] or r["canonical_name"],
                    "link_type": r["link_type"],
                    "direction": "this_is_alias_of",
                })

        low_conf_row = await (await gdb.execute(
            "SELECT 1 FROM author_links "
            "WHERE person_id = ? AND link_confidence = 'low' LIMIT 1",
            (person_id,),
        )).fetchone()
        low_confidence = low_conf_row is not None
    finally:
        await gdb.close()

    return {
        "person_id": p.id,
        "canonical_name": p.canonical_name,
        "display_name": p.display_name,
        "normalized_name": p.normalized_name,
        "display_name_override": p.display_name_override,
        "bio": p.bio,
        "image_url": p.image_url,
        "source_ids": union_source_ids,
        "libraries": libraries_out,
        "pen_names": pen_names,
        "global_stats": {
            "owned": global_owned,
            "missing": max(0, global_total - global_owned),
            "total": global_total,
            "series_count": len(all_series_names),
        },
        "low_confidence": low_confidence,
    }


# ─── Phase 3 — Source-ID badge management ─────────────────


def _normalize_source_key(source: str) -> str:
    """Strip the `_id` suffix the badge UI may include. The API
    contract is `source_name='amazon'`, but `'amazon_id'` is the
    column name and a reasonable thing for a client to send. Accept
    both."""
    return source[:-3] if source.endswith("_id") else source


@router.get("/persons/{person_id}/source-id/preview")
async def preview_source_id(
    person_id: int,
    source: str = Query(...),
    value: str = Query(""),
):
    """Parse a pasted ID or URL without committing — the badge edit
    modal calls this on every keystroke so the user sees a live
    "parsed as X → URL Y" hint before saving. Empty value previews
    as `parsed=null, url=null` (clearing the ID)."""
    source = _normalize_source_key(source)
    if source not in known_sources():
        raise HTTPException(400, f"unknown source: {source!r}")
    p = await get_person(person_id)
    if p is None:
        raise HTTPException(404, f"Person {person_id} not found")
    try:
        parsed = parse_source_id(source, value)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    url = canonical_author_url(source, parsed) if parsed else None
    return {
        "source": source,
        "parsed": parsed,
        "url": url,
    }


@router.patch("/persons/{person_id}/source-id")
async def patch_source_id(person_id: int, data: dict = Body(...)):
    """Commit a source-ID edit from the badge UI.

    Body: `{"source": "amazon", "value": "<id or URL>"}`. Empty value
    clears the ID (writes NULL via `mirror_source_id`). Parses the
    value canonically, writes through to every linked per-library
    `authors.{source}_id` column, logs an audit row.

    Returns the canonical ID + URL preview + count of mirrored rows.
    """
    source = _normalize_source_key(data.get("source", ""))
    value = data.get("value", "")
    if not source:
        raise HTTPException(400, "source is required")
    if source not in known_sources():
        raise HTTPException(400, f"unknown source: {source!r}")
    column = f"{source}_id"
    if column not in MIRRORABLE_SOURCE_ID_COLUMNS:
        raise HTTPException(
            400,
            f"{source!r} is not editable via the badge UI "
            f"(library-local sync identifier)",
        )
    p = await get_person(person_id)
    if p is None:
        raise HTTPException(404, f"Person {person_id} not found")

    # Empty or whitespace-only value → clear.
    canonical: Optional[str]
    if value is None or not str(value).strip():
        canonical = None
    else:
        try:
            canonical = parse_source_id(source, str(value))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if canonical is None:
            raise HTTPException(
                400,
                f"unrecognized {source} value: paste an ID or "
                f"a {source} URL",
            )

    # Read old value (union from any linked library row) for audit.
    links = await linked_authors(person_id)
    old_value: Optional[str] = None
    for slug, aid in links:
        from app.discovery.author_identity import _open_per_library
        per_lib = await _open_per_library(slug)
        try:
            row = await (await per_lib.execute(
                f"SELECT {column} FROM authors WHERE id = ?",  # nosec B608
                (aid,),
            )).fetchone()
            if row and row[0]:
                old_value = str(row[0])
                break
        finally:
            await per_lib.close()

    touched = 0
    if links:
        # v2.20.1 — mirror_source_id is now exclusive (skips the
        # caller's slug to avoid self-deadlock against an open write
        # transaction in the lookup path). The PATCH endpoint has no
        # such transaction, so write the entry-point row first, THEN
        # mirror to the rest. `touched + 1` accounts for the manual
        # write below.
        from app.discovery.author_identity import _open_per_library
        first_slug, first_aid = links[0]
        per_lib = await _open_per_library(first_slug)
        try:
            await per_lib.execute(
                f"UPDATE authors SET {column} = ? WHERE id = ?",  # nosec B608
                (canonical, first_aid),
            )
            await per_lib.commit()
        finally:
            await per_lib.close()
        touched = 1 + await mirror_source_id(
            first_slug, first_aid, column, canonical,
        )

    # Audit log row.
    gdb = await get_global_db()
    try:
        await gdb.execute(
            "INSERT INTO author_id_audit_log "
            "(person_id, source_name, old_value, new_value) "
            "VALUES (?, ?, ?, ?)",
            (person_id, source, old_value, canonical),
        )
        await gdb.commit()
    finally:
        await gdb.close()

    return {
        "person_id": person_id,
        "source": source,
        "parsed": canonical,
        "url": canonical_author_url(source, canonical) if canonical else None,
        "old_value": old_value,
        "mirrored_rows": touched,
    }


@router.get("/persons/{person_id}/source-id/history")
async def get_source_id_history(person_id: int, limit: int = Query(50)):
    """Return the audit log for source-ID edits on this person, newest
    first. Limit defaults to 50 — Phase 5 triage UI may use this to
    show "history of fixes" alongside the badge row."""
    p = await get_person(person_id)
    if p is None:
        raise HTTPException(404, f"Person {person_id} not found")
    gdb = await get_global_db()
    try:
        rows = await (await gdb.execute(
            "SELECT id, source_name, old_value, new_value, changed_at "
            "FROM author_id_audit_log "
            "WHERE person_id = ? "
            "ORDER BY changed_at DESC, id DESC "
            "LIMIT ?",
            (person_id, max(1, min(limit, 500))),
        )).fetchall()
        return {"history": [dict(r) for r in rows]}
    finally:
        await gdb.close()


def _spawn_lookup_task(scan_type: str, total: int, runner) -> None:
    """Spawn `runner` as a background asyncio task tracked by state._lookup_task.

    Single-author and bulk-author scans run as real background tasks
    so the Dashboard's Stop button can cancel them via the standard
    `/lookup/cancel` endpoint — that endpoint only knows about
    `_lookup_task`, so any scan that doesn't register itself there
    silently dodges the user's cancel request.

    Endpoints that call this return immediately with
    `{"status": "started"}`. The frontend polls `/api/scan-status`
    (and listens for the `seshat:scan-started` window event)
    to surface progress and completion.

    `runner` is a zero-arg async callable that returns when the work
    is done. Exceptions inside it are caught and stored in
    `_lookup_progress["status"]` so the unified widget can surface
    them.
    """
    if state._lookup_progress.get("running"):
        raise HTTPException(409, "An author scan is already running")
    if state._lookup_task and not state._lookup_task.done():
        raise HTTPException(409, "An author scan is already running")

    state._lookup_progress = {
        "running": True, "checked": 0, "total": total, "current_author": "",
        "current_book": "",
        "new_books": 0, "status": "scanning", "type": scan_type,
    }

    async def _do():
        try:
            await runner()
            state._lookup_progress.update({"running": False, "status": "complete"})
            try:
                from app.discovery.notify import notify_scan_complete
                # Pick a friendly label per scan_type. For single-author
                # scans, the runner already wrote `current_author` into
                # state — use it so the notification reads
                # "Scan complete: William D. Arand" rather than
                # "Author Scan complete".
                if scan_type in (
                    "single_author", "single_author_full",
                    "single_author_cross", "single_author_full_cross",
                ):
                    label = state._lookup_progress.get("current_author") or "Author"
                    authors_total = 1
                else:
                    label = {
                        "bulk_authors": "Bulk Author Scan",
                        "bulk_books":   "Bulk Book Scan",
                    }.get(scan_type, "Author Scan")
                    authors_total = int(state._lookup_progress.get("total", 0) or 1)
                await notify_scan_complete(
                    label=label,
                    new_books=int(state._lookup_progress.get("new_books", 0)),
                    authors_total=authors_total,
                )
            except Exception:
                logger.debug("author-scan notify failed", exc_info=True)
            # In-browser toast parallel to the ntfy push. The
            # `trigger_lookup` endpoint in scan.py has its own toast
            # for the Dashboard-originated full scan — this covers the
            # other code path (single-author lookup, full-rescan, bulk
            # scans triggered from the author detail page, etc.) which
            # all route through _spawn_lookup_task.
            try:
                from app.orchestrator.sse_publishers import publish_toast
                new_books = int(state._lookup_progress.get("new_books", 0))
                await publish_toast(
                    "success",
                    f"{label} scan complete: {new_books} new book(s)",
                )
            except Exception:
                logger.debug("author-scan toast failed", exc_info=True)
        except asyncio.CancelledError:
            # User clicked Stop on the Dashboard widget. Mark cancelled
            # and let the exception propagate so any further `await` in
            # the runner unwinds cleanly.
            state._lookup_progress.update({"running": False, "status": "cancelled"})
            raise
        except Exception as e:
            logger.error(f"Author scan task error: {e}", exc_info=True)
            state._lookup_progress.update({"running": False, "status": f"error: {e}"})
            try:
                from app.orchestrator.sse_publishers import publish_toast
                await publish_toast("error", f"Author scan failed: {e}")
            except Exception:
                logger.debug("author-scan error toast failed", exc_info=True)

    state._lookup_task = asyncio.create_task(_do())


async def _trigger_single_author_scan(
    aid: int,
    slug: Optional[str],
    content_type: Optional[str],
    *,
    full_scan: bool,
    scan_type_single: str,
    scan_type_cross: str,
) -> dict:
    """Shared body for `/authors/{aid}/lookup` and
    `/authors/{aid}/full-rescan`.

    Two modes:

    1. **Slug mode (legacy)** — `content_type=None`. Scan the author
       in a single library (the URL's slug, or the currently active
       library). Spawns a `single_author` / `single_author_full`
       lookup task; the runner flips active library for the scan and
       restores on finish.

    2. **Cross-library mode (v2.12.0)** — `content_type="ebook"` or
       `"audiobook"`. Resolve the author's name from the URL row,
       iterate every library of that content_type, and run a scan in
       each matching library. The author may not exist in every
       target library; libraries without a matching name are skipped
       at name-resolution time, not failed.

       The cross-library path is what the new "Scan Ebooks" / "Scan
       Audiobooks" buttons drive — author detail pages always offer
       both buttons regardless of which library the author originated
       from, so an audiobook-library author can be re-discovered in
       every ebook library and vice-versa.
    """
    s = load_settings()
    if not s.get("author_scanning_enabled", True):
        raise HTTPException(400, "Author scanning is disabled — enable it in Settings")
    from app.discovery.database import set_active_library as _set_active
    original_slug = get_active_library()
    target_slug = slug or original_slug
    db = await get_db(target_slug)
    try:
        r = await (await db.execute("SELECT * FROM authors WHERE id=?", (aid,))).fetchone()
        if not r:
            raise HTTPException(404)
    finally:
        await db.close()
    name = dict(r)["name"]

    if content_type:
        # Cross-library mode — iterate every library of the requested
        # type, looking up THIS library's local author row by name.
        target_libs = libraries_for(content_type)
        if not target_libs:
            return {"status": "ok", "total": 0,
                    "author": name,
                    "message": f"No {content_type} libraries found."}
        pre_resolved: list[tuple[dict, list]] = []
        for lib in target_libs:
            tslug = lib.get("slug")
            if not tslug:
                continue
            try:
                ldb = await get_db(tslug)
            except Exception as e:
                logger.warning(f"single-author scan: cannot open lib {tslug}: {e}")
                continue
            try:
                rows = await ldb.execute_fetchall(
                    "SELECT id, name FROM authors WHERE name = ?", (name,),
                )
            finally:
                await ldb.close()
            if rows:
                pre_resolved.append((lib, list(rows)))
        total_tasks = sum(len(rows) for _, rows in pre_resolved)
        if total_tasks == 0:
            return {"status": "ok", "total": 0,
                    "author": name,
                    "message": f"'{name}' has no matching row in any {content_type} library."}

        async def _runner_cross():
            for lib, rows in pre_resolved:
                tslug = lib.get("slug")
                if not tslug:
                    continue
                if tslug != get_active_library():
                    _set_active(tslug)
                for row in rows:
                    laid = row[0]
                    state._lookup_progress.update({"current_author": name})
                    def _on_source(running):
                        state._lookup_progress["new_books"] = int(running)
                    try:
                        new_books = await lookup_author(
                            laid, name, full_scan=full_scan, on_progress=_on_source,
                        )
                        state._lookup_progress.update({
                            "checked": state._lookup_progress.get("checked", 0) + 1,
                            "new_books": int(new_books or 0),
                        })
                    except Exception as e:
                        logger.error(f"single-author cross-lib scan error in {tslug}: {e}")
            if original_slug and original_slug != get_active_library():
                _set_active(original_slug)

        _spawn_lookup_task(scan_type_cross, total=total_tasks, runner=_runner_cross)
        return {"status": "started", "author": name, "total": total_tasks,
                "libraries": len(pre_resolved)}

    # Slug mode (legacy single-library scan).
    flip = bool(slug and slug != original_slug)

    async def _runner_single():
        if flip:
            _set_active(slug)
        try:
            state._lookup_progress.update({"current_author": name})
            def _on_source(running):
                state._lookup_progress["new_books"] = int(running)
            new_books = await lookup_author(
                aid, name, full_scan=full_scan, on_progress=_on_source,
            )
            state._lookup_progress.update({
                "checked": 1, "new_books": int(new_books or 0),
            })
        finally:
            if flip and original_slug:
                _set_active(original_slug)

    _spawn_lookup_task(scan_type_single, total=1, runner=_runner_single)
    return {"status": "started", "author": name, "total": 1}


@router.post("/authors/{aid}/lookup")
async def trigger_author_lookup(
    aid: int,
    slug: Optional[str] = None,
    content_type: Optional[str] = None,
):
    """Run a source scan for one author.

    Two modes:

    - `slug=X` (legacy single-library): temporarily sets the active
      library to X for the duration of the scan, then restores it on
      finish. The URL's `aid` is THIS library's author id, so the flip
      is required or we resolve the wrong person.
    - `content_type=ebook|audiobook` (v2.12.0 cross-library): resolves
      the author by name in every matching library and scans each.
      Powers the new "Scan Ebooks" / "Scan Audiobooks" buttons on the
      Author Detail page.

    `slug` and `content_type` are mutually compatible — `slug` is used
    only to resolve the initial author name from the URL aid. The scan
    itself runs in the slug library (legacy mode) OR in every matching
    library (cross-library mode).
    """
    return await _trigger_single_author_scan(
        aid, slug, content_type,
        full_scan=False,
        scan_type_single="single_author",
        scan_type_cross="single_author_cross",
    )


@router.post("/authors/{aid}/full-rescan")
async def trigger_author_full_rescan(
    aid: int,
    slug: Optional[str] = None,
    content_type: Optional[str] = None,
):
    """Full re-scan for a single author.

    See `/authors/{aid}/lookup` for the two-mode semantics. This
    endpoint passes `full_scan=True` to `lookup_author`, which forces
    DETAIL fetches even for books already URL-resolved (the bulk of
    a full rescan's wall-clock budget).
    """
    return await _trigger_single_author_scan(
        aid, slug, content_type,
        full_scan=True,
        scan_type_single="single_author_full",
        scan_type_cross="single_author_full_cross",
    )


async def _clear_authors_in_library(
    db, author_ids: list[int], clear_source: bool, clear_mam: bool,
) -> int:
    """Run the clear-scan-data SQL against one library's DB.

    Returns the number of books deleted (only set when `clear_source`).
    Caller owns commit + connection lifecycle.
    """
    placeholders = ",".join(["?" for _ in author_ids])
    affected = 0
    if clear_source:
        count_row = await db.execute_fetchall(
            f"SELECT COUNT(*) FROM books WHERE author_id IN ({placeholders}) "
            f"AND owned=0 AND calibre_id IS NULL",
            author_ids,
        )
        affected = count_row[0][0] if count_row else 0
        await db.execute(
            f"DELETE FROM books WHERE author_id IN ({placeholders}) "
            f"AND owned=0 AND calibre_id IS NULL",
            author_ids,
        )
        await db.execute(
            f"UPDATE books SET source_url=NULL WHERE author_id IN "
            f"({placeholders}) AND owned=1",
            author_ids,
        )
        await db.execute(
            f"UPDATE authors SET last_lookup_at=NULL WHERE id IN ({placeholders})",
            author_ids,
        )
    if clear_mam:
        await db.execute(
            f"UPDATE books SET mam_url=NULL, mam_status=NULL, mam_formats=NULL, "
            f"mam_torrent_id=NULL, mam_has_multiple=0, mam_my_snatched=0, "
            f"mam_is_bundle=0 "
            f"WHERE author_id IN ({placeholders})",
            author_ids,
        )
    return affected


@router.post("/authors/clear-scan-data")
async def clear_author_scan_data(data: dict = Body(...)):
    """Clear source and/or MAM scan data for specified authors.

    `content_type` optional: "ebook" or "audiobook" scopes the clear
    to libraries of that type. "all" or omitted clears across every
    discovered library — matches how the cross-library Authors view
    aggregates, so a Clear from that view actually wipes every copy
    of the author. Without this split, a user on the cross-library
    view clicks Clear Source and only the currently-active library
    gets touched, silently leaving the other copy's data behind.

    `author_names`: optional list of author display names. When
    provided AND content_type is set, each library resolves names to
    its OWN local author IDs, sidestepping the cross-library ID
    collision class — see /authors/scan-sources docstring for the
    Touko Amekawa / Roger Black canary explanation. The author_ids
    parameter is still accepted as a fallback for callers that don't
    have names handy.
    """
    author_ids = data.get("author_ids", [])
    author_names = data.get("author_names")
    clear_source = data.get("clear_source", False)
    clear_mam = data.get("clear_mam", False)
    content_type = data.get("content_type")
    if not author_ids and not author_names:
        return {"error": "No authors specified"}
    if not clear_source and not clear_mam:
        return {"error": "Nothing to clear — specify clear_source and/or clear_mam"}

    # Build the library list. `content_type=None` (active-lib only) is
    # the pre-refactor behavior and remains the default so callers that
    # don't know about libraries keep working.
    if content_type is None:
        target_libs = None  # signal: use active library via get_db()
    else:
        target_libs = libraries_for(content_type)
        if not target_libs:
            return {"status": "ok", "authors_cleared": 0, "books_deleted": 0,
                    "message": f"No {content_type} libraries found."}

    total_deleted = 0
    libs_touched = 0
    if target_libs is None:
        db = await get_db()
        try:
            total_deleted += await _clear_authors_in_library(
                db, author_ids, clear_source, clear_mam,
            )
            await db.commit()
            if clear_source and total_deleted > 0:
                cleaned = await cleanup_empty_series(db)
                if cleaned:
                    logger.info(f"  Empty series cleanup: removed {cleaned} orphaned series")
            libs_touched = 1
        finally:
            await db.close()
    else:
        # Cross-library clear. When author_names was supplied, each
        # library resolves its OWN local author IDs by name — the
        # POSTed `author_ids` are merged-response IDs that mean nothing
        # outside the originating library, so we can't use them across
        # the iteration. Names are the only reliable cross-library
        # identifier (and they're already the merge key the cross-
        # library Authors view uses to aggregate rows).
        for lib in target_libs:
            slug = lib.get("slug")
            if not slug:
                continue
            db = await get_db(slug)
            try:
                if author_names:
                    ph = ",".join(["?" for _ in author_names])
                    name_rows = await db.execute_fetchall(
                        f"SELECT id FROM authors WHERE name IN ({ph})",
                        list(author_names),
                    )
                    lib_ids = [r[0] for r in name_rows]
                    if not lib_ids:
                        continue
                else:
                    # Legacy callers — pass IDs as-is. Subject to the
                    # cross-library ID-collision bug; new clients
                    # should send author_names.
                    lib_ids = author_ids
                deleted = await _clear_authors_in_library(
                    db, lib_ids, clear_source, clear_mam,
                )
                await db.commit()
                if clear_source and deleted > 0:
                    cleaned = await cleanup_empty_series(db)
                    if cleaned:
                        logger.info(
                            f"  [{slug}] empty series cleanup: removed {cleaned} orphaned series"
                        )
                total_deleted += deleted
                libs_touched += 1
            finally:
                await db.close()

    n_authors = len(author_names) if author_names else len(author_ids)
    logger.info(
        f"Cleared scan data for {n_authors} authors across {libs_touched} "
        f"libraries (content_type={content_type or 'active'}, "
        f"source={clear_source}, mam={clear_mam}), {total_deleted} books deleted"
    )
    return {"status": "ok", "authors_cleared": n_authors,
            "books_deleted": total_deleted, "libraries_touched": libs_touched}


@router.post("/authors/bulk-hide-books")
async def bulk_hide_authors_books(data: dict = Body(...)):
    """v2.17.0 Feat C — cascade Hide across selected authors' books.

    Authors don't have their own `hidden` column; "Hide" on the
    Authors list means "hide every one of these authors' books"
    (so the per-library tile vanishes from non-Hidden listings).
    Reuses books-side semantics so it composes cleanly with the
    existing single-book Hide and the bulk-hide-books endpoint.

    Cross-library: like `clear-scan-data`, accepts `author_names`
    as the portable identifier across libraries (per-library IDs
    collide). Returns per-library counts so the frontend can
    surface a "X books across Y libraries" toast.
    """
    author_names = data.get("author_names") or []
    if not author_names:
        return {"error": "No authors specified"}

    total_hidden = 0
    libs_touched = 0
    for lib in state._discovered_libraries:
        slug = lib.get("slug")
        if not slug:
            continue
        db = await get_db(slug)
        try:
            ph = ",".join(["?" for _ in author_names])
            name_rows = await (await db.execute(
                f"SELECT id FROM authors WHERE name IN ({ph})",
                list(author_names),
            )).fetchall()
            lib_ids = [r["id"] for r in name_rows]
            if not lib_ids:
                continue
            id_ph = ",".join(["?" for _ in lib_ids])
            # Affected series — we'll recompute authority after the
            # hide so a now-empty per-author series doesn't keep
            # claiming a shared author tag.
            sid_rows = await (await db.execute(
                f"SELECT DISTINCT series_id FROM books "
                f"WHERE author_id IN ({id_ph}) "
                f"AND series_id IS NOT NULL AND hidden = 0",
                lib_ids,
            )).fetchall()
            affected_sids = [r["series_id"] for r in sid_rows]

            cur = await db.execute(
                f"UPDATE books SET hidden = 1 "
                f"WHERE author_id IN ({id_ph}) AND hidden = 0",
                lib_ids,
            )
            n = cur.rowcount or 0
            await db.execute(
                f"DELETE FROM book_series_suggestions "
                f"WHERE book_id IN (SELECT id FROM books "
                f"WHERE author_id IN ({id_ph}))",
                lib_ids,
            )
            if affected_sids:
                await _recompute_series_author(db, affected_sids)
            await db.commit()
            total_hidden += n
            libs_touched += 1
        finally:
            await db.close()

    logger.info(
        f"bulk-hide-authors-books: {len(author_names)} authors → "
        f"hid {total_hidden} books across {libs_touched} libraries"
    )
    return {
        "status": "ok",
        "authors": len(author_names),
        "books_hidden": total_hidden,
        "libraries_touched": libs_touched,
    }


@router.post("/authors/bulk-delete-books")
async def bulk_delete_authors_books(data: dict = Body(...)):
    """v2.17.0 Feat C — cascade Delete across selected authors'
    UNOWNED books. Mirrors the books-side `bulk-delete` behavior of
    silently skipping Calibre/ABS-synced rows.

    Author rows themselves stay intact so the v2.12.1 dual-row
    mirror pattern keeps working — if a hard "remove the author
    record" verb is ever needed, it'll be a separate endpoint
    with its own design.
    """
    author_names = data.get("author_names") or []
    if not author_names:
        return {"error": "No authors specified"}

    total_deleted = 0
    total_skipped = 0
    libs_touched = 0
    for lib in state._discovered_libraries:
        slug = lib.get("slug")
        if not slug:
            continue
        db = await get_db(slug)
        try:
            ph = ",".join(["?" for _ in author_names])
            name_rows = await (await db.execute(
                f"SELECT id FROM authors WHERE name IN ({ph})",
                list(author_names),
            )).fetchall()
            lib_ids = [r["id"] for r in name_rows]
            if not lib_ids:
                continue
            id_ph = ",".join(["?" for _ in lib_ids])
            # Count what we'd skip (Calibre-synced + ABS-synced).
            skipped_row = await (await db.execute(
                f"SELECT COUNT(*) AS c FROM books "
                f"WHERE author_id IN ({id_ph}) "
                f"AND (calibre_id IS NOT NULL OR audiobookshelf_id IS NOT NULL)",
                lib_ids,
            )).fetchone()
            n_skipped = (skipped_row["c"] if skipped_row else 0) or 0
            # Capture series for post-delete authority recomputation.
            sid_rows = await (await db.execute(
                f"SELECT DISTINCT series_id FROM books "
                f"WHERE author_id IN ({id_ph}) "
                f"AND series_id IS NOT NULL "
                f"AND calibre_id IS NULL AND audiobookshelf_id IS NULL",
                lib_ids,
            )).fetchall()
            affected_sids = [r["series_id"] for r in sid_rows]

            cur = await db.execute(
                f"DELETE FROM books "
                f"WHERE author_id IN ({id_ph}) "
                f"AND calibre_id IS NULL AND audiobookshelf_id IS NULL",
                lib_ids,
            )
            n_deleted = cur.rowcount or 0
            if affected_sids:
                await _recompute_series_author(db, affected_sids)
            await db.commit()
            total_deleted += n_deleted
            total_skipped += n_skipped
            libs_touched += 1
        finally:
            await db.close()

    logger.info(
        f"bulk-delete-authors-books: {len(author_names)} authors → "
        f"deleted {total_deleted}, skipped {total_skipped} library-synced "
        f"across {libs_touched} libraries"
    )
    return {
        "status": "ok",
        "authors": len(author_names),
        "books_deleted": total_deleted,
        "books_skipped": total_skipped,
        "libraries_touched": libs_touched,
    }


async def _resolve_names_for_ids(
    author_ids: list[int],
) -> list[str]:
    """Look up author display names for the given IDs, trying every
    library until they resolve. Used when cross-library scanning needs
    to find the same-named author in libraries other than the active
    one.
    """
    needed = set(author_ids)
    names: dict[int, str] = {}
    # Active first (fast path for the common single-library case).
    db = await get_db()
    try:
        placeholders = ",".join(["?" for _ in needed])
        rows = await db.execute_fetchall(
            f"SELECT id, name FROM authors WHERE id IN ({placeholders})",
            list(needed),
        )
        for r in rows:
            names[r[0]] = r[1]
    finally:
        await db.close()
    # Any unresolved IDs? Fan out to other libraries. This covers the
    # cross-library selection case where a row came from library X but
    # the active lib is Y — row.id is X-scoped and won't be in Y's DB.
    unresolved = needed - set(names.keys())
    if unresolved:
        for lib in state._discovered_libraries:
            if not unresolved:
                break
            slug = lib.get("slug")
            if not slug or slug == get_active_library():
                continue
            try:
                db2 = await get_db(slug)
            except Exception:
                continue
            try:
                ph = ",".join(["?" for _ in unresolved])
                rows = await db2.execute_fetchall(
                    f"SELECT id, name FROM authors WHERE id IN ({ph})",
                    list(unresolved),
                )
                for r in rows:
                    if r[0] in unresolved:
                        names[r[0]] = r[1]
                        unresolved.discard(r[0])
            finally:
                await db2.close()
    # Deduplicate names so a user picking the same author in two
    # libraries doesn't scan them twice per-library.
    seen: set[str] = set()
    out: list[str] = []
    for aid in author_ids:
        n = names.get(aid)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


@router.post("/authors/scan-sources")
async def scan_authors_sources(data: dict = Body(...)):
    """Run a source-plugin lookup for each of the given authors.

    Used by the Authors page bulk-select bar. Loops sequentially because
    lookup_author is rate-limited per-source and parallelizing would just
    queue up against the existing semaphores.

    `content_type`: optional "ebook" / "audiobook". When set, iterate
    libraries of that type and scan each selected author by NAME in
    every matching library. Omitted → legacy active-library-only path.

    `author_names`: optional list of pre-resolved author display names.
    When provided alongside `content_type`, the backend uses those names
    directly and skips the ID→name resolver. This is the safe path for
    cross-library selections: the merged Authors view returns each
    author with an `id` from whichever library was first encountered,
    so an audiobook-only author (e.g. Touko Amekawa) ends up with the
    audiobook library's ID. If that ID happens to collide with a
    different author's ID in the active ebook library (Roger Black's
    ebook id was 17, Touko's audiobook id was 17), the resolver's
    active-library lookup picks up the WRONG name and the wrong author
    gets scanned. Sending names sidesteps the collision entirely. The
    frontend reads names from its merged Authors response and POSTs
    them; the resolver fallback path remains for older callers that
    only have IDs.
    """
    author_ids = data.get("author_ids", [])
    author_names = data.get("author_names")
    content_type = data.get("content_type")
    logger.info(
        "scan-sources POST: author_ids=%s names=%s content_type=%r active=%s",
        author_ids, author_names, content_type, get_active_library(),
    )
    if not author_ids and not author_names:
        return {"error": "No authors specified"}

    if content_type is None:
        # Active-library scan — legacy path.
        db = await get_db()
        try:
            placeholders = ",".join(["?" for _ in author_ids])
            # ORDER BY sort_name so the scan progresses alphabetically
            # by last name (e.g. "Anderson, J" → "Brown, K"), matching
            # what the user sees in the Authors page list. Without the
            # ORDER BY, SQLite returns rows in physical (rowid) order,
            # which is insertion order from initial Calibre sync — not
            # at all what users expect when they multi-select an A-letter
            # batch and watch the dashboard scan progress.
            rows = await db.execute_fetchall(
                f"SELECT id, name FROM authors WHERE id IN ({placeholders}) "
                f"ORDER BY sort_name",
                author_ids,
            )
        finally:
            await db.close()
        if not rows:
            raise HTTPException(404, "No matching authors found")

        async def _runner():
            nonlocal_state = {"scanned": 0, "errors": 0, "new": 0}
            for row in rows:
                aid, name = row[0], row[1]
                state._lookup_progress.update({"current_author": name})
                def _on_source(running, _baseline=nonlocal_state["new"]):
                    state._lookup_progress["new_books"] = _baseline + int(running)
                try:
                    new_books = await lookup_author(aid, name, on_progress=_on_source)
                    nonlocal_state["new"] += int(new_books or 0)
                    nonlocal_state["scanned"] += 1
                    if new_books:
                        try:
                            from app.discovery.notify import notify_new_books
                            await notify_new_books(name, int(new_books))
                        except Exception:
                            logger.debug("per-author notify failed", exc_info=True)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Bulk source scan error for author {aid} ({name}): {e}")
                    nonlocal_state["errors"] += 1
                state._lookup_progress.update({
                    "checked": nonlocal_state["scanned"] + nonlocal_state["errors"],
                    "new_books": nonlocal_state["new"],
                })

        _spawn_lookup_task("bulk_authors", total=len(rows), runner=_runner)
        return {"status": "started", "total": len(rows)}

    # Cross-library scan by content_type — iterate matching libraries
    # and scan each selected author by NAME in each. Prefer
    # caller-supplied names (the safe path against ID collisions —
    # see the docstring); fall back to the legacy ID resolver for
    # older callers / direct API users.
    target_libs = libraries_for(content_type)
    if not target_libs:
        logger.warning(
            "scan-sources: NO %s LIBRARIES FOUND (discovered=%s)",
            content_type,
            [(l.get("slug"), l.get("content_type")) for l in state._discovered_libraries],
        )
        return {"status": "ok", "total": 0,
                "message": f"No {content_type} libraries found."}
    if author_names:
        # Preserve caller order while deduping.
        seen: set[str] = set()
        names = []
        for n in author_names:
            if n and n not in seen:
                seen.add(n)
                names.append(n)
    else:
        names = await _resolve_names_for_ids(author_ids)
    if not names:
        raise HTTPException(404, "No matching authors found")

    # Pre-resolve per-library matches so the dashboard's progress
    # total reflects ACTUAL author scans, not the optimistic
    # names×libraries product. A name in `names` that isn't an
    # author in a given library (e.g. an audiobook-only author
    # appearing in an ebook-content_type scan) is filtered here
    # — the scan loop later sees only matched rows and the user-
    # facing count matches what'll actually run. Same SQL the
    # loop used to do per-iteration; we just hoist it.
    pre_resolved: list[tuple[dict, list]] = []
    ph = ",".join(["?" for _ in names])
    for lib in target_libs:
        slug = lib.get("slug")
        if not slug:
            continue
        try:
            ldb = await get_db(slug)
        except Exception as e:
            logger.warning(f"scan-sources: cannot open lib {slug}: {e}")
            continue
        try:
            lib_rows = await ldb.execute_fetchall(
                f"SELECT id, name FROM authors WHERE name IN ({ph}) "
                f"ORDER BY sort_name",
                names,
            )
        finally:
            await ldb.close()
        pre_resolved.append((lib, list(lib_rows)))

    total_tasks = sum(len(rows) for _, rows in pre_resolved)
    if total_tasks == 0:
        logger.warning(
            "scan-sources: NO MATCHING AUTHORS — names=%s target_libs=%s pre_resolved_counts=%s",
            names,
            [l.get("slug") for l in target_libs],
            [(lib.get("slug"), len(rows)) for lib, rows in pre_resolved],
        )
        # v2.12.1 #3 — name the author(s) so the toast can read them
        # back to the user. Single-author selections get a fully
        # personalized message; multi-author selections list up to 3
        # names then "and N more" to keep the toast compact.
        if len(names) == 1:
            msg = (
                f"No {content_type}-library match for '{names[0]}'. "
                f"Add this author to a {content_type} library first, "
                f"or scan the other content type instead."
            )
        else:
            preview = ", ".join(f"'{n}'" for n in names[:3])
            more = "" if len(names) <= 3 else f" and {len(names) - 3} more"
            msg = (
                f"No {content_type}-library match for any of: "
                f"{preview}{more}."
            )
        return {"status": "ok", "total": 0,
                "requested": len(names),
                "names": names,
                "message": msg}

    async def _runner():
        from app.discovery.database import set_active_library
        original_active = get_active_library()
        nonlocal_state = {"scanned": 0, "errors": 0, "new": 0}
        try:
            for lib, lib_rows in pre_resolved:
                slug = lib.get("slug")
                if not slug:
                    continue
                if slug != get_active_library():
                    set_active_library(slug)
                for row in lib_rows:
                    aid, name = row[0], row[1]
                    state._lookup_progress.update({"current_author": name})
                    def _on_source(running, _baseline=nonlocal_state["new"]):
                        state._lookup_progress["new_books"] = _baseline + int(running)
                    try:
                        new_books = await lookup_author(aid, name, on_progress=_on_source)
                        nonlocal_state["new"] += int(new_books or 0)
                        nonlocal_state["scanned"] += 1
                        if new_books:
                            try:
                                from app.discovery.notify import notify_new_books
                                await notify_new_books(name, int(new_books))
                            except Exception:
                                logger.debug("per-author notify failed", exc_info=True)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.error(
                            f"Bulk source scan error for {name} in {slug}: {e}"
                        )
                        nonlocal_state["errors"] += 1
                    state._lookup_progress.update({
                        "checked": nonlocal_state["scanned"] + nonlocal_state["errors"],
                        "new_books": nonlocal_state["new"],
                    })
        finally:
            if original_active and original_active != get_active_library():
                set_active_library(original_active)

    _spawn_lookup_task("bulk_authors", total=total_tasks, runner=_runner)
    return {"status": "started", "total": total_tasks,
            "requested": len(names),
            "libraries": len(target_libs), "authors": len(names)}


async def _skip_mam_for_author_ids_in_library(
    db, author_ids: list[int],
) -> int:
    """UPDATE every book under the given author_ids to mam_status=
    'not_applicable', clearing the URL/torrent_id/formats so a stale
    prior match doesn't linger on a row the user just declared
    irrelevant. Returns affected row count. Caller owns commit."""
    placeholders = ",".join(["?" for _ in author_ids])
    cur = await db.execute(
        f"UPDATE books SET mam_url=NULL, mam_status='not_applicable', "
        f"mam_formats=NULL, mam_torrent_id=NULL, mam_has_multiple=0, "
        f"mam_my_snatched=0, mam_is_bundle=0 WHERE author_id IN ({placeholders})",
        author_ids,
    )
    return cur.rowcount or 0


@router.post("/authors/skip-mam")
async def skip_authors_mam(data: dict = Body(...)):
    """Bulk Skip MAM — mark every book belonging to the given authors
    as `mam_status='not_applicable'` so the rescan loop never visits
    them again. Driven by the Authors page multi-select dropdown.

    Same cross-library + author_names contract as
    `/authors/clear-scan-data` (see that handler for the
    cross-library ID-collision rationale). content_type=None reads
    the active library; 'ebook'/'audiobook'/'all' iterate libraries
    of that type, resolving author names per-library to sidestep ID
    collisions.

    Snekguy-style use case: an author whose works are free on the
    public web and almost never end up on MAM. Pre-skip wipes them
    from the rescan queue so the v2.3.6 widened predicate doesn't
    keep retrying never-findable rows every tick.
    """
    author_ids = data.get("author_ids", [])
    author_names = data.get("author_names")
    content_type = data.get("content_type")
    if not author_ids and not author_names:
        return {"error": "No authors specified"}

    if content_type is None:
        target_libs = None
    else:
        target_libs = libraries_for(content_type)
        if not target_libs:
            return {"status": "ok", "authors_skipped": 0,
                    "books_skipped": 0, "libraries_touched": 0,
                    "message": f"No {content_type} libraries found."}

    total_skipped = 0
    libs_touched = 0
    if target_libs is None:
        db = await get_db()
        try:
            ids = author_ids
            if not ids and author_names:
                ph = ",".join(["?" for _ in author_names])
                name_rows = await db.execute_fetchall(
                    f"SELECT id FROM authors WHERE name IN ({ph})",
                    list(author_names),
                )
                ids = [r[0] for r in name_rows]
            if ids:
                total_skipped += await _skip_mam_for_author_ids_in_library(db, ids)
                await db.commit()
                libs_touched = 1
        finally:
            await db.close()
    else:
        for lib in target_libs:
            slug = lib.get("slug")
            if not slug:
                continue
            db = await get_db(slug)
            try:
                if author_names:
                    ph = ",".join(["?" for _ in author_names])
                    name_rows = await db.execute_fetchall(
                        f"SELECT id FROM authors WHERE name IN ({ph})",
                        list(author_names),
                    )
                    lib_ids = [r[0] for r in name_rows]
                    if not lib_ids:
                        continue
                else:
                    lib_ids = author_ids
                deleted = await _skip_mam_for_author_ids_in_library(db, lib_ids)
                await db.commit()
                total_skipped += deleted
                libs_touched += 1
            finally:
                await db.close()

    n_authors = len(author_names) if author_names else len(author_ids)
    logger.info(
        f"Skip MAM: marked {total_skipped} books not_applicable across "
        f"{libs_touched} libraries for {n_authors} authors "
        f"(content_type={content_type or 'active'})"
    )
    return {
        "status": "ok",
        "authors_skipped": n_authors,
        "books_skipped": total_skipped,
        "libraries_touched": libs_touched,
    }


@router.post("/authors/scan-mam")
async def scan_authors_mam(data: dict = Body(...)):
    """Run a MAM scan for every un-scanned book belonging to the given authors.

    v2.3.7 — multi-library aware. Accepts `content_type` (None | "ebook"
    | "audiobook" | "all") and `author_names` matching the
    `/authors/clear-scan-data` + `/authors/scan-sources` contract.
    Iterates each matching library, resolves author_names locally to
    sidestep cross-library ID collisions (Roger Black id=17 in Calibre
    vs Touko Amekawa id=17 in ABS), and routes per-library MAM
    category (ebook/audiobook) by the lib's own content_type.

    Pre-v2.3.7 this only touched the active library, which silently
    left the unselected library's matching rows unscanned — Mark's
    Snekguy bulk-skip use case exposed this when a "Scan MAM" on a
    cross-library author list missed half the books.

    Runs as a background task with progress tracked in
    state._mam_scan_progress so the Dashboard scan widget shows
    progress in real time.
    """
    from app.discovery.sources.mam import (
        _NEEDS_SCAN_BASIC_ALIASED,
        check_book as mam_check_book,
        _resolve_mam_languages,
    )
    from app.discovery.cross_library import libraries_for
    from app import state

    author_ids = data.get("author_ids", [])
    author_names = data.get("author_names")
    content_type = data.get("content_type")
    if not author_ids and not author_names:
        return {"error": "No authors specified"}

    s = load_settings()
    from app.discovery.routers.mam import _mam_ready, _get_mam_token
    if not await _mam_ready(s):
        return {"error": "MAM not configured or not enabled"}
    if not s.get("mam_scanning_enabled", True):
        return {"error": "MAM scanning is disabled — enable it in Settings"}
    if state._mam_scan_progress.get("running"):
        return {"error": "A MAM scan is already running"}

    # Build target library list. content_type=None preserves the
    # pre-v2.3.7 active-library-only behavior for callers that haven't
    # been updated; otherwise iterate per content_type.
    if content_type is None:
        target_libs = None
    else:
        target_libs = libraries_for(content_type)
        if not target_libs:
            return {
                "status": "complete",
                "message": f"No {content_type} libraries found.",
                "scanned": 0, "found": 0, "possible": 0, "not_found": 0,
            }

    delay = s.get("rate_mam", 2)
    token = await _get_mam_token()
    lang_ids = _resolve_mam_languages(s.get("languages", ["English"]))
    ebook_fp = s.get("mam_format_priority")
    audio_fp = s.get("audiobook_format_priority")

    # Per-library snapshot of scannable books — taken upfront so a
    # concurrent author scan adding new books mid-run doesn't inflate
    # this scan's queue (matches the manual /scan endpoint's pattern).
    per_lib: list[tuple[dict, list[tuple[int, str, str]]]] = []
    libs_iter = target_libs if target_libs is not None else [None]
    for lib in libs_iter:
        slug = lib.get("slug") if lib else None
        try:
            lib_db = await get_db(slug) if slug else await get_db()
        except Exception as e:
            logger.warning(f"authors/scan-mam: cannot open lib {slug}: {e}")
            continue
        try:
            # Resolve author identity per-library. With author_names we
            # look up local IDs to dodge the cross-library collision
            # class (see /authors/scan-sources). Without names, fall
            # back to the supplied IDs (legacy single-library callers).
            if author_names:
                ph = ",".join(["?" for _ in author_names])
                name_rows = await lib_db.execute_fetchall(
                    f"SELECT id FROM authors WHERE name IN ({ph})",
                    list(author_names),
                )
                lib_aids = [r[0] for r in name_rows]
            else:
                lib_aids = author_ids
            if not lib_aids:
                continue
            placeholders = ",".join(["?" for _ in lib_aids])
            # series JOIN required so series_name reaches check_book →
            # Fix E (series-bundle promote) can fire. UAT 2026-05-11
            # round 4 — see books.py:scan_books_mam comment.
            rows = await lib_db.execute_fetchall(
                f"SELECT b.id, b.title, a.name, s.name AS series_name "
                f"FROM books b "
                f"JOIN authors a ON b.author_id=a.id "
                f"LEFT JOIN series s ON b.series_id = s.id "
                f"WHERE b.author_id IN ({placeholders}) "
                f"AND {_NEEDS_SCAN_BASIC_ALIASED} "
                f"ORDER BY a.sort_name, b.title",
                lib_aids,
            )
            books = [(r[0], r[1], r[2], r[3] or "") for r in rows]
            if books:
                per_lib.append((lib or {"slug": None, "content_type": "ebook"}, books))
        finally:
            await lib_db.close()

    total = sum(len(books) for _, books in per_lib)
    if total == 0:
        return {
            "status": "complete",
            "message": "No un-scanned books for these authors",
            "scanned": 0, "found": 0, "possible": 0, "not_found": 0,
        }

    state._mam_scan_progress.update({
        "running": True, "scanned": 0, "total": total,
        "found": 0, "possible": 0, "not_found": 0, "errors": 0,
        "status": "scanning", "type": "multi_author",
        "current_book": "", "current_library": "",
    })

    async def _do():
        try:
            for lib, books in per_lib:
                if not state._mam_scan_progress.get("running"):
                    state._mam_scan_progress.update({"status": "cancelled"})
                    break
                slug = lib.get("slug")
                lib_name = lib.get("display_name") or lib.get("name") or slug or "active"
                ct = lib.get("content_type") or "ebook"
                format_priority = audio_fp if ct == "audiobook" else ebook_fp
                state._mam_scan_progress["current_library"] = lib_name
                try:
                    db2 = await get_db(slug) if slug else await get_db()
                except Exception as e:
                    logger.error(f"authors/scan-mam: cannot open lib {slug} for write: {e}")
                    state._mam_scan_progress["errors"] = (
                        state._mam_scan_progress.get("errors", 0) + len(books)
                    )
                    continue
                try:
                    from app.discovery.cover_phash import ensure_cover_phash
                    for bid, btitle, aname, bseries in books:
                        if not state._mam_scan_progress.get("running"):
                            state._mam_scan_progress.update({"status": "cancelled"})
                            break
                        state._mam_scan_progress["current_book"] = btitle[:60]
                        try:
                            seshat_phash = await ensure_cover_phash(db2, bid, token=token)
                            check = await mam_check_book(
                                token, btitle, aname, format_priority,
                                delay, lang_ids=lang_ids,
                                series_name=bseries,
                                content_type=ct,
                                seshat_cover_phash=seshat_phash,
                            )
                        except Exception as e:
                            logger.error(
                                f"Bulk author MAM scan error on book {bid} "
                                f"({btitle[:40]}) in {lib_name}: {e}"
                            )
                            state._mam_scan_progress["errors"] = (
                                state._mam_scan_progress.get("errors", 0) + 1
                            )
                            continue
                        await db2.execute(
                            """
                            UPDATE books SET mam_url=?, mam_status=?, mam_formats=?,
                                   mam_torrent_id=?, mam_has_multiple=?, mam_my_snatched=?,
                                   mam_is_bundle=?
                            WHERE id=?
                            """,
                            (
                                check["mam_url"], check["status"], check["mam_formats"],
                                check["mam_torrent_id"],
                                1 if check["mam_has_multiple"] else 0,
                                1 if check.get("mam_my_snatched") else 0,
                                1 if check.get("mam_is_bundle") else 0,
                                bid,
                            ),
                        )
                        state._mam_scan_progress["scanned"] = (
                            state._mam_scan_progress.get("scanned", 0) + 1
                        )
                        st = check["status"]
                        if st in ("found", "possible", "not_found"):
                            state._mam_scan_progress[st] = (
                                state._mam_scan_progress.get(st, 0) + 1
                            )
                    await db2.commit()
                finally:
                    await db2.close()
            state._mam_scan_progress.update({
                "running": False, "status": "complete",
                "current_book": "", "current_library": "",
            })
        except Exception as e:
            logger.error(f"Bulk author MAM scan error: {e}", exc_info=True)
            state._mam_scan_progress.update({
                "running": False, "status": f"error: {e}",
                "current_book": "", "current_library": "",
            })

    state._mam_scan_task = asyncio.create_task(_do())
    return {
        "status": "started",
        "total": total,
        "libraries": [lib.get("slug") for lib, _ in per_lib if lib.get("slug")],
    }


@router.post("/sources/reset")
async def reset_all_source_scan_data():
    """Reset all source scan data across the entire library.

    Deletes every non-Calibre, non-owned book (i.e. books discovered by source
    scans), clears source_url on owned/Calibre books, and resets last_lookup_at
    on every author so future scans treat all authors as never-scanned.
    MAM data is left untouched.
    """
    db = await get_db()
    try:
        # Count discovered books that will be deleted
        count_row = await db.execute_fetchall(
            "SELECT COUNT(*) FROM books WHERE owned=0 AND calibre_id IS NULL"
        )
        affected = count_row[0][0] if count_row else 0
        # Delete all non-owned discovered books
        await db.execute("DELETE FROM books WHERE owned=0 AND calibre_id IS NULL")
        # Clear source URLs on owned books
        await db.execute("UPDATE books SET source_url=NULL WHERE owned=1")
        # Reset every author's last_lookup_at so the next scheduled scan picks them all up
        await db.execute("UPDATE authors SET last_lookup_at=NULL")
        await db.commit()
        cleaned = await cleanup_empty_series(db)
        if cleaned:
            logger.info(f"  Empty series cleanup: removed {cleaned} orphaned series")
        logger.info(f"Reset all source scan data: {affected} discovered books deleted")
        return {"status": "ok", "books_deleted": affected, "series_cleaned": cleaned}
    finally:
        await db.close()


# ─── Pen-Name Linking ───────────────────────────────────────

VALID_LINK_TYPES = {"pen_name", "co_author"}


@router.get("/authors/{aid}/pen-names")
async def get_pen_name_links(aid: int):
    """Get all author-link rows for an author (both directions).

    Endpoint is named pen-names for backward compat; the rows now carry
    a `link_type` discriminator (`pen_name` | `co_author`). The backend
    treats both identically — they only differ in the UI label.
    """
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT p.id, p.canonical_author_id, p.alias_author_id, p.link_type, "
            "a1.name as canonical_name, a2.name as alias_name "
            "FROM pen_name_links p "
            "JOIN authors a1 ON p.canonical_author_id = a1.id "
            "JOIN authors a2 ON p.alias_author_id = a2.id "
            "WHERE p.canonical_author_id = ? OR p.alias_author_id = ?",
            (aid, aid),
        )).fetchall()
        return {"links": [dict(r) for r in rows]}
    finally:
        await db.close()


@router.post("/authors/link-pen-names")
async def link_pen_names(data: dict = Body(...)):
    """Link two authors so source scans treat them as one identity.

    The canonical_author_id is the 'primary' identity; alias_author_id
    is the linked author. Source scans for either one check owned books
    under BOTH for dedup and series matching. The `link_type` field
    (default `pen_name`) only controls the UI label — backend dedup
    behavior is identical for both link types.
    """
    canonical_id = data.get("canonical_author_id")
    alias_id = data.get("alias_author_id")
    link_type = (data.get("link_type") or "pen_name").lower()
    if link_type not in VALID_LINK_TYPES:
        raise HTTPException(400, f"link_type must be one of {sorted(VALID_LINK_TYPES)}")
    if not canonical_id or not alias_id:
        raise HTTPException(400, "Both canonical_author_id and alias_author_id required")
    if canonical_id == alias_id:
        raise HTTPException(400, "Cannot link an author to themselves")
    db = await get_db()
    try:
        # Verify both authors exist
        for aid in (canonical_id, alias_id):
            row = await (await db.execute("SELECT id FROM authors WHERE id=?", (aid,))).fetchone()
            if not row:
                raise HTTPException(404, f"Author {aid} not found")
        # Check for existing link (either direction). If found, update
        # the link_type to the new value rather than creating a duplicate
        # — lets the user reclassify a pen-name link as co-author.
        existing = await (await db.execute(
            "SELECT id, link_type FROM pen_name_links WHERE "
            "(canonical_author_id=? AND alias_author_id=?) OR "
            "(canonical_author_id=? AND alias_author_id=?)",
            (canonical_id, alias_id, alias_id, canonical_id),
        )).fetchone()
        if existing:
            if existing["link_type"] != link_type:
                await db.execute(
                    "UPDATE pen_name_links SET link_type=? WHERE id=?",
                    (link_type, existing["id"]),
                )
                await db.commit()
                logger.info(
                    f"Reclassified author link {existing['id']}: "
                    f"{existing['link_type']} → {link_type}"
                )
                return {"status": "updated", "link_id": existing["id"], "link_type": link_type}
            return {"status": "already_linked", "link_id": existing["id"], "link_type": link_type}
        cur = await db.execute(
            "INSERT INTO pen_name_links (canonical_author_id, alias_author_id, link_type) "
            "VALUES (?, ?, ?)",
            (canonical_id, alias_id, link_type),
        )
        await db.commit()
        logger.info(
            f"Linked authors as {link_type}: {canonical_id} ↔ {alias_id}"
        )
        # v2.20.0 — also write the global pen_name_links_v2 row so the
        # unified `/persons/{person_id}` view picks up newly-created
        # links without waiting for the next migration sweep. Best-
        # effort: if either author isn't yet in author_links (e.g. a
        # row inserted between sync hooks), we log + skip the v2 write.
        # The legacy row already landed above, so cross-library scans
        # still respect the link via the existing per-library code path.
        await _dual_write_pen_name_to_v2(
            get_active_library(), canonical_id, alias_id, link_type,
        )
        return {"status": "ok", "link_id": cur.lastrowid, "link_type": link_type}
    finally:
        await db.close()


async def _dual_write_pen_name_to_v2(
    library_slug: str,
    canonical_author_id: int,
    alias_author_id: int,
    link_type: str,
) -> None:
    """Resolve a (canonical_author_id, alias_author_id) pair in the
    given library context to (canonical_person_id, alias_person_id)
    via author_links, and INSERT into pen_name_links_v2. Idempotent —
    UNIQUE collisions are swallowed."""
    canonical_person = await person_id_for(library_slug, canonical_author_id)
    alias_person = await person_id_for(library_slug, alias_author_id)
    if canonical_person is None or alias_person is None:
        logger.debug(
            "dual_write_pen_name_to_v2: unresolved person_id "
            "(%s/%d -> %s, %s/%d -> %s) — skipping v2 write",
            library_slug, canonical_author_id, canonical_person,
            library_slug, alias_author_id, alias_person,
        )
        return
    if canonical_person == alias_person:
        # Same person on both ends — typo-fix case, not a real link.
        return
    gdb = await get_global_db()
    try:
        try:
            await gdb.execute(
                "INSERT INTO pen_name_links_v2 "
                "(canonical_person_id, alias_person_id, link_type) "
                "VALUES (?, ?, ?)",
                (canonical_person, alias_person, link_type),
            )
            await gdb.commit()
        except Exception as exc:
            # UNIQUE collision is expected when the migration already
            # populated this pair; any other error gets logged but
            # doesn't fail the legacy write that already landed.
            logger.debug(
                "dual_write_pen_name_to_v2: v2 INSERT skipped (%s)", exc,
            )
    finally:
        await gdb.close()


@router.post("/persons/link-pen-names")
async def link_pen_names_via_persons(data: dict = Body(...)):
    """v2.20.0 Phase 4 — person-level pen-name linking.

    Body: `{canonical_person_id, alias_person_id, link_type}`. Writes
    a row to `pen_name_links_v2` keyed by person_id. Also writes the
    legacy per-library `pen_name_links` row when both persons share
    at least one library (so source-scan dedup, which still reads
    per-library, picks up the link). Falls back to v2-only when the
    persons have no library overlap.
    """
    canonical_pid = data.get("canonical_person_id")
    alias_pid = data.get("alias_person_id")
    link_type = (data.get("link_type") or "pen_name").lower()
    if link_type not in VALID_LINK_TYPES:
        raise HTTPException(
            400, f"link_type must be one of {sorted(VALID_LINK_TYPES)}",
        )
    if not canonical_pid or not alias_pid:
        raise HTTPException(
            400, "Both canonical_person_id and alias_person_id required",
        )
    if canonical_pid == alias_pid:
        raise HTTPException(400, "Cannot link a person to themselves")
    # Verify both persons exist.
    if await get_person(canonical_pid) is None:
        raise HTTPException(404, f"Person {canonical_pid} not found")
    if await get_person(alias_pid) is None:
        raise HTTPException(404, f"Person {alias_pid} not found")

    gdb = await get_global_db()
    try:
        # Reclassify if a v2 link already exists (either direction).
        existing = await (await gdb.execute(
            "SELECT id, link_type FROM pen_name_links_v2 "
            "WHERE (canonical_person_id=? AND alias_person_id=?) "
            "   OR (canonical_person_id=? AND alias_person_id=?)",
            (canonical_pid, alias_pid, alias_pid, canonical_pid),
        )).fetchone()
        if existing:
            if existing["link_type"] != link_type:
                await gdb.execute(
                    "UPDATE pen_name_links_v2 SET link_type=? WHERE id=?",
                    (link_type, existing["id"]),
                )
                await gdb.commit()
                logger.info(
                    f"Reclassified person link {existing['id']}: "
                    f"{existing['link_type']} → {link_type}"
                )
                return {
                    "status": "updated",
                    "v2_link_id": existing["id"],
                    "link_type": link_type,
                }
            return {
                "status": "already_linked",
                "v2_link_id": existing["id"],
                "link_type": link_type,
            }

        cur = await gdb.execute(
            "INSERT INTO pen_name_links_v2 "
            "(canonical_person_id, alias_person_id, link_type) "
            "VALUES (?, ?, ?)",
            (canonical_pid, alias_pid, link_type),
        )
        await gdb.commit()
        v2_id = cur.lastrowid
    finally:
        await gdb.close()

    # Best-effort legacy per-library write — pen_name_links lives in
    # per-library DBs and is what source-scan dedup reads. We pick
    # a library where BOTH persons have a row (so the legacy schema's
    # FK to per-library authors holds).
    canonical_links = await linked_authors(canonical_pid)
    alias_links = await linked_authors(alias_pid)
    canonical_by_slug = {s: a for s, a in canonical_links}
    legacy_link_id = None
    for slug, alias_aid in alias_links:
        canonical_aid = canonical_by_slug.get(slug)
        if canonical_aid is None:
            continue
        # Both endpoints present in `slug` — write the legacy row.
        try:
            ldb = await get_db(slug)
        except Exception:
            continue
        try:
            # Skip if a legacy row already exists for this pair.
            existing_legacy = await (await ldb.execute(
                "SELECT id FROM pen_name_links WHERE "
                "(canonical_author_id=? AND alias_author_id=?) OR "
                "(canonical_author_id=? AND alias_author_id=?)",
                (canonical_aid, alias_aid, alias_aid, canonical_aid),
            )).fetchone()
            if existing_legacy:
                legacy_link_id = existing_legacy["id"]
            else:
                cur = await ldb.execute(
                    "INSERT INTO pen_name_links "
                    "(canonical_author_id, alias_author_id, link_type) "
                    "VALUES (?, ?, ?)",
                    (canonical_aid, alias_aid, link_type),
                )
                await ldb.commit()
                legacy_link_id = cur.lastrowid
            # One legacy row per pair is enough — pen-name dedup is
            # per-library and reads only the local table.
            break
        finally:
            await ldb.close()

    logger.info(
        f"Linked persons as {link_type}: {canonical_pid} ↔ {alias_pid} "
        f"(v2={v2_id}, legacy={legacy_link_id})"
    )
    return {
        "status": "ok",
        "v2_link_id": v2_id,
        "legacy_link_id": legacy_link_id,
        "link_type": link_type,
    }


@router.delete("/persons/pen-name-link/{v2_link_id}")
async def unlink_persons_pen_name(v2_link_id: int):
    """v2.20.0 Phase 4 — delete a person-level pen-name link.

    Drops the v2 row, then walks per-library DBs to remove any
    legacy `pen_name_links` rows whose endpoints resolve to the
    same (canonical_person_id, alias_person_id) pair via author_links.
    """
    gdb = await get_global_db()
    try:
        row = await (await gdb.execute(
            "SELECT canonical_person_id, alias_person_id "
            "FROM pen_name_links_v2 WHERE id=?",
            (v2_link_id,),
        )).fetchone()
        if not row:
            return {"status": "not_found"}
        canonical_pid = row["canonical_person_id"]
        alias_pid = row["alias_person_id"]
        await gdb.execute(
            "DELETE FROM pen_name_links_v2 WHERE id=?", (v2_link_id,),
        )
        await gdb.commit()
    finally:
        await gdb.close()

    # Find + delete legacy per-library rows that match this pair.
    canonical_links = await linked_authors(canonical_pid)
    alias_links = await linked_authors(alias_pid)
    canonical_by_slug = {s: a for s, a in canonical_links}
    for slug, alias_aid in alias_links:
        canonical_aid = canonical_by_slug.get(slug)
        if canonical_aid is None:
            continue
        try:
            ldb = await get_db(slug)
        except Exception:
            continue
        try:
            await ldb.execute(
                "DELETE FROM pen_name_links WHERE "
                "(canonical_author_id=? AND alias_author_id=?) OR "
                "(canonical_author_id=? AND alias_author_id=?)",
                (canonical_aid, alias_aid, alias_aid, canonical_aid),
            )
            await ldb.commit()
        finally:
            await ldb.close()
    return {"status": "ok"}


@router.delete("/authors/pen-name-link/{link_id}")
async def unlink_pen_names(link_id: int):
    """Remove a pen-name link.

    v2.20.0 — also drops the matching pen_name_links_v2 row so the
    unified `/persons/{person_id}` view stays in sync. The v2 row is
    identified by resolving the legacy row's (canonical_author_id,
    alias_author_id) to person_ids via author_links — best-effort,
    same as the dual-write path.
    """
    db = await get_db()
    try:
        legacy = await (await db.execute(
            "SELECT canonical_author_id, alias_author_id "
            "FROM pen_name_links WHERE id=?",
            (link_id,),
        )).fetchone()
        await db.execute("DELETE FROM pen_name_links WHERE id=?", (link_id,))
        await db.commit()
    finally:
        await db.close()
    if legacy:
        canonical_person = await person_id_for(
            get_active_library(), legacy["canonical_author_id"],
        )
        alias_person = await person_id_for(
            get_active_library(), legacy["alias_author_id"],
        )
        if canonical_person is not None and alias_person is not None:
            gdb = await get_global_db()
            try:
                # Match in both directions — the v2 row may have been
                # inserted with the endpoints swapped.
                await gdb.execute(
                    "DELETE FROM pen_name_links_v2 "
                    "WHERE (canonical_person_id=? AND alias_person_id=?) "
                    "   OR (canonical_person_id=? AND alias_person_id=?)",
                    (canonical_person, alias_person,
                     alias_person, canonical_person),
                )
                await gdb.commit()
            finally:
                await gdb.close()
    return {"status": "ok"}
