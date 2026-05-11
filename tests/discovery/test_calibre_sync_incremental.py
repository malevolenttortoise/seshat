"""Incremental Calibre sync tests.

Verifies that:
- `_read_calibre_db(last_modified_threshold=...)` filters at the
  SQLite level against a real metadata.db built on the fly.
- `_read_calibre_ids` returns the full ID set regardless of threshold.
- `_read_calibre_series_authors` returns the full shallow shape for
  Pass 2's multi-author detection.
- `sync_calibre` in incremental mode upserts only filtered books,
  prunes only books that vanished from the full ID set, and returns
  `mode="incremental"`.
- A book that vanished between syncs is pruned even if no
  last_modified change is in the filtered set.

Timestamps are anchored to `time.time()` because `resolve_threshold`'s
weekly safety net compares last_full_sync_ts to wall-clock now — a
fixed past timestamp would trip the 7-day fallback and force full mode.
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
import time as _time
from pathlib import Path

import pytest


def _iso(unix_ts: float) -> str:
    """Format a unix timestamp the way Calibre writes `last_modified`."""
    return _dt.datetime.fromtimestamp(
        unix_ts, tz=_dt.timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S.%f+00:00")


# ── Synthetic Calibre DB helpers ─────────────────────────────────


CALIBRE_SCHEMA = """
    CREATE TABLE books (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL DEFAULT 'Unknown',
        sort TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        pubdate TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        series_index REAL NOT NULL DEFAULT 1.0,
        author_sort TEXT,
        path TEXT NOT NULL DEFAULT '',
        uuid TEXT,
        has_cover BOOL DEFAULT 0,
        last_modified TIMESTAMP NOT NULL DEFAULT '2000-01-01 00:00:00+00:00'
    );
    CREATE TABLE authors (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        sort TEXT
    );
    CREATE TABLE series (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        sort TEXT
    );
    CREATE TABLE books_authors_link (
        id INTEGER PRIMARY KEY,
        book INTEGER NOT NULL,
        author INTEGER NOT NULL
    );
    CREATE TABLE books_series_link (
        id INTEGER PRIMARY KEY,
        book INTEGER NOT NULL,
        series INTEGER NOT NULL
    );
    CREATE TABLE comments (
        id INTEGER PRIMARY KEY,
        book INTEGER NOT NULL,
        text TEXT
    );
    CREATE TABLE identifiers (
        id INTEGER PRIMARY KEY,
        book INTEGER NOT NULL,
        type TEXT,
        val TEXT
    );
    CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
    CREATE TABLE books_tags_link (
        id INTEGER PRIMARY KEY,
        book INTEGER NOT NULL,
        tag INTEGER NOT NULL
    );
    CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INTEGER);
    CREATE TABLE books_ratings_link (
        id INTEGER PRIMARY KEY,
        book INTEGER NOT NULL,
        rating INTEGER NOT NULL
    );
    CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT);
    CREATE TABLE books_languages_link (
        id INTEGER PRIMARY KEY,
        book INTEGER NOT NULL,
        lang_code INTEGER NOT NULL
    );
    CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT);
    CREATE TABLE books_publishers_link (
        id INTEGER PRIMARY KEY,
        book INTEGER NOT NULL,
        publisher INTEGER NOT NULL
    );
    CREATE TABLE data (
        id INTEGER PRIMARY KEY,
        book INTEGER NOT NULL,
        format TEXT NOT NULL,
        name TEXT
    );
