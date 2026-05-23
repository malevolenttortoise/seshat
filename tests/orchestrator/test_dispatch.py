"""
Unit tests for the orchestration dispatcher.

The dispatcher is the integration layer where everything we built
in Phase 1 comes together. Each test exercises one path through
the pipeline using the real database (via the temp_db fixture)
plus injected fakes for the .torrent fetcher and the qBit client.

Coverage targets:
  - Filter says skip → audit row, no grab row, no fetch attempt
  - Filter says allow + budget OK → fetch + submit happy path
  - Filter says allow + budget full + queue mode → queue
  - Filter says allow + budget full + drop mode → drop
  - Fetch failure: cookie_expired → grab marked failed_cookie_expired
  - Fetch failure: torrent_not_found → grab marked failed_torrent_gone
  - qBit failure: rejected → grab marked failed_qbit_rejected
  - inject_grab bypasses filter, still goes through rate limiter
  - Bad torrent bytes → grab marked failed_qbit_rejected (bencode error)
"""
from typing import Optional

import pytest

from app.clients.base import AddResult, TorrentClient, TorrentInfo
from app.database import get_db
from app.filter.gate import Announce, FilterConfig
from app.filter.normalize import normalize_author, normalize_category
from app.mam.grab import GrabResult
from app.orchestrator.dispatch import (
    DispatcherDeps,
    handle_announce,
    inject_grab,
)
from app.policy.engine import PolicyConfig
from app.rate_limit import ledger as ledger_mod
from app.rate_limit import queue as queue_mod
from app.storage import grabs as grabs_storage
from tests.fake_mam import MINIMAL_BENCODED_TORRENT


# ─── Fakes ───────────────────────────────────────────────────


class _FakeQbit:
    """Minimal TorrentClient implementation for dispatcher tests.

    Doesn't speak HTTP at all — just records every call and lets
    each test program the response shape. Satisfies the
    `TorrentClient` Protocol structurally; no inheritance needed.
    """

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
            {
                "size": len(torrent_bytes),
                "category": category,
                "tags": tags,
                "save_path": save_path,
            }
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


def _make_fetch(result: GrabResult):
    """Build a fetch_torrent fake that returns a fixed result."""
    calls: list[tuple[str, str]] = []

    async def fake_fetch(torrent_id: str, token: str, **kwargs) -> GrabResult:
        calls.append((torrent_id, token))
        return result

    fake_fetch.calls = calls  # type: ignore[attr-defined]
    return fake_fetch


def _make_filter_config(
    *,
    allowed: list[str] = None,
    ignored: list[str] = None,
    categories: list[str] = None,
) -> FilterConfig:
    cats = categories if categories is not None else [
        "Ebooks - Fantasy",
        "Audiobooks - Fantasy",
    ]
    return FilterConfig(
        allowed_categories=frozenset(normalize_category(c) for c in cats),
        allowed_authors=frozenset(normalize_author(a) for a in (allowed or [])),
        ignored_authors=frozenset(normalize_author(i) for i in (ignored or [])),
    )


def _make_deps(
    filter_config: FilterConfig = None,
    *,
    fetch_result: GrabResult = None,
    qbit: TorrentClient = None,
    budget_cap: int = 200,
    queue_max: int = 100,
    queue_mode_enabled: bool = True,
    mam_token: str = "good_token",
) -> DispatcherDeps:
    return DispatcherDeps(
        filter_config=filter_config or _make_filter_config(),
        mam_token=mam_token,
        qbit_category="mam-complete",
        budget_cap=budget_cap,
        queue_max=queue_max,
        queue_mode_enabled=queue_mode_enabled,
        seed_seconds_required=72 * 3600,
        db_factory=get_db,
        fetch_torrent=_make_fetch(
            fetch_result
            or GrabResult(success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT)
        ),
        qbit=qbit or _FakeQbit(),
    )


def _make_announce(
    torrent_id: str = "1234",
    *,
    category: str = "Ebooks - Fantasy",
    author_blob: str = "Brandon Sanderson",
    torrent_name: str = "The Way of Kings",
) -> Announce:
    return Announce(
        torrent_id=torrent_id,
        torrent_name=torrent_name,
        category=category,
        author_blob=author_blob,
    )


