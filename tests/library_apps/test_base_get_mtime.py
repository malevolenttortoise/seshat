"""
v2.3.4.1 — Calibre WAL-aware mtime.

Pre-v2.3.4.1 the file-based default of `get_mtime` only checked the
main `metadata.db` mtime. Calibre / CWA run with SQLite WAL mode by
default, so writes land in `metadata.db-wal` first and only
checkpoint back to `metadata.db` periodically. UAT 2026-05-07
showed Mark's metadata.db at ~24h stale while metadata.db-wal had
4MB of pending writes including 21 newly-added books. Scheduled
sync read the main file's mtime, saw "unchanged", skipped every
tick.

The fix: take the **max** mtime across `.db`, `.db-wal`, `.db-shm`.
Files that don't exist (libraries not in WAL mode) contribute
nothing — the function naturally collapses to the main `.db`.
"""
from __future__ import annotations

import os

import pytest


class _DummyApp:
    """Minimal subclass that uses the base get_mtime unchanged."""
    pass


@pytest.fixture
def calibre_dir(tmp_path):
    """Build a fake Calibre library directory with metadata.db plus
    optional WAL/SHM siblings. Returns the dir path."""
    return tmp_path


def _touch(path, mtime: float):
    path.write_bytes(b"")
    os.utime(path, (mtime, mtime))


async def test_returns_main_db_mtime_when_no_wal(calibre_dir):
    from app.library_apps.base import LibraryApp
    db = calibre_dir / "metadata.db"
    _touch(db, 1000.0)
    class _Concrete(LibraryApp):
        app_type = "test"; display_name = "Test"; db_filename = "metadata.db"; env_root_var = ""
        def discover(self, root_path: str): return []
        def get_root_path(self): return ""
        async def sync(self, library): return {}
        def get_cover_path(self, book_path, library_path): return None
    app = _Concrete()
    lib = {"source_db_path": str(db)}
    assert await app.get_mtime(lib) == 1000.0


async def test_picks_wal_mtime_when_newer(calibre_dir):
    """Main .db at T=1000, .db-wal at T=2000 → return 2000.
    The Calibre canary: pending writes haven't checkpointed yet, but
    the data IS in the WAL so we must catch it."""
    from app.library_apps.base import LibraryApp
    db = calibre_dir / "metadata.db"
    wal = calibre_dir / "metadata.db-wal"
    _touch(db, 1000.0)
    _touch(wal, 2000.0)
    class _Concrete(LibraryApp):
        app_type = "test"; display_name = "Test"; db_filename = "metadata.db"; env_root_var = ""
        def discover(self, root_path: str): return []
        def get_root_path(self): return ""
        async def sync(self, library): return {}
        def get_cover_path(self, book_path, library_path): return None
    app = _Concrete()
    lib = {"source_db_path": str(db)}
    assert await app.get_mtime(lib) == 2000.0


async def test_picks_shm_mtime_when_newest(calibre_dir):
    """Three siblings: db=1000, wal=2000, shm=3000 → return 3000."""
    from app.library_apps.base import LibraryApp
    db = calibre_dir / "metadata.db"
    wal = calibre_dir / "metadata.db-wal"
    shm = calibre_dir / "metadata.db-shm"
    _touch(db, 1000.0)
    _touch(wal, 2000.0)
    _touch(shm, 3000.0)
    class _Concrete(LibraryApp):
        app_type = "test"; display_name = "Test"; db_filename = "metadata.db"; env_root_var = ""
        def discover(self, root_path: str): return []
        def get_root_path(self): return ""
        async def sync(self, library): return {}
        def get_cover_path(self, book_path, library_path): return None
    app = _Concrete()
    lib = {"source_db_path": str(db)}
    assert await app.get_mtime(lib) == 3000.0


async def test_main_db_wins_when_newer_than_siblings(calibre_dir):
    """Post-checkpoint scenario — .db got rewritten and is now the
    newest. Should still return its mtime (max wins)."""
    from app.library_apps.base import LibraryApp
    db = calibre_dir / "metadata.db"
    wal = calibre_dir / "metadata.db-wal"
    _touch(db, 5000.0)
    _touch(wal, 4000.0)
    class _Concrete(LibraryApp):
        app_type = "test"; display_name = "Test"; db_filename = "metadata.db"; env_root_var = ""
        def discover(self, root_path: str): return []
        def get_root_path(self): return ""
        async def sync(self, library): return {}
        def get_cover_path(self, book_path, library_path): return None
    app = _Concrete()
    lib = {"source_db_path": str(db)}
    assert await app.get_mtime(lib) == 5000.0


async def test_missing_db_returns_zero(calibre_dir):
    """Source path doesn't exist — return 0.0 (unchanged from
    pre-v2.3.4.1 behavior)."""
    from app.library_apps.base import LibraryApp
    class _Concrete(LibraryApp):
        app_type = "test"; display_name = "Test"; db_filename = "metadata.db"; env_root_var = ""
        def discover(self, root_path: str): return []
        def get_root_path(self): return ""
        async def sync(self, library): return {}
        def get_cover_path(self, book_path, library_path): return None
    app = _Concrete()
    lib = {"source_db_path": str(calibre_dir / "missing.db")}
    assert await app.get_mtime(lib) == 0.0


async def test_no_source_path_returns_zero():
    """library dict missing source_db_path → 0.0."""
    from app.library_apps.base import LibraryApp
    class _Concrete(LibraryApp):
        app_type = "test"; display_name = "Test"; db_filename = "metadata.db"; env_root_var = ""
        def discover(self, root_path: str): return []
        def get_root_path(self): return ""
        async def sync(self, library): return {}
        def get_cover_path(self, book_path, library_path): return None
    app = _Concrete()
    assert await app.get_mtime({}) == 0.0
