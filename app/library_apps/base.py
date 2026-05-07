"""
Library app base class — the interface every library backend implements.

Each library app represents a different ebook or audiobook management
application that Seshat can sync from for its discovery domain.

Two discovery shapes are supported:

  * **File-based** (Calibre) — `db_filename` sentinel + directory scan.
    `get_root_path()` reads `env_root_var` from the environment, and
    `discover()` walks that path one level deep looking for directories
    containing `db_filename`.
  * **API-based** (Audiobookshelf) — override `discover()` to call a
    remote API. The base `discover()` default becomes a no-op when
    `db_filename` is empty. Credentials live in the encrypted secrets
    store, not in the library dict.

The sync contract takes a `library: dict` produced by `discover()` so
app-specific fields (e.g. `abs_library_id`, `base_url`) ride along
without forcing the base class to enumerate them.
"""
import os
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

logger = logging.getLogger("seshat.library_apps")


class LibraryApp(ABC):
    """Abstract base class for library source applications."""

    app_type: str = ""
    content_type: str = "ebook"
    display_name: str = ""
    db_filename: str = ""
    env_root_var: str = ""
    env_extra_var: str = ""

    def get_root_path(self) -> str:
        return os.getenv(self.env_root_var, "") if self.env_root_var else ""

    def get_extra_paths(self) -> list:
        if not self.env_extra_var:
            return []
        raw = os.getenv(self.env_extra_var, "")
        if not raw:
            return []
        paths = [p.strip() for p in raw.split(",") if p.strip()]
        valid = []
        for p in paths:
            try:
                exists = Path(p).exists()
            except (PermissionError, OSError) as e:
                logger.warning(f"{self.display_name}: extra path unreadable ({e}): {p}")
                exists = False
            if exists:
                valid.append(p)
            else:
                logger.warning(f"{self.display_name}: extra path does not exist: {p}")
        return valid

    def discover(self, root_path: str) -> list:
        """Discover libraries under a root path.

        Default behavior: scan one level deep for directories containing
        `db_filename`. API-based apps should override this entirely —
        see `AudiobookshelfApp.discover` for the API shape.

        Returns [] when `db_filename` is empty (API-based apps that
        haven't provided an override yet).
        """
        from app.config import slugify
        if not self.db_filename:
            return []
        libraries = []
        seen_slugs = set()
        root = Path(root_path)

        try:
            if not root.exists():
                logger.warning(f"{self.display_name}: root path does not exist: {root_path}")
                return []
        except PermissionError:
            logger.warning(f"{self.display_name}: cannot stat root path: {root_path}")
            return []

        def _add(mdb_path):
            parent = mdb_path.parent
            name = parent.name
            slug = slugify(name)
            base_slug = slug
            counter = 2
            while slug in seen_slugs:
                slug = f"{base_slug}-{counter}"
                counter += 1
            seen_slugs.add(slug)
            libraries.append({
                "name": name,
                "slug": slug,
                "app_type": self.app_type,
                "content_type": self.content_type,
                "display_name": self.display_name,
                "source_db_path": str(mdb_path),
                "library_path": str(parent),
            })

        def _safe_db_exists(db_file: Path) -> bool:
            try:
                return db_file.exists()
            except (PermissionError, OSError):
                return False

        try:
            children = sorted(root.iterdir())
        except (PermissionError, OSError) as e:
            logger.warning(f"{self.display_name}: cannot list {root_path} ({e})")
            return []

        for child in children:
            if child.name.startswith("."):
                continue
            try:
                is_dir = child.is_dir()
            except (PermissionError, OSError):
                continue
            if is_dir:
                db_file = child / self.db_filename
                if _safe_db_exists(db_file):
                    _add(db_file)

        root_db = root / self.db_filename
        if _safe_db_exists(root_db):
            _add(root_db)

        return libraries

    @abstractmethod
    async def sync(self, library: dict) -> dict:
        """Sync from the source into Seshat's discovery database.

        `library` is one of the dicts returned by `discover()`. Apps
        pull app-specific fields (`source_db_path`, `library_path`,
        `abs_library_id`, `base_url`, …) as needed.
        """
        pass

    @abstractmethod
    def get_cover_path(self, book_path: str, library_path: str) -> Optional[str]:
        """Get the filesystem path to a book's cover image.

        API-based apps that don't expose local cover paths should
        return None; the frontend falls back to an API-served cover
        URL in that case.
        """
        pass

    async def get_mtime(self, library: dict) -> float | str:
        """Return a monotonic mtime-like value for change detection.

        File-based default reads `source_db_path`'s mtime — but takes
        the **max** across the SQLite triplet (`.db`, `.db-wal`,
        `.db-shm`) when those exist. CWA / Calibre run with WAL mode
        on by default; new writes land in `.db-wal` and only checkpoint
        back to `.db` periodically, so `os.path.getmtime` on the main
        file alone misses pending writes (Mark's UAT 2026-05-07: 21
        new books in metadata.db-wal but `.db` mtime stale by
        ~24h, scheduled sync skipped every tick).

        API-based apps should override to fetch a *fresh* remote
        timestamp (e.g. ABS's library `lastUpdate` via
        `/api/libraries`) so the scheduled sync can skip unchanged
        libraries — async because remote sources need to hit the
        network on each tick rather than read a value cached at
        startup. API overrides may return any hashable comparable
        value (string fingerprint, float, etc.) — the `==` test in
        `sync_all_libraries` doesn't care about the type as long as
        equal-means-equal.
        """
        path = library.get("source_db_path")
        if not path:
            return 0.0
        # SQLite WAL/SHM siblings: include them in the mtime check.
        # Files that don't exist contribute nothing (suppressed by
        # the OSError filter); a library not in WAL mode just
        # collapses to the main `.db` mtime.
        candidates = [path, f"{path}-wal", f"{path}-shm"]
        max_m = 0.0
        for p in candidates:
            try:
                m = os.path.getmtime(p)
                if m > max_m:
                    max_m = m
            except OSError:
                continue
        return max_m
