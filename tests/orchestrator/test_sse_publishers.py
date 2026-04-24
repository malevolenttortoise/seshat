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