# ─── handle_announce: filter skip ────────────────────────────


class TestFilterSkip:
    async def test_disallowed_category_audited_no_grab(self, temp_db):
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"])
        )
        announce = _make_announce(category="Ebooks - Romance")
        result = await handle_announce(deps, announce)

        assert result.action == "skip"
        assert result.reason == "category_not_allowed"
        assert result.grab_id is None
        # Audit row was written
        assert result.announce_id > 0

        # Fetcher must NOT have been called
        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]

    async def test_unknown_author_skipped_no_grab(self, temp_db):
        deps = _make_deps(filter_config=_make_filter_config())
        announce = _make_announce(author_blob="Some Random Author")
        result = await handle_announce(deps, announce)

        assert result.action == "skip"
        assert result.reason == "author_not_allowlisted"
        assert result.grab_id is None
        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]


# ─── handle_announce: happy submit path ──────────────────────


class TestSubmitPath:
    async def test_happy_path(self, temp_db):
        qbit = _FakeQbit()
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
            qbit=qbit,
        )
        result = await handle_announce(deps, _make_announce())

        assert result.action == "submit"
        assert result.reason == "ok"
        assert result.grab_id is not None
        assert result.qbit_hash is not None
        assert len(result.qbit_hash) == 40

        # qBit was called once with the right category and bytes.
        assert len(qbit.add_calls) == 1
        assert qbit.add_calls[0]["category"] == "mam-complete"
        assert qbit.add_calls[0]["size"] == len(MINIMAL_BENCODED_TORRENT)

    async def test_grab_row_state_is_submitted(self, temp_db):
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"])
        )
        result = await handle_announce(deps, _make_announce())

        db = await get_db()
        try:
            grab = await grabs_storage.get_grab(db, result.grab_id)
            assert grab is not None
            assert grab.state == grabs_storage.STATE_SUBMITTED
            assert grab.qbit_hash == result.qbit_hash
            assert grab.submitted_at is not None
        finally:
            await db.close()

    async def test_ledger_records_submitted_grab(self, temp_db):
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"])
        )
        result = await handle_announce(deps, _make_announce())

        db = await get_db()
        try:
            assert await ledger_mod.count_active(db) == 1
            row = await ledger_mod.get_row(db, result.grab_id)
            assert row is not None
            assert row.qbit_hash == result.qbit_hash
            assert row.released_at is None
        finally:
            await db.close()

    async def test_qbit_tags_threaded_through_to_add_call(self, temp_db):
        # The dispatcher must pass DispatcherDeps.qbit_tags into the
        # qBit add_torrent call so the user's seshat-seed tag
        # actually lands on every torrent.
        qbit = _FakeQbit()
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
            qbit=qbit,
        )
        # Patch the deps to set tags (the helper defaults to empty)
        deps = DispatcherDeps(
            **{**deps.__dict__, "qbit_tags": ["seshat-seed"]}
        )
        await handle_announce(deps, _make_announce())

        assert len(qbit.add_calls) == 1
        assert qbit.add_calls[0]["tags"] == ["seshat-seed"]

    async def test_empty_qbit_tags_passes_none_not_empty_list(self, temp_db):
        # Defensive: an empty tag list means "no tagging", and the
        # dispatcher should pass None (not []) so the qBit client
        # omits the form field entirely. Without this guard the qBit
        # client would receive `tags=[]`, which on its add path
        # produces no form field anyway, but we want the contract
        # explicit so future refactors don't accidentally start
        # sending `tags=` (which qBit could interpret as "clear all
        # tags" on update endpoints).
        qbit = _FakeQbit()
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
            qbit=qbit,
        )
        # Default qbit_tags is empty
        await handle_announce(deps, _make_announce())

        assert qbit.add_calls[0]["tags"] is None


# ─── handle_announce: queue path ─────────────────────────────


