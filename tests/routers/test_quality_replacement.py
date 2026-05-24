"""
Tests for the v2.26.0 replacement-opportunity + library-safety endpoints
on `app/routers/quality.py`.

Three endpoints:
  - GET    /api/quality/library-safety
  - GET    /api/quality/replacement-opportunities (+ /counts)
  - PATCH  /api/quality/replacement-opportunities/{id}

The opportunity endpoints walk the real storage helpers via a temp DB;
the safety endpoint exercises the discovered-libraries + settings
composition. The PATCH endpoint pins the 400/404 error contract the
UI relies on.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import state
from app.quality.opportunities import record_opportunity
from app.routers.quality import router as quality_router


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(quality_router)
    return app


@pytest.fixture
async def client(temp_db):
    """ASGI client + temp app DB. `temp_db` patches APP_DB_PATH already."""
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ─── library-safety ──────────────────────────────────────────


@pytest.fixture
def _libraries(monkeypatch):
    """Two libraries: one safe (calibre), one overlap (abs)."""
    libs = [
        {
            "slug": "calibre-main", "name": "Calibre",
            "app_type": "calibre", "content_type": "ebook",
            "library_path": "/calibre-library",
        },
        {
            "slug": "abs-audiobooks", "name": "Audiobookshelf",
            "app_type": "audiobookshelf", "content_type": "audiobook",
            "library_path": "/downloads/audiobooks",  # OVERLAP with qBit
        },
    ]
    monkeypatch.setattr(state, "_discovered_libraries", libs)
    return libs


@pytest.fixture
def _settings(monkeypatch):
    """Stub load_settings() to a minimal known dict."""
    import app.routers.quality as qrouter

    fake = {
        "local_path_prefix": "/downloads",
        "active_replacement_enabled_by_slug": {
            "calibre-main": True,
            "abs-audiobooks": True,  # toggled on but OVERLAP hard-disables
        },
    }
    monkeypatch.setattr(qrouter, "load_settings", lambda: fake)
    return fake


async def test_library_safety_returns_one_entry_per_library(
    client, _libraries, _settings,
):
    resp = await client.get("/api/quality/library-safety")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["libraries"]) == 2
    by_slug = {row["slug"]: row for row in body["libraries"]}
    assert by_slug["calibre-main"]["safety"] == "safe"
    assert by_slug["calibre-main"]["effective"] is True
    assert by_slug["abs-audiobooks"]["safety"] == "overlap"
    assert by_slug["abs-audiobooks"]["effective"] is False
    assert by_slug["abs-audiobooks"]["enabled"] is True


async def test_library_safety_empty_when_no_libraries(client, monkeypatch):
    monkeypatch.setattr(state, "_discovered_libraries", [])
    import app.routers.quality as qrouter
    monkeypatch.setattr(qrouter, "load_settings", lambda: {})
    resp = await client.get("/api/quality/library-safety")
    assert resp.status_code == 200
    assert resp.json() == {"libraries": []}


# ─── replacement-opportunities GET ──────────────────────────


async def _seed_opportunity(db, **overrides):
    defaults = dict(
        candidate_grab_id=101,
        candidate_mam_torrent_id="9001",
        candidate_format="m4b",
        candidate_score=(0, 0),
        owned_library_slug="abs-audiobooks",
        owned_book_id=42,
        owned_mam_torrent_id="5000",
        owned_format="m4b",
        owned_score=(0, 3),
        media_type="audiobook",
    )
    defaults.update(overrides)
    await record_opportunity(db, **defaults)
    await db.commit()


async def test_get_opportunities_returns_detected_by_default(client):
    from app.database import get_db
    db = await get_db()
    try:
        await _seed_opportunity(db, candidate_grab_id=101)
        await _seed_opportunity(db, candidate_grab_id=102)
    finally:
        await db.close()

    resp = await client.get("/api/quality/replacement-opportunities")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["opportunities"]) == 2
    assert body["counts"] == {"detected": 2, "enacted": 0, "dismissed": 0}
    # Score tuples come back as lists (JSON has no tuple type).
    assert body["opportunities"][0]["candidate_score"] == [0, 0]


async def test_get_opportunities_filter_by_library_slug(client):
    from app.database import get_db
    db = await get_db()
    try:
        await _seed_opportunity(db, candidate_grab_id=101, owned_library_slug="lib-a")
        await _seed_opportunity(db, candidate_grab_id=102, owned_library_slug="lib-b")
    finally:
        await db.close()

    resp = await client.get(
        "/api/quality/replacement-opportunities?library_slug=lib-a",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["opportunities"]) == 1
    assert body["opportunities"][0]["owned_library_slug"] == "lib-a"


async def test_get_opportunities_status_empty_returns_all(client):
    from app.database import get_db
    db = await get_db()
    try:
        await _seed_opportunity(db, candidate_grab_id=101)
    finally:
        await db.close()

    # Mark it dismissed, then ask for "all statuses" via status=""
    resp = await client.get("/api/quality/replacement-opportunities")
    op_id = resp.json()["opportunities"][0]["id"]
    patch = await client.patch(
        f"/api/quality/replacement-opportunities/{op_id}",
        json={"status": "dismissed"},
    )
    assert patch.status_code == 200

    all_resp = await client.get(
        "/api/quality/replacement-opportunities?status=",
    )
    assert all_resp.status_code == 200
    assert len(all_resp.json()["opportunities"]) == 1


async def test_get_counts_endpoint(client):
    from app.database import get_db
    db = await get_db()
    try:
        await _seed_opportunity(db, candidate_grab_id=101)
        await _seed_opportunity(db, candidate_grab_id=102)
    finally:
        await db.close()

    resp = await client.get("/api/quality/replacement-opportunities/counts")
    assert resp.status_code == 200
    assert resp.json() == {"detected": 2, "enacted": 0, "dismissed": 0}


# ─── PATCH /replacement-opportunities/{id} ──────────────────


async def test_patch_dismiss_succeeds(client):
    from app.database import get_db
    db = await get_db()
    try:
        await _seed_opportunity(db)
    finally:
        await db.close()

    resp = await client.get("/api/quality/replacement-opportunities")
    op_id = resp.json()["opportunities"][0]["id"]

    patch = await client.patch(
        f"/api/quality/replacement-opportunities/{op_id}",
        json={"status": "dismissed"},
    )
    assert patch.status_code == 200
    body = patch.json()
    assert body["status"] == "dismissed"
    assert body["acted_by"] == "user"
    assert body["acted_at"] is not None


async def test_patch_undismiss_back_to_detected(client):
    from app.database import get_db
    db = await get_db()
    try:
        await _seed_opportunity(db)
    finally:
        await db.close()

    resp = await client.get("/api/quality/replacement-opportunities")
    op_id = resp.json()["opportunities"][0]["id"]
    await client.patch(
        f"/api/quality/replacement-opportunities/{op_id}",
        json={"status": "dismissed"},
    )

    # Undismiss
    patch = await client.patch(
        f"/api/quality/replacement-opportunities/{op_id}",
        json={"status": "detected"},
    )
    assert patch.status_code == 200
    assert patch.json()["status"] == "detected"


async def test_patch_enacted_rejected(client):
    """The 'enacted' status is reserved for the Phase 5b file-swap
    path — the UI must not be allowed to forge it."""
    from app.database import get_db
    db = await get_db()
    try:
        await _seed_opportunity(db)
    finally:
        await db.close()

    resp = await client.get("/api/quality/replacement-opportunities")
    op_id = resp.json()["opportunities"][0]["id"]
    patch = await client.patch(
        f"/api/quality/replacement-opportunities/{op_id}",
        json={"status": "enacted"},
    )
    assert patch.status_code == 400


async def test_patch_unknown_id_404(client):
    patch = await client.patch(
        "/api/quality/replacement-opportunities/99999",
        json={"status": "dismissed"},
    )
    assert patch.status_code == 404


async def test_patch_invalid_status_400(client):
    from app.database import get_db
    db = await get_db()
    try:
        await _seed_opportunity(db)
    finally:
        await db.close()

    resp = await client.get("/api/quality/replacement-opportunities")
    op_id = resp.json()["opportunities"][0]["id"]
    patch = await client.patch(
        f"/api/quality/replacement-opportunities/{op_id}",
        json={"status": "bogus"},
    )
    assert patch.status_code == 400
