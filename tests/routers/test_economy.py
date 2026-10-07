"""
HTTP-level tests for `/api/v1/mam/economy/*`.

Uses the same FastAPI + ASGITransport harness as `test_inject.py`,
isolates settings.json through `monkeypatch`, and points bonusBuy /
user_status / torrent_info at the shared `fake_mam` fixture.

Scope covered:
  - GET/PUT /config (known-key filtering, round-trip)
  - POST /vip/buy (happy path, audit row, timestamp bump, failure)
  - POST /upload/buy (explicit gb, max_affordable, failure modes)
  - POST /personal-fl/buy (happy path, audit row, cache invalidation)
  - GET /audit (read + action filter)
  - POST /preflight (sufficient + insufficient + shortfall math)
  - Missing-token 412 on every buy endpoint
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app import config
from app.database import get_db
from app.mam.user_status import invalidate_cache as invalidate_user_status
from app.mam.torrent_info import invalidate_cache as invalidate_torrent_info
from app.routers.economy import router as economy_router
from app.storage import economy_audit


# ─── Fixtures ───────────────────────────────────────────────


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", p)
    config._settings_cache["data"] = None
    config._settings_cache["mtime"] = object()

    # Pin token resolution to settings.json. v2.2.8 routed
    # `_require_token()` through `mam.cookie.get_active_token()` which
    # cascades in-memory → encrypted store → settings.json. Without
    # the override, tests would hit whatever token sits in the dev
    # box's auth DB and could even make real MAM network calls.
    from app.mam import cookie as mam_cookie_mod

    async def _fake_get_active_token():
        return config.load_settings().get("mam_session_id", "") or ""

    monkeypatch.setattr(
        mam_cookie_mod, "get_active_token", _fake_get_active_token
    )

    yield p
    config._settings_cache["data"] = None
    config._settings_cache["mtime"] = object()


def _write_settings(path: Path, overrides: dict) -> None:
    merged = {**config.DEFAULT_SETTINGS, **overrides}
    path.write_text(json.dumps(merged))
    config._settings_cache["data"] = None


@pytest.fixture(autouse=True)
def _clear_caches():
    invalidate_user_status()
    invalidate_torrent_info()
    yield
    invalidate_user_status()
    invalidate_torrent_info()


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(economy_router)
    return a


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _set_token(isolated_settings_path: Path, token: str) -> None:
    _write_settings(isolated_settings_path, {"mam_session_id": token})


async def _audit_rows(*, action: str = None):
    db = await get_db()
    try:
        return await economy_audit.list_recent(db, action=action)
    finally:
        await db.close()


# ─── Config ────────────────────────────────────────────────


class TestConfigRoundTrip:
    async def test_get_returns_defaults(
        self, client, temp_db, isolated_settings
    ):
        _write_settings(isolated_settings, {})
        resp = await client.get("/api/v1/mam/economy/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["mam_economy_vip_enabled"] is False
        assert body["mam_economy_upload_ratio_chunk_gb"] == 50
        # Read-only fields surface too.
        assert "mam_economy_last_vip_buy_at" in body

    async def test_put_merges_known_keys(
        self, client, temp_db, isolated_settings
    ):
        _write_settings(isolated_settings, {})
        resp = await client.put(
            "/api/v1/mam/economy/config",
            json={
                "mam_economy_vip_enabled": True,
                "mam_economy_upload_ratio_floor": 1.8,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["mam_economy_vip_enabled"] is True
        assert body["mam_economy_upload_ratio_floor"] == 1.8

        # Confirm persistence.
        persisted = json.loads(Path(isolated_settings).read_text())
        assert persisted["mam_economy_vip_enabled"] is True

    async def test_put_drops_unknown_keys_silently(
        self, client, temp_db, isolated_settings
    ):
        _write_settings(isolated_settings, {})
        resp = await client.put(
            "/api/v1/mam/economy/config",
            json={
                "mam_economy_vip_enabled": True,
                "policy_use_wedge": True,  # not an economy key — should be ignored
            },
        )
        assert resp.status_code == 200
        persisted = json.loads(Path(isolated_settings).read_text())
        # The economy key went through
        assert persisted["mam_economy_vip_enabled"] is True
        # The non-economy key stayed at its default — the PUT path
        # must never let a caller corrupt unrelated settings.
        assert persisted["policy_use_wedge"] is False

    async def test_put_empty_body_returns_400(
        self, client, temp_db, isolated_settings
    ):
        _write_settings(isolated_settings, {})
        resp = await client.put("/api/v1/mam/economy/config", json={})
        assert resp.status_code == 400

    async def test_put_delegates_to_apply_settings_patch(
        self, client, temp_db, isolated_settings, monkeypatch
    ):
        """Regression guard: toggling `mam_economy_buffer_gate_enabled`
        via MamPage has to rebuild the dispatcher (PolicyConfig is
        snapshotted at startup). If someone "simplifies" this back to
        a plain save_settings call, the buffer gate flag would persist
        to disk but stay at False in memory until the next container
        restart — exactly the bug that reached UAT on 2026-04-23.
        """
        from app.routers import settings as settings_router
        called_with: dict = {}

        async def fake_apply(body):
            called_with.update(body)
            from app.routers.settings import PatchResponse
            return PatchResponse(ok=True, updated=list(body), rejected=[])

        monkeypatch.setattr(settings_router, "apply_settings_patch", fake_apply)
        _write_settings(isolated_settings, {})
        resp = await client.put(
            "/api/v1/mam/economy/config",
            json={"mam_economy_buffer_gate_enabled": True},
        )
        assert resp.status_code == 200
        assert called_with == {"mam_economy_buffer_gate_enabled": True}


# ─── VIP buy ────────────────────────────────────────────────


class TestVipBuy:
    async def test_happy_path_writes_success_row_and_bumps_timestamp(
        self, client, temp_db, isolated_settings, fake_mam
    ):
        _set_token(isolated_settings, "tok")
        resp = await client.post(
            "/api/v1/mam/economy/vip/buy", json={"weeks": 4}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["new_seedbonus"] == pytest.approx(26512.091)
        assert body["amount"] == "4"

        rows = await _audit_rows(action=economy_audit.ACTION_VIP)
        assert len(rows) == 1
        assert rows[0].trigger == economy_audit.TRIGGER_MANUAL
        assert rows[0].outcome == economy_audit.OUTCOME_SUCCESS
        assert rows[0].cost_points == pytest.approx(71088 - 26512.091)

        persisted = json.loads(Path(isolated_settings).read_text())
        assert persisted["mam_economy_last_vip_buy_at"] > 0

    async def test_no_token_returns_412(
        self, client, temp_db, isolated_settings, fake_mam
    ):
        _write_settings(isolated_settings, {})
        resp = await client.post(
            "/api/v1/mam/economy/vip/buy", json={"weeks": 4}
        )
        assert resp.status_code == 412

    async def test_mam_rejects_writes_failure_row(
        self, client, temp_db, isolated_settings, fake_mam
    ):
        _set_token(isolated_settings, "tok")
        fake_mam.bonus_buy.body = (
            b'{"success":false,"error":"Not enough bonus, s1"}'
        )
        resp = await client.post(
            "/api/v1/mam/economy/vip/buy", json={"weeks": 4}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "Not enough bonus" in body["message"]

        rows = await _audit_rows(action=economy_audit.ACTION_VIP)
        assert rows[0].outcome == economy_audit.OUTCOME_FAILURE
        persisted = json.loads(Path(isolated_settings).read_text())
        # Failure doesn't bump the shared timestamp — next auto-buy
        # tick can retry immediately if BP recovers.
        assert persisted["mam_economy_last_vip_buy_at"] == 0.0


# ─── Upload buy ─────────────────────────────────────────────


class TestUploadBuy:
    async def test_explicit_gb(
        self, client, temp_db, isolated_settings, fake_mam
    ):
        _set_token(isolated_settings, "tok")
        resp = await client.post(
            "/api/v1/mam/economy/upload/buy", json={"gb": 50}
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        rows = await _audit_rows(action=economy_audit.ACTION_UPLOAD)
        assert rows[0].amount == "50"

    async def test_max_affordable_derives_from_seedbonus(
        self, client, temp_db, isolated_settings, fake_mam
    ):
        _set_token(isolated_settings, "tok")
        # Default fake user_status has seedbonus 71088 → 71088//500 = 142
        resp = await client.post(
            "/api/v1/mam/economy/upload/buy", json={"mode": "max_affordable"}
        )
        assert resp.status_code == 200
        rows = await _audit_rows(action=economy_audit.ACTION_UPLOAD)
        assert rows[0].amount == "142"

    async def test_max_affordable_with_tiny_balance_returns_400(
        self, client, temp_db, isolated_settings, fake_mam
    ):
        _set_token(isolated_settings, "tok")
        fake_mam.user_status.body = (
            b'{"seedbonus":100,"wedges":0,"ratio":1.0,"uid":1,'
            b'"username":"t","uploaded_bytes":0,"downloaded_bytes":0}'
        )
        resp = await client.post(
            "/api/v1/mam/economy/upload/buy", json={"mode": "max_affordable"}
        )
        assert resp.status_code == 400
        assert "seedbonus" in resp.json()["detail"].lower()

    async def test_missing_gb_and_mode_returns_400(
        self, client, temp_db, isolated_settings, fake_mam
    ):
        _set_token(isolated_settings, "tok")
        resp = await client.post(
            "/api/v1/mam/economy/upload/buy", json={}
        )
        assert resp.status_code == 400


# ─── Personal-FL buy ───────────────────────────────────────


class TestPersonalFlBuy:
    async def test_happy_path(
        self, client, temp_db, isolated_settings, fake_mam
    ):
        _set_token(isolated_settings, "tok")
        resp = await client.post(
            "/api/v1/mam/economy/personal-fl/buy",
            json={"torrent_id": "12345"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        rows = await _audit_rows(action=economy_audit.ACTION_PERSONAL_FL)
        assert len(rows) == 1
        assert rows[0].torrent_id == "12345"
        assert rows[0].trigger == economy_audit.TRIGGER_MANUAL
        # Personal-FL doesn't have a scheduler timestamp — confirm
        # the two scheduler timestamps stayed at 0.
        persisted = json.loads(Path(isolated_settings).read_text())
        assert persisted["mam_economy_last_vip_buy_at"] == 0.0
        assert persisted["mam_economy_last_upload_buy_at"] == 0.0

    async def test_empty_torrent_id_returns_422(
        self, client, temp_db, isolated_settings, fake_mam
    ):
        _set_token(isolated_settings, "tok")
        resp = await client.post(
            "/api/v1/mam/economy/personal-fl/buy",
            json={"torrent_id": ""},
        )
        assert resp.status_code == 422  # pydantic min_length=1


# ─── Audit ─────────────────────────────────────────────────


class TestAudit:
    async def _seed(self):
        db = await get_db()
        try:
            await economy_audit.record(
                db, action=economy_audit.ACTION_VIP,
                trigger=economy_audit.TRIGGER_SCHEDULED,
                outcome=economy_audit.OUTCOME_SUCCESS,
            )
            await economy_audit.record(
                db, action=economy_audit.ACTION_UPLOAD,
                trigger=economy_audit.TRIGGER_SCHEDULED,
                outcome=economy_audit.OUTCOME_SKIP_NO_TRIGGER,
            )
        finally:
            await db.close()

    async def test_returns_most_recent_first(
        self, client, temp_db, isolated_settings
    ):
        await self._seed()
        resp = await client.get("/api/v1/mam/economy/audit")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 2
        assert rows[0]["action"] == economy_audit.ACTION_UPLOAD

    async def test_action_filter(
        self, client, temp_db, isolated_settings
    ):
        await self._seed()
        resp = await client.get(
            "/api/v1/mam/economy/audit",
            params={"action": economy_audit.ACTION_VIP},
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["action"] == economy_audit.ACTION_VIP

    async def test_limit_out_of_range_returns_400(
        self, client, temp_db, isolated_settings
    ):
        resp = await client.get(
            "/api/v1/mam/economy/audit", params={"limit": 0}
        )
        assert resp.status_code == 400


# ─── Preflight ─────────────────────────────────────────────


class TestPreflight:
    @staticmethod
    def _torrent_info_body(size_bytes: int) -> bytes:
        return json.dumps({
            "perpage": 1, "start": 0, "found": 1,
            "data": [{
                "id": "1234", "language": "1", "main_cat": "14",
                "category": "63", "catname": "Ebooks - Fantasy",
                "size": str(size_bytes), "numfiles": "1",
                "vip": "0", "free": "0", "fl_vip": "0",
                "personal_freeleech": "0",
                "title": "Test Book", "name": "Test Book",
                "author_info": '{"1": "Author"}',
                "seeders": "5", "leechers": "0", "times_completed": "1",
            }],
        }).encode()

    @staticmethod
    def _user_status_body(buffer_bytes: int) -> bytes:
        return json.dumps({
            "classname": "Power User", "ratio": 2.0, "seedbonus": 100,
            "uid": 1, "username": "t",
            "uploaded_bytes": 1_000_000_000_000,
            "downloaded_bytes": 500_000_000_000,
            "upload_buffer": buffer_bytes, "wedges": 5,
        }).encode()

    async def test_sufficient_buffer(
        self, client, temp_db, isolated_settings, fake_mam
    ):
        _set_token(isolated_settings, "tok")
        fake_mam.search.body = self._torrent_info_body(2_000_000_000)
        fake_mam.user_status.body = self._user_status_body(20_000_000_000)

        resp = await client.post(
            "/api/v1/mam/economy/preflight", json={"torrent_id": "1234"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["sufficient"] is True
        assert body["size_gb"] == pytest.approx(2.0)
        assert body["buffer_gb"] == pytest.approx(20.0)
        assert body["shortfall_gb"] == 0.0
        assert body["recommended_buy_gb"] == 0.0

    async def test_reads_mams_human_readable_size(
        self, client, temp_db, isolated_settings, fake_mam
    ):
        """MAM sends size as "9.3 GiB". `int()` on that read 0 GB, so the
        preflight called every torrent affordable (fixed 2026-10-06)."""
        _set_token(isolated_settings, "tok")
        body = json.loads(self._torrent_info_body(0))
        body["data"][0]["size"] = "9.3 GiB"
        fake_mam.search.body = json.dumps(body).encode()
        fake_mam.user_status.body = self._user_status_body(4_000_000_000)

        resp = await client.post(
            "/api/v1/mam/economy/preflight", json={"torrent_id": "1234"}
        )
        assert resp.status_code == 200
        out = resp.json()
        assert out["size_gb"] == pytest.approx(9.3 * 1024 ** 3 / 1e9)
        assert out["sufficient"] is False

    async def test_insufficient_buffer_computes_shortfall(
        self, client, temp_db, isolated_settings, fake_mam
    ):
        _set_token(isolated_settings, "tok")
        # Default safety margin is 1 GB
        fake_mam.search.body = self._torrent_info_body(10_000_000_000)  # 10 GB
        fake_mam.user_status.body = self._user_status_body(4_000_000_000)  # 4 GB

        resp = await client.post(
            "/api/v1/mam/economy/preflight", json={"torrent_id": "1234"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["sufficient"] is False
        # 10 GB + 1 GB margin − 4 GB = 7 GB shortfall
        assert body["shortfall_gb"] == pytest.approx(7.0)
        # Whole-GB rounded-up recommendation
        assert body["recommended_buy_gb"] == 7.0
        assert body["recommended_buy_cost_bp"] == 7 * 500

    async def test_fractional_shortfall_rounds_up(
        self, client, temp_db, isolated_settings, fake_mam
    ):
        _set_token(isolated_settings, "tok")
        fake_mam.search.body = self._torrent_info_body(8_500_000_000)  # 8.5 GB
        fake_mam.user_status.body = self._user_status_body(3_000_000_000)  # 3 GB

        resp = await client.post(
            "/api/v1/mam/economy/preflight", json={"torrent_id": "1234"}
        )
        body = resp.json()
        # 8.5 + 1 − 3 = 6.5 GB shortfall → recommend 7 GB
        assert body["shortfall_gb"] == pytest.approx(6.5)
        assert body["recommended_buy_gb"] == 7.0

    async def test_no_token_returns_412(
        self, client, temp_db, isolated_settings, fake_mam
    ):
        _write_settings(isolated_settings, {})
        resp = await client.post(
            "/api/v1/mam/economy/preflight", json={"torrent_id": "1234"}
        )
        assert resp.status_code == 412