class TestQueuePath:
    async def test_full_budget_queues_grab(self, temp_db):
        qbit = _FakeQbit()
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
            qbit=qbit,
            budget_cap=0,           # zero cap, every grab is full
            queue_mode_enabled=True,
        )
        result = await handle_announce(deps, _make_announce())

        assert result.action == "queue"
        assert result.grab_id is not None
        assert result.qbit_hash is not None  # we still computed it

        # qBit was NOT called
        assert qbit.add_calls == []

        db = await get_db()
        try:
            assert await queue_mod.size(db) == 1
            grab = await grabs_storage.get_grab(db, result.grab_id)
            assert grab.state == grabs_storage.STATE_PENDING_QUEUE
            # Ledger should NOT have an entry yet — only submitted
            # grabs count against the active budget.
            assert await ledger_mod.count_active(db) == 0
        finally:
            await db.close()


# ─── handle_announce: drop path ──────────────────────────────


class TestDropPath:
    async def test_drop_mode_full_budget_drops(self, temp_db):
        qbit = _FakeQbit()
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
            qbit=qbit,
            budget_cap=0,
            queue_mode_enabled=False,
        )
        result = await handle_announce(deps, _make_announce())

        assert result.action == "drop"
        assert result.reason == "budget_full_drop_mode"
        assert result.grab_id is None
        # No fetch, no qBit submission
        assert deps.fetch_torrent.calls == []  # type: ignore[attr-defined]
        assert qbit.add_calls == []

        # But the audit row exists
        assert result.announce_id > 0


# ─── handle_announce: failure modes ──────────────────────────


class TestFetchFailures:
    async def test_cookie_expired_marks_grab_failed_cookie(self, temp_db):
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
            fetch_result=GrabResult(
                success=False,
                failure_kind="cookie_expired",
                failure_detail="MAM returned HTML login page",
            ),
        )
        result = await handle_announce(deps, _make_announce())

        assert result.action == "submit"  # the rate decision
        assert result.reason == "fetch_failed:cookie_expired"
        assert result.error == "MAM returned HTML login page"
        assert result.grab_id is not None

        db = await get_db()
        try:
            grab = await grabs_storage.get_grab(db, result.grab_id)
            assert grab.state == grabs_storage.STATE_FAILED_COOKIE_EXPIRED
            # The cookie-rotation retry job in the next phase scans
            # for this exact state — pin it down here.
        finally:
            await db.close()

    async def test_torrent_not_found_marks_failed_gone(self, temp_db):
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
            fetch_result=GrabResult(
                success=False,
                failure_kind="torrent_not_found",
                failure_detail="HTTP 404 from MAM",
            ),
        )
        result = await handle_announce(deps, _make_announce())

        db = await get_db()
        try:
            grab = await grabs_storage.get_grab(db, result.grab_id)
            assert grab.state == grabs_storage.STATE_FAILED_TORRENT_GONE
        finally:
            await db.close()

    async def test_unknown_failure_marks_failed_unknown(self, temp_db):
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
            fetch_result=GrabResult(
                success=False,
                failure_kind="network_error",
                failure_detail="connection timed out",
            ),
        )
        result = await handle_announce(deps, _make_announce())

        db = await get_db()
        try:
            grab = await grabs_storage.get_grab(db, result.grab_id)
            assert grab.state == grabs_storage.STATE_FAILED_UNKNOWN
        finally:
            await db.close()


