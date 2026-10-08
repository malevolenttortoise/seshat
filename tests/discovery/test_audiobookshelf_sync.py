"""
Tests for `sync_audiobookshelf` — the ABS → discovery-DB pipeline.

Follows the same `discovery_db` fixture shape as `test_calibre_sync_prune.py`
so both sync paths exercise the same per-library database setup.
"""
from __future__ import annotations

import pytest


# ─── _flatten_item (pure function) ─────────────────────────────

class TestFlattenItem:
    def test_basic_item_flattens_every_field(self):
        from app.discovery.audiobookshelf_sync import _flatten_item
        item = {
            "id": "item-1",
            "media": {
                "duration": 12345.6, "numAudioFiles": 5,
                "metadata": {
                    "title": "The Final Empire",
                    "authorName": "Brandon Sanderson",
                    "narratorName": "Michael Kramer",
                    "seriesName": "Mistborn",
                    "publishedYear": "2006",
                    "isbn": "9780765311788",
                    "asin": "B0041KLD5I",
                    "abridged": False,
                    "language": "English",
                    "publisher": "Tor Books",
                    "description": "A dark fantasy...",
                },
            },
        }
        out = _flatten_item(item)
        assert out["abs_id"] == "item-1"
        assert out["title"] == "The Final Empire"
        assert out["authors"] == ["Brandon Sanderson"]
        assert out["narrator"] == "Michael Kramer"
        assert out["series_name"] == "Mistborn"
        assert out["series_index"] is None
        assert out["duration_sec"] == 12345.6
        assert out["abridged"] is False
        assert out["asin"] == "B0041KLD5I"
        assert out["isbn"] == "9780765311788"
        assert out["audio_formats"] == "audiobook"

    def test_missing_title_returns_none(self):
        from app.discovery.audiobookshelf_sync import _flatten_item
        assert _flatten_item({"media": {"metadata": {"authorName": "A"}}}) is None

    def test_missing_author_returns_none(self):
        from app.discovery.audiobookshelf_sync import _flatten_item
        assert _flatten_item({"media": {"metadata": {"title": "T"}}}) is None

    def test_multi_author_splits_on_comma_space(self):
        from app.discovery.audiobookshelf_sync import _flatten_item
        item = {"id": "x", "media": {"metadata": {
            "title": "Black House", "authorName": "Stephen King, Peter Straub"
        }}}
        assert _flatten_item(item)["authors"] == ["Stephen King", "Peter Straub"]

    def test_asin_equal_to_isbn_nulls_isbn(self):
        """ABS mirrors ASIN into the ISBN field when no real ISBN exists."""
        from app.discovery.audiobookshelf_sync import _flatten_item
        item = {"id": "x", "media": {"metadata": {
            "title": "T", "authorName": "A",
            "asin": "B00BPVBI4A", "isbn": "B00BPVBI4A"
        }}}
        out = _flatten_item(item)
        assert out["asin"] == "B00BPVBI4A"
        assert out["isbn"] is None

    def test_trailing_hash_number_parses_series_index(self):
        from app.discovery.audiobookshelf_sync import _flatten_item
        item = {"id": "x", "media": {"metadata": {
            "title": "T", "authorName": "A", "seriesName": "Halo #7",
        }}}
        out = _flatten_item(item)
        assert out["series_name"] == "Halo"
        assert out["series_index"] == 7.0

    def test_abridged_flag_becomes_bool(self):
        from app.discovery.audiobookshelf_sync import _flatten_item
        item = {"id": "x", "media": {"metadata": {
            "title": "T", "authorName": "A", "abridged": True
        }}}
        assert _flatten_item(item)["abridged"] is True

    def test_description_truncated_to_1000(self):
        from app.discovery.audiobookshelf_sync import _flatten_item
        long = "x" * 2000
        item = {"id": "x", "media": {"metadata": {
            "title": "T", "authorName": "A", "description": long
        }}}
        assert len(_flatten_item(item)["description"]) == 1000


