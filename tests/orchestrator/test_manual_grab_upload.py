"""Manual Grab, slice B — drop a .torrent the user downloaded themselves.

An upload is never fetched from MAM (ADR-0023): every test here asserts
`fetch_torrent` was not called, success included. The file must prove
it is this account's own MAM download (`MID=` + our `UID=` in its
comment); refusals leave no grab row behind.
"""
from __future__ import annotations

import asyncio
import base64
import time

import httpx
import pytest
from fastapi import FastAPI

from app import state
from app.clients.base import AddResult
from app.database import get_db
from app.mam import user_status
from app.mam.torrent_meta import info_hash
from app.mam.user_status import UserStatus
from app.orchestrator import manual_grab, torrent_store
from app.orchestrator.dispatch import grab_uploaded_torrent, inject_grab
from app.policy.engine import PolicyConfig
from app.storage import grabs as grabs_storage
from tests.orchestrator.test_dispatch import _FakeQbit, _make_deps
from tests.orchestrator.test_manual_grab import (  # noqa: F401 (fixtures)
    TID,
    _item,
    _no_leftover_jobs,
    _own_cover_cache,
    _run_job,
    covers,
    fake_clock,
    mam_search,
)

MY_UID = 224285


def _bstr(b: bytes) -> bytes:
    return str(len(b)).encode() + b":" + b


def _torrent(mid: str | None = TID, uid: int | None = MY_UID, name: bytes = b"The Way of Kings.epub") -> bytes:
    """A .torrent shaped like the ones MAM serves (comment carries MID/UID)."""
    parts = [b"d"]
    if mid is not None:
        comment = f"MID={mid}" + (f",UID={uid}" if uid is not None else "")
        parts += [_bstr(b"comment"), _bstr(comment.encode())]
    parts += [
        _bstr(b"announce"), _bstr(b"https://t.myanonamouse.net/tracker.php/PASSKEY/announce"),
        _bstr(b"info"), b"d",
        _bstr(b"length"), b"i2000000e",
        _bstr(b"name"), _bstr(name),
        _bstr(b"piece length"), b"i16384e",
        _bstr(b"pieces"), _bstr(b"\x00" * 20),
        _bstr(b"private"), b"i1e",
        b"e", b"e",
    ]
    return b"".join(parts)


@pytest.fixture
def me():
    """The account's user status, cached (as after any MAM status read)."""
    token = "good_token"
    user_status._cache[user_status._cache_key(token)] = (
        time.monotonic(),
        UserStatus(
            ratio=5.0, wedges=3, seedbonus=100000, classname="Power User",
            username="me", uid=MY_UID, uploaded_bytes=10**12,
            downloaded_bytes=10**11, upload_buffer_bytes=10**12,
        ),
    )
    yield
    user_status.invalidate_cache()


@pytest.fixture(autouse=True)
def _clean_user_status():
    yield
    user_status.invalidate_cache()


async def _grabs() -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM grabs ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _seed_grab(*, tid: str, qbit_hash: str | None, state: str = grabs_storage.STATE_COMPLETE) -> int:
    db = await get_db()
    try:
        return await grabs_storage.create_grab(
            db, announce_id=None, mam_torrent_id=tid, torrent_name="x",
            category="Ebooks - Fantasy", author_blob="y", state=state,
            qbit_hash=qbit_hash,
        )
    finally:
        await db.close()


# ─── The bytes-in grab ───────────────────────────────────────


