"""
Unit tests for `app.orchestrator.sse_publishers` — the diff + transition
logic that turns budget-watcher state into SSE events.
"""
from __future__ import annotations

import asyncio
import pytest

from app.clients.base import TorrentInfo
from app.mam.user_status import UserStatus
from app.orchestrator import sse_broadcast, sse_publishers


@pytest.fixture(autouse=True)
def _reset():
    sse_broadcast.reset_for_tests()
    sse_publishers.reset_for_tests()
    yield
    sse_broadcast.reset_for_tests()
    sse_publishers.reset_for_tests()


def _torrent(hash_: str, *, progress: float = 0.0, dlspeed: int = 0,
             state: str = "downloading", name: str = "T") -> TorrentInfo:
    return TorrentInfo(
        hash=hash_, name=name, category="seshat", state=state,
        seeding_seconds=0, save_path="/d", added_on=0,
        progress=progress, dlspeed=dlspeed, eta=0, size=1024,
    )


# ─── diff_torrent_progress ────────────────────────────────────

class TestDiffTorrentProgress:
    def test_first_call_emits_every_torrent(self):
        current = [_torrent("a", progress=0.1), _torrent("b", progress=0.5)]
        events = sse_publishers.diff_torrent_progress(current)
        assert {e.hash for e in events} == {"a", "b"}

    def test_unchanged_torrents_emit_nothing(self):
        current = [_torrent("a", progress=0.1, dlspeed=100, state="downloading")]
        sse_publishers.diff_torrent_progress(current)
        # Same fields next tick → no event.
        assert sse_publishers.diff_torrent_progress(current) == []

    def test_progress_change_emits(self):
        sse_publishers.diff_torrent_progress([_torrent("a", progress=0.1)])
        events = sse_publishers.diff_torrent_progress(
            [_torrent("a", progress=0.2)]
        )
        assert [e.hash for e in events] == ["a"]
        assert events[0].progress == 0.2

    def test_state_change_emits(self):
        sse_publishers.diff_torrent_progress(
            [_torrent("a", progress=1.0, state="downloading")]
        )
        events = sse_publishers.diff_torrent_progress(
            [_torrent("a", progress=1.0, state="uploading")]
        )
        assert [e.state for e in events] == ["uploading"]

    def test_dlspeed_change_alone_emits(self):
        sse_publishers.diff_torrent_progress(
            [_torrent("a", progress=0.5, dlspeed=1000)]
        )
        events = sse_publishers.diff_torrent_progress(
            [_torrent("a", progress=0.5, dlspeed=2000)]
        )
        assert [e.dlspeed for e in events] == [2000]

    def test_removed_torrent_is_silent(self):
        sse_publishers.diff_torrent_progress(
            [_torrent("a", progress=0.5), _torrent("b", progress=0.5)]
        )
        events = sse_publishers.diff_torrent_progress(
            [_torrent("a", progress=0.5)]
        )
        # `b` is gone from current — we deliberately don't emit a
        # "removed" event; the UI drops torrents another way.
        assert events == []

    def test_missing_hash_is_skipped(self):
        events = sse_publishers.diff_torrent_progress([_torrent("")])
        assert events == []


# ─── publish_torrent_progress ─────────────────────────────────

class TestPublishTorrentProgress:
    async def test_publish_sends_to_subscriber(self):
        q = sse_broadcast.register()
        try:
            await sse_publishers.publish_torrent_progress(
                [_torrent("a", progress=0.25, name="Book A")]
            )
            event_type, data = await asyncio.wait_for(q.get(), timeout=1)
            assert event_type == "torrent-progress"
            assert data["hash"] == "a"
            assert data["progress"] == 0.25
            assert data["name"] == "Book A"
        finally:
            sse_broadcast.unregister(q)

    async def test_publish_noop_when_no_changes(self):
        q = sse_broadcast.register()
        try:
            await sse_publishers.publish_torrent_progress(
                [_torrent("a", progress=0.5, dlspeed=100)]
            )
            # Drain the initial event.
            await asyncio.wait_for(q.get(), timeout=1)
            # Same snapshot → nothing to publish.
            await sse_publishers.publish_torrent_progress(
                [_torrent("a", progress=0.5, dlspeed=100)]
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.1)
        finally:
            sse_broadcast.unregister(q)


# ─── publish_client_status ────────────────────────────────────

