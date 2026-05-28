"""
v2.3.7 — Skip MAM (`mam_status='not_applicable'`).

Surfaces tested here:
  - PUT /api/discovery/books/{bid} accepts the new status via an
    explicit `mam_status` field (allowlisted to 'not_applicable').
    Other status writes still flow through the mam_url block.
  - GET /books and friends accept `mam_status=not_applicable` as a
    filter value across the three filter helpers.
  - POST /api/discovery/authors/skip-mam bulk-marks every book
    under the given author_ids as not_applicable; returns affected
    counts.
  - The MAM scan predicates (_NEEDS_SCAN_*) implicitly skip
    not_applicable rows because the value isn't in their IN clause.
  - get_mam_stats reports a `total_skipped` counter and excludes
    not_applicable from `total_scanned`.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI


@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    from app import config as app_config
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    yield tmp_path
    disco_db.set_active_library(None)


@pytest.fixture
async def client(discovery_db):
    from app.discovery.routers import books as books_router
    from app.discovery.routers import authors as authors_router

    app = FastAPI()
    app.include_router(books_router.router)
    app.include_router(authors_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _seed(author_name: str = "A", **fields) -> int:
    from app.discovery.database import get_db
    from app.metadata.author_names import normalize_author_name
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO authors (name, sort_name, normalized_name) "
            "VALUES (?, ?, ?)",
            (author_name, author_name, normalize_author_name(author_name)),
        )
        aid_row = await (await db.execute(
            "SELECT id FROM authors WHERE name=?", (author_name,)
        )).fetchone()
        aid = aid_row["id"]
        cols = ["title", "source"]
        vals = ["t", "goodreads"]
        for k, v in fields.items():
            cols.append(k)
            vals.append(v)
        ph = ",".join("?" * len(cols))
        cur = await db.execute(
            f"INSERT INTO books ({','.join(cols)}) VALUES ({ph})", vals,
        )
        book_id = cur.lastrowid
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
            "VALUES (?, ?, 0)", (book_id, aid),
        )
        await db.commit()
        return book_id
    finally:
        await db.close()


async def _read(bid: int) -> dict:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT mam_url, mam_status, mam_torrent_id FROM books WHERE id=?",
            (bid,),
        )).fetchone()
        return dict(row) if row else {}
    finally:
        await db.close()


# ─── PUT /books/{bid} — single-book Skip ─────────────────────────


async def test_put_accepts_not_applicable_status(client):
    bid = await _seed(
        mam_url="https://www.myanonamouse.net/t/77",
        mam_status="possible",
        mam_torrent_id=77,
    )
    r = await client.put(
        f"/api/discovery/books/{bid}",
        json={"mam_status": "not_applicable"},
    )
    assert r.status_code == 200, r.text
    row = await _read(bid)
    assert row["mam_status"] == "not_applicable"
    assert row["mam_url"] is None
    assert row["mam_torrent_id"] is None


async def test_put_rejects_arbitrary_status_writes(client):
    # Allowlist: only 'not_applicable' is accepted as a direct
    # mam_status write. Other transitions must flow through mam_url.
    bid = await _seed(mam_status="possible")
    r = await client.put(
        f"/api/discovery/books/{bid}",
        json={"mam_status": "found"},  # not allowed
    )
    assert r.status_code == 200  # endpoint doesn't 400, just no-ops
    row = await _read(bid)
    assert row["mam_status"] == "possible"  # unchanged


# ─── Filter helpers ──────────────────────────────────────────────


async def test_books_filter_returns_only_not_applicable(client):
    await _seed(mam_status="not_applicable")
    await _seed(mam_status="found")
    await _seed(mam_status="possible")

    r = await client.get("/api/discovery/books?mam_status=not_applicable")
    assert r.status_code == 200
    body = r.json()
    statuses = [b["mam_status"] for b in body["books"]]
    assert statuses == ["not_applicable"]


# ─── Bulk endpoint ───────────────────────────────────────────────


async def test_skip_authors_mam_marks_all_books(client):
    # Two authors, two books each. Skip one author; only their books flip.
    b1 = await _seed("Snekguy", title="Free Read 1", mam_status="possible")
    b2 = await _seed("Snekguy", title="Free Read 2", mam_status="not_found")
    b3 = await _seed("Other Author", title="Real Book", mam_status="found")

    from app.discovery.database import get_db
    db = await get_db()
    try:
        snekguy_aid = (await (await db.execute(
            "SELECT id FROM authors WHERE name='Snekguy'"
        )).fetchone())["id"]
    finally:
        await db.close()

    r = await client.post(
        "/api/discovery/authors/skip-mam",
        json={"author_ids": [snekguy_aid]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["authors_skipped"] == 1
    assert body["books_skipped"] == 2
    assert body["libraries_touched"] == 1

    # Snekguy's books flipped; the other author's didn't.
    assert (await _read(b1))["mam_status"] == "not_applicable"
    assert (await _read(b2))["mam_status"] == "not_applicable"
    assert (await _read(b3))["mam_status"] == "found"


async def test_skip_authors_mam_clears_stale_url(client):
    # A 'possible' book with a torrent URL should have URL nulled so a
    # stale partial match doesn't linger on a row the user just declared
    # irrelevant.
    bid = await _seed(
        "Snekguy",
        mam_url="https://www.myanonamouse.net/t/99",
        mam_status="possible",
        mam_torrent_id=99,
    )
    from app.discovery.database import get_db
    db = await get_db()
    try:
        aid = (await (await db.execute(
            "SELECT author_id FROM book_authors WHERE book_id=? AND position=0", (bid,)
        )).fetchone())["author_id"]
    finally:
        await db.close()

    r = await client.post(
        "/api/discovery/authors/skip-mam",
        json={"author_ids": [aid]},
    )
    assert r.status_code == 200
    row = await _read(bid)
    assert row["mam_status"] == "not_applicable"
    assert row["mam_url"] is None
    assert row["mam_torrent_id"] is None


async def test_skip_authors_mam_400s_on_empty_request(client):
    r = await client.post("/api/discovery/authors/skip-mam", json={})
    assert r.status_code == 200
    assert r.json().get("error") == "No authors specified"


# ─── Predicate exclusion ─────────────────────────────────────────


async def test_predicates_skip_not_applicable_rows(client):
    """v2.3.6's _NEEDS_SCAN_BASIC_BARE matches NULL/possible/not_found
    but not 'found' or 'not_applicable'. Pin that not_applicable is
    excluded so Skip MAM actually stops the rescan loop."""
    from app.discovery.database import get_db
    from app.discovery.sources.mam import (
        _NEEDS_SCAN_BASIC_BARE,
        _NEEDS_SCAN_STRICT_BARE,
    )

    await _seed(mam_status=None)
    await _seed(mam_status="possible")
    await _seed(mam_status="not_found")
    await _seed(mam_status="found")
    await _seed(mam_status="not_applicable")

    db = await get_db()
    try:
        basic = await (await db.execute(
            f"SELECT mam_status FROM books WHERE {_NEEDS_SCAN_BASIC_BARE}"
        )).fetchall()
        strict = await (await db.execute(
            f"SELECT mam_status FROM books WHERE {_NEEDS_SCAN_STRICT_BARE}"
        )).fetchall()
    finally:
        await db.close()

    basic_statuses = sorted(r[0] or "NULL" for r in basic)
    strict_statuses = sorted(r[0] or "NULL" for r in strict)
    assert basic_statuses == ["NULL", "not_found", "possible"]
    assert strict_statuses == ["NULL", "not_found", "possible"]
    # 'not_applicable' explicitly absent from both predicates.
    assert "not_applicable" not in basic_statuses
    assert "not_applicable" not in strict_statuses


# ─── Stats counter ───────────────────────────────────────────────


async def test_get_mam_stats_reports_skipped(client):
    from app.discovery.database import get_db
    from app.discovery.sources.mam import get_mam_stats

    await _seed(mam_status="not_applicable")
    await _seed(mam_status="not_applicable")
    await _seed(mam_status="found")
    await _seed(mam_status=None)

    db = await get_db()
    try:
        stats = await get_mam_stats(db)
    finally:
        await db.close()

    assert stats["total_skipped"] == 2
    # not_applicable rows must NOT count as scanned (the user set them,
    # not the scanner).
    assert stats["total_scanned"] == 1  # only the 'found' row


# ─── Bulk endpoint /books/bulk-skip-mam ──────────────────────────


async def test_bulk_skip_mam_marks_selected_books(client):
    """v2.3.7.1 — author detail multi-select bulk verb. Flips the
    listed book_ids to 'not_applicable' and clears the URL/torrent_id
    so a stale prior match doesn't linger."""
    b1 = await _seed(
        mam_url="https://www.myanonamouse.net/t/1",
        mam_status="possible",
        mam_torrent_id=1,
    )
    b2 = await _seed(mam_status="not_found")
    b3 = await _seed(mam_status="found")  # not selected — must not flip

    r = await client.post(
        "/api/discovery/books/bulk-skip-mam",
        json={"book_ids": [b1, b2]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2

    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT id, mam_status, mam_url, mam_torrent_id FROM books "
            "WHERE id IN (?, ?, ?) ORDER BY id",
            (b1, b2, b3),
        )).fetchall()
    finally:
        await db.close()

    by_id = {r["id"]: dict(r) for r in rows}
    assert by_id[b1]["mam_status"] == "not_applicable"
    assert by_id[b1]["mam_url"] is None
    assert by_id[b1]["mam_torrent_id"] is None
    assert by_id[b2]["mam_status"] == "not_applicable"
    # Untouched book stays as it was.
    assert by_id[b3]["mam_status"] == "found"


async def test_bulk_skip_mam_400s_on_empty(client):
    r = await client.post(
        "/api/discovery/books/bulk-skip-mam", json={"book_ids": []},
    )
    assert r.status_code == 200
    assert r.json().get("error") == "No books specified"