class TestUploadGrab:
    async def test_grabs_the_uploaded_bytes_never_fetching(self, temp_db, mam_search, me):
        qbit = _FakeQbit()
        deps = _make_deps(qbit=qbit)
        data = _torrent()
        result = await grab_uploaded_torrent(deps, torrent_bytes=data)

        assert result.action == "submit" and result.error is None, result
        assert deps.fetch_torrent.calls == []
        assert [c["size"] for c in qbit.add_calls] == [len(data)]
        [grab] = await _grabs()
        assert grab["mam_torrent_id"] == TID
        assert grab["qbit_hash"] == info_hash(data)
        assert grab["state"] == grabs_storage.STATE_SUBMITTED
        assert grab["torrent_name"] == "The Way of Kings"
        assert grab["author_blob"] == "Brandon Sanderson"

    async def test_my_snatched_is_expected_not_refused(self, temp_db, mam_search, me):
        mam_search["items"][TID] = _item(my_snatched=1)
        deps = _make_deps()
        result = await grab_uploaded_torrent(deps, torrent_bytes=_torrent())
        assert result.action == "submit" and result.error is None
        assert deps.fetch_torrent.calls == []

    @pytest.mark.parametrize("data,reason", [
        (b"not bencode", "bad_torrent_file"),
        (_torrent(mid=None), "not_mam_file"),
        (_torrent(uid=99999), "foreign_file"),
        (_torrent(uid=None), "foreign_file"),
    ], ids=["not-bencode", "no-mid", "other-account", "no-uid"])
    async def test_refused_files_cost_nothing(self, temp_db, mam_search, me, data, reason):
        qbit = _FakeQbit()
        deps = _make_deps(qbit=qbit)
        result = await grab_uploaded_torrent(deps, torrent_bytes=data)
        assert (result.action, result.reason) == ("skip", reason)
        assert result.error
        assert deps.fetch_torrent.calls == [] and qbit.add_calls == []
        assert mam_search["calls"] == []   # not even a lookup
        assert await _grabs() == []

    async def test_unknown_account_never_fails_open(self, temp_db, mam_search):
        # No cached status, and the real MAM client refuses in tests.
        qbit = _FakeQbit()
        deps = _make_deps(qbit=qbit)
        result = await grab_uploaded_torrent(deps, torrent_bytes=_torrent())
        assert result.reason == "uid_unknown"
        assert qbit.add_calls == [] and await _grabs() == []

    async def test_already_grabbed_by_torrent_id(self, temp_db, mam_search, me):
        await _seed_grab(tid=TID, qbit_hash="b" * 40)
        qbit = _FakeQbit()
        deps = _make_deps(qbit=qbit)
        result = await grab_uploaded_torrent(deps, torrent_bytes=_torrent())
        assert result.reason == "already_grabbed"
        assert qbit.add_calls == [] and deps.fetch_torrent.calls == []

    async def test_already_grabbed_by_info_hash(self, temp_db, mam_search, me):
        data = _torrent()
        # e.g. an orphan-adopted manual add: no torrent ID, same hash.
        await _seed_grab(tid="", qbit_hash=info_hash(data))
        qbit = _FakeQbit()
        deps = _make_deps(qbit=qbit)
        result = await grab_uploaded_torrent(deps, torrent_bytes=data)
        assert result.reason == "already_grabbed"
        assert qbit.add_calls == []

    async def test_removed_from_mam(self, temp_db, mam_search, me):
        mam_search["items"].pop(TID)
        qbit = _FakeQbit()
        deps = _make_deps(qbit=qbit)
        result = await grab_uploaded_torrent(deps, torrent_bytes=_torrent())
        assert result.reason == "torrent_removed_from_mam"
        assert qbit.add_calls == [] and await _grabs() == []

    async def test_buffer_gate_applies(self, temp_db, mam_search, me):
        user_status.invalidate_cache()
        user_status._cache[user_status._cache_key("good_token")] = (
            time.monotonic(),
            UserStatus(ratio=1.0, wedges=0, seedbonus=0, classname="", username="",
                       uid=MY_UID, uploaded_bytes=0, downloaded_bytes=0,
                       upload_buffer_bytes=0),
        )
        qbit = _FakeQbit()
        deps = _make_deps(qbit=qbit)
        deps.policy_config = PolicyConfig(buffer_gate_enabled=True)
        result = await grab_uploaded_torrent(deps, torrent_bytes=_torrent())
        assert result.reason == "policy:buffer_insufficient"
        assert qbit.add_calls == [] and await _grabs() == []

        bought = await grab_uploaded_torrent(
            deps, torrent_bytes=_torrent(), personal_fl_bought=True,
        )
        assert bought.action == "submit" and bought.error is None

    async def test_full_budget_and_queue_leave_no_row(self, temp_db, mam_search, me):
        qbit = _FakeQbit()
        deps = _make_deps(qbit=qbit, budget_cap=0, queue_max=0)
        result = await grab_uploaded_torrent(deps, torrent_bytes=_torrent())
        assert result.action == "drop"
        assert qbit.add_calls == [] and await _grabs() == []

    async def test_full_budget_queues_the_bytes(self, temp_db, mam_search, me):
        qbit = _FakeQbit()
        deps = _make_deps(qbit=qbit, budget_cap=0, queue_max=5)
        data = _torrent()
        result = await grab_uploaded_torrent(deps, torrent_bytes=data)
        assert result.action == "queue" and result.error is None
        [grab] = await _grabs()
        assert grab["state"] == grabs_storage.STATE_PENDING_QUEUE
        assert torrent_store.load(grab["torrent_file_path"]) == data
        assert deps.fetch_torrent.calls == [] and qbit.add_calls == []

    async def test_qbit_rejection_is_a_failed_grab_not_a_fetch(self, temp_db, mam_search, me):
        qbit = _FakeQbit(add_result=AddResult(success=False, failure_kind="rejected", failure_detail="nope"))
        deps = _make_deps(qbit=qbit)
        result = await grab_uploaded_torrent(deps, torrent_bytes=_torrent())
        assert result.error == "nope"
        assert deps.fetch_torrent.calls == []

    async def test_concurrent_upload_and_paste_grab_once(self, temp_db, mam_search, me):
        qbit = _FakeQbit()
        deps = _make_deps(qbit=qbit)
        await asyncio.gather(
            grab_uploaded_torrent(deps, torrent_bytes=_torrent()),
            inject_grab(deps, torrent_id=TID, apply_claim_for_owned=False),
        )
        grabs = await _grabs()
        assert len(grabs) == 1
        assert len(deps.fetch_torrent.calls) <= 1
        assert len(qbit.add_calls) == 1


# ─── Preview of an uploaded file ─────────────────────────────


