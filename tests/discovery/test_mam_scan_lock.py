"""One MAM scan at a time (audit issue 05, L1-06; G25).

Every scan entry point claims `state.claim_mam_scan` before doing
anything and keeps it until its scan ends. Until the audit, `/full-scan`
and `/scan-book` didn't check at all (a full scan ran alongside a regular
one, overwrote the widget's progress and doubled the search rate), and
the others checked a flag they set only after several awaits.
Debug-match is a read-only trace and stays outside the lock (G25).

Refusals are proven by asserting no search was made.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from app import state
from app.discovery.sources import mam as mam_source
from tests.discovery.test_mam_scan_auth_stop import library  # noqa: F401 (fixture)

_NOT_FOUND = {
    "status": "not_found", "mam_url": None, "mam_formats": None,
    "mam_torrent_id": None, "mam_has_multiple": False,
}


@pytest.fixture
async def mam_scans(tmp_path, monkeypatch):
    """MAM scanning on, a cookie set; every search recorded, answered
    not-found, and held until `gate` is set."""
    from app import config as app_config
    from app.discovery import cover_phash
    from app.discovery.routers import mam as mam_router

    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        **app_config.DEFAULT_SETTINGS,
        "mam_enabled": True, "mam_scanning_enabled": True, "rate_mam": 0,
        "mam_debug_match_enabled": True,
    }))
    monkeypatch.setattr(app_config, "SETTINGS_PATH", p)
    app_config._settings_cache["data"] = None
    app_config._settings_cache["mtime"] = object()

    async def token():
        return "tok"

    async def no_phash(*_a, **_kw):
        return None

    async def noop():
        return None

    monkeypatch.setattr(mam_router, "_get_mam_token", token)
    monkeypatch.setattr(mam_router, "_notify_mam_done", noop)
    monkeypatch.setattr(mam_source, "_resolve_mam_languages", lambda _: [1])
    monkeypatch.setattr(cover_phash, "ensure_cover_phash", no_phash)

    box = {"searched": [], "gate": asyncio.Event()}
    box["gate"].set()

    async def check_book(token, title, *_a, **_kw):
        box["searched"].append(title)
        await box["gate"].wait()
        return dict(_NOT_FOUND)

    monkeypatch.setattr(mam_source, "check_book", check_book)
    monkeypatch.setattr(mam_router, "mam_check_book", check_book)  # bound at import
    state._mam_scan_progress = {"running": False}
    state._mam_scan_task = None
    state._mam_full_scan_task = None
    yield box
    box["gate"].set()
    # A held scan a test left running finishes here, not after its loop closes.
    if state._mam_scan_task is not None and not state._mam_scan_task.done():
        await asyncio.wait_for(state._mam_scan_task, timeout=5)
    app_config._settings_cache["data"] = None
    app_config._settings_cache["mtime"] = object()
    state._mam_scan_progress = {"running": False}
    state._mam_scan_task = None
    state._mam_full_scan_task = None


def _app() -> FastAPI:
    from app.discovery.routers import authors, books
    from app.discovery.routers import mam as mam_router

    app = FastAPI()
    for r in (mam_router.router, books.router, authors.router):
        app.include_router(r)
    return app


async def _post(path: str, body: dict | None = None) -> httpx.Response:
    """POST with a deadline: an entry point that should have refused but
    searched instead sits on the held gate, and must fail, not hang."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://test",
    ) as c:
        return await asyncio.wait_for(
            c.post(path, json=body) if body is not None else c.post(path),
            timeout=5,
        )


async def _start_held_scan(library, mam_scans) -> None:
    """A books scan that sits on its first search until the gate opens."""
    mam_scans["gate"].clear()
    r = await _post(
        "/api/discovery/books/scan-mam?slug=ebooks", {"book_ids": library["book_ids"]},
    )
    assert r.json().get("status") == "started", r.json()
    for _ in range(50):
        if mam_scans["searched"]:
            break
        await asyncio.sleep(0.01)
    assert mam_scans["searched"] == ["Book 0"]


