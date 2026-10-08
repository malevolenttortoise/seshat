"""Phase 0 S1 — snatch safety: MAM sees one download per torrent, ever.

Three guards sit in front of the .torrent fetch:

  - `already_grabbed` — Seshat itself already fetched (or is fetching)
    this torrent ID. No override. Which rows block is decided by
    `grabs_storage.find_blocking_grab`: `qbit_hash` set, or a state in
    `BLOCKING_STATES`. Pre-fetch failures stay retryable.
  - `already_snatched_on_mam` — MAM's `my_snatched` flag. Overridable
    with `override_mam_snatched` (an explicit user confirm).
  - `torrent_removed_from_mam` — the search API no longer has the ID.
    User/programmatic grabs only; IRC fails open (index lag).

Every refusal is proven by asserting `fetch_torrent` was NOT called —
never by fetching anything twice.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from app.database import get_db
from app.mam.grab import GrabResult
from app.mam.torrent_info import (
    TorrentInfo,
    TorrentInfoError,
    TorrentNotFoundError,
    invalidate_cache,
)
from app.orchestrator import cookie_retry, dispatch
from app.orchestrator.dispatch import DispatchResult, handle_announce, inject_grab
from app.storage import grabs as grabs_storage
from app.storage import tentative as tentative_storage
from tests.orchestrator.test_dispatch import (
    _FakeQbit,
    _make_announce,
    _make_deps,
    _make_filter_config,
)
from tests.orchestrator.test_manual_grab import TID as MG_TID
from tests.orchestrator.test_manual_grab import mam_search  # noqa: F401

TID = "1234"
HASH = "a" * 40


def _info(**overrides) -> TorrentInfo:
    fields = dict(
        torrent_id=TID, vip=False, free=False, fl_vip=False,
        personal_freeleech=False, category="Ebooks - Fantasy",
        title="The Way of Kings", size="1000",
    )
    fields.update(overrides)
    return TorrentInfo(**fields)


@pytest.fixture
def mam_says(monkeypatch):
    """Program what MAM's search API answers for the dispatcher's
    torrent_info lookup: a TorrentInfo, or an exception to raise."""
    box: dict = {"answer": _info(), "calls": []}

    async def fake_get_torrent_info(torrent_id, token=None, ttl=120):
        box["calls"].append(torrent_id)
        answer = box["answer"]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(dispatch, "get_torrent_info", fake_get_torrent_info)
    return box


async def _seed_grab(
    state: str, *, qbit_hash: str | None = None, tid: str = TID,
) -> int:
    db = await get_db()
    try:
        return await grabs_storage.create_grab(
            db, announce_id=None, mam_torrent_id=tid,
            torrent_name="The Way of Kings", category="Ebooks - Fantasy",
            author_blob="Brandon Sanderson", state=state, qbit_hash=qbit_hash,
        )
    finally:
        await db.close()


async def _grab_ids(tid: str = TID) -> list[int]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT id FROM grabs WHERE mam_torrent_id = ? ORDER BY id", (tid,),
        )
        return [int(r["id"]) for r in await cur.fetchall()]
    finally:
        await db.close()


async def _announce_decision(announce_id: int) -> tuple[str, str]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT decision, decision_reason FROM announces WHERE id = ?",
            (announce_id,),
        )
        row = await cur.fetchone()
        return row["decision"], row["decision_reason"]
    finally:
        await db.close()


def _irc_deps(**kw):
    return _make_deps(
        filter_config=_make_filter_config(allowed=["Brandon Sanderson"]), **kw,
    )


# ─── find_blocking_grab: the state table ─────────────────────


PRE_FETCH_FAILURES = [
    grabs_storage.STATE_FAILED_COOKIE_EXPIRED,
    grabs_storage.STATE_FAILED_TORRENT_GONE,
    grabs_storage.STATE_FAILED_UNKNOWN,
]


class TestFindBlockingGrab:
    @pytest.mark.parametrize("state", sorted(grabs_storage.BLOCKING_STATES))
    async def test_in_flight_or_post_fetch_state_blocks(self, temp_db, state):
        gid = await _seed_grab(state)
        db = await get_db()
        try:
            prior = await grabs_storage.find_blocking_grab(db, TID)
        finally:
            await db.close()
        assert prior is not None and prior.id == gid

    @pytest.mark.parametrize("state", PRE_FETCH_FAILURES)
    async def test_pre_fetch_failure_does_not_block(self, temp_db, state):
        await _seed_grab(state)
        db = await get_db()
        try:
            assert await grabs_storage.find_blocking_grab(db, TID) is None
        finally:
            await db.close()

    @pytest.mark.parametrize("state", PRE_FETCH_FAILURES)
    async def test_failure_after_mam_served_the_bytes_blocks(self, temp_db, state):
        # e.g. failed_torrent_gone is also the download watcher's
        # "absent from qBit for 12h" — the hash is what tells them apart.
        gid = await _seed_grab(state, qbit_hash=HASH)
        db = await get_db()
        try:
            prior = await grabs_storage.find_blocking_grab(db, TID)
        finally:
            await db.close()
        assert prior is not None and prior.id == gid

    async def test_older_download_found_under_newer_harmless_failure(self, temp_db):
        downloaded = await _seed_grab(grabs_storage.STATE_COMPLETE, qbit_hash=HASH)
        await _seed_grab(grabs_storage.STATE_FAILED_COOKIE_EXPIRED)
        db = await get_db()
        try:
            prior = await grabs_storage.find_blocking_grab(db, TID)
        finally:
            await db.close()
        assert prior is not None and prior.id == downloaded

    async def test_exclude_grab_id_and_other_ids_and_empty_id(self, temp_db):
        gid = await _seed_grab(grabs_storage.STATE_SUBMITTED, qbit_hash=HASH)
        db = await get_db()
        try:
            assert await grabs_storage.find_blocking_grab(
                db, TID, exclude_grab_id=gid,
            ) is None
            assert await grabs_storage.find_blocking_grab(db, "9999") is None
            assert await grabs_storage.find_blocking_grab(db, "") is None
        finally:
            await db.close()


# ─── already_grabbed ─────────────────────────────────────────


class TestAlreadyGrabbed:
    async def test_inject_refused_without_fetch(self, temp_db, mam_says):
        prior = await _seed_grab(grabs_storage.STATE_COMPLETE, qbit_hash=HASH)
        deps = _make_deps()

        result = await inject_grab(deps, torrent_id=TID)

        assert result.action == "skip"
        assert result.reason == "already_grabbed"
        assert result.grab_id == prior
        assert f"grab #{prior}" in (result.error or "")
        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]
        assert await _grab_ids() == [prior]
        assert await _announce_decision(result.announce_id) == (
            "skip", "already_grabbed",
        )

    async def test_irc_announce_refused_without_fetch(self, temp_db, mam_says):
        prior = await _seed_grab(grabs_storage.STATE_SUBMITTED, qbit_hash=HASH)
        deps = _irc_deps()

        result = await handle_announce(deps, _make_announce(TID))

        assert result.reason == "already_grabbed"
        assert result.grab_id == prior
        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]

    async def test_in_flight_queued_grab_blocks(self, temp_db, mam_says):
        # pending_queue is written before the fetch: a grab racing
        # through dispatch right now must block a second one.
        prior = await _seed_grab(grabs_storage.STATE_PENDING_QUEUE)
        deps = _make_deps()

        result = await inject_grab(deps, torrent_id=TID)

        assert result.reason == "already_grabbed"
        assert result.grab_id == prior
        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]

    async def test_override_flags_do_not_bypass_it(self, temp_db, mam_says):
        await _seed_grab(grabs_storage.STATE_COMPLETE, qbit_hash=HASH)
        deps = _make_deps()

        result = await inject_grab(
            deps, torrent_id=TID,
            override_mam_snatched=True, apply_format_dedup=False,
            force_fl_wedge=True,
        )

        assert result.reason == "already_grabbed"
        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]

    @pytest.mark.parametrize("state", PRE_FETCH_FAILURES)
    async def test_retry_after_pre_fetch_failure_is_allowed(
        self, temp_db, mam_says, state,
    ):
        await _seed_grab(state)
        deps = _make_deps()

        result = await inject_grab(deps, torrent_id=TID)

        assert result.action == "submit" and result.reason == "ok"
        assert deps.fetch_torrent.calls == [(TID, "good_token")]  # type: ignore[attr-defined]

    async def test_concurrent_grabs_of_same_id_fetch_once(
        self, temp_db, monkeypatch,
    ):
        # Hold both grabs at the torrent_info lookup — i.e. AFTER the
        # early guard, which both pass because no row exists yet — and
        # release them together. Only the re-check under
        # `grab_claim_lock` stands between them and two fetches.
        arrived = 0
        both_here = asyncio.Event()

        async def barrier_info(torrent_id, token=None, ttl=120):
            nonlocal arrived
            arrived += 1
            if arrived >= 2:
                both_here.set()
            await asyncio.wait_for(both_here.wait(), timeout=5)
            return _info()

        monkeypatch.setattr(dispatch, "get_torrent_info", barrier_info)
        deps = _make_deps()

        results = await asyncio.gather(
            inject_grab(deps, torrent_id=TID),
            inject_grab(deps, torrent_id=TID),
        )

        assert deps.fetch_torrent.calls == [(TID, "good_token")]  # type: ignore[attr-defined]
        assert sorted(r.reason for r in results) == ["already_grabbed", "ok"]
        assert len(await _grab_ids()) == 1


    async def test_concurrent_grabs_training_the_same_author_do_not_deadlock(
        self, temp_db, monkeypatch,
    ):
        # Regression (CI, py3.12): both grabs auto-train the same author;
        # the loser's INSERT failed without a rollback and it carried
        # SQLite's write lock into the claim lock the winner held →
        # deadlock until busy_timeout (30s). Seeded cache = both grabs
        # see the same authoritative author list.
        import time

        from app.mam import torrent_info as torrent_info_mod

        torrent_info_mod._cache[TID] = (
            time.monotonic(), _info(authors={"9": "Shared Author"}),
        )
        arrived = 0
        both_here = asyncio.Event()

        async def barrier_info(torrent_id, token=None, ttl=120):
            nonlocal arrived
            arrived += 1
            if arrived >= 2:
                both_here.set()
            await asyncio.wait_for(both_here.wait(), timeout=5)
            return _info()

        monkeypatch.setattr(dispatch, "get_torrent_info", barrier_info)
        deps = _make_deps()

        results = await asyncio.wait_for(
            asyncio.gather(
                inject_grab(deps, torrent_id=TID),
                inject_grab(deps, torrent_id=TID),
            ),
            timeout=10,
        )

        assert sorted(r.reason for r in results) == ["already_grabbed", "ok"]
        assert len(deps.fetch_torrent.calls) == 1  # type: ignore[attr-defined]


# ─── already_snatched_on_mam ─────────────────────────────────


class TestMySnatched:
    async def test_inject_refused_without_fetch(self, temp_db, mam_says):
        mam_says["answer"] = _info(my_snatched=True)
        deps = _make_deps()

        result = await inject_grab(deps, torrent_id=TID)

        assert result.action == "skip"
        assert result.reason == "already_snatched_on_mam"
        assert result.grab_id is None
        assert "Reingest from disk" in (result.error or "")
        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]
        assert await _grab_ids() == []
        assert await _announce_decision(result.announce_id) == (
            "skip", "already_snatched_on_mam",
        )

    async def test_irc_announce_refused_too(self, temp_db, mam_says):
        mam_says["answer"] = _info(my_snatched=True)
        deps = _irc_deps()

        result = await handle_announce(deps, _make_announce(TID))

        assert result.reason == "already_snatched_on_mam"
        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]

    async def test_explicit_override_grabs_once(self, temp_db, mam_says):
        mam_says["answer"] = _info(my_snatched=True)
        deps = _make_deps()

        result = await inject_grab(
            deps, torrent_id=TID, override_mam_snatched=True,
        )

        assert result.action == "submit" and result.reason == "ok"
        assert deps.fetch_torrent.calls == [(TID, "good_token")]  # type: ignore[attr-defined]

    async def test_not_snatched_grabs_normally(self, temp_db, mam_says):
        deps = _make_deps()
        result = await inject_grab(deps, torrent_id=TID)
        assert result.reason == "ok"
        assert len(deps.fetch_torrent.calls) == 1  # type: ignore[attr-defined]


# ─── torrent_removed_from_mam ────────────────────────────────


class TestRemovedFromMam:
    async def test_inject_of_removed_torrent_skips_fetch(self, temp_db, mam_says):
        mam_says["answer"] = TorrentNotFoundError(
            f"torrent {TID} not found in search results"
        )
        deps = _make_deps()

        result = await inject_grab(deps, torrent_id=TID)

        assert result.action == "skip"
        assert result.reason == "torrent_removed_from_mam"
        assert "no longer on MAM" in (result.error or "")
        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]
        assert await _grab_ids() == []

    async def test_irc_announce_fails_open_on_not_found(self, temp_db, mam_says, monkeypatch):
        # A fresh announce beats MAM's search index: the grab waits for
        # it (D37), and a torrent MAM still doesn't list when the wait
        # runs out is grabbed anyway rather than dropping a real upload.
        mam_says["answer"] = TorrentNotFoundError("not found in search results")
        monkeypatch.setattr(dispatch, "_index_sleep", lambda s: asyncio.sleep(0))
        deps = _irc_deps()

        result = await handle_announce(deps, _make_announce(TID))
        assert result.reason == "waiting_for_mam_index"
        await asyncio.gather(*list(dispatch._held_grabs))

        assert len(deps.fetch_torrent.calls) == 1  # type: ignore[attr-defined]

    async def test_transient_lookup_failure_fails_open(self, temp_db, mam_says):
        mam_says["answer"] = TorrentInfoError("HTTP 503 from search API")
        deps = _make_deps()

        result = await inject_grab(deps, torrent_id=TID)

        assert result.reason == "ok"
        assert len(deps.fetch_torrent.calls) == 1  # type: ignore[attr-defined]


# ─── excluded uploader ───────────────────────────────────────


def _search_body(**overrides) -> bytes:
    item = {
        "id": TID, "title": "The Way of Kings", "catname": "Ebooks - Fantasy",
        "size": "1000", "vip": "0", "free": "0", "fl_vip": "0",
        "personal_freeleech": "0", "my_snatched": "0",
        "author_info": '{"1": "Brandon Sanderson"}',
    }
    item.update(overrides)
    return json.dumps({"perpage": 1, "found": 1, "data": [item]}).encode()


class TestExcludedUploader:
    """Your own uploads are never grabbed (MAM counts that as a re-snatch).
    Runs the real search-API parse through FakeMAM: `ownership` arrives as
    a JSON string (live, 2026-10-07), and the guard was inert while the
    parser only accepted a list."""

    @pytest.fixture(autouse=True)
    def _fresh_info_cache(self):
        invalidate_cache()
        yield
        invalidate_cache()

    async def test_irc_announce_refused_without_fetch(self, temp_db, fake_mam):
        fake_mam.search.body = _search_body(ownership='[12345,"MyAccount"]')
        deps = replace(_irc_deps(), excluded_uploaders=frozenset({"myaccount"}))

        result = await handle_announce(deps, _make_announce(TID))

        assert result.action == "skip"
        assert result.reason == "excluded_uploader:MyAccount"
        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]
        assert await _grab_ids() == []

    async def test_inject_refused_without_fetch(self, temp_db, fake_mam):
        fake_mam.search.body = _search_body(ownership='[12345,"MyAccount"]')
        deps = replace(_make_deps(), excluded_uploaders=frozenset({"myaccount"}))

        result = await inject_grab(deps, torrent_id=TID)

        assert result.reason == "excluded_uploader:MyAccount"
        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]

    async def test_other_uploader_grabs_once(self, temp_db, fake_mam):
        fake_mam.search.body = _search_body(ownership='[1,"SomeoneElse"]')
        deps = replace(_irc_deps(), excluded_uploaders=frozenset({"myaccount"}))

        result = await handle_announce(deps, _make_announce(TID))

        assert result.reason == "ok"
        assert len(deps.fetch_torrent.calls) == 1  # type: ignore[attr-defined]

    async def test_refusal_is_recorded_on_the_announce_row(self, temp_db, fake_mam):
        # Audit L2-07: the row used to keep the filter's `allow`.
        fake_mam.search.body = _search_body(ownership='[12345,"MyAccount"]')
        deps = replace(_irc_deps(), excluded_uploaders=frozenset({"myaccount"}))

        result = await handle_announce(deps, _make_announce(TID))

        assert await _announce_decision(result.announce_id) == (
            "skip", "excluded_uploader:MyAccount",
        )
        assert result.error == "The uploader is on your excluded-uploaders list."


# ─── one torrent-ID form (audit L1-13) ───────────────────────


class TestTorrentIdForm:
    @pytest.mark.parametrize("asked", [" 1234", "1234 ", "01234"])
    async def test_other_spellings_of_a_grabbed_id_are_refused(
        self, temp_db, mam_says, asked,
    ):
        await _seed_grab(grabs_storage.STATE_SUBMITTED, qbit_hash=HASH)
        deps = _make_deps()

        result = await inject_grab(deps, torrent_id=asked)

        assert result.reason == "already_grabbed"
        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]

    async def test_id_carrying_a_wedge_flag_is_refused_as_invalid(
        self, temp_db, mam_says,
    ):
        deps = _make_deps()

        result = await inject_grab(deps, torrent_id="1234&fl=1")

        assert (result.action, result.reason) == ("skip", "invalid_torrent_id")
        assert result.error
        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]
        assert mam_says["calls"] == []
        assert await _grab_ids() == []

    async def test_normalised_id_is_what_gets_fetched(self, temp_db, mam_says):
        deps = _make_deps()
        await inject_grab(deps, torrent_id=" 01234 ")
        assert [c[0] for c in deps.fetch_torrent.calls] == [TID]  # type: ignore[attr-defined]


# ─── one torrent-info lookup per grab (audit L1-14) ─────────


class TestOneLookupPerGrab:
    """`get_torrent_info` caches only successes, so a failing lookup used
    to be repeated by the snatch guard, co-author auto-train and the
    economic context. Counted at MAM's search endpoint (`mam_search`)."""

    @pytest.fixture(autouse=True)
    def _fresh_info_cache(self):
        invalidate_cache()
        yield
        invalidate_cache()

    @staticmethod
    def _wedge_policy_deps(**kw):
        from app.policy.engine import PolicyConfig
        deps = _make_deps(**kw)
        deps.policy_config = PolicyConfig(use_wedge=True)  # eco ctx needs the lookup
        return deps

    async def test_inject_with_mam_unreachable_searches_once(self, temp_db, mam_search):
        import httpx
        mam_search["items"][MG_TID] = httpx.ConnectError("down")
        deps = self._wedge_policy_deps()

        result = await inject_grab(deps, torrent_id=MG_TID)

        assert mam_search["calls"] == [MG_TID]
        assert result.reason == "ok"  # fails open, as before

    async def test_irc_with_mam_unreachable_searches_once(self, temp_db, mam_search):
        import httpx
        mam_search["items"][MG_TID] = httpx.ConnectError("down")
        deps = self._wedge_policy_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
        )

        result = await handle_announce(deps, _make_announce(MG_TID))

        assert mam_search["calls"] == [MG_TID]
        assert result.reason == "ok"

    async def test_found_torrent_searches_once(self, temp_db, mam_search):
        deps = self._wedge_policy_deps()
        await inject_grab(deps, torrent_id=MG_TID)
        assert mam_search["calls"] == [MG_TID]


