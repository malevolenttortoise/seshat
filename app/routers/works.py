"""
Cross-library work-linking endpoints.

    GET    /api/v1/works                      — list works (paginated)
    GET    /api/v1/works/{work_id}            — single work + its books
    POST   /api/v1/works/rebuild              — re-run the auto-matcher
    POST   /api/v1/works/link                 — manually merge books into a work
    DELETE /api/v1/works/link/{library}/{id}  — unlink a single book
    GET    /api/v1/works/author-preferences           — list per-author prefs
    GET    /api/v1/works/author-preferences/{author}  — single pref (404 if unset)
    PUT    /api/v1/works/author-preferences/{author}  — set pref
    DELETE /api/v1/works/author-preferences/{author}  — clear pref

Manual links (created via POST /link) are never overwritten by the
auto-matcher — see `app/works/matcher.py` for the precedence rule.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field

from app.works import matcher, preferences, storage
from app.works.storage import WorkLink

_log = logging.getLogger("seshat.routers.works")

router = APIRouter(prefix="/api/v1/works", tags=["works"])


# ─── Response models ──────────────────────────────────────────

class WorkLinkOut(BaseModel):
    id: int
    work_id: str
    library_slug: str
    book_id: int
    content_type: str
    link_source: str
    created_at: float
    # Denormalized book metadata for UI rendering. Populated by joining
    # against the per-library discovery DB at list/read time — None when
    # the book has been deleted from its source library since the link
    # was created (reconcile hasn't caught up yet).
    title: Optional[str] = None
    author_name: Optional[str] = None
    cover_url: Optional[str] = None
    series_name: Optional[str] = None
    series_index: Optional[float] = None


class WorkSummary(BaseModel):
    work_id: str
    links: list[WorkLinkOut]


class WorksListResponse(BaseModel):
    total: int
    items: list[WorkSummary]


class LinkRequest(BaseModel):
    """Manually merge a set of books into a (possibly-new) work."""
    work_id: Optional[str] = Field(
        default=None,
        description="Existing work_id to merge into. Omit to mint a new one.",
    )
    members: list[dict] = Field(
        ...,
        description=(
            "List of {library_slug, book_id, content_type} dicts to link."
        ),
    )


class RebuildResult(BaseModel):
    works_created: int
    links_added: int
    links_skipped_manual: int
    stale_auto_removed: int
    orphans_pruned: int
    total_bucketed: int


class AuthorPrefOut(BaseModel):
    normalized_name: str
    display_name: str
    tracking_mode: str
    updated_at: float


class AuthorPrefRequest(BaseModel):
    tracking_mode: str = Field(
        ..., description='"ebook" | "audiobook" | "both"'
    )


class SimpleOk(BaseModel):
    ok: bool


# ─── Works ────────────────────────────────────────────────────

@router.get("", response_model=WorksListResponse)
async def list_works(
    library_slug: Optional[str] = Query(default=None),
    content_type: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> WorksListResponse:
    """List works, filtered by library/content_type."""
    work_ids = await storage.list_works(
        library_slug=library_slug, content_type=content_type,
    )
    total = len(work_ids)
    window = work_ids[offset:offset + limit]
    items: list[WorkSummary] = []
    for wid in window:
        links = await storage.get_work_members(wid)
        items.append(WorkSummary(
            work_id=wid,
            links=await _hydrate_links(links),
        ))
    return WorksListResponse(total=total, items=items)


@router.post("/rebuild", response_model=RebuildResult)
async def rebuild_works() -> RebuildResult:
    """Re-run the auto-matcher across every discovered library.

    Safe to call mid-day — auto links get refreshed from scratch, but
    manual links are preserved. Mostly useful after the user has
    tweaked their library contents and wants an immediate refresh
    without waiting for the next scheduled sync.
    """
    result = await matcher.rebuild_matches()
    return RebuildResult(
        works_created=result.works_created,
        links_added=result.links_added,
        links_skipped_manual=result.links_skipped_manual,
        stale_auto_removed=result.stale_auto_removed,
        orphans_pruned=result.orphans_pruned,
        total_bucketed=result.total_bucketed,
    )


@router.post("/link", response_model=WorkSummary)
async def manual_link(req: LinkRequest) -> WorkSummary:
    """Manually merge `members` into `work_id`.

    `work_id` omitted → a new one is minted. Existing members get
    re-homed to the target work_id; their `link_source` is set to
    "manual" so the auto-matcher won't stomp them.
    """
    if not req.members:
        raise HTTPException(status_code=400, detail="members list is empty")
    for m in req.members:
        for f in ("library_slug", "book_id", "content_type"):
            if m.get(f) in (None, ""):
                raise HTTPException(
                    status_code=400,
                    detail=f"each member requires non-empty {f}",
                )

    target_work_id = req.work_id or storage.generate_work_id()

    # If the caller gave an existing work_id, validate it actually exists.
    if req.work_id:
        existing = await storage.get_work_members(req.work_id)
        if not existing:
            raise HTTPException(
                status_code=404,
                detail=f"work_id {req.work_id} not found",
            )

    from app.database import get_db
    db = await get_db()
    try:
        for m in req.members:
            # Upsert: if the (lib, book) row already exists, UPDATE
            # its work_id + flip to manual. Otherwise INSERT as manual.
            existing = await (await db.execute(
                "SELECT id FROM work_links WHERE library_slug = ? AND book_id = ?",
                (m["library_slug"], m["book_id"]),
            )).fetchone()
            if existing:
                await db.execute(
                    "UPDATE work_links SET work_id = ?, link_source = 'manual' "
                    "WHERE id = ?",
                    (target_work_id, existing["id"]),
                )
            else:
                await db.execute(
                    "INSERT INTO work_links "
                    "(work_id, library_slug, book_id, content_type, link_source) "
                    "VALUES (?, ?, ?, ?, 'manual')",
                    (
                        target_work_id, m["library_slug"],
                        int(m["book_id"]), m["content_type"],
                    ),
                )
        await db.commit()
    finally:
        await db.close()

    members = await storage.get_work_members(target_work_id)
    return WorkSummary(
        work_id=target_work_id,
        links=await _hydrate_links(members),
    )


@router.delete("/link/{library_slug}/{book_id}", response_model=SimpleOk)
async def unlink_book(
    library_slug: str = Path(...),
    book_id: int = Path(..., ge=1),
) -> SimpleOk:
    """Remove a single (library, book) from its work."""
    removed = await storage.unlink_book(library_slug, book_id)
    return SimpleOk(ok=removed)


# ─── Per-author format preferences ────────────────────────────

@router.get("/author-preferences", response_model=list[AuthorPrefOut])
async def list_author_prefs() -> list[AuthorPrefOut]:
    prefs = await preferences.list_preferences()
    return [_pref_to_out(p) for p in prefs]


@router.get(
    "/author-preferences/{author_name}", response_model=AuthorPrefOut,
)
async def get_author_pref(
    author_name: str = Path(...),
) -> AuthorPrefOut:
    pref = await preferences.get_preference(author_name)
    if pref is None:
        raise HTTPException(
            status_code=404, detail="no explicit preference set",
        )
    return _pref_to_out(pref)


@router.put(
    "/author-preferences/{author_name}", response_model=AuthorPrefOut,
)
async def set_author_pref(
    author_name: str,
    req: AuthorPrefRequest,
) -> AuthorPrefOut:
    try:
        await preferences.set_preference(author_name, req.tracking_mode)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    pref = await preferences.get_preference(author_name)
    # `get_preference` only returns None for empty normalized names —
    # we just wrote a row, so this is effectively an invariant.
    assert pref is not None
    return _pref_to_out(pref)


@router.delete(
    "/author-preferences/{author_name}", response_model=SimpleOk,
)
async def clear_author_pref(
    author_name: str = Path(...),
) -> SimpleOk:
    removed = await preferences.clear_preference(author_name)
    return SimpleOk(ok=removed)


# Must come AFTER every static-prefix route ("/rebuild", "/link/...",
# "/author-preferences/...") so FastAPI's first-match semantics don't
# silently swallow them as `work_id` values.
@router.get("/{work_id}", response_model=WorkSummary)
async def get_work(work_id: str = Path(...)) -> WorkSummary:
    links = await storage.get_work_members(work_id)
    if not links:
        raise HTTPException(status_code=404, detail="work not found")
    return WorkSummary(
        work_id=work_id,
        links=await _hydrate_links(links),
    )


# ─── Helpers ──────────────────────────────────────────────────

def _link_to_out(link: WorkLink) -> WorkLinkOut:
    return WorkLinkOut(
        id=link.id,
        work_id=link.work_id,
        library_slug=link.library_slug,
        book_id=link.book_id,
        content_type=link.content_type,
        link_source=link.link_source,
        created_at=link.created_at,
    )


async def _hydrate_links(links: list[WorkLink]) -> list[WorkLinkOut]:
    """Join links against per-library discovery DBs for display metadata.

    One aiosqlite connection per library-slug encountered — opened
    lazily and closed at the end. N+1 avoided by a single IN(...)
    query per library for all books in that library's subset.
    """
    from app.discovery.database import get_db as get_library_db

    by_library: dict[str, list[WorkLink]] = {}
    for link in links:
        by_library.setdefault(link.library_slug, []).append(link)

    metadata: dict[tuple[str, int], dict] = {}
    for slug, slug_links in by_library.items():
        ids = [str(link.book_id) for link in slug_links]
        placeholders = ",".join("?" * len(ids))
        try:
            db = await get_library_db(slug)
        except Exception:
            continue
        try:
            rows = await (await db.execute(
                f"SELECT b.id, b.title, b.cover_path, b.audiobookshelf_id, "
                f"a.name AS author_name, "
                f"s.name AS series_name, b.series_index "
                f"FROM books b "
                f"JOIN book_authors bpa ON bpa.book_id = b.id AND bpa.position = 0 "
                f"JOIN authors a ON a.id = bpa.author_id "
                f"LEFT JOIN series s ON s.id = b.series_id "
                f"WHERE b.id IN ({placeholders})",
                ids,
            )).fetchall()
            for r in rows:
                cover_url = None
                # Cover endpoint handles both local files (Calibre) and
                # ABS proxy (audiobookshelf_id set, cover_path NULL).
                if r["cover_path"] or r["audiobookshelf_id"]:
                    cover_url = f"/api/discovery/covers/{slug}/{r['id']}"
                metadata[(slug, r["id"])] = {
                    "title": r["title"],
                    "author_name": r["author_name"],
                    "series_name": r["series_name"],
                    "series_index": r["series_index"],
                    "cover_url": cover_url,
                }
        finally:
            await db.close()

    out: list[WorkLinkOut] = []
    for link in links:
        base = _link_to_out(link)
        meta = metadata.get((link.library_slug, link.book_id))
        if meta:
            base = base.model_copy(update=meta)
        out.append(base)
    return out


def _pref_to_out(pref) -> AuthorPrefOut:
    return AuthorPrefOut(
        normalized_name=pref.normalized_name,
        display_name=pref.display_name,
        tracking_mode=pref.tracking_mode,
        updated_at=pref.updated_at,
    )
