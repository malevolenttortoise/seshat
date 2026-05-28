"""
Cross-library work matcher.

Scans the user's library DBs (one per discovered library) and groups
books that share the same normalized (author, title) pair into a
shared `work_id`. Writes the resulting membership rows into
`work_links` in the pipeline DB.

Runs as a post-sync pass from `calibre_sync` / `audiobookshelf_sync`.
The matcher opens each library's DB in turn, reads (author, title,
id) for every non-hidden book, normalizes, buckets by match_key, and
then persists per-bucket:

  * Bucket with 1 member (no cross-library twin) — no link row needed.
    Phase 5 omits "singleton" rows to keep the table compact; a later
    phase may add them for manual-link lookups.
  * Bucket with 2+ members — if any already has a `work_links` row,
    reuse that work_id; otherwise mint a new one. Insert membership
    rows for the rest (auto only — never stomp a manual link).

Manual links (`link_source='manual'`) are respected: the matcher will
not re-home a manually-linked row, even if its bucket peer has a
different auto-assigned work_id. The frontend's "unlink" button is
the only way to undo a manual link.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from app import state
from app.database import get_db
from app.discovery.database import get_db as get_library_db
from app.works import storage
from app.works.normalize import match_keys

_log = logging.getLogger("seshat.works.matcher")


@dataclass(frozen=True)
class MatchResult:
    works_created: int
    links_added: int
    links_skipped_manual: int
    # Auto rows dropped at rebuild start because the book's current
    # normalized (author, title) key no longer agrees with the work's
    # prevailing key (e.g. ABS's matcher renamed the file, shifting the
    # key out from under an existing link).
    stale_auto_removed: int
    # Link rows for books that have disappeared from their source
    # library entirely (user deleted from Calibre / ABS).
    orphans_pruned: int
    total_bucketed: int


@dataclass(frozen=True)
class _BookRow:
    library_slug: str
    book_id: int
    content_type: str
    # Every match_key variant this book should be indexed under. Two
    # books share a bucket if ANY of their keys overlap; see the
    # connected-components walk inside `rebuild_matches`.
    keys: tuple[str, ...]

    @property
    def key(self) -> str:
        """Primary (strict) key — the first variant returned by match_keys.

        Kept for backwards compatibility with the stale-link cleanup
        pass, which picks a canonical key per existing work. Multi-key
        bucketing uses `keys` directly, not this.
        """
        return self.keys[0] if self.keys else ""


async def rebuild_matches(
    libraries: Optional[list[dict]] = None,
) -> MatchResult:
    """Re-match every discovered library from scratch.

    Idempotent — re-running leaves `work_links` in the same shape
    provided the source library data hasn't changed. `auto` rows may
    be added or (via reconcile) removed; `manual` rows are always
    preserved.

    `libraries` defaults to `state._discovered_libraries`. Callers
    with a specific library list (tests, one-off rescans after a manual
    link) can pass their own to scope the run.
    """
    libs = libraries if libraries is not None else list(state._discovered_libraries)
    if len(libs) < 2:
        # A single library has nothing to link across — just reconcile
        # orphans and exit. Two-library scans (the normal ABS + Calibre
        # case) are where all the actual work happens.
        _log.info("works matcher: skipping — fewer than 2 libraries")
        return MatchResult(0, 0, 0, 0, 0, 0)

    books = await _collect_all_books(libs)
    current_key_by_pair = {
        (b.library_slug, b.book_id): b.key for b in books
    }
    buckets = _bucket_by_connected_components(books)

    works_created = 0
    links_added = 0
    links_skipped_manual = 0
    total_bucketed = 0

    db = await get_db()
    try:
        # Step 0: drop auto link rows whose book's current match_key no
        # longer agrees with the work's prevailing key. Handles the
        # "ABS renamed an audiobook" case where the old auto-link
        # becomes stale — cleaned up before re-bucketing so the
        # renamed book can re-home into a fresh work.
        stale_auto_removed = await _cleanup_stale_auto_links(
            db, current_key_by_pair,
        )
        for members in buckets:
            if len(members) < 2:
                continue  # singletons aren't worth a work_links row
            total_bucketed += len(members)

            # Pull any pre-existing link rows for this bucket — we want
            # to reuse an existing work_id rather than minting a new one.
            existing_by_pair: dict[tuple[str, int], dict] = {}
            for m in members:
                row = await (await db.execute(
                    "SELECT work_id, link_source FROM work_links "
                    "WHERE library_slug = ? AND book_id = ?",
                    (m.library_slug, m.book_id),
                )).fetchone()
                if row is not None:
                    existing_by_pair[(m.library_slug, m.book_id)] = {
                        "work_id": row["work_id"],
                        "link_source": row["link_source"],
                    }

            # Pick the canonical work_id:
            #   1. Any manual link in the bucket wins (don't stomp user intent)
            #   2. Else the first existing auto link
            #   3. Else a fresh UUID
            canonical_work_id = None
            for existing in existing_by_pair.values():
                if existing["link_source"] == "manual":
                    canonical_work_id = existing["work_id"]
                    break
            if canonical_work_id is None:
                for existing in existing_by_pair.values():
                    canonical_work_id = existing["work_id"]
                    break
            if canonical_work_id is None:
                canonical_work_id = storage.generate_work_id()
                works_created += 1

            # Insert / re-point each member:
            for m in members:
                key_pair = (m.library_slug, m.book_id)
                existing = existing_by_pair.get(key_pair)
                if existing is None:
                    await db.execute(
                        "INSERT INTO work_links "
                        "(work_id, library_slug, book_id, content_type, "
                        " link_source) VALUES (?, ?, ?, ?, 'auto')",
                        (canonical_work_id, m.library_slug, m.book_id,
                         m.content_type),
                    )
                    links_added += 1
                elif existing["link_source"] == "manual":
                    # Never alter a manual link. If the bucket disagrees
                    # with it, that's the user's call to make via the UI.
                    if existing["work_id"] != canonical_work_id:
                        links_skipped_manual += 1
                elif existing["work_id"] != canonical_work_id:
                    await db.execute(
                        "UPDATE work_links SET work_id = ?, link_source = 'auto' "
                        "WHERE library_slug = ? AND book_id = ?",
                        (canonical_work_id, m.library_slug, m.book_id),
                    )

        await db.commit()

        # Reconcile: drop link rows for books that are no longer in
        # their source library (user deleted from Calibre / ABS). We
        # already have the live book-id lists from _collect_all_books.
        orphans_pruned = 0
        per_lib_live: dict[str, list[int]] = {}
        for b in books:
            per_lib_live.setdefault(b.library_slug, []).append(b.book_id)
        for lib in libs:
            slug = lib.get("slug")
            if not slug:
                continue
            live_ids = per_lib_live.get(slug, [])
            if not live_ids:
                # Same safety net as calibre_sync: empty lives probably
                # means a transient read error, not a deliberate wipe.
                continue
            orphans_pruned += await storage.reconcile_library(
                slug, live_ids, db=db,
            )
        await db.commit()

    finally:
        await db.close()

    result = MatchResult(
        works_created=works_created,
        links_added=links_added,
        links_skipped_manual=links_skipped_manual,
        stale_auto_removed=stale_auto_removed,
        orphans_pruned=orphans_pruned,
        total_bucketed=total_bucketed,
    )
    _log.info(
        "works matcher: +%d works, +%d links, %d manual skipped, "
        "%d stale auto dropped, %d orphan(s) pruned",
        result.works_created, result.links_added,
        result.links_skipped_manual, result.stale_auto_removed,
        result.orphans_pruned,
    )
    return result


async def _cleanup_stale_auto_links(
    db, current_key_by_pair: dict[tuple[str, int], str],
) -> int:
    """Drop auto rows whose book's current key disagrees with the work's.

    For each existing work_id:
      - If any manual member exists, its current key is canonical. The
        manual row itself is never dropped — user intent wins — but
        auto rows whose key doesn't match the manual key get cleared.
      - Otherwise, the plurality current key (strictly more members
        than any other) is canonical. Auto rows in the minority get
        dropped.
      - Otherwise (no consensus: 2 members with 2 different keys, or
        a perfect tie across more members), all auto rows are dropped
        — no basis to declare a winner.

    Books that have disappeared from their library (no key available)
    are passed over; the `reconcile_library` pass handles their
    removal as "orphans" with its own zero-count safety net.
    """
    rows = await (await db.execute(
        "SELECT id, work_id, library_slug, book_id, link_source "
        "FROM work_links"
    )).fetchall()
    by_work: dict[str, list[dict]] = {}
    for r in rows:
        by_work.setdefault(r["work_id"], []).append({
            "id": r["id"],
            "library_slug": r["library_slug"],
            "book_id": r["book_id"],
            "link_source": r["link_source"],
        })

    removed = 0
    for _work_id, members in by_work.items():
        if len(members) < 2:
            # 1-member works have nothing to compare against. Stale
            # singletons from prior runs stay put — if the user wants
            # them gone they can hit DELETE /link/{lib}/{id}.
            continue

        # Canonical key selection.
        canonical_key: str | None = None
        for m in members:
            if m["link_source"] != "manual":
                continue
            k = current_key_by_pair.get(
                (m["library_slug"], m["book_id"]), "",
            )
            if k:
                canonical_key = k
                break

        if canonical_key is None:
            key_counts: dict[str, int] = {}
            for m in members:
                k = current_key_by_pair.get(
                    (m["library_slug"], m["book_id"]), "",
                )
                if not k:
                    continue
                key_counts[k] = key_counts.get(k, 0) + 1
            if key_counts:
                sorted_keys = sorted(
                    key_counts.items(), key=lambda kv: -kv[1],
                )
                # Strictly greater than runner-up. Perfect ties leave
                # canonical None → all autos get dropped below.
                if len(sorted_keys) == 1 or sorted_keys[0][1] > sorted_keys[1][1]:
                    canonical_key = sorted_keys[0][0]

        # Evaluate each auto member.
        for m in members:
            if m["link_source"] != "auto":
                continue
            k = current_key_by_pair.get(
                (m["library_slug"], m["book_id"]), "",
            )
            if not k:
                # Book probably gone from its library — let the orphan
                # reconcile pass handle it rather than dropping here.
                continue
            if canonical_key is None or k != canonical_key:
                await db.execute(
                    "DELETE FROM work_links WHERE id = ?", (m["id"],),
                )
                removed += 1

    if removed:
        await db.commit()
    return removed


async def _collect_all_books(libraries: list[dict]) -> list[_BookRow]:
    """Read (author_name, title, book_id, content_type) from each library.

    Each book carries every normalized match_key variant it should be
    indexed under — the matcher's connected-component walk uses the
    full list. `match_key` (singular) is still exposed on `_BookRow.key`
    as the "primary" key for the stale-cleanup pass.
    """
    rows: list[_BookRow] = []
    for lib in libraries:
        slug = lib.get("slug")
        content_type = lib.get("content_type") or "ebook"
        if not slug:
            continue
        db = await get_library_db(slug)
        try:
            cur = await db.execute(
                "SELECT b.id AS book_id, b.title, a.name AS author_name "
                "FROM books b "
                "JOIN book_authors bpa ON bpa.book_id = b.id AND bpa.position = 0 "
                "JOIN authors a ON a.id = bpa.author_id "
                "WHERE b.hidden = 0 AND b.owned = 1"
            )
            for r in await cur.fetchall():
                rows.append(_BookRow(
                    library_slug=slug,
                    book_id=r["book_id"],
                    content_type=content_type,
                    keys=tuple(match_keys(r["author_name"], r["title"])),
                ))
        finally:
            await db.close()
    return rows


def _bucket_by_connected_components(books: list[_BookRow]) -> list[list[_BookRow]]:
    """Group books into components where any shared key joins them.

    Builds an index `key -> [books with that key]`, then BFS-walks
    from each unvisited book to collect everything reachable via any
    shared key. Books with no keys (unscannable title/author) become
    their own singleton "component" which the bucket loop skips.

    The BFS is O(N·K) where K is the average number of keys per book
    (1 or 2 in practice); well below the couple-thousand-row threshold
    where this would matter.
    """
    key_index: dict[str, list[_BookRow]] = {}
    for b in books:
        for k in b.keys:
            key_index.setdefault(k, []).append(b)

    visited: set[int] = set()
    components: list[list[_BookRow]] = []
    for start in books:
        if id(start) in visited:
            continue
        if not start.keys:
            visited.add(id(start))
            continue
        component: list[_BookRow] = []
        queue: list[_BookRow] = [start]
        while queue:
            current = queue.pop()
            if id(current) in visited:
                continue
            visited.add(id(current))
            component.append(current)
            for k in current.keys:
                for neighbor in key_index.get(k, ()):
                    if id(neighbor) not in visited:
                        queue.append(neighbor)
        if component:
            components.append(component)
    return components