# ─── cookie-retry job ────────────────────────────────────────


class TestCookieRetryGuard:
    async def test_row_mam_already_served_is_retired_not_refetched(self, temp_db):
        gid = await _seed_grab(
            grabs_storage.STATE_FAILED_COOKIE_EXPIRED, qbit_hash=HASH,
        )
        deps = _make_deps()

        await cookie_retry.tick(deps)

        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]
        db = await get_db()
        try:
            grab = await grabs_storage.get_grab(db, gid)
        finally:
            await db.close()
        assert grab.state == grabs_storage.STATE_FAILED_UNKNOWN
        assert "already served" in (grab.failed_reason or "")

    async def test_row_superseded_by_newer_grab_is_not_refetched(self, temp_db):
        old = await _seed_grab(grabs_storage.STATE_FAILED_COOKIE_EXPIRED)
        newer = await _seed_grab(grabs_storage.STATE_SUBMITTED, qbit_hash=HASH)
        deps = _make_deps()

        await cookie_retry.tick(deps)

        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]
        db = await get_db()
        try:
            grab = await grabs_storage.get_grab(db, old)
        finally:
            await db.close()
        assert grab.state == grabs_storage.STATE_FAILED_UNKNOWN
        assert f"superseded by grab #{newer}" in (grab.failed_reason or "")

    async def test_never_fetched_row_is_retried_once(self, temp_db):
        gid = await _seed_grab(grabs_storage.STATE_FAILED_COOKIE_EXPIRED)
        qbit = _FakeQbit()
        deps = _make_deps(qbit=qbit)

        result = await cookie_retry.tick(deps)

        assert result.succeeded == 1
        assert deps.fetch_torrent.calls == [(TID, "good_token")]  # type: ignore[attr-defined]
        assert len(qbit.add_calls) == 1
        db = await get_db()
        try:
            assert (await grabs_storage.get_grab(db, gid)).state == (
                grabs_storage.STATE_SUBMITTED
            )
        finally:
            await db.close()

    async def test_failed_retry_lands_back_in_a_retryable_state(self, temp_db):
        # The row is claimed as `fetched` before the fetch; a fetch that
        # fails again must not leave it stuck there (fetched blocks).
        gid = await _seed_grab(grabs_storage.STATE_FAILED_COOKIE_EXPIRED)
        deps = _make_deps(fetch_result=GrabResult(
            success=False, failure_kind="cookie_expired",
            failure_detail="HTTP 403 from MAM",
        ))

        await cookie_retry.tick(deps)

        db = await get_db()
        try:
            grab = await grabs_storage.get_grab(db, gid)
            assert grab.state == grabs_storage.STATE_FAILED_COOKIE_EXPIRED
            assert await grabs_storage.find_blocking_grab(db, TID) is None
        finally:
            await db.close()