class TestPublishClientStatus:
    async def test_first_call_always_publishes(self):
        q = sse_broadcast.register()
        try:
            await sse_publishers.publish_client_status(True)
            event_type, data = await asyncio.wait_for(q.get(), timeout=1)
            assert event_type == "client-status"
            assert data == {"reachable": True}
        finally:
            sse_broadcast.unregister(q)

    async def test_steady_state_suppresses_duplicates(self):
        q = sse_broadcast.register()
        try:
            await sse_publishers.publish_client_status(True)
            await asyncio.wait_for(q.get(), timeout=1)
            # Second call with the same value — must not publish.
            await sse_publishers.publish_client_status(True)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.1)
        finally:
            sse_broadcast.unregister(q)

    async def test_transition_publishes(self):
        q = sse_broadcast.register()
        try:
            await sse_publishers.publish_client_status(True)
            await asyncio.wait_for(q.get(), timeout=1)
            await sse_publishers.publish_client_status(False)
            event_type, data = await asyncio.wait_for(q.get(), timeout=1)
            assert event_type == "client-status"
            assert data == {"reachable": False}
        finally:
            sse_broadcast.unregister(q)


# ─── publish_mam_stats ────────────────────────────────────────

def _status(**overrides) -> UserStatus:
    defaults = dict(
        ratio=1000.0, wedges=5, seedbonus=5000.0, classname="Power User",
        username="u", uid=1,
        uploaded_bytes=10_000_000_000, downloaded_bytes=5_000_000_000,
        upload_buffer_bytes=5_000_000_000,
    )
    defaults.update(overrides)
    return UserStatus(**defaults)


class TestPublishMamStats:
    async def test_first_call_publishes(self):
        q = sse_broadcast.register()
        try:
            await sse_publishers.publish_mam_stats(_status(ratio=2500.5))
            event_type, data = await asyncio.wait_for(q.get(), timeout=1)
            assert event_type == "mam-stats"
            assert data["ratio"] == 2500.5
            assert data["seedbonus"] == 5000.0
            assert data["wedges"] == 5
            assert data["upload_buffer_bytes"] == 5_000_000_000
        finally:
            sse_broadcast.unregister(q)

    async def test_unchanged_stats_suppressed(self):
        q = sse_broadcast.register()
        try:
            await sse_publishers.publish_mam_stats(_status())
            await asyncio.wait_for(q.get(), timeout=1)
            # Re-publish identical status — drops.
            await sse_publishers.publish_mam_stats(_status())
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.1)
        finally:
            sse_broadcast.unregister(q)

    async def test_seedbonus_change_publishes(self):
        q = sse_broadcast.register()
        try:
            await sse_publishers.publish_mam_stats(_status(seedbonus=5000.0))
            await asyncio.wait_for(q.get(), timeout=1)
            await sse_publishers.publish_mam_stats(_status(seedbonus=7500.0))
            event_type, data = await asyncio.wait_for(q.get(), timeout=1)
            assert event_type == "mam-stats"
            assert data["seedbonus"] == 7500.0
        finally:
            sse_broadcast.unregister(q)

    async def test_sub_tenth_ratio_jitter_is_suppressed(self):
        q = sse_broadcast.register()
        try:
            await sse_publishers.publish_mam_stats(_status(ratio=2500.51))
            await asyncio.wait_for(q.get(), timeout=1)
            # Both 2500.51 and 2500.53 round to 2500.5 — no second event.
            await sse_publishers.publish_mam_stats(_status(ratio=2500.53))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.1)
        finally:
            sse_broadcast.unregister(q)


# ─── publish_toast ────────────────────────────────────────────

class TestPublishToast:
    async def test_valid_levels_pass_through(self):
        q = sse_broadcast.register()
        try:
            for level in ("success", "info", "warn", "error"):
                await sse_publishers.publish_toast(level, f"msg-{level}")
                event_type, data = await asyncio.wait_for(q.get(), timeout=1)
                assert event_type == "toast"
                assert data == {"level": level, "message": f"msg-{level}"}
        finally:
            sse_broadcast.unregister(q)

    async def test_invalid_level_coerces_to_info(self):
        q = sse_broadcast.register()
        try:
            await sse_publishers.publish_toast("critical", "boom")
            event_type, data = await asyncio.wait_for(q.get(), timeout=1)
            assert event_type == "toast"
            assert data == {"level": "info", "message": "boom"}
        finally:
            sse_broadcast.unregister(q)

    async def test_every_publish_fires(self):
        # Unlike client-status / mam-stats, toasts don't dedupe — the
        # caller explicitly decides to notify, so two identical
        # messages should both surface.
        q = sse_broadcast.register()
        try:
            await sse_publishers.publish_toast("info", "hi")
            await sse_publishers.publish_toast("info", "hi")
            a = await asyncio.wait_for(q.get(), timeout=1)
            b = await asyncio.wait_for(q.get(), timeout=1)
            assert a == b == ("toast", {"level": "info", "message": "hi"})
        finally:
            sse_broadcast.unregister(q)


# ─── last_state accessors ─────────────────────────────────────

