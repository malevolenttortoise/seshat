"""v3.x (ADR-0015 slice 04) — HTTP-level tests for the source-ID
conflicts surface.

The two new endpoints expose what slice 01 records:

  - ``GET  /api/discovery/persons/source-id-conflicts``
  - ``POST /api/discovery/persons/source-id-conflicts/{id}/dismiss``

Slice 04 contract under test:

  - List returns open conflicts in the documented shape, newest first.
  - Dismiss flips ``status='dismissed'`` and is idempotent.
  - Dismissed rows disappear from the open list but remain in the
    table (recoverable / auditable).
  - The new specific routes register **before** ``GET /persons/{person_id}``
    (FastAPI greedy-match — v2.22.4 lesson).
  - Each list row carries the per-library author's display name
    (the conflict row stores the *incoming* name, which can differ
    from the on-file row — operators identify the conflicting row by
    its on-file name).
"""
from __future__ import annotations

import aiosqlite
import httpx
import pytest
from fastapi import FastAPI


_PER_LIB_AUTHORS_DDL = """
CREATE TABLE authors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sort_name TEXT NOT NULL DEFAULT '',
    normalized_name TEXT,
    bio TEXT,
    image_url TEXT,
    amazon_id TEXT,
    goodreads_id TEXT,
    hardcover_id TEXT,
    kobo_id TEXT,
    ibdb_id TEXT,
    google_books_id TEXT,
    openlibrary_id TEXT,
    audible_id TEXT,
    audiobookshelf_id TEXT,
    fictiondb_id TEXT,
    calibre_id INTEGER,
    UNIQUE(name)
);
"""


@pytest.fixture
async def conflicts_env(tmp_path, monkeypatch):
    """Wire DATA_DIR / APP_DB_PATH onto a tmp_path; create per-library
    DB(s); init the global schema (which carries the
    ``author_source_id_conflicts`` table)."""
    from app import config, database
    from app.discovery import author_identity

    global_path = tmp_path / "seshat.db"
    monkeypatch.setattr(config, "APP_DB_PATH", global_path)
    monkeypatch.setattr(database, "APP_DB_PATH", global_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(author_identity, "DATA_DIR", tmp_path)

    await database.init_db()

    # One per-library DB with two real authors so the on_file_name
    # join has something to find.
    slug = "calibre-library"
    per_lib_path = tmp_path / f"seshat_{slug}.db"
    db = await aiosqlite.connect(str(per_lib_path))
    try:
        await db.executescript(_PER_LIB_AUTHORS_DDL)
        await db.execute(
            "INSERT INTO authors "
            "(id, name, sort_name, normalized_name, goodreads_id) "
            "VALUES (11, 'Robert Heinlein', 'Heinlein, Robert', "
            "        'robert heinlein', 'GR-CANONICAL')"
        )
        await db.execute(
            "INSERT INTO authors "
            "(id, name, sort_name, normalized_name, amazon_id) "
            "VALUES (12, 'Octavia Butler', 'Butler, Octavia', "
            "        'octavia butler', 'B00AZ-CANONICAL')"
        )
        await db.commit()
    finally:
        await db.close()

    async def insert_conflict(
        *,
        library_slug: str = slug,
        author_id: int,
        source: str,
        existing_id: str,
        incoming_id: str,
        incoming_name: str | None = None,
        status: str = "open",
    ) -> int:
        gdb = await aiosqlite.connect(str(global_path))
        try:
            cur = await gdb.execute(
                "INSERT INTO author_source_id_conflicts "
                "(library_slug, author_id, source, existing_id, "
                " incoming_id, incoming_name, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (library_slug, author_id, source, existing_id,
                 incoming_id, incoming_name, status),
            )
            await gdb.commit()
            return cur.lastrowid
        finally:
            await gdb.close()

    async def conflict_status(conflict_id: int) -> str | None:
        gdb = await aiosqlite.connect(str(global_path))
        gdb.row_factory = aiosqlite.Row
        try:
            row = await (await gdb.execute(
                "SELECT status FROM author_source_id_conflicts WHERE id = ?",
                (conflict_id,),
            )).fetchone()
            return row["status"] if row else None
        finally:
            await gdb.close()

    yield {
        "slug": slug,
        "insert_conflict": insert_conflict,
        "conflict_status": conflict_status,
    }


@pytest.fixture
async def client(conflicts_env):
    from app.discovery.routers.authors import router

    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


# ─── 1. List endpoint — happy path ─────────────────────────────