class TestQbitFailures:
    async def test_qbit_rejected_marks_failed_qbit_rejected(self, temp_db):
        qbit = _FakeQbit(
            add_result=AddResult(
                success=False,
                failure_kind="rejected",
                failure_detail="HTTP 415 unsupported media",
            )
        )
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
            qbit=qbit,
        )
        result = await handle_announce(deps, _make_announce())

        assert result.error == "HTTP 415 unsupported media"
        db = await get_db()
        try:
            grab = await grabs_storage.get_grab(db, result.grab_id)
            assert grab.state == grabs_storage.STATE_FAILED_QBIT_REJECTED
            # No ledger entry — never made it to "active in qBit"
            assert await ledger_mod.count_active(db) == 0
        finally:
            await db.close()

    async def test_client_auth_failure_queues_for_retry(self, temp_db):
        """When the download client is unreachable (auth_failed), the
        grab is queued for retry instead of permanently failing."""
        qbit = _FakeQbit(
            add_result=AddResult(
                success=False,
                failure_kind="auth_failed",
                failure_detail="client re-login failed",
            )
        )
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
            qbit=qbit,
        )
        result = await handle_announce(deps, _make_announce())

        assert result.action == "queue"
        assert "client_unreachable" in result.reason

        db = await get_db()
        try:
            grab = await grabs_storage.get_grab(db, result.grab_id)
            assert grab.state == grabs_storage.STATE_PENDING_QUEUE
        finally:
            await db.close()

    async def test_qbit_duplicate_marks_duplicate_in_qbit(self, temp_db):
        # qBit's "Fails." response (HTTP 200 + literal body) means
        # the torrent is already in the client. Seshat classifies
        # this as `duplicate` (not a real failure — the torrent IS
        # in qBit, which is what we wanted) and routes it to a
        # distinct grab state so the audit log + UI can distinguish
        # "we tried to grab something already there" from "we tried
        # and qBit barfed."
        qbit = _FakeQbit(
            add_result=AddResult(
                success=False,
                failure_kind="duplicate",
                failure_detail="qBit reports torrent already exists",
            )
        )
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
            qbit=qbit,
        )
        result = await handle_announce(deps, _make_announce())

        db = await get_db()
        try:
            grab = await grabs_storage.get_grab(db, result.grab_id)
            assert grab.state == grabs_storage.STATE_DUPLICATE_IN_QBIT
            # No ledger entry — Seshat never registered the torrent
            # against its budget. Future iteration could detect the
            # duplicate via list_torrents and create a ledger row
            # against the existing qBit hash, but Phase 1 just logs
            # and moves on.
            assert await ledger_mod.count_active(db) == 0
        finally:
            await db.close()


class TestBadTorrentFile:
    async def test_unparseable_bytes_short_circuits_qbit(self, temp_db):
        qbit = _FakeQbit()
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
            fetch_result=GrabResult(
                success=True,
                torrent_bytes=b"this is definitely not bencoded",
            ),
            qbit=qbit,
        )
        result = await handle_announce(deps, _make_announce())

        assert result.action == "submit"
        assert result.reason == "bad_torrent_file"
        assert qbit.add_calls == []  # never reached

        db = await get_db()
        try:
            grab = await grabs_storage.get_grab(db, result.grab_id)
            assert grab.state == grabs_storage.STATE_FAILED_QBIT_REJECTED
            assert "unparseable" in (grab.failed_reason or "").lower()
        finally:
            await db.close()


# ─── inject_grab ─────────────────────────────────────────────


class TestInjectGrab:
    async def test_bypasses_filter(self, temp_db):
        # Filter would skip this (no allowed authors at all), but
        # inject_grab should still go through because the user
        # explicitly asked for it.
        qbit = _FakeQbit()
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=[]),
            qbit=qbit,
        )
        result = await inject_grab(
            deps,
            torrent_id="9999",
            torrent_name="A Manually Requested Book",
        )

        assert result.action == "submit"
        assert result.grab_id is not None
        assert len(qbit.add_calls) == 1

    async def test_respects_rate_limiter(self, temp_db):
        # Even an injected grab should queue when budget is full —
        # the user might be hitting the inject endpoint repeatedly
        # and we still need to respect MAM's snatch cap.
        qbit = _FakeQbit()
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=[]),
            qbit=qbit,
            budget_cap=0,
            queue_mode_enabled=True,
        )
        result = await inject_grab(deps, torrent_id="9999")

        assert result.action == "queue"
        assert qbit.add_calls == []

    async def test_audit_row_uses_manual_inject_reason(self, temp_db):
        deps = _make_deps(filter_config=_make_filter_config(allowed=[]))
        result = await inject_grab(deps, torrent_id="9999")

        # The audit row should record `manual_inject` as the
        # decision reason, not whatever the filter would have said.
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT decision_reason FROM announces WHERE id = ?",
                (result.announce_id,),
            )
            row = await cursor.fetchone()
            assert row[0] == "manual_inject"
        finally:
            await db.close()


# ─── Event hook ──────────────────────────────────────────────


