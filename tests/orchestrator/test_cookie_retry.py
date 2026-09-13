"""
Unit tests for the cookie-rotation retry job.

Tests target `tick()` directly. Coverage:
  - No-op when no failed_cookie_expired grabs exist
  - Retries all failed_cookie_expired grabs
  - Successful retry transitions to submitted + ledger entry
  - Re-failure with cookie_expired leaves state unchanged
  - Re-failure with torrent_not_found transitions state
  - Re-failure with qBit rejection transitions state
"""
from typing import Optional

from app.clients.base import AddResult, TorrentInfo
from app.database import get_db
from app.filter.gate import FilterConfig
from app.mam import cookie as cookie_mod
from app.mam.grab import GrabResult
from app.orchestrator.cookie_retry import tick
from app.orchestrator.dispatch import DispatcherDeps
from app.rate_limit import ledger as ledger_mod
from app.storage import grabs as grabs_storage
from tests.fake_mam import MINIMAL_BENCODED_TORRENT


# ─── Fakes ───────────────────────────────────────────────────


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


def _make_fetch(result: GrabResult = None):
    final = result or GrabResult(
        success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT
    )
    calls: list[tuple[str, str]] = []

    async def fake_fetch(torrent_id: str, token: str) -> GrabResult:
        calls.append((torrent_id, token))
        return final

    fake_fetch.calls = calls  # type: ignore[attr-defined]
    return fake_fetch


def _make_deps(
    *,
    qbit: _FakeQbit = None,
    fetch_result: GrabResult = None,
) -> DispatcherDeps:
    return DispatcherDeps(
        filter_config=FilterConfig(allowed_categories=frozenset()),
        mam_token="fresh_cookie",
        qbit_category="mam-complete",
        budget_cap=200,
        queue_max=100,
        queue_mode_enabled=True,
        seed_seconds_required=72 * 3600,
        db_factory=get_db,
        fetch_torrent=_make_fetch(fetch_result),
        qbit=qbit or _FakeQbit(),
    )


async def _insert_failed_grab(db, torrent_id: str = "12345") -> int:
    """Insert a grab in failed_cookie_expired state."""
    grab_id = await grabs_storage.create_grab(
        db,
        announce_id=None,
        mam_torrent_id=torrent_id,
        torrent_name="Test Book",
        category="ebooks fantasy",
        author_blob="Test Author",
        state=grabs_storage.STATE_FAILED_COOKIE_EXPIRED,
    )
    await grabs_storage.set_state(
        db,
        grab_id,
        grabs_storage.STATE_FAILED_COOKIE_EXPIRED,
        failed_reason="HTTP 403 from MAM",
    )
    return grab_id


# ─── No-op when nothing to retry ────────────────────────────


class TestNoOp:
    async def test_empty_db(self, temp_db):
        deps = _make_deps()
        result = await tick(deps)
        assert result.found == 0
        assert result.retried == 0
        assert result.succeeded == 0
        assert result.failed_again == 0
        assert result.error is None

    async def test_no_cookie_expired_grabs(self, temp_db):
        # Insert a grab in a different failed state.
        db = await get_db()
        try:
            grab_id = await grabs_storage.create_grab(
                db,
                announce_id=None,
                mam_torrent_id="99999",
                torrent_name="Other Book",
                category="ebooks fantasy",
                author_blob="Other Author",
                state=grabs_storage.STATE_FAILED_UNKNOWN,
            )
        finally:
            await db.close()

        deps = _make_deps()
        result = await tick(deps)
        assert result.found == 0


# ─── Successful retry ───────────────────────────────────────


