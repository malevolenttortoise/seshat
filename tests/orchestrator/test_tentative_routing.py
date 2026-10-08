"""
Dispatcher tentative / ignored-seen routing tests.

When the filter skips an announce for `author_not_allowlisted`, the
dispatcher should capture a `tentative_torrents` row instead of just
logging it. When the filter skips for `ignored_author`, it should
capture an `ignored_torrents_seen` row.

Both rows are visible in the weekly review queue so the user can
change their mind.
"""
import pytest

from app.database import get_db
from app.filter.gate import Announce, FilterConfig
from app.filter.normalize import normalize_category
from app.orchestrator.dispatch import DispatcherDeps, handle_announce
from app.storage import tentative as tentative_storage

# Minimal shared test infrastructure copied from test_dispatch.py so
# this file stands alone. We don't need fetching / qBit state for
# filter-skip tests — the mock fetch should never be called.
from tests.orchestrator.test_dispatch import (
    MINIMAL_BENCODED_TORRENT,
    _FakeQbit,
    _make_fetch,
)
from app.mam.grab import GrabResult


def _filter_config() -> FilterConfig:
    return FilterConfig(
        allowed_categories=frozenset(
            normalize_category(c) for c in ["Ebooks - Fantasy"]
        ),
        allowed_authors=frozenset(),
        ignored_authors=frozenset({"ignored author"}),
    )


def _deps() -> DispatcherDeps:
    return DispatcherDeps(
        filter_config=_filter_config(),
        mam_token="good_token",
        qbit_category="mam-complete",
        budget_cap=200,
        queue_max=100,
        queue_mode_enabled=True,
        seed_seconds_required=72 * 3600,
        db_factory=get_db,
        fetch_torrent=_make_fetch(
            GrabResult(success=True, torrent_bytes=MINIMAL_BENCODED_TORRENT)
        ),
        qbit=_FakeQbit(),
    )


class TestTentativeCapture:
    async def test_author_not_allowlisted_creates_tentative_row(self, temp_db):
        deps = _deps()
        announce = Announce(
            torrent_id="9001",
            torrent_name="Unknown Book",
            category="Ebooks - Fantasy",
            author_blob="Nobody Famous",
        )
        result = await handle_announce(deps, announce)

        assert result.action == "skip"
        assert result.reason == "author_not_allowlisted"

        db = await get_db()
        try:
            rows = await tentative_storage.list_tentative(db)
            assert len(rows) == 1
            assert rows[0].mam_torrent_id == "9001"
            assert rows[0].author_blob == "Nobody Famous"
            assert rows[0].status == tentative_storage.TENTATIVE_PENDING
        finally:
            await db.close()

    async def test_category_skip_does_not_create_tentative(self, temp_db):
        deps = _deps()
        announce = Announce(
            torrent_id="9002",
            torrent_name="Romance Book",
            category="Ebooks - Romance",
            author_blob="Nobody Famous",
        )
        result = await handle_announce(deps, announce)
        assert result.reason == "category_not_allowed"

        db = await get_db()
        try:
            rows = await tentative_storage.list_tentative(db)
            assert rows == []
        finally:
            await db.close()

    async def test_duplicate_announce_reuses_pending_row(self, temp_db):
        deps = _deps()
        announce = Announce(
            torrent_id="9003",
            torrent_name="Unknown Book",
            category="Ebooks - Fantasy",
            author_blob="Nobody Famous",
        )
        await handle_announce(deps, announce)
        await handle_announce(deps, announce)

        db = await get_db()
        try:
            rows = await tentative_storage.list_tentative(db)
            assert len(rows) == 1  # upsert, not duplicate
        finally:
            await db.close()


class TestIgnoredSeenCapture:
    async def test_ignored_author_captured(self, temp_db):
        deps = _deps()
        announce = Announce(
            torrent_id="9100",
            torrent_name="Book by ignored",
            category="Ebooks - Fantasy",
            author_blob="Ignored Author",
        )
        result = await handle_announce(deps, announce)
        assert result.reason == "ignored_author"

        db = await get_db()
        try:
            seen = await tentative_storage.list_ignored_seen_since(db, hours=1)
            assert len(seen) == 1
            assert seen[0].author_blob == "Ignored Author"
            assert seen[0].mam_torrent_id == "9100"
        finally:
            await db.close()


# ─── Cover fetches for skipped announces (audit L1-15) ───────


@pytest.fixture
def cover_fetches(monkeypatch, tmp_path):
    """Record MAM cover fetches instead of making them; covers land
    under a tmp staging path."""
    from app.metadata import covers

    calls: list[str] = []

    async def fake_fetch(torrent_id, *, dest_dir, basename="cover-mam", token=""):
        calls.append(torrent_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{basename}.jpg"
        path.write_bytes(b"x" * 200)
        return path

    monkeypatch.setattr(covers, "fetch_mam_cover", fake_fetch)
    return calls


def _staged_deps(tmp_path) -> DispatcherDeps:
    from dataclasses import replace
    return replace(_deps(), staging_path=str(tmp_path))


class TestSkipCovers:
    async def test_ignored_author_fetches_no_cover(self, temp_db, cover_fetches, tmp_path):
        announce = Announce(
            torrent_id="9200", torrent_name="Book by ignored",
            category="Ebooks - Fantasy", author_blob="Ignored Author",
        )
        result = await handle_announce(_staged_deps(tmp_path), announce)
        assert result.reason == "ignored_author"
        assert cover_fetches == []

    async def test_tentative_fetches_its_cover_once(self, temp_db, cover_fetches, tmp_path):
        announce = Announce(
            torrent_id="9201", torrent_name="Unknown Book",
            category="Ebooks - Fantasy", author_blob="Nobody Famous",
        )
        deps = _staged_deps(tmp_path)
        await handle_announce(deps, announce)
        await handle_announce(deps, announce)  # re-announce

        assert cover_fetches == ["9201"]
        db = await get_db()
        try:
            [row] = await tentative_storage.list_tentative(db)
        finally:
            await db.close()
        assert row.cover_path and row.cover_path.endswith("cover-mam.jpg")
