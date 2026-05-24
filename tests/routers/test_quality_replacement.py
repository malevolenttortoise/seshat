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


# ─── Phase 5b — enact / restore endpoints ────────────────────


def _stub_enact(monkeypatch, *, status, **kwargs):
    """Patch the router's `enact_opportunity` to return a canned
    EnactmentResult so we can drive each HTTP code path without
    touching the orchestrator's internals (the orchestrator + its
    sink integrations are covered by their own tests)."""
    from app.orchestrator.active_replacement import EnactmentResult
    import app.routers.quality as qrouter

    async def fake(db, opportunity_id, *, acted_by=None, settings=None, libraries=None):
        return EnactmentResult(
            status=status,
            opportunity_id=opportunity_id,
            enactment_id=kwargs.get("enactment_id"),
            detail=kwargs.get("detail", f"stubbed {status}"),
            error=kwargs.get("error"),
        )
    monkeypatch.setattr(qrouter, "enact_opportunity", fake)


def _stub_restore(monkeypatch, *, status, **kwargs):
    from app.orchestrator.active_replacement import EnactmentResult
    import app.routers.quality as qrouter

    async def fake(db, enactment_id, *, restored_by=None, settings=None, libraries=None):
        return EnactmentResult(
            status=status,
            opportunity_id=kwargs.get("opportunity_id", 0),
            enactment_id=enactment_id,
            detail=kwargs.get("detail", f"stubbed {status}"),
            error=kwargs.get("error"),
        )
    monkeypatch.setattr(qrouter, "restore_enactment", fake)


async def _seed_and_get_id(client, db):
    await _seed_opportunity(db)
    resp = await client.get("/api/quality/replacement-opportunities")
    return resp.json()["opportunities"][0]["id"]