class TestSuccessfulRetry:
    async def test_retries_and_submits(self, temp_db):
        db = await get_db()
        try:
            grab_id = await _insert_failed_grab(db, "11111")
        finally:
            await db.close()

        deps = _make_deps()
        result = await tick(deps)

        assert result.found == 1
        assert result.retried == 1
        assert result.succeeded == 1
        assert result.failed_again == 0

        # Verify the grab is now submitted.
        db = await get_db()
        try:
            grab = await grabs_storage.get_grab(db, grab_id)
            assert grab.state == grabs_storage.STATE_SUBMITTED
            assert grab.qbit_hash is not None

            # Verify ledger entry was created.
            active = await ledger_mod.count_active(db)
            assert active == 1
        finally:
            await db.close()

    async def test_retries_multiple_grabs(self, temp_db):
        db = await get_db()
        try:
            await _insert_failed_grab(db, "11111")
            await _insert_failed_grab(db, "22222")
            await _insert_failed_grab(db, "33333")
        finally:
            await db.close()

        deps = _make_deps()
        result = await tick(deps)

        assert result.found == 3
        assert result.retried == 3
        assert result.succeeded == 3

    async def test_uses_fresh_token(self, temp_db):
        """The retry fetch uses the live cookie, not the deps snapshot.

        `app.mam.cookie` is the authority for the current mam_id — it
        is written both by rotation capture and by the Settings save
        path — so `live_mam_token()` prefers it over the `mam_token`
        frozen into DispatcherDeps at build time.
        """
        db = await get_db()
        try:
            await _insert_failed_grab(db)
        finally:
            await db.close()

        fetch = _make_fetch()
        deps = DispatcherDeps(
            filter_config=FilterConfig(allowed_categories=frozenset()),
            mam_token="stale_snapshot_cookie",
            qbit_category="mam-complete",
            budget_cap=200,
            queue_max=100,
            queue_mode_enabled=True,
            seed_seconds_required=72 * 3600,
            db_factory=get_db,
            fetch_torrent=fetch,
            qbit=_FakeQbit(),
        )

        saved = cookie_mod._current_token
        try:
            cookie_mod.set_current_token("brand_new_cookie")
            await tick(deps)
        finally:
            cookie_mod._current_token = saved

        assert fetch.calls[0][1] == "brand_new_cookie"

    async def test_falls_back_to_deps_token_when_no_live_token(self, temp_db):
        """With nothing seeded live, the deps snapshot is still used.

        Keeps the accessor safe for tests and for the window between
        process start and the first `set_current_token()` seed.
        """
        db = await get_db()
        try:
            await _insert_failed_grab(db)
        finally:
            await db.close()

        fetch = _make_fetch()
        deps = DispatcherDeps(
            filter_config=FilterConfig(allowed_categories=frozenset()),
            mam_token="deps_fallback_cookie",
            qbit_category="mam-complete",
            budget_cap=200,
            queue_max=100,
            queue_mode_enabled=True,
            seed_seconds_required=72 * 3600,
            db_factory=get_db,
            fetch_torrent=fetch,
            qbit=_FakeQbit(),
        )

        saved = cookie_mod._current_token
        try:
            cookie_mod._current_token = None
            await tick(deps)
        finally:
            cookie_mod._current_token = saved

        assert fetch.calls[0][1] == "deps_fallback_cookie"

    async def test_cookie_saved_after_loop_started_is_picked_up(self, temp_db):
        """Regression: v3.10.0 — grabs 401'd until container restart.

        The background loops (`cookie_retry`, `budget_watcher`, the IRC
        listener) capture ONE DispatcherDeps at startup — `main.py`'s
        `deps_for_loops`. Saving a new MAM cookie rebuilds
        `state.dispatcher`, but those already-running loops keep their
        original object forever, so they went on replaying the dead
        token and every retry came back `cookie_expired (HTTP 401)`
        even though the cookie had been updated correctly.

        This reproduces that exact shape: a loop holding a deps object
        built BEFORE the credential save must still send the new token.
        """
        db = await get_db()
        try:
            await _insert_failed_grab(db)
        finally:
            await db.close()

        fetch = _make_fetch()
        # Deps as captured at startup, with the since-expired cookie.
        deps_captured_at_startup = DispatcherDeps(
            filter_config=FilterConfig(allowed_categories=frozenset()),
            mam_token="expired_cookie_from_boot",
            qbit_category="mam-complete",
            budget_cap=200,
            queue_max=100,
            queue_mode_enabled=True,
            seed_seconds_required=72 * 3600,
            db_factory=get_db,
            fetch_torrent=fetch,
            qbit=_FakeQbit(),
        )

        saved = cookie_mod._current_token
        try:
            # The user pastes a new cookie into Settings. This is what
            # `app/routers/credentials.py::_apply_credential` does.
            cookie_mod.set_current_token("cookie_pasted_into_settings")

            # The loop ticks with its stale captured deps.
            await tick(deps_captured_at_startup)
        finally:
            cookie_mod._current_token = saved

        assert fetch.calls[0][1] == "cookie_pasted_into_settings"
        assert fetch.calls[0][1] != deps_captured_at_startup.mam_token


# ─── Failed retry ───────────────────────────────────────────


class TestFailedRetry:
    async def test_still_cookie_expired(self, temp_db):
        db = await get_db()
        try:
            grab_id = await _insert_failed_grab(db)
        finally:
            await db.close()

        deps = _make_deps(
            fetch_result=GrabResult(
                success=False,
                failure_kind="cookie_expired",
                failure_detail="HTTP 403 again",
            )
        )
        result = await tick(deps)

        assert result.found == 1
        assert result.succeeded == 0
        assert result.failed_again == 1

        # State stays as failed_cookie_expired.
        db = await get_db()
        try:
            grab = await grabs_storage.get_grab(db, grab_id)
            assert grab.state == grabs_storage.STATE_FAILED_COOKIE_EXPIRED
        finally:
            await db.close()

    async def test_torrent_gone(self, temp_db):
        db = await get_db()
        try:
            grab_id = await _insert_failed_grab(db)
        finally:
            await db.close()

        deps = _make_deps(
            fetch_result=GrabResult(
                success=False,
                failure_kind="torrent_not_found",
                failure_detail="HTTP 404",
            )
        )
        result = await tick(deps)

        assert result.failed_again == 1

        # State transitions to failed_torrent_gone.
        db = await get_db()
        try:
            grab = await grabs_storage.get_grab(db, grab_id)
            assert grab.state == grabs_storage.STATE_FAILED_TORRENT_GONE
        finally:
            await db.close()

    async def test_qbit_rejection(self, temp_db):
        db = await get_db()
        try:
            grab_id = await _insert_failed_grab(db)
        finally:
            await db.close()

        qbit = _FakeQbit(
            add_result=AddResult(
                success=False,
                failure_kind="rejected",
                failure_detail="qBit rejected the torrent",
            )
        )
        deps = _make_deps(qbit=qbit)
        result = await tick(deps)

        assert result.failed_again == 1

        db = await get_db()
        try:
            grab = await grabs_storage.get_grab(db, grab_id)
            assert grab.state == grabs_storage.STATE_FAILED_QBIT_REJECTED
        finally:
            await db.close()

    async def test_qbit_duplicate(self, temp_db):
        db = await get_db()
        try:
            grab_id = await _insert_failed_grab(db)
        finally:
            await db.close()

        qbit = _FakeQbit(
            add_result=AddResult(
                success=False,
                failure_kind="duplicate",
                failure_detail="torrent already exists",
            )
        )
        deps = _make_deps(qbit=qbit)
        result = await tick(deps)

        assert result.failed_again == 1

        db = await get_db()
        try:
            grab = await grabs_storage.get_grab(db, grab_id)
            assert grab.state == grabs_storage.STATE_DUPLICATE_IN_QBIT
        finally:
            await db.close()