async def test_list_returns_open_conflicts_with_on_file_name(
    client, conflicts_env,
):
    """The list endpoint returns each open conflict with its full
    documented shape, and the on_file_name comes from the per-library
    author row (not from the conflict row's incoming_name)."""
    cid = await conflicts_env["insert_conflict"](
        author_id=11, source="goodreads",
        existing_id="GR-CANONICAL", incoming_id="GR-OTHER-99",
        incoming_name="Robert A. Heinlein",
    )
    r = await client.get("/api/discovery/persons/source-id-conflicts")
    assert r.status_code == 200
    body = r.json()
    assert "conflicts" in body
    assert len(body["conflicts"]) == 1
    c = body["conflicts"][0]
    assert c["id"] == cid
    assert c["library_slug"] == conflicts_env["slug"]
    assert c["author_id"] == 11
    # On-file name comes from the per-library DB (NOT the conflict's
    # incoming_name).
    assert c["on_file_name"] == "Robert Heinlein"
    assert c["source"] == "goodreads"
    assert c["existing_id"] == "GR-CANONICAL"
    assert c["incoming_id"] == "GR-OTHER-99"
    assert c["incoming_name"] == "Robert A. Heinlein"
    assert "first_seen_at" in c
    assert "last_seen_at" in c


async def test_list_excludes_dismissed_conflicts(client, conflicts_env):
    """Dismissed rows must NOT appear on the open list (they remain
    in the table for audit / a future "show dismissed" toggle)."""
    cid_open = await conflicts_env["insert_conflict"](
        author_id=11, source="goodreads",
        existing_id="GR-CANONICAL", incoming_id="GR-OPEN",
    )
    cid_dismissed = await conflicts_env["insert_conflict"](
        author_id=12, source="amazon",
        existing_id="B00AZ-CANONICAL", incoming_id="B00AZ-OLD",
        status="dismissed",
    )
    r = await client.get("/api/discovery/persons/source-id-conflicts")
    body = r.json()
    ids = [c["id"] for c in body["conflicts"]]
    assert cid_open in ids
    assert cid_dismissed not in ids


async def test_list_orders_by_last_seen_desc(client, conflicts_env):
    """Newest-active conflict first — operator triage uses
    last_seen_at as the freshness signal."""
    cid_a = await conflicts_env["insert_conflict"](
        author_id=11, source="goodreads",
        existing_id="GR-CANONICAL", incoming_id="GR-A",
    )
    cid_b = await conflicts_env["insert_conflict"](
        author_id=12, source="amazon",
        existing_id="B00AZ-CANONICAL", incoming_id="B00AZ-B",
    )
    r = await client.get("/api/discovery/persons/source-id-conflicts")
    ids_in_order = [c["id"] for c in r.json()["conflicts"]]
    # SQLite strftime('%s','now') is 1-sec resolution; on a fast test
    # both rows share a timestamp. The endpoint then falls back to
    # ``id DESC`` — cid_b > cid_a → cid_b first.
    assert ids_in_order == [cid_b, cid_a]


async def test_list_empty_returns_empty_conflicts(client):
    r = await client.get("/api/discovery/persons/source-id-conflicts")
    assert r.status_code == 200
    assert r.json() == {"conflicts": []}


# ─── 2. Dismiss endpoint ───────────────────────────────────────


async def test_dismiss_flips_status(client, conflicts_env):
    cid = await conflicts_env["insert_conflict"](
        author_id=11, source="goodreads",
        existing_id="GR-CANONICAL", incoming_id="GR-DISMISS-ME",
    )
    r = await client.post(
        f"/api/discovery/persons/source-id-conflicts/{cid}/dismiss"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["already_dismissed"] is False
    assert await conflicts_env["conflict_status"](cid) == "dismissed"


async def test_dismiss_is_idempotent(client, conflicts_env):
    cid = await conflicts_env["insert_conflict"](
        author_id=11, source="goodreads",
        existing_id="GR-CANONICAL", incoming_id="GR-DISMISS-AGAIN",
        status="dismissed",
    )
    r = await client.post(
        f"/api/discovery/persons/source-id-conflicts/{cid}/dismiss"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["already_dismissed"] is True
    assert await conflicts_env["conflict_status"](cid) == "dismissed"


async def test_dismiss_missing_returns_404(client):
    r = await client.post(
        "/api/discovery/persons/source-id-conflicts/99999/dismiss"
    )
    assert r.status_code == 404


# ─── 3. Route ordering regression ──────────────────────────────


async def test_specific_route_registered_before_parameterized(client):
    """The list path must NOT 422 against the parameterized
    `/persons/{person_id}` route's int validation (v2.22.4 lesson).
    A 422 here would mean a FastAPI ordering regression."""
    r = await client.get("/api/discovery/persons/source-id-conflicts")
    assert r.status_code == 200
    assert r.status_code != 422