class TestEnactEndpoint:
    async def test_200_on_enacted(self, client, monkeypatch):
        from app.database import get_db
        db = await get_db()
        try:
            op_id = await _seed_and_get_id(client, db)
        finally:
            await db.close()

        _stub_enact(
            monkeypatch, status="enacted",
            enactment_id=7, detail="enacted via calibre sink",
        )
        resp = await client.post(
            f"/api/quality/replacement-opportunities/{op_id}/enact",
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "enacted"
        assert body["enactment_id"] == 7
        assert body["opportunity_id"] == op_id
        # Opportunity row included for UI single-round-trip rendering.
        assert body["opportunity"] is not None
        assert body["opportunity"]["id"] == op_id

    async def test_404_on_not_found(self, client, monkeypatch):
        _stub_enact(
            monkeypatch, status="not_found",
            detail="opportunity 9999 does not exist",
        )
        resp = await client.post(
            "/api/quality/replacement-opportunities/9999/enact",
        )
        assert resp.status_code == 404
        # FastAPI HTTPException JSON shape: top-level "detail" carries our dict.
        nested = resp.json()["detail"]
        assert nested["status"] == "not_found"
        assert nested["opportunity_id"] == 9999

    async def test_409_on_blocked(self, client, monkeypatch):
        from app.database import get_db
        db = await get_db()
        try:
            op_id = await _seed_and_get_id(client, db)
        finally:
            await db.close()

        _stub_enact(
            monkeypatch, status="blocked",
            detail="active replacement gate failed",
        )
        resp = await client.post(
            f"/api/quality/replacement-opportunities/{op_id}/enact",
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["status"] == "blocked"

    async def test_503_on_no_sink(self, client, monkeypatch):
        from app.database import get_db
        db = await get_db()
        try:
            op_id = await _seed_and_get_id(client, db)
        finally:
            await db.close()

        _stub_enact(
            monkeypatch, status="no_sink",
            detail="no Calibre sink available",
        )
        resp = await client.post(
            f"/api/quality/replacement-opportunities/{op_id}/enact",
        )
        assert resp.status_code == 503
        assert resp.json()["detail"]["status"] == "no_sink"

    async def test_500_on_failed_with_rollback_payload(self, client, monkeypatch):
        """Failure-with-rollback (sink-remove fails AFTER soft-delete)
        returns 500 but the body still carries the enactment_id so the
        UI can link the operator to the audit row's failed_reason."""
        from app.database import get_db
        db = await get_db()
        try:
            op_id = await _seed_and_get_id(client, db)
        finally:
            await db.close()

        _stub_enact(
            monkeypatch, status="failed", enactment_id=42,
            detail="sink calibre remove failed; soft-delete rolled back",
            error="exit 2: locked",
        )
        resp = await client.post(
            f"/api/quality/replacement-opportunities/{op_id}/enact",
        )
        assert resp.status_code == 500
        nested = resp.json()["detail"]
        assert nested["status"] == "failed"
        assert nested["enactment_id"] == 42
        assert "exit 2" in nested["error"]


class TestBulkEnactEndpoint:
    async def test_returns_200_with_per_item_results(self, client, monkeypatch):
        """Mixed-outcome bulk MUST be HTTP 200 — per-item statuses
        live in `results[]`. The UI surfaces a summary toast like
        "2 enacted, 1 blocked"."""
        from app.database import get_db
        from app.orchestrator.active_replacement import EnactmentResult
        import app.routers.quality as qrouter

        db = await get_db()
        try:
            id_a = await _seed_and_get_id(client, db)
            await _seed_opportunity(db, candidate_grab_id=102)
        finally:
            await db.close()

        resp = await client.get("/api/quality/replacement-opportunities")
        ids = [r["id"] for r in resp.json()["opportunities"]]

        # Per-id canned response so we can verify per-item carry-through.
        per_id_status = {ids[0]: "enacted", ids[1]: "blocked"}

        async def fake(db, opportunity_id, *, acted_by=None, **kw):
            return EnactmentResult(
                status=per_id_status[opportunity_id],
                opportunity_id=opportunity_id,
                enactment_id=99 if per_id_status[opportunity_id] == "enacted" else None,
                detail=f"stubbed {per_id_status[opportunity_id]}",
            )
        monkeypatch.setattr(qrouter, "enact_opportunity", fake)

        resp = await client.post(
            "/api/quality/replacement-opportunities/enact-bulk",
            json={"ids": ids},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["results"]) == 2
        statuses = [r["status"] for r in body["results"]]
        assert sorted(statuses) == ["blocked", "enacted"]
        assert body["counts"] == {"enacted": 1, "blocked": 1}

    async def test_empty_ids_returns_empty(self, client):
        resp = await client.post(
            "/api/quality/replacement-opportunities/enact-bulk",
            json={"ids": []},
        )
        assert resp.status_code == 200
        assert resp.json() == {"results": [], "counts": {}}

    async def test_unhandled_exception_per_item_doesnt_500_batch(
        self, client, monkeypatch,
    ):
        """One id raising shouldn't fail the whole batch — the UI
        loses the ability to act on the other rows. Per-item failure
        rows carry status='failed' with an error string."""
        from app.database import get_db
        from app.orchestrator.active_replacement import EnactmentResult
        import app.routers.quality as qrouter

        db = await get_db()
        try:
            await _seed_opportunity(db)
            await _seed_opportunity(db, candidate_grab_id=102)
        finally:
            await db.close()

        resp = await client.get("/api/quality/replacement-opportunities")
        ids = [r["id"] for r in resp.json()["opportunities"]]
        a, b = ids[0], ids[1]

        async def fake(db, opportunity_id, *, acted_by=None, **kw):
            if opportunity_id == a:
                raise RuntimeError("orchestrator exploded")
            return EnactmentResult(
                status="enacted", opportunity_id=opportunity_id,
                enactment_id=1, detail="ok",
            )
        monkeypatch.setattr(qrouter, "enact_opportunity", fake)

        resp = await client.post(
            "/api/quality/replacement-opportunities/enact-bulk",
            json={"ids": [a, b]},
        )
        assert resp.status_code == 200
        results = {r["opportunity_id"]: r for r in resp.json()["results"]}
        assert results[a]["status"] == "failed"
        assert "RuntimeError" in results[a]["error"]
        assert results[b]["status"] == "enacted"


class TestRestoreEndpoint:
    async def test_404_when_no_active_enactment(self, client):
        from app.database import get_db
        db = await get_db()
        try:
            op_id = await _seed_and_get_id(client, db)
        finally:
            await db.close()
        # No enactment seeded → endpoint should 404 with "no active
        # enactment" detail rather than calling restore_enactment.
        resp = await client.post(
            f"/api/quality/replacement-opportunities/{op_id}/restore",
        )
        assert resp.status_code == 404
        assert "no active enactment" in resp.json()["detail"]["detail"]

    async def test_404_when_opportunity_missing(self, client):
        resp = await client.post(
            "/api/quality/replacement-opportunities/9999/restore",
        )
        assert resp.status_code == 404

    async def test_200_when_active_enactment_restored(self, client, monkeypatch):
        from app.database import get_db
        from app.quality.enactments import record_enactment
        db = await get_db()
        try:
            op_id = await _seed_and_get_id(client, db)
            # Seed an active enactment.
            await record_enactment(
                db,
                opportunity_id=op_id, acted_by="user",
                library_slug="my-library",
                owned_book_id_before=42,
                owned_path_before="/lib/Author/Title",
                owned_path_after="/lib/.seshat-replaced/123/Title",
                owned_size_bytes=100, candidate_path=None,
                candidate_size_bytes=None, sink_result="ok",
            )
            await db.commit()
        finally:
            await db.close()

        _stub_restore(
            monkeypatch, status="restored",
            opportunity_id=op_id, detail="restored ok",
        )

        resp = await client.post(
            f"/api/quality/replacement-opportunities/{op_id}/restore",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "restored"
        assert body["opportunity"]["id"] == op_id

    async def test_409_when_restore_blocked(self, client, monkeypatch):
        """Restore can also return 'blocked' status (e.g., original path
        now occupied). The router maps that to HTTP 409 the same way
        the enact path does."""
        from app.database import get_db
        from app.quality.enactments import record_enactment
        db = await get_db()
        try:
            op_id = await _seed_and_get_id(client, db)
            await record_enactment(
                db,
                opportunity_id=op_id, acted_by="user",
                library_slug="my-library",
                owned_book_id_before=42,
                owned_path_before="/lib/Author/Title",
                owned_path_after="/lib/.seshat-replaced/123/Title",
                owned_size_bytes=100, candidate_path=None,
                candidate_size_bytes=None, sink_result="ok",
            )
            await db.commit()
        finally:
            await db.close()

        _stub_restore(
            monkeypatch, status="blocked",
            opportunity_id=op_id,
            detail="original path /lib/Author/Title is occupied",
        )
        resp = await client.post(
            f"/api/quality/replacement-opportunities/{op_id}/restore",
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["status"] == "blocked"