# ─── sync_audiobookshelf (integration with DB) ─────────────────

@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    """Tmp per-library discovery DB, active slug set, schema initialized."""
    from app import config as app_config
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    # The sync's fire-and-forget Goodreads author-ID backfill resolves
    # seeded ISBNs against goodreads.com and openlibrary.org; the suite's
    # no-network guard (tests/_network_guard.py) caught it.
    from app.discovery import goodreads_author_backfill

    async def _no_backfill(**_kw):
        return {}

    monkeypatch.setattr(
        goodreads_author_backfill, "backfill_missing_author_ids", _no_backfill,
    )
    disco_db.set_active_library("abs-test")
    await disco_db.init_db("abs-test")
    yield tmp_path
    disco_db.set_active_library(None)


def _fake_item(abs_id: str, title: str, author: str = "Test Author",
               series: str = None, asin: str = None, narrator: str = None):
    """Minimal ABS item shape."""
    return {
        "id": abs_id,
        "media": {
            "duration": 3600.0, "numAudioFiles": 1,
            "metadata": {
                "title": title,
                "authorName": author,
                "narratorName": narrator or "",
                "seriesName": series or "",
                "asin": asin or "",
                "abridged": False,
            },
        },
    }


async def _all_books():
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT title, asin, narrator, duration_sec, abridged, "
            "audiobookshelf_id, source, owned FROM books ORDER BY id"
        )).fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def _patch_abs_pipeline(monkeypatch, items: list[dict]):
    """Install a fake ABS client + secrets so `sync_audiobookshelf` runs purely offline."""
    from app.library_apps import audiobookshelf as abs_mod

    async def fake_get_key():
        return "fake-bearer-token"
    monkeypatch.setattr(abs_mod, "_get_abs_api_key", fake_get_key)

    async def fake_iter(self, library_id, page_size=500):
        for it in items:
            yield it
    monkeypatch.setattr(abs_mod.AudiobookshelfClient, "iter_all_items", fake_iter)


async def test_initial_sync_inserts_books(discovery_db, monkeypatch):
    from app.discovery.audiobookshelf_sync import sync_audiobookshelf

    await _patch_abs_pipeline(monkeypatch, [
        _fake_item("abs-1", "Book One", "Jane Doe"),
        _fake_item("abs-2", "Book Two", "Jane Doe", series="Jane Series"),
    ])

    result = await sync_audiobookshelf({
        "slug": "abs-test",
        "abs_base_url": "http://abs:13378",
        "abs_library_id": "lib-xxx",
    })

    assert result["books_new"] == 2
    assert result["books_pruned"] == 0
    books = await _all_books()
    assert len(books) == 2
    assert {b["source"] for b in books} == {"audiobookshelf"}
    assert {b["owned"] for b in books} == {1}
    assert {b["audiobookshelf_id"] for b in books} == {"abs-1", "abs-2"}


async def test_second_sync_updates_not_dupes(discovery_db, monkeypatch):
    from app.discovery.audiobookshelf_sync import sync_audiobookshelf

    lib = {"slug": "abs-test", "abs_base_url": "http://abs", "abs_library_id": "lib"}
    await _patch_abs_pipeline(monkeypatch, [_fake_item("abs-1", "Book One", "Jane Doe")])
    first = await sync_audiobookshelf(lib)
    assert first["books_new"] == 1

    # Re-sync with the same item but updated narrator → should UPDATE, not INSERT.
    await _patch_abs_pipeline(monkeypatch, [
        _fake_item("abs-1", "Book One", "Jane Doe", narrator="New Narrator"),
    ])
    second = await sync_audiobookshelf(lib)
    assert second["books_new"] == 0
    books = await _all_books()
    assert len(books) == 1
    assert books[0]["narrator"] == "New Narrator"


