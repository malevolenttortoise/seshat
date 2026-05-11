"""Incremental ABS sync tests.

Verifies that:
- `sync_audiobookshelf` resolves mode via `sync_state.resolve_threshold`
  and returns `mode` in the result.
- Incremental mode filters Pass 3 by item `updatedAt`, leaves Pass 1+2
  running on the full set (so authors/series of un-modified books still
  resolve).
- Pass 4 prune uses the raw ABS-id set (not the flattened-book set),
  so items that fail `_flatten_item` don't trigger spurious prunes.
- `mode` is reported in both result and progress dict.

Timestamps are anchored to `time.time()` so `resolve_threshold`'s
7-day weekly-full safety net doesn't trip during the test.
"""
from __future__ import annotations

import json
import time as _time
from pathlib import Path

import pytest


def _abs_item(abs_id, title, author="Author", *, updated_at_ms,
              series_name=None, series_index=None):
    """Mimic the shape `iter_all_items` yields (raw ABS API item)."""
    return {
        "id": abs_id,
        "updatedAt": updated_at_ms,
        "media": {
            "duration": 3600,
            "numAudioFiles": 1,
            "metadata": {
                "title": title,
                "authorName": author,
                "narratorName": None,
                "seriesName": (
                    f"{series_name} #{series_index}" if series_name and series_index
                    else series_name
                ),
                "publishedDate": None,
                "description": None,
                "language": None,
                "publisher": None,
                "asin": None,
                "isbn": None,
                "abridged": False,
            },
        },
    }


@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    """tmp_path-isolated discovery DB + settings.json + cleared cache."""
    from app import config as app_config
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(
        app_config, "_settings_cache", {"data": None, "mtime": None},
    )
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    yield tmp_path
    disco_db.set_active_library(None)


def _fake_library() -> dict:
    return {
        "abs_base_url": "http://abs:13378",
        "abs_library_id": "lib-x",
        "slug": "test",
        "name": "Test",
    }


def _seed_for_incremental(
    settings_path: Path, *, slug: str = "test",
    threshold_unix: float,
) -> None:
    """Shape settings.json so resolve_threshold returns incremental."""
    from app.discovery.sync_state import DRIFT_BIAS_SECONDS
    from app import config as app_config
    now = _time.time()
    settings_path.write_text(json.dumps({
        "library_sync_state": {
            slug: {
                "last_mtime": 0,
                "last_sync_ts": threshold_unix + DRIFT_BIAS_SECONDS,
                "last_full_sync_ts": now - 300.0,
            }
        }
    }))
    app_config._settings_cache["data"] = None
    app_config._settings_cache["mtime"] = None


def _patch_abs_layers(monkeypatch, items: list[dict]):
    """Stub the network-dependent ABS layers so tests drive sync from
    a raw-item list. Returns the items list so the caller can mutate
    between test phases (e.g. delete an item between syncs)."""
    from app.library_apps import audiobookshelf as abs_mod

    async def _fake_iter(self, library_id, page_size=500):
        for it in items:
            yield it
    monkeypatch.setattr(
        abs_mod.AudiobookshelfClient, "iter_all_items", _fake_iter
    )

    async def _fake_get_key():
        return "tok"
    monkeypatch.setattr(abs_mod, "_get_abs_api_key", _fake_get_key)
    return items


async def _book_titles_in_db():
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT title FROM books WHERE source='audiobookshelf' "
            "ORDER BY title"
        )).fetchall()
        return [r["title"] for r in rows]
    finally:
        await db.close()