class TestEventHook:
    async def test_events_emitted_in_order(self, temp_db):
        events: list[tuple[str, dict]] = []

        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
        )
        deps.on_event = lambda name, payload: events.append((name, payload))

        await handle_announce(deps, _make_announce())

        names = [e[0] for e in events]
        assert "announce_recorded" in names
        assert "rate_decision" in names
        assert "submitted" in names

    async def test_event_hook_exception_does_not_break_dispatch(self, temp_db):
        # A buggy on_event hook should NOT take down the dispatcher.
        def bad_hook(name, payload):
            raise RuntimeError("simulated observability bug")

        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
        )
        deps.on_event = bad_hook

        result = await handle_announce(deps, _make_announce())
        assert result.action == "submit"  # dispatch still completed


# ─── Dry-run mode ───────────────────────────────────────────


class TestDryRun:
    async def test_dry_run_skips_fetch(self, temp_db):
        """Dry-run mode runs filter + policy but never calls fetch_torrent."""
        fetch = _make_fetch(
            GrabResult(success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT)
        )
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
            fetch_result=GrabResult(
                success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT
            ),
        )
        deps.dry_run = True
        deps.fetch_torrent = fetch

        result = await handle_announce(deps, _make_announce())

        assert result.action == "skip"
        assert "dry_run" in result.reason
        # fetch_torrent should never have been called.
        assert len(fetch.calls) == 0

    async def test_dry_run_still_writes_audit_row(self, temp_db):
        """Dry-run still records the announce for audit purposes."""
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
        )
        deps.dry_run = True

        result = await handle_announce(deps, _make_announce())

        assert result.announce_id > 0

    async def test_dry_run_filter_skip_still_works(self, temp_db):
        """If the filter skips, dry-run doesn't change the behavior."""
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Nobody"]),
        )
        deps.dry_run = True

        result = await handle_announce(deps, _make_announce())

        assert result.action == "skip"
        assert "dry_run" not in result.reason  # regular filter skip

    async def test_dry_run_no_grab_row_created(self, temp_db):
        """Dry-run should not create a grab row."""
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
        )
        deps.dry_run = True

        result = await handle_announce(deps, _make_announce())

        assert result.grab_id is None


# ─── v2.21.2 — live-read runtime kill switches ─────────────────
#
# The `dry_run` and `mam_irc_enabled` settings are operator-facing
# kill switches in Settings → MAM. Pre-v2.21.2 they were read once
# at lifespan startup / dispatcher build, so flipping them on a
# running container had no effect until the next restart — the
# toggles looked like they did something but didn't actually stop
# grabs from happening. Critical safety gap caught during a
# real incident 2026-05-23. v2.21.2 reads both from live settings
# at the top of `handle_announce` and `inject_grab`.