class TestUploadPreview:
    async def test_ready_file_row(self, temp_db, mam_search, me, fake_clock, covers):
        mam_search["items"][TID] = _item(my_snatched=1)  # the upload is the snatch
        data = _torrent()
        row = await manual_grab.preview_file(_make_deps(), "x.torrent", data)
        assert row.status == manual_grab.STATUS_READY
        assert row.kind == "file" and row.torrent_id == TID
        assert row.info_hash == info_hash(data)
        assert row.wedge_eligible is False
        assert row.title == "The Way of Kings"

    @pytest.mark.parametrize("data,status", [
        (b"junk", manual_grab.STATUS_BAD_FILE),
        (_torrent(mid=None), manual_grab.STATUS_NOT_MAM_FILE),
        (_torrent(uid=99999), manual_grab.STATUS_FOREIGN_FILE),
    ], ids=["not-bencode", "no-mid", "other-account"])
    async def test_refused_files_never_look_up(self, temp_db, mam_search, me, data, status):
        row = await manual_grab.preview_file(_make_deps(), "x.torrent", data)
        assert row.status == status
        assert row.status in manual_grab.BLOCKING_STATUSES
        assert mam_search["calls"] == []

    async def test_uid_unknown(self, temp_db, mam_search, fake_clock):
        row = await manual_grab.preview_file(_make_deps(), "x.torrent", _torrent())
        assert row.status == manual_grab.STATUS_UID_UNKNOWN

    async def test_already_grabbed_by_hash(self, temp_db, mam_search, me):
        data = _torrent()
        await _seed_grab(tid="", qbit_hash=info_hash(data))
        row = await manual_grab.preview_file(_make_deps(), "x.torrent", data)
        assert row.status == manual_grab.STATUS_ALREADY_GRABBED
        assert mam_search["calls"] == []


# ─── Grab all with file rows ─────────────────────────────────


class TestUploadJob:
    async def test_file_row_is_grabbed_without_a_fetch(self, temp_db, mam_search, me, fake_clock):
        deps = _make_deps()
        job = await _run_job(deps, manual_grab.GrabRequestItem(kind="file", value="x.torrent", data=_torrent()))
        assert job.rows[0].status == "submitted", job.rows[0].message
        assert job.rows[0].torrent_id == TID
        assert deps.fetch_torrent.calls == []

    async def test_foreign_file_costs_no_lookup(self, temp_db, mam_search, me, fake_clock):
        deps = _make_deps()
        job = await _run_job(deps, manual_grab.GrabRequestItem(kind="file", value="x.torrent", data=_torrent(uid=1)))
        assert job.rows[0].reason == "foreign_file"
        assert mam_search["calls"] == []

    async def test_personal_fl_on_a_file_row(self, temp_db, mam_search, me, fake_clock, monkeypatch):
        from app.mam import torrent_info
        from app.routers import inject as inject_router

        async def fake_buy(tid, token):
            torrent_info.invalidate_cache()
            return True

        monkeypatch.setattr(inject_router, "_buy_personal_fl_for_inject", fake_buy)
        deps = _make_deps()
        job = await _run_job(deps, manual_grab.GrabRequestItem(
            kind="file", value="x.torrent", data=_torrent(), buy_personal_fl=True,
        ))
        assert job.rows[0].status == "submitted"
        assert job.rows[0].personal_fl_bought is True
        assert deps.fetch_torrent.calls == []


# ─── Router: files over the wire ─────────────────────────────


def _client() -> httpx.AsyncClient:
    from app.routers.manual_grab import router

    app = FastAPI()
    app.include_router(router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class TestRouterFiles:
    async def test_link_and_file_of_one_torrent_is_a_duplicate(self, temp_db, monkeypatch):
        started: list = []
        monkeypatch.setattr(manual_grab, "start_job", lambda deps, items: started.append(items))
        state.dispatcher = _make_deps()
        try:
            async with _client() as c:
                resp = await c.post("/api/v1/manual-grab/grab", json={"items": [
                    {"kind": "link", "value": TID},
                    {"kind": "file", "name": "x.torrent", "data_b64": _b64(_torrent())},
                ]})
        finally:
            state.dispatcher = None
        assert resp.status_code == 422
        assert started == []

    async def test_bad_base64_is_422(self, temp_db):
        state.dispatcher = _make_deps()
        try:
            async with _client() as c:
                resp = await c.post("/api/v1/manual-grab/preview", json={
                    "kind": "file", "name": "x.torrent", "data_b64": "!!!not base64",
                })
        finally:
            state.dispatcher = None
        assert resp.status_code == 422

    async def test_file_preview_over_http(self, temp_db, mam_search, me, fake_clock, covers):
        state.dispatcher = _make_deps()
        try:
            async with _client() as c:
                resp = await c.post("/api/v1/manual-grab/preview", json={
                    "kind": "file", "name": "x.torrent", "data_b64": _b64(_torrent()),
                })
        finally:
            state.dispatcher = None
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready" and body["kind"] == "file"
        assert body["torrent_id"] == TID