# ─── tentative approve ───────────────────────────────────────


class TestTentativeApprove:
    async def _approve(self, monkeypatch, result: DispatchResult, **kwargs):
        from app import state
        from app.routers import tentative as tentative_router

        seen: dict = {}

        async def fake_inject(*args, **kw):
            seen.update(kw)
            return result

        class _NullDispatcher:
            pass

        monkeypatch.setattr(state, "dispatcher", _NullDispatcher())
        monkeypatch.setattr(tentative_router, "inject_grab", fake_inject)

        db = await get_db()
        try:
            tent_id = await tentative_storage.upsert_tentative(
                db, mam_torrent_id=TID, torrent_name="The Way of Kings",
                author_blob="Brandon Sanderson",
            )
        finally:
            await db.close()
        resp = await tentative_router.approve(tent_id, **kwargs)
        db = await get_db()
        try:
            row = await tentative_storage.get_tentative(db, tent_id)
        finally:
            await db.close()
        return resp, row.status, seen

    async def test_mam_snatched_skip_leaves_row_pending(self, temp_db, monkeypatch):
        resp, status, seen = await self._approve(monkeypatch, DispatchResult(
            action="skip", reason="already_snatched_on_mam", announce_id=1,
            error="MAM says this account already snatched torrent 1234.",
        ))
        assert resp.ok is False
        assert resp.status == tentative_storage.TENTATIVE_PENDING
        assert status == tentative_storage.TENTATIVE_PENDING
        assert "already snatched" in (resp.error or "")
        assert seen["override_mam_snatched"] is False

    async def test_override_is_passed_through(self, temp_db, monkeypatch):
        resp, status, seen = await self._approve(
            monkeypatch,
            DispatchResult(action="submit", reason="ok", announce_id=1, grab_id=7),
            override_mam_snatched=True,
        )
        assert resp.ok is True
        assert status == tentative_storage.TENTATIVE_APPROVED
        assert seen["override_mam_snatched"] is True

    async def test_already_grabbed_skip_still_approves(self, temp_db, monkeypatch):
        resp, status, _ = await self._approve(monkeypatch, DispatchResult(
            action="skip", reason="already_grabbed", announce_id=1, grab_id=3,
            error="Seshat already grabbed MAM torrent 1234 (grab #3, complete)",
        ))
        assert resp.ok is False
        assert resp.grab_id == 3
        assert status == tentative_storage.TENTATIVE_APPROVED