"""


def _build_calibre_db(path: Path, books: list[dict]) -> None:
    """Materialize a synthetic Calibre metadata.db from a book list.

    Each book: {"id", "title", "last_modified", "authors":[(id,name)],
    "series":[(id,name)]}. Missing fields get sensible defaults.
    """
    conn = sqlite3.connect(path)
    try:
        conn.executescript(CALIBRE_SCHEMA)
        authors_seen: set[int] = set()
        series_seen: set[int] = set()
        for b in books:
            conn.execute(
                "INSERT INTO books (id, title, path, last_modified) "
                "VALUES (?, ?, ?, ?)",
                (b["id"], b["title"],
                 f"{b['title']}/{b['id']}", b["last_modified"]),
            )
            for a_id, a_name in b.get("authors", []):
                if a_id not in authors_seen:
                    conn.execute(
                        "INSERT INTO authors (id, name, sort) VALUES (?, ?, ?)",
                        (a_id, a_name, a_name),
                    )
                    authors_seen.add(a_id)
                conn.execute(
                    "INSERT INTO books_authors_link (book, author) VALUES (?, ?)",
                    (b["id"], a_id),
                )
            for s_id, s_name in b.get("series", []):
                if s_id not in series_seen:
                    conn.execute(
                        "INSERT INTO series (id, name, sort) VALUES (?, ?, ?)",
                        (s_id, s_name, s_name),
                    )
                    series_seen.add(s_id)
                conn.execute(
                    "INSERT INTO books_series_link (book, series) VALUES (?, ?)",
                    (b["id"], s_id),
                )
        conn.commit()
    finally:
        conn.close()


# ── Low-level read tests ────────────────────────────────────────


class TestReadFunctions:
    def test_threshold_filters_at_sql_level(self, tmp_path):
        from app.discovery.calibre_sync import _read_calibre_db

        db_path = tmp_path / "metadata.db"
        _build_calibre_db(db_path, [
            {"id": 1, "title": "Old",
             "last_modified": "2024-01-01 00:00:00+00:00",
             "authors": [(10, "Alice")]},
            {"id": 2, "title": "Fresh",
             "last_modified": "2026-05-11 12:00:00+00:00",
             "authors": [(10, "Alice")]},
            {"id": 3, "title": "AlsoFresh",
             "last_modified": "2026-05-11 13:00:00.999999",  # no tz suffix
             "authors": [(10, "Alice")]},
        ])
        # Threshold at 2026-05-01 — drops the 2024 row, keeps 2026 rows.
        threshold = 1746057600  # 2026-05-01 unix seconds
        result = _read_calibre_db(
            str(db_path), str(tmp_path),
            last_modified_threshold=threshold,
        )
        ids = sorted(b["book_id"] for b in result["books"])
        assert ids == [2, 3]

    def test_no_threshold_returns_all(self, tmp_path):
        from app.discovery.calibre_sync import _read_calibre_db

        db_path = tmp_path / "metadata.db"
        _build_calibre_db(db_path, [
            {"id": 1, "title": "Old",
             "last_modified": "2024-01-01 00:00:00+00:00",
             "authors": [(10, "Alice")]},
            {"id": 2, "title": "Fresh",
             "last_modified": "2026-05-11 12:00:00+00:00",
             "authors": [(10, "Alice")]},
        ])
        result = _read_calibre_db(str(db_path), str(tmp_path))
        assert len(result["books"]) == 2

    def test_read_calibre_ids(self, tmp_path):
        from app.discovery.calibre_sync import _read_calibre_ids

        db_path = tmp_path / "metadata.db"
        _build_calibre_db(db_path, [
            {"id": 1, "title": "A",
             "last_modified": "2024-01-01 00:00:00+00:00",
             "authors": [(10, "Alice")]},
            {"id": 42, "title": "B",
             "last_modified": "2026-05-11 12:00:00+00:00",
             "authors": [(10, "Alice")]},
        ])
        assert sorted(_read_calibre_ids(str(db_path))) == [1, 42]

    def test_read_calibre_series_authors_full_shape(self, tmp_path):
        from app.discovery.calibre_sync import _read_calibre_series_authors

        db_path = tmp_path / "metadata.db"
        _build_calibre_db(db_path, [
            {"id": 1, "title": "Halo 1",
             "last_modified": "2024-01-01 00:00:00+00:00",
             "authors": [(10, "Greg Bear")],
             "series": [(100, "Halo")]},
            {"id": 2, "title": "Halo 2",
             "last_modified": "2024-01-01 00:00:00+00:00",
             "authors": [(11, "Joseph Staten")],
             "series": [(100, "Halo")]},
            {"id": 3, "title": "Standalone",
             "last_modified": "2024-01-01 00:00:00+00:00",
             "authors": [(10, "Greg Bear")]},
        ])
        rows = _read_calibre_series_authors(str(db_path))
        by_id = {r["book_id"]: r for r in rows}
        assert set(by_id.keys()) == {1, 2, 3}
        assert by_id[1]["authors"] == [{"id": 10}]
        assert by_id[1]["series"] == [{"id": 100}]
        assert by_id[2]["authors"] == [{"id": 11}]
        assert by_id[2]["series"] == [{"id": 100}]
        # Book with no series still appears, with an empty series list.
        assert by_id[3]["series"] == []


# ── sync_calibre incremental tests ──────────────────────────────


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


def _seed_sync_state(
    settings_path: Path, *, slug: str = "test",
    last_sync_ts: float, last_full_sync_ts: float,
) -> None:
    """Write a settings.json that puts the sync into incremental mode.

    Also invalidates `app.config._settings_cache` so the next
    `load_settings()` re-reads from disk (the cache key is file mtime,
    which can collide across rapid in-test writes on coarse-mtime FSes).
    """
    import json
    from app import config as app_config
    settings_path.write_text(json.dumps({
        "library_sync_state": {
            slug: {
                "last_mtime": 0,
                "last_sync_ts": last_sync_ts,
                "last_full_sync_ts": last_full_sync_ts,
            }
        }
    }))
    app_config._settings_cache["data"] = None
    app_config._settings_cache["mtime"] = None


def _seed_for_incremental(
    settings_path: Path, *, slug: str = "test",
    threshold_unix: float,
) -> None:
    """Helper: shape state so `resolve_threshold` returns roughly
    `threshold_unix` in incremental mode.

    `last_full_sync_ts` is pinned to 5 minutes before now, well within
    the 7-day safety-net window. `last_sync_ts` is `threshold_unix +
    DRIFT_BIAS_SECONDS` so that `last_sync_ts - drift_bias_seconds ==
    threshold_unix` per `resolve_threshold`'s formula.
    """
    from app.discovery.sync_state import DRIFT_BIAS_SECONDS
    now = _time.time()
    _seed_sync_state(
        settings_path, slug=slug,
        last_sync_ts=threshold_unix + DRIFT_BIAS_SECONDS,
        last_full_sync_ts=now - 300.0,
    )


async def _book_titles_in_db():
    from app.discovery.database import get_db
    db = await get_db()
    try:
        rows = await (await db.execute(
            "SELECT title FROM books WHERE source='calibre' ORDER BY title"
        )).fetchall()
        return [r["title"] for r in rows]
    finally:
        await db.close()


class TestSyncCalibreIncremental:
    async def test_incremental_mode_skips_unmodified_books(
        self, discovery_db, monkeypatch,
    ):
        """Threshold filters out un-modified books from the upsert loop."""
        from app.discovery import calibre_sync

        now = _time.time()
        threshold = now - 3600.0           # threshold 1 hour ago
        old_ts = now - 7 * 86400.0         # 7 days ago
        fresh_ts = now - 60.0              # 1 minute ago — after threshold

        db_path = discovery_db / "metadata.db"
        _build_calibre_db(db_path, [
            {"id": 1, "title": "Old Book A",
             "last_modified": _iso(old_ts),
             "authors": [(10, "Alice")]},
            {"id": 2, "title": "Old Book B",
             "last_modified": _iso(old_ts),
             "authors": [(10, "Alice")]},
            {"id": 3, "title": "Modified Book C",
             "last_modified": _iso(fresh_ts),
             "authors": [(10, "Alice")]},
        ])
        _seed_for_incremental(
            discovery_db / "settings.json",
            threshold_unix=threshold,
        )

        result = await calibre_sync.sync_calibre(
            calibre_db_path=str(db_path),
            calibre_library_path=str(discovery_db),
        )

        assert result["mode"] == "incremental"
        # Only the modified book reaches the upsert loop.
        titles = await _book_titles_in_db()
        assert titles == ["Modified Book C"]

    async def test_full_mode_processes_everything(
        self, discovery_db, monkeypatch,
    ):
        """No sync state = first sync = full mode."""
        from app.discovery import calibre_sync

        now = _time.time()
        db_path = discovery_db / "metadata.db"
        _build_calibre_db(db_path, [
            {"id": 1, "title": "Old",
             "last_modified": _iso(now - 7 * 86400),
             "authors": [(10, "Alice")]},
            {"id": 2, "title": "Fresh",
             "last_modified": _iso(now - 60),
             "authors": [(10, "Alice")]},
        ])
        # No settings.json → first_sync → full mode.

        result = await calibre_sync.sync_calibre(
            calibre_db_path=str(db_path),
            calibre_library_path=str(discovery_db),
        )

        assert result["mode"] == "full"
        titles = await _book_titles_in_db()
        assert titles == ["Fresh", "Old"]

    async def test_incremental_prunes_deleted_books(
        self, discovery_db, monkeypatch,
    ):
        """A book missing from the full ID set gets pruned even on
        incremental (where it doesn't appear in the filtered subset)."""
        from app.discovery import calibre_sync

        now = _time.time()
        old_ts = now - 7 * 86400.0
        db_path = discovery_db / "metadata.db"
        _build_calibre_db(db_path, [
            {"id": 1, "title": "Survivor A",
             "last_modified": _iso(old_ts),
             "authors": [(10, "Alice")]},
            {"id": 2, "title": "Will-Be-Deleted",
             "last_modified": _iso(old_ts),
             "authors": [(10, "Alice")]},
            {"id": 3, "title": "Survivor B",
             "last_modified": _iso(old_ts),
             "authors": [(10, "Alice")]},
        ])

        # First: full sync — populates Seshat with all 3.
        result1 = await calibre_sync.sync_calibre(
            calibre_db_path=str(db_path),
            calibre_library_path=str(discovery_db),
        )
        assert result1["mode"] == "full"
        assert sorted(await _book_titles_in_db()) == [
            "Survivor A", "Survivor B", "Will-Be-Deleted",
        ]

        # Delete book 2 from Calibre; seed state for incremental.
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM books WHERE id = 2")
        conn.execute("DELETE FROM books_authors_link WHERE book = 2")
        conn.commit()
        conn.close()
        # Threshold just AFTER the old books' timestamp — no book has a
        # newer last_modified, so the filtered set is empty but Pass 4
        # still runs the ID-set diff.
        _seed_for_incremental(
            discovery_db / "settings.json",
            threshold_unix=old_ts + 60.0,
        )

        result2 = await calibre_sync.sync_calibre(
            calibre_db_path=str(db_path),
            calibre_library_path=str(discovery_db),
        )
        assert result2["mode"] == "incremental"
        assert result2["books_pruned"] == 1
        assert sorted(await _book_titles_in_db()) == ["Survivor A", "Survivor B"]

    async def test_incremental_preserves_shared_series_detection(
        self, discovery_db, monkeypatch,
    ):
        """Pass 2 reads the full library shallow set even on incremental,
        so a multi-author series remains correctly classified as shared
        when only one of its books was modified."""
        from app.discovery import calibre_sync

        now = _time.time()
        old_ts = now - 7 * 86400.0
        fresh_ts = now - 60.0
        db_path = discovery_db / "metadata.db"
        # Halo: Greg Bear's book is OLD; Joseph Staten's is FRESH.
        _build_calibre_db(db_path, [
            {"id": 1, "title": "Halo: Cryptum",
             "last_modified": _iso(old_ts),
             "authors": [(10, "Greg Bear")],
             "series": [(100, "Halo")]},
            {"id": 2, "title": "Halo: Contact Harvest",
             "last_modified": _iso(fresh_ts),
             "authors": [(11, "Joseph Staten")],
             "series": [(100, "Halo")]},
        ])
        # First: full sync to populate.
        await calibre_sync.sync_calibre(
            calibre_db_path=str(db_path),
            calibre_library_path=str(discovery_db),
        )

        # Threshold between old and fresh — only Staten's book is in
        # the filtered set, but Pass 2 must still see Bear's contribution
        # via the shallow full read.
        _seed_for_incremental(
            discovery_db / "settings.json",
            threshold_unix=old_ts + 3600.0,
        )

        result = await calibre_sync.sync_calibre(
            calibre_db_path=str(db_path),
            calibre_library_path=str(discovery_db),
        )
        assert result["mode"] == "incremental"

        # Halo should be a SHARED series (author_id=NULL), not per-author.
        from app.discovery.database import get_db
        db = await get_db()
        try:
            rows = await (await db.execute(
                "SELECT name, author_id FROM series WHERE LOWER(name)='halo'"
            )).fetchall()
        finally:
            await db.close()
        # Exactly one row, shared (author_id NULL).
        assert len(rows) == 1
        assert rows[0]["author_id"] is None
