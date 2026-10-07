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


# ─── Commit 6: per-grab wedge / personal-FL flags ──────────


class TestInjectWedgeFlags:
    """Covers the two per-grab flags on the manual-inject dialog:

      - `use_wedge_override=True` — forces `&fl=1` on the fetch
        irrespective of the policy engine's decision.
      - `buy_personal_fl=True` — spends 50k BP via bonusBuy.php
        BEFORE the inject, flagging the torrent as personal FL on
        MAM's side so the subsequent grab picks up the free tier.
    """

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

    async def test_buy_personal_fl_writes_audit_then_injects(
        self, temp_db, fake_mam
    ):
        # buy_personal_freeleech hits fake_mam's bonusBuy endpoint
        # (default: success). We verify a personal_fl audit row is
        # recorded and the inject still completes normally.
        from app.storage import economy_audit
        from tests.fake_mam import DEFAULT_TORRENT_INFO_BODY

        # The torrent must exist on (fake) MAM: an empty search result
        # now means "removed from MAM" and the inject skips the fetch.
        fake_mam.search.body = DEFAULT_TORRENT_INFO_BODY
        state.dispatcher = _make_deps()
        try:
            async with _client(_make_app()) as client:
                resp = await client.post(
                    "/api/v1/grabs/inject",
                    json={
                        "torrent_id": "777",
                        "buy_personal_fl": True,
                    },
                )
            assert resp.status_code == 200
            assert resp.json()["ok"] is True

            db = await get_db()
            try:
                rows = await economy_audit.list_recent(
                    db, action=economy_audit.ACTION_PERSONAL_FL,
                )
            finally:
                await db.close()
            assert len(rows) == 1
            assert rows[0].torrent_id == "777"
            assert rows[0].trigger == economy_audit.TRIGGER_USER_GRAB
            assert rows[0].outcome == economy_audit.OUTCOME_SUCCESS
        finally:
            state.dispatcher = None

    async def test_buy_personal_fl_failure_still_proceeds_with_inject(
        self, temp_db, fake_mam
    ):
        from app.storage import economy_audit

        from tests.fake_mam import DEFAULT_TORRENT_INFO_BODY

        fake_mam.bonus_buy.body = (
            b'{"success":false,"error":"Not enough bonus, s1"}'
        )
        fake_mam.search.body = DEFAULT_TORRENT_INFO_BODY  # torrent exists
        state.dispatcher = _make_deps()
        try:
            async with _client(_make_app()) as client:
                resp = await client.post(
                    "/api/v1/grabs/inject",
                    json={
                        "torrent_id": "888",
                        "buy_personal_fl": True,
                    },
                )
            # Inject still completes — the PFL buy is best-effort.
            assert resp.status_code == 200
            assert resp.json()["ok"] is True

            db = await get_db()
            try:
                rows = await economy_audit.list_recent(
                    db, action=economy_audit.ACTION_PERSONAL_FL,
                )
            finally:
                await db.close()
            assert rows[0].outcome == economy_audit.OUTCOME_FAILURE
        finally:
            state.dispatcher = None

    async def test_a_successful_buy_marks_the_grab_free(self, temp_db, monkeypatch):
        """MAM's search API lags 5-20 min on personal_freeleech, so the
        buy's own result is what tells the dispatcher the grab is free."""
        from app.routers import inject as inject_mod
        from app.orchestrator.dispatch import DispatchResult

        outcomes = iter([True, False])
        seen: list[bool] = []

        async def fake_buy(torrent_id, token):
            return next(outcomes)

        async def fake_inject(deps, **kwargs):
            seen.append(kwargs["personal_fl_bought"])
            return DispatchResult(action="submit", reason="ok", announce_id=1)

        monkeypatch.setattr(inject_mod, "_buy_personal_fl_for_inject", fake_buy)
        monkeypatch.setattr(inject_mod, "inject_grab", fake_inject)
        state.dispatcher = _make_deps()
        try:
            async with _client(_make_app()) as client:
                for tid in ("1", "2"):
                    resp = await client.post(
                        "/api/v1/grabs/inject",
                        json={"torrent_id": tid, "buy_personal_fl": True},
                    )
                    assert resp.status_code == 200
                resp = await client.post("/api/v1/grabs/inject", json={"torrent_id": "3"})
            assert seen == [True, False, False]
        finally:
            state.dispatcher = None
