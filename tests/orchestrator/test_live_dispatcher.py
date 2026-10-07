"""Issue 01 (audit 2026-10, L1-02): the background loops use the live dispatcher.

Every settings / credential / metadata-source save replaces
`state.dispatcher`. The IRC bridge, the budget watcher, cookie retry,
review timeout, hold release and held index-wait grabs used to keep the
copy built at startup, so a grab-policy change, a new excluded uploader
or an approved author never reached IRC until a restart. They now
resolve `state.dispatcher` when they use it.

Safety is proven by asserting `fetch_torrent` is NOT called.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace

from app import main, state
from app.database import get_db
from app.orchestrator import cookie_retry, dispatch
from app.orchestrator.dispatch import handle_announce
from app.policy.engine import PolicyConfig
from app.storage import authors as authors_storage
from tests.orchestrator.test_dispatch import _make_announce, _make_deps, _make_filter_config
from tests.orchestrator.test_manual_grab import TID, _item, mam_search  # noqa: F401
from tests.orchestrator.test_wedge_guard import mam_index  # noqa: F401


def _sanderson_deps(**changes):
    deps = _make_deps(filter_config=_make_filter_config(allowed=["Brandon Sanderson"]))
    return replace(deps, **changes) if changes else deps


async def _announce_rows() -> list[tuple[str, str]]:
    db = await get_db()
    try:
        async with db.execute(
            "SELECT decision, decision_reason FROM announces ORDER BY id"
        ) as cur:
            return [(r[0], r[1]) for r in await cur.fetchall()]
    finally:
        await db.close()


class TestIrcBridge:
    async def test_grab_policy_saved_after_startup_reaches_irc(self, temp_db, mam_search):
        startup = _sanderson_deps()
        state.dispatcher = startup
        rebuilt = _sanderson_deps(policy_config=PolicyConfig(vip_only=True))
        state.replace_dispatcher(rebuilt)

        await main._on_irc_announce(_make_announce(torrent_id=TID))

        assert startup.fetch_torrent.calls == []  # type: ignore[attr-defined]
        assert rebuilt.fetch_torrent.calls == []  # type: ignore[attr-defined]

    async def test_excluded_uploader_saved_after_startup_reaches_irc(
        self, temp_db, mam_search,
    ):
        mam_search["items"][TID] = _item(ownership='[12345,"MyAccount"]')
        startup = _sanderson_deps()
        state.dispatcher = startup
        rebuilt = _sanderson_deps(excluded_uploaders=frozenset({"myaccount"}))
        state.replace_dispatcher(rebuilt)

        await main._on_irc_announce(_make_announce(torrent_id=TID))

        assert startup.fetch_torrent.calls == []  # type: ignore[attr-defined]
        assert rebuilt.fetch_torrent.calls == []  # type: ignore[attr-defined]

    async def test_author_approved_after_a_rebuild_reaches_irc(self, temp_db, mam_search):
        # The prod symptom: an author approved after any settings save
        # was still skipped `author_not_allowlisted` by IRC.
        startup = _make_deps()
        state.dispatcher = startup
        rebuilt = _make_deps()
        state.replace_dispatcher(rebuilt)
        db = await get_db()
        try:
            await authors_storage.add_allowed(db, "Brandon Sanderson")
        finally:
            await db.close()
        await state.refresh_filter_authors()

        await main._on_irc_announce(_make_announce(torrent_id=TID))

        assert (await _announce_rows())[0][0] == "allow"
        assert len(rebuilt.fetch_torrent.calls) == 1  # type: ignore[attr-defined]
        assert startup.fetch_torrent.calls == []  # type: ignore[attr-defined]

    async def test_no_dispatcher_drops_the_announce(self, temp_db):
        state.dispatcher = None
        await main._on_irc_announce(_make_announce(torrent_id=TID))
        assert await _announce_rows() == []


class TestHeldGrab:
    async def test_policy_saved_during_the_index_wait_applies(self, temp_db, mam_index):
        startup = _sanderson_deps()
        state.dispatcher = startup
        result = await handle_announce(startup, _make_announce(torrent_id=TID))
        assert result.action == "hold"
        rebuilt = _sanderson_deps(policy_config=PolicyConfig(vip_only=True))
        state.replace_dispatcher(rebuilt)

        await asyncio.gather(*list(dispatch._held_grabs))

        assert startup.fetch_torrent.calls == []  # type: ignore[attr-defined]
        assert rebuilt.fetch_torrent.calls == []  # type: ignore[attr-defined]


class TestLoops:
    async def test_each_tick_resolves_the_dispatcher_again(self, monkeypatch):
        startup, rebuilt = _make_deps(), _make_deps()
        state.dispatcher = startup
        seen: list = []
        stop = asyncio.Event()

        async def fake_tick(deps):
            seen.append(deps)
            if len(seen) == 1:
                state.replace_dispatcher(rebuilt)
            else:
                stop.set()
            return cookie_retry.RetryResult(0, 0, 0, 0)

        monkeypatch.setattr(cookie_retry, "tick", fake_tick)
        await cookie_retry.run_loop(
            main._live_dispatcher, interval_seconds=0.001, stop_event=stop,
        )

        assert seen == [startup, rebuilt]


class TestRetiredEnricher:
    async def test_old_enricher_closed_after_the_grace_not_at_once(self, monkeypatch):
        closed: list[str] = []

        class _Enricher:
            def __init__(self, name):
                self.name = name

            async def aclose(self):
                closed.append(self.name)

        monkeypatch.setattr(state, "_RETIRED_ENRICHER_GRACE_S", 0.01)
        state.dispatcher = replace(_make_deps(), metadata_enricher=_Enricher("old"))
        state.replace_dispatcher(replace(_make_deps(), metadata_enricher=_Enricher("new")))
        assert closed == []

        await asyncio.sleep(0.05)
        await asyncio.gather(*list(state._retiring_enrichers))
        assert closed == ["old"]