class TestEveryEntryPointWaitsItsTurn:
    async def test_full_scan_is_refused_while_a_scan_runs(
        self, temp_db, library, mam_scans, monkeypatch,
    ):
        from app.discovery.routers import mam as mam_router

        started: list = []

        async def fake_start(db):
            started.append(db)
            return {"id": 1, "total_books": 3}

        monkeypatch.setattr(mam_router, "mam_start_full_scan", fake_start)
        await _start_held_scan(library, mam_scans)

        r = await _post("/api/discovery/mam/full-scan")

        assert r.json() == {"error": "A MAM scan is already running"}
        assert started == []
        assert mam_scans["searched"] == ["Book 0"]   # nothing more searched
        assert state._mam_scan_progress["type"] == "books_bulk"   # widget untouched

    @pytest.mark.parametrize("path,body", [
        ("/api/discovery/mam/scan", None),
        ("/api/discovery/mam/test-scan", None),
        ("/api/discovery/mam/scan-book/{book}?slug=ebooks", None),
        ("/api/discovery/books/scan-mam?slug=ebooks", {"book_ids": "{books}"}),
        ("/api/discovery/authors/scan-mam",
         {"author_names": ["Some Author"], "content_type": "ebook"}),
    ])
    async def test_entry_point_is_refused_while_a_scan_runs(
        self, temp_db, library, mam_scans, path, body,
    ):
        await _start_held_scan(library, mam_scans)
        path = path.replace("{book}", str(library["book_ids"][1]))
        if body and body.get("book_ids") == "{books}":
            body = {"book_ids": library["book_ids"]}

        r = await _post(path, body)

        assert r.status_code == 200
        assert "already running" in r.json()["error"]
        assert mam_scans["searched"] == ["Book 0"]

    async def test_single_author_scan_is_refused_with_its_409(
        self, temp_db, library, mam_scans,
    ):
        await _start_held_scan(library, mam_scans)
        r = await _post(f"/api/discovery/mam/scan-author/{library['author_id']}")
        assert r.status_code == 409
        assert mam_scans["searched"] == ["Book 0"]

    async def test_debug_match_still_runs_during_a_scan(
        self, temp_db, library, mam_scans, monkeypatch,
    ):
        """G25: a read-only trace; the pacer bounds the rate anyway."""
        from app.discovery.sources import mam as source
        from app.routers import mam as status_router

        traced: list[str] = []

        async def fake_trace(**kw):
            traced.append(kw["title"])
            return {"passes": [], "cover_input": {}}

        async def cookie_set():
            return "tok"

        monkeypatch.setattr(source, "debug_check_book", fake_trace)
        monkeypatch.setattr(status_router.mam_cookie, "get_active_token", cookie_set)
        await _start_held_scan(library, mam_scans)

        app = FastAPI()
        app.include_router(status_router.router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as c:
            r = await c.get(
                "/api/v1/mam/debug-match", params={"title": "X", "author": "Y"},
            )

        assert r.status_code == 200, r.text
        assert traced == ["X"]


class TestTheClaimEnds:
    async def test_released_when_the_scan_task_finishes(
        self, temp_db, library, mam_scans,
    ):
        await _start_held_scan(library, mam_scans)
        assert state.mam_scan_running()

        mam_scans["gate"].set()
        await asyncio.wait_for(state._mam_scan_task, timeout=5)
        await asyncio.sleep(0)   # the done-callback

        assert state._mam_scan_claim is None
        assert not state.mam_scan_running()
        r = await _post(
            "/api/discovery/books/scan-mam?slug=ebooks", {"book_ids": library["book_ids"]},
        )
        assert r.json().get("status") == "started"

    async def test_released_when_there_is_nothing_to_scan(self, temp_db, mam_scans):
        state._discovered_libraries = []
        r = await _post("/api/discovery/mam/scan")
        assert r.json()["status"] == "complete"
        assert state._mam_scan_claim is None

    async def test_released_when_the_start_fails(self, temp_db, library, mam_scans):
        r = await _post("/api/discovery/mam/scan-author/99999?slug=ebooks")
        assert r.status_code == 404
        assert state._mam_scan_claim is None

    async def test_released_after_a_single_book_scan(self, temp_db, library, mam_scans):
        r = await _post(f"/api/discovery/mam/scan-book/{library['book_ids'][0]}?slug=ebooks")
        assert r.json()["status"] == "not_found"
        assert state._mam_scan_claim is None


class TestClaim:
    def test_only_one_holder(self):
        first = state.claim_mam_scan("manual")
        assert first is not None
        assert state.claim_mam_scan("full_scan") is None
        state.release_mam_scan(first)
        assert state.claim_mam_scan("full_scan") is not None

    async def test_handed_off_claim_survives_the_request(self):
        claim = state.claim_mam_scan("manual")
        hold = asyncio.Event()
        task = asyncio.create_task(hold.wait())
        state.hand_mam_scan_to(claim, task)
        state.release_mam_scan_unless_handed_off(claim)
        assert state.mam_scan_running()
        hold.set()
        await task
        await asyncio.sleep(0)
        assert not state.mam_scan_running()

    async def test_a_finished_task_never_holds_it(self):
        claim = state.claim_mam_scan("manual")
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        claim.task = task   # handed off without the callback having run
        assert not state.mam_scan_running()
        assert state.claim_mam_scan("manual") is not None

    def test_a_stale_releaser_cannot_free_a_newer_claim(self):
        old = state.claim_mam_scan("manual")
        state.release_mam_scan(old)
        new = state.claim_mam_scan("full_scan")
        state.release_mam_scan(old)
        assert state._mam_scan_claim is new


async def _one_scheduler_tick(monkeypatch, tmp_path, scan_batch) -> None:
    """Run `mam_scheduler_loop` for exactly one tick (its MAM calls stubbed)."""
    import time

    from app import config as app_config
    from app.discovery import scheduled_jobs
    from app.discovery.routers import mam as mam_router

    p = tmp_path / "sched-settings.json"
    p.write_text(json.dumps({
        **app_config.DEFAULT_SETTINGS,
        "mam_enabled": True, "mam_scanning_enabled": True,
        "mam_scan_interval_minutes": 1,
        "last_mam_validated_at": time.time(), "mam_validation_ok": True,
    }))
    monkeypatch.setattr(app_config, "SETTINGS_PATH", p)
    app_config._settings_cache["data"] = None
    app_config._settings_cache["mtime"] = object()
    state._library_sync_in_progress = False

    async def token():
        return "tok"

    async def validate(_token, _flag):
        return {"success": True, "message": "ok"}

    monkeypatch.setattr(mam_router, "_get_mam_token", token)
    monkeypatch.setattr(scheduled_jobs, "mam_validate", validate)
    monkeypatch.setattr(scheduled_jobs, "mam_scan_batch", scan_batch)

    ticks = {"n": 0}
    real_sleep = asyncio.sleep

    async def one_tick_sleep(_seconds):
        ticks["n"] += 1
        if ticks["n"] >= 2:
            raise asyncio.CancelledError()
        await real_sleep(0)

    monkeypatch.setattr(scheduled_jobs.asyncio, "sleep", one_tick_sleep)
    with pytest.raises(asyncio.CancelledError):
        await scheduled_jobs.mam_scheduler_loop()


class TestScheduledScan:
    async def test_skips_its_tick_while_another_scan_runs(
        self, temp_db, library, monkeypatch, tmp_path,
    ):
        scanned: list = []

        async def scan_batch(*_a, **_kw):
            scanned.append(1)
            return {"scanned": 0, "found": 0, "possible": 0,
                    "not_found": 0, "errors": 0, "error": None}

        held = state.claim_mam_scan("manual")
        await _one_scheduler_tick(monkeypatch, tmp_path, scan_batch)

        assert scanned == []
        assert state._mam_scan_claim is held

    async def test_holds_the_claim_while_scanning_then_lets_go(
        self, temp_db, library, monkeypatch, tmp_path,
    ):
        seen: list = []

        async def scan_batch(*_a, **_kw):
            seen.append(getattr(state._mam_scan_claim, "kind", None))
            return {"scanned": 0, "found": 0, "possible": 0,
                    "not_found": 0, "errors": 0, "error": None}

        await _one_scheduler_tick(monkeypatch, tmp_path, scan_batch)

        assert seen == ["scheduled"]
        assert state._mam_scan_claim is None