class TestSyncAudiobookshelfMode:
    async def test_full_mode_first_sync(self, discovery_db, monkeypatch):
        from app.discovery.audiobookshelf_sync import sync_audiobookshelf

        now = _time.time()
        items = [
            _abs_item("a-1", "Old One",
                      updated_at_ms=int((now - 7 * 86400) * 1000)),
            _abs_item("a-2", "Fresh One",
                      updated_at_ms=int((now - 60) * 1000)),
        ]
        _patch_abs_layers(monkeypatch, items)

        result = await sync_audiobookshelf(_fake_library())

        assert result["mode"] == "full"
        assert sorted(await _book_titles_in_db()) == ["Fresh One", "Old One"]

    async def test_incremental_filters_by_updated_at(
        self, discovery_db, monkeypatch,
    ):
        from app.discovery.audiobookshelf_sync import sync_audiobookshelf

        now = _time.time()
        threshold = now - 3600.0  # 1 hour ago
        old_ts_ms = int((now - 7 * 86400) * 1000)
        fresh_ts_ms = int((now - 60) * 1000)

        items = [
            _abs_item("a-1", "Old A", updated_at_ms=old_ts_ms),
            _abs_item("a-2", "Old B", updated_at_ms=old_ts_ms),
            _abs_item("a-3", "Modified C", updated_at_ms=fresh_ts_ms),
        ]
        _patch_abs_layers(monkeypatch, items)
        _seed_for_incremental(
            discovery_db / "settings.json",
            threshold_unix=threshold,
        )

        result = await sync_audiobookshelf(_fake_library())

        assert result["mode"] == "incremental"
        # Only the modified book reaches Pass 3's upsert.
        assert await _book_titles_in_db() == ["Modified C"]

    async def test_incremental_prunes_deleted_items(
        self, discovery_db, monkeypatch,
    ):
        """A deleted ABS item is pruned on incremental even when no
        items in the filtered set forced Pass 4 to consider it."""
        from app.discovery.audiobookshelf_sync import sync_audiobookshelf

        now = _time.time()
        old_ts_ms = int((now - 7 * 86400) * 1000)

        # Round 1: 3 items, full sync.
        items = [
            _abs_item("a-1", "Survivor A", updated_at_ms=old_ts_ms),
            _abs_item("a-2", "Will Be Deleted", updated_at_ms=old_ts_ms),
            _abs_item("a-3", "Survivor B", updated_at_ms=old_ts_ms),
        ]
        _patch_abs_layers(monkeypatch, items)
        await sync_audiobookshelf(_fake_library())
        assert sorted(await _book_titles_in_db()) == [
            "Survivor A", "Survivor B", "Will Be Deleted",
        ]

        # Round 2: delete item a-2, threshold AFTER all updatedAts.
        # Filtered set is empty; Pass 4 should still prune.
        items.pop(1)  # mutate in place; the fake_iter sees new state
        _seed_for_incremental(
            discovery_db / "settings.json",
            threshold_unix=now,  # newer than every item's updated_at
        )

        result = await sync_audiobookshelf(_fake_library())
        assert result["mode"] == "incremental"
        assert result["books_pruned"] == 1
        assert sorted(await _book_titles_in_db()) == [
            "Survivor A", "Survivor B",
        ]

    async def test_pass4_uses_raw_item_ids_not_flattened_books(
        self, discovery_db, monkeypatch,
    ):
        """An item that fails `_flatten_item` (missing title or author)
        must STILL count as "exists in ABS" for prune purposes —
        otherwise the matching discovery row gets spuriously pruned.

        This is the latent bug closed alongside the incremental work.
        """
        from app.discovery.audiobookshelf_sync import sync_audiobookshelf
        from app.discovery.database import get_db

        now = _time.time()
        old_ts_ms = int((now - 7 * 86400) * 1000)

        # Round 1: two real items, full sync.
        items = [
            _abs_item("a-1", "Real Book", updated_at_ms=old_ts_ms),
            _abs_item("a-2", "Other Book", updated_at_ms=old_ts_ms),
        ]
        _patch_abs_layers(monkeypatch, items)
        await sync_audiobookshelf(_fake_library())
        assert len(await _book_titles_in_db()) == 2

        # Round 2: a-2 develops bad metadata (no title). It SHOULD
        # remain in the discovery DB because it still exists in ABS.
        # Pre-fix behavior: a-2 gets pruned because it's not in
        # flattened-books → not in the old current_abs_ids list.
        items[1]["media"]["metadata"]["title"] = ""  # _flatten_item returns None
        result = await sync_audiobookshelf(_fake_library())
        assert result["books_pruned"] == 0
        # The discovery row for a-2 should still be there.
        db = await get_db()
        try:
            row = await (await db.execute(
                "SELECT id FROM books WHERE audiobookshelf_id = ?", ("a-2",)
            )).fetchone()
        finally:
            await db.close()
        assert row is not None

    async def test_progress_sync_mode_is_recorded(
        self, discovery_db, monkeypatch,
    ):
        from app import state
        from app.discovery.audiobookshelf_sync import sync_audiobookshelf

        now = _time.time()
        items = [
            _abs_item("a-1", "Some Book",
                      updated_at_ms=int((now - 60) * 1000)),
        ]
        _patch_abs_layers(monkeypatch, items)
        _seed_for_incremental(
            discovery_db / "settings.json",
            threshold_unix=now - 3600.0,
        )

        await sync_audiobookshelf(_fake_library())
        progress = state.get_lib_progress("test")
        assert progress["sync_mode"] == "incremental"
