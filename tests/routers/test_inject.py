"""
HTTP-level tests for the manual inject endpoint.

Wires up a real FastAPI app with a real dispatcher pointed at the
temp_db fixture, plus the same fake-qBit + fake-fetch pieces from
the dispatcher tests. Hits the endpoint via httpx ASGITransport so
no real network is involved — but the full request → router →
dispatcher → DB pipeline is exercised end to end.

The test surface is intentionally narrow because the dispatcher's
own test file already covers every path through `inject_grab`.
What this file confirms:

  - The router serializes / deserializes the request and response
    correctly
  - The 503 path fires when no dispatcher is installed
  - The dispatcher's `DispatchResult` flows through to the
    `InjectResponse` shape with the expected `ok` boolean
"""
from typing import Optional

import httpx
from fastapi import FastAPI

from app import state
from app.clients.base import AddResult, TorrentInfo
from app.database import get_db
from app.filter.gate import FilterConfig
from app.mam.grab import GrabResult
from app.orchestrator.dispatch import DispatcherDeps
from app.routers.inject import router as inject_router
from tests.fake_mam import MINIMAL_BENCODED_TORRENT


# ─── Helpers (small subset of the dispatcher test fakes) ─────


class _FakeQbit:
    def __init__(self, *, add_result: Optional[AddResult] = None):
        self.add_result = add_result or AddResult(success=True)
        self.add_calls: list[dict] = []

    async def login(self) -> bool:
        return True

    async def add_torrent(
        self,
        torrent_bytes: bytes,
        category: Optional[str] = None,
        save_path: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> AddResult:
        self.add_calls.append(
            {"size": len(torrent_bytes), "category": category, "tags": tags}
        )
        return self.add_result

    async def list_torrents(
        self, category: Optional[str] = None
    ) -> list[TorrentInfo]:
        return []

    async def get_torrent(self, torrent_hash: str) -> Optional[TorrentInfo]:
        return None

    async def aclose(self) -> None:
        return None


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(inject_router)
    return app


def _make_deps(qbit=None, fetch_result=None) -> DispatcherDeps:
    async def fake_fetch(torrent_id: str, token: str, **kwargs) -> GrabResult:
        return fetch_result or GrabResult(
            success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT
        )

    return DispatcherDeps(
        filter_config=FilterConfig(
            allowed_categories=frozenset(),
            allowed_authors=frozenset(),
            ignored_authors=frozenset(),
        ),
        mam_token="test",
        qbit_category="mam-complete",
        budget_cap=200,
        queue_max=100,
        queue_mode_enabled=True,
        seed_seconds_required=72 * 3600,
        db_factory=get_db,
        fetch_torrent=fake_fetch,
        qbit=qbit or _FakeQbit(),
    )


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# ─── Tests ───────────────────────────────────────────────────


class TestInjectEndpoint:
    async def test_happy_path_returns_submit(self, temp_db):
        qbit = _FakeQbit()
        state.dispatcher = _make_deps(qbit=qbit)
        try:
            async with _client(_make_app()) as client:
                resp = await client.post(
                    "/api/v1/grabs/inject",
                    json={
                        "torrent_id": "1234",
                        "torrent_name": "Test Book",
                        "category": "Ebooks - Fantasy",
                        "author_blob": "Test Author",
                    },
                )

            assert resp.status_code == 200
            body = resp.json()
            assert body["ok"] is True
            assert body["action"] == "submit"
            assert body["reason"] == "ok"
            assert body["grab_id"] is not None
            assert body["qbit_hash"] is not None
            assert len(body["qbit_hash"]) == 40
        finally:
            state.dispatcher = None

    async def test_minimal_request_only_torrent_id(self, temp_db):
        state.dispatcher = _make_deps()
        try:
            async with _client(_make_app()) as client:
                resp = await client.post(
                    "/api/v1/grabs/inject",
                    json={"torrent_id": "5678"},
                )

            assert resp.status_code == 200
            assert resp.json()["ok"] is True
        finally:
            state.dispatcher = None

    async def test_queue_mode_returns_ok_true_with_queue_action(self, temp_db):
        qbit = _FakeQbit()
        deps = _make_deps(qbit=qbit)
        deps.budget_cap = 0  # force queue
        state.dispatcher = deps
        try:
            async with _client(_make_app()) as client:
                resp = await client.post(
                    "/api/v1/grabs/inject",
                    json={"torrent_id": "9999"},
                )

            body = resp.json()
            # `ok` is True for both submit AND queue — both are
            # successful pipeline outcomes from the operator's view.
            assert body["ok"] is True
            assert body["action"] == "queue"
            assert qbit.add_calls == []
        finally:
            state.dispatcher = None

    async def test_drop_returns_ok_false(self, temp_db):
        deps = _make_deps()
        deps.budget_cap = 0
        deps.queue_mode_enabled = False
        state.dispatcher = deps
        try:
            async with _client(_make_app()) as client:
                resp = await client.post(
                    "/api/v1/grabs/inject",
                    json={"torrent_id": "9999"},
                )

            body = resp.json()
            # Drop is a valid outcome but not "in the pipeline" —
            # ok=False so the operator UI can flag it visibly.
            assert body["ok"] is False
            assert body["action"] == "drop"
            assert body["grab_id"] is None
        finally:
            state.dispatcher = None

    async def test_fetch_failure_returns_error(self, temp_db):
        deps = _make_deps(
            fetch_result=GrabResult(
                success=False,
                failure_kind="cookie_expired",
                failure_detail="MAM returned login HTML",
            )
        )
        state.dispatcher = deps
        try:
            async with _client(_make_app()) as client:
                resp = await client.post(
                    "/api/v1/grabs/inject",
                    json={"torrent_id": "1234"},
                )

            assert resp.status_code == 200  # not an HTTP error
            body = resp.json()
            assert body["ok"] is False
            assert "fetch_failed" in body["reason"]
            assert body["error"] == "MAM returned login HTML"
        finally:
            state.dispatcher = None

    async def test_503_when_no_dispatcher(self, temp_db):
        state.dispatcher = None
        async with _client(_make_app()) as client:
            resp = await client.post(
                "/api/v1/grabs/inject",
                json={"torrent_id": "1234"},
            )

        assert resp.status_code == 503
        assert "dispatcher" in resp.json()["detail"].lower()

    async def test_empty_torrent_id_rejected_by_pydantic(self, temp_db):
        state.dispatcher = _make_deps()
        try:
            async with _client(_make_app()) as client:
                resp = await client.post(
                    "/api/v1/grabs/inject",
                    json={"torrent_id": ""},
                )
            assert resp.status_code == 422  # validation error
        finally:
            state.dispatcher = None


# ─── Commit 6: per-grab wedge flag ──────────────────────────


class TestInjectWedgeFlags:
    """`use_wedge_override=True` forces `&fl=1` on the fetch irrespective
    of the policy engine's decision (on a torrent MAM confirms is paid).
    The old `buy_personal_fl` flag is gone: MAM refuses personalFL via
    the API ("Not allowed via API", 2026-10-07)."""

    async def test_use_wedge_override_forces_fl_on_fetch(self, temp_db, fake_mam):
        # Capture the kwargs the fetcher was called with so we can
        # assert `use_fl_wedge=True` reached it. The torrent must be
        # one MAM confirms isn't free: a wedge is never sent otherwise.
        from tests.fake_mam import DEFAULT_TORRENT_INFO_BODY

        fake_mam.search.body = DEFAULT_TORRENT_INFO_BODY
        captured: dict = {}

        async def capturing_fetch(torrent_id, token, **kwargs):
            captured.update(kwargs)
            return GrabResult(success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT)

        deps = _make_deps()
        deps.fetch_torrent = capturing_fetch
        state.dispatcher = deps
        try:
            async with _client(_make_app()) as client:
                resp = await client.post(
                    "/api/v1/grabs/inject",
                    json={
                        "torrent_id": "1234",
                        "use_wedge_override": True,
                    },
                )
            assert resp.status_code == 200
            assert captured.get("use_fl_wedge") is True
        finally:
            state.dispatcher = None

    async def test_use_wedge_override_false_does_not_force_fl(self, temp_db):
        captured: dict = {}

        async def capturing_fetch(torrent_id, token, **kwargs):
            captured.update(kwargs)
            return GrabResult(success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT)

        deps = _make_deps()
        deps.fetch_torrent = capturing_fetch
        state.dispatcher = deps
        try:
            async with _client(_make_app()) as client:
                resp = await client.post(
                    "/api/v1/grabs/inject",
                    json={"torrent_id": "5555"},
                )
            assert resp.status_code == 200
            # Default PolicyConfig has use_wedge=False; override is
            # False; so the fetcher sees use_fl_wedge=False.
            assert captured.get("use_fl_wedge") is False
        finally:
            state.dispatcher = None

    async def test_a_stale_buy_personal_fl_flag_is_ignored(self, temp_db, fake_mam):
        """Old callers may still send it: no bonusBuy request, grab proceeds."""
        from tests.fake_mam import DEFAULT_TORRENT_INFO_BODY

        fake_mam.search.body = DEFAULT_TORRENT_INFO_BODY
        state.dispatcher = _make_deps()
        try:
            async with _client(_make_app()) as client:
                resp = await client.post(
                    "/api/v1/grabs/inject",
                    json={"torrent_id": "777", "buy_personal_fl": True},
                )
            assert resp.status_code == 200
            assert resp.json()["ok"] is True
            assert not any("bonusBuy" in str(r.url) for r in fake_mam.requests)
        finally:
            state.dispatcher = None