async def test_prune_removes_vanished_items(discovery_db, monkeypatch):
    from app.discovery.audiobookshelf_sync import sync_audiobookshelf

    lib = {"slug": "abs-test", "abs_base_url": "http://abs", "abs_library_id": "lib"}
    await _patch_abs_pipeline(monkeypatch, [
        _fake_item("abs-1", "Book One"),
        _fake_item("abs-2", "Book Two"),
        _fake_item("abs-3", "Book Three"),
    ])
    await sync_audiobookshelf(lib)
    assert len(await _all_books()) == 3

    # abs-2 disappears from ABS → prune on next sync.
    await _patch_abs_pipeline(monkeypatch, [
        _fake_item("abs-1", "Book One"),
        _fake_item("abs-3", "Book Three"),
    ])
    result = await sync_audiobookshelf(lib)
    assert result["books_pruned"] == 1
    rows = await _all_books()
    assert {r["audiobookshelf_id"] for r in rows} == {"abs-1", "abs-3"}


async def test_empty_payload_skips_prune(discovery_db, monkeypatch):
    """Zero items = treat as transient read error, keep existing rows."""
    from app.discovery.audiobookshelf_sync import sync_audiobookshelf

    lib = {"slug": "abs-test", "abs_base_url": "http://abs", "abs_library_id": "lib"}
    await _patch_abs_pipeline(monkeypatch, [_fake_item("abs-1", "Book One")])
    await sync_audiobookshelf(lib)
    assert len(await _all_books()) == 1

    # Empty ABS read — book must survive.
    await _patch_abs_pipeline(monkeypatch, [])
    result = await sync_audiobookshelf(lib)
    assert result["books_pruned"] == 0
    assert len(await _all_books()) == 1


async def test_asin_and_narrator_persist_on_insert(discovery_db, monkeypatch):
    from app.discovery.audiobookshelf_sync import sync_audiobookshelf

    lib = {"slug": "abs-test", "abs_base_url": "http://abs", "abs_library_id": "lib"}
    await _patch_abs_pipeline(monkeypatch, [
        _fake_item("abs-1", "A Book", asin="B00SOMETHING", narrator="Big Voice"),
    ])
    await sync_audiobookshelf(lib)
    rows = await _all_books()
    assert rows[0]["asin"] == "B00SOMETHING"
    assert rows[0]["narrator"] == "Big Voice"
    assert rows[0]["duration_sec"] == 3600.0


async def test_raises_when_no_api_key(discovery_db, monkeypatch):
    """No configured key is a hard error during sync, not a silent no-op."""
    from app.discovery.audiobookshelf_sync import sync_audiobookshelf
    from app.library_apps import audiobookshelf as abs_mod

    async def no_key():
        return None
    monkeypatch.setattr(abs_mod, "_get_abs_api_key", no_key)

    with pytest.raises(RuntimeError, match="no abs_api_key"):
        await sync_audiobookshelf({
            "slug": "abs-test",
            "abs_base_url": "http://abs",
            "abs_library_id": "lib",
        })


async def test_raises_without_base_url(discovery_db, monkeypatch):
    from app.discovery.audiobookshelf_sync import sync_audiobookshelf

    with pytest.raises(ValueError, match="abs_base_url"):
        await sync_audiobookshelf({"slug": "abs-test", "abs_library_id": "lib"})


async def test_sync_ends_with_every_series_mode_set(discovery_db, monkeypatch):
    """2026-10 audit issue 10: the sync recomputes series author mode
    (ADR-0010) at the end, so a sync-made series isn't left NULL."""
    from app.discovery.audiobookshelf_sync import sync_audiobookshelf
    from app.discovery.database import get_db

    await _patch_abs_pipeline(monkeypatch, [
        _fake_item("abs-1", "Saga One", "Jane Doe", series="Jane Saga"),
        _fake_item("abs-2", "Saga Two", "Jane Doe", series="Jane Saga"),
    ])
    await sync_audiobookshelf({
        "slug": "abs-test",
        "abs_base_url": "http://abs:13378",
        "abs_library_id": "lib-xxx",
    })

    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT author_mode FROM series WHERE name = 'Jane Saga'")).fetchone()
    finally:
        await db.close()
    assert row["author_mode"] == "per_author"