class TestLiveKillSwitches:
    async def test_live_dry_run_skips_irc_announce(
        self, temp_db, monkeypatch,
    ):
        """Flipping `dry_run` in settings should stop the next
        IRC announce from grabbing, even when `deps.dry_run=False`
        (which is the case on a running dispatcher that was built
        before the toggle)."""
        from app import config as app_config
        fake_settings = {"dry_run": True, "mam_irc_enabled": True}
        monkeypatch.setattr(
            app_config, "load_settings", lambda: dict(fake_settings),
        )
        fetch = _make_fetch(
            GrabResult(success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT)
        )
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
            fetch_result=GrabResult(
                success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT,
            ),
        )
        deps.dry_run = False  # canonical "dispatcher built before toggle"
        deps.fetch_torrent = fetch

        result = await handle_announce(deps, _make_announce())

        assert result.action == "skip"
        assert result.reason == "dry_run_live"
        # fetch_torrent must not have been called — kill switch is
        # at the top of handle_announce, before any side effects.
        assert len(fetch.calls) == 0

    async def test_live_irc_disabled_skips_irc_announce(
        self, temp_db, monkeypatch,
    ):
        """Flipping `mam_irc_enabled=False` in settings should stop
        the next IRC announce from grabbing — the kill switch the
        operator expected the UI toggle to be."""
        from app import config as app_config
        fake_settings = {"mam_irc_enabled": False, "dry_run": False}
        monkeypatch.setattr(
            app_config, "load_settings", lambda: dict(fake_settings),
        )
        fetch = _make_fetch(
            GrabResult(success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT)
        )
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
            fetch_result=GrabResult(
                success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT,
            ),
        )
        deps.fetch_torrent = fetch

        result = await handle_announce(deps, _make_announce())

        assert result.action == "skip"
        assert result.reason == "irc_listener_disabled"
        assert len(fetch.calls) == 0

    async def test_live_kill_switches_default_permissive(
        self, temp_db, monkeypatch,
    ):
        """Missing settings keys default to permissive: announces
        flow through normally. Critical for back-compat with users
        who never touched the toggles."""
        from app import config as app_config
        # Empty settings — no kill-switch fields set.
        monkeypatch.setattr(app_config, "load_settings", lambda: {})
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
            fetch_result=GrabResult(
                success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT,
            ),
        )

        result = await handle_announce(deps, _make_announce())

        assert result.action == "submit"

    async def test_live_dry_run_skips_manual_inject(
        self, temp_db, monkeypatch,
    ):
        """Manual injects also honor the live dry_run toggle —
        a user-initiated inject under an active dry_run should
        NOT grab. (IRC-disabled does NOT apply to manual injects
        since those are user-initiated regardless of listener state.)"""
        from app import config as app_config
        from app.orchestrator.dispatch import inject_grab
        # IRC disabled + dry_run on. inject_grab should honor dry_run
        # but NOT honor irc_disabled.
        fake_settings = {"mam_irc_enabled": False, "dry_run": True}
        monkeypatch.setattr(
            app_config, "load_settings", lambda: dict(fake_settings),
        )
        fetch = _make_fetch(
            GrabResult(success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT)
        )
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=[]),
            fetch_result=GrabResult(
                success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT,
            ),
        )
        deps.dry_run = False
        deps.fetch_torrent = fetch

        result = await inject_grab(
            deps, torrent_id="123456",
            torrent_name="test", category="Audiobooks - Fantasy",
        )

        assert result.action == "skip"
        assert result.reason == "dry_run_live"
        assert len(fetch.calls) == 0

    async def test_live_irc_disabled_does_not_block_manual_inject(
        self, temp_db, monkeypatch,
    ):
        """Manual injects DO NOT honor the IRC-disabled toggle —
        they're user-initiated and should still work when the
        listener is paused. (Only dry_run blocks manual injects.)"""
        from app import config as app_config
        from app.orchestrator.dispatch import inject_grab
        fake_settings = {"mam_irc_enabled": False, "dry_run": False}
        monkeypatch.setattr(
            app_config, "load_settings", lambda: dict(fake_settings),
        )
        deps = _make_deps(
            filter_config=_make_filter_config(allowed=[]),
            fetch_result=GrabResult(
                success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT,
            ),
        )

        result = await inject_grab(
            deps, torrent_id="123456",
            torrent_name="test", category="Audiobooks - Fantasy",
        )

        # Should NOT be the irc_listener_disabled skip — the manual
        # inject path bypasses that kill switch by design.
        assert result.reason != "irc_listener_disabled"


# ─── Buffer gate (commit 5 — MouseSearch Tier 1) ────────────