class TestLastStateAccessors:
    def test_client_status_last_state_none_before_first_publish(self):
        assert sse_publishers.client_status_last_state() is None

    async def test_client_status_last_state_tracks_last_publish(self):
        await sse_publishers.publish_client_status(True)
        assert sse_publishers.client_status_last_state() == {"reachable": True}
        await sse_publishers.publish_client_status(False)
        assert sse_publishers.client_status_last_state() == {"reachable": False}

    def test_mam_stats_last_state_none_before_first_publish(self):
        assert sse_publishers.mam_stats_last_state() is None

    async def test_mam_stats_last_state_returns_full_payload(self):
        await sse_publishers.publish_mam_stats(_status(
            ratio=2500.5, seedbonus=7000.0, wedges=5,
            upload_buffer_bytes=1_000_000_000,
        ))
        state = sse_publishers.mam_stats_last_state()
        assert state == {
            "ratio": 2500.5,
            "seedbonus": 7000.0,
            "upload_buffer_bytes": 1_000_000_000,
            "wedges": 5,
        }

    async def test_mam_stats_jitter_does_not_reset_last_payload(self):
        # Sub-0.1 ratio jitter is dedup'd by the key filter but the
        # last-published payload must survive unchanged so new
        # subscribers see the real value (not a rounded placeholder).
        await sse_publishers.publish_mam_stats(_status(ratio=2500.51))
        first = sse_publishers.mam_stats_last_state()
        # Same rounded bucket — publish is suppressed, payload unchanged.
        await sse_publishers.publish_mam_stats(_status(ratio=2500.53))
        assert sse_publishers.mam_stats_last_state() == first


# ─── seed_new_subscriber ──────────────────────────────────────

class TestSeedNewSubscriber:
    async def test_seeds_nothing_when_no_publishes_yet(self):
        # Fresh process state: no publishes have happened, so the
        # new subscriber's queue stays empty. The generator in the
        # SSE route will then block on the next real publish.
        q = sse_broadcast.register()
        try:
            sse_publishers.seed_new_subscriber(q)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.05)
        finally:
            sse_broadcast.unregister(q)

    async def test_seeds_client_status_after_first_publish(self):
        # Simulate the real scenario: backend publishes client-status
        # before any tab connects. Tab opens AFTER → seeding must
        # replay the current state onto the fresh queue.
        await sse_publishers.publish_client_status(True)

        q = sse_broadcast.register()
        try:
            sse_publishers.seed_new_subscriber(q)
            event_type, data = await asyncio.wait_for(q.get(), timeout=1)
            assert event_type == "client-status"
            assert data == {"reachable": True}
        finally:
            sse_broadcast.unregister(q)

    async def test_seeds_both_stateful_events(self):
        # Both client-status AND mam-stats were published before this
        # subscriber arrived. Both should land on its queue.
        await sse_publishers.publish_client_status(True)
        await sse_publishers.publish_mam_stats(_status(ratio=1500.0))

        q = sse_broadcast.register()
        try:
            sse_publishers.seed_new_subscriber(q)
            events = []
            for _ in range(2):
                events.append(
                    await asyncio.wait_for(q.get(), timeout=1)
                )
            event_types = {e[0] for e in events}
            assert event_types == {"client-status", "mam-stats"}
        finally:
            sse_broadcast.unregister(q)

    async def test_torrent_progress_not_seeded(self):
        # torrent-progress deliberately doesn't replay — the budget
        # watcher's next tick (≤60s) re-emits changed torrents anyway,
        # and a full snapshot would flood the queue on every connect.
        await sse_publishers.publish_torrent_progress([_torrent(
            "abc", progress=0.5, dlspeed=1000, state="downloading",
        )])

        q = sse_broadcast.register()
        try:
            sse_publishers.seed_new_subscriber(q)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(q.get(), timeout=0.05)
        finally:
            sse_broadcast.unregister(q)

    async def test_existing_subscribers_unaffected_by_new_connection(self):
        # Seeding a new subscriber pushes to THAT queue only — the
        # already-connected clients don't get a second copy of the
        # current state.
        await sse_publishers.publish_client_status(True)

        old_q = sse_broadcast.register()
        try:
            # Old subscriber would have received the publish live if
            # it had been connected; in this test it missed it.
            new_q = sse_broadcast.register()
            try:
                sse_publishers.seed_new_subscriber(new_q)
                # New subscriber got a client-status event.
                event = await asyncio.wait_for(new_q.get(), timeout=1)
                assert event[0] == "client-status"
                # Old subscriber still has nothing.
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(old_q.get(), timeout=0.05)
            finally:
                sse_broadcast.unregister(new_q)
        finally:
            sse_broadcast.unregister(old_q)