class TestBufferGate:
    """Integration tests for the dispatcher wiring the buffer gate.

    Policy-engine logic is covered exhaustively in
    `tests/policy/test_buffer_gate.py`. These tests verify three
    dispatcher-level contracts:

      - `_build_economic_context` fetches torrent_size_bytes and
        user_upload_buffer_bytes when `buffer_gate_enabled=True`.
      - A `buffer_insufficient` policy decision produces an
        `economy_audit` row with the right `action` + `trigger`.
      - The trigger distinguishes IRC autograbs from user-inject
        grabs (the MamPage history filters on this).
    """

    @staticmethod
    def _buffer_gate_config(buffer_bytes: int) -> bytes:
        # A custom user_status body exposing a specific upload_buffer
        # value so the test can drive the gate into skip territory.
        import json
        return json.dumps({
            "classname": "Power User",
            "ratio": 2.0,
            "seedbonus": 100,
            "uid": 1,
            "username": "tester",
            "uploaded_bytes": 1_000_000_000_000,
            "downloaded_bytes": 500_000_000_000,
            "upload_buffer": buffer_bytes,
            "wedges": 5,
        }).encode()

    @staticmethod
    def _torrent_info_body(size_bytes: int) -> bytes:
        # Matches the search-by-id response shape.
        import json
        return json.dumps({
            "perpage": 1, "start": 0, "found": 1,
            "data": [{
                "id": "1234", "language": "1", "main_cat": "14",
                "category": "63", "catname": "Ebooks - Fantasy",
                "size": str(size_bytes), "numfiles": "1",
                "vip": "0", "free": "0", "fl_vip": "0",
                "personal_freeleech": "0",
                "title": "Test Book", "name": "Test Book",
                "author_info": '{"1": "Brandon Sanderson"}',
                "seeders": "5", "leechers": "0", "times_completed": "1",
            }],
        }).encode()

    async def test_irc_autograb_block_writes_audit_row(
        self, temp_db, fake_mam
    ):
        from app.mam import torrent_info as ti
        from app.mam import user_status as us
        from app.storage import economy_audit

        ti.invalidate_cache()
        us.invalidate_cache()

        # 50 GB torrent vs 10 GB buffer — gate blocks.
        fake_mam.search.body = self._torrent_info_body(50_000_000_000)
        fake_mam.user_status.body = self._buffer_gate_config(10_000_000_000)

        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
        )
        deps.policy_config = PolicyConfig(buffer_gate_enabled=True)

        result = await handle_announce(deps, _make_announce())

        assert result.action == "skip"
        assert result.reason == "policy:buffer_insufficient"

        db = await get_db()
        try:
            rows = await economy_audit.list_recent(
                db, action=economy_audit.ACTION_BUFFER_GATE_BLOCK,
            )
        finally:
            await db.close()
        assert len(rows) == 1
        assert rows[0].trigger == economy_audit.TRIGGER_IRC_AUTOGRAB
        assert rows[0].torrent_id == "1234"
        assert "50.00 GB" in (rows[0].message or "")
        assert "10.00 GB" in (rows[0].message or "")

    async def test_user_inject_block_writes_user_trigger(
        self, temp_db, fake_mam
    ):
        from app.mam import torrent_info as ti
        from app.mam import user_status as us
        from app.orchestrator.dispatch import inject_grab
        from app.storage import economy_audit

        ti.invalidate_cache()
        us.invalidate_cache()

        fake_mam.search.body = self._torrent_info_body(50_000_000_000)
        fake_mam.user_status.body = self._buffer_gate_config(10_000_000_000)

        deps = _make_deps()
        deps.policy_config = PolicyConfig(buffer_gate_enabled=True)

        result = await inject_grab(deps, torrent_id="1234")

        assert result.action == "skip"
        assert result.reason == "policy:buffer_insufficient"

        db = await get_db()
        try:
            rows = await economy_audit.list_recent(
                db, action=economy_audit.ACTION_BUFFER_GATE_BLOCK,
            )
        finally:
            await db.close()
        assert len(rows) == 1
        assert rows[0].trigger == economy_audit.TRIGGER_USER_GRAB

    async def test_sufficient_buffer_allows_grab(
        self, temp_db, fake_mam
    ):
        # Same wiring, but a 2 GB torrent against a 20 GB buffer — gate
        # stays quiet and the normal submit path runs through.
        from app.mam import torrent_info as ti
        from app.mam import user_status as us
        from app.storage import economy_audit

        ti.invalidate_cache()
        us.invalidate_cache()

        fake_mam.search.body = self._torrent_info_body(2_000_000_000)
        fake_mam.user_status.body = self._buffer_gate_config(
            20_000_000_000
        )

        deps = _make_deps(
            filter_config=_make_filter_config(allowed=["Brandon Sanderson"]),
        )
        deps.policy_config = PolicyConfig(buffer_gate_enabled=True)

        result = await handle_announce(deps, _make_announce())
        assert result.action == "submit"

        db = await get_db()
        try:
            rows = await economy_audit.list_recent(
                db, action=economy_audit.ACTION_BUFFER_GATE_BLOCK,
            )
        finally:
            await db.close()
        assert rows == []  # no block row when the grab went through
