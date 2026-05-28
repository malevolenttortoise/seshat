"""
HTTP-level tests for `/api/discovery/covers/...`.

Covers resolve through two paths — a local `cover_path` on disk (Calibre)
and an ABS proxy call for audiobooks. These tests exercise every branch:

  * local hit  → FileResponse with the file bytes
  * local path set but missing on disk, no abs_id → 404
  * local path missing but abs_id set → proxy fallback
  * abs_id only → proxy streams bytes + preserves webp content-type
  * abs_id but abs_url/api_key unset → 404
  * abs_id but proxy returns 404 → our endpoint returns 404
  * abs_id but proxy raises connect error → 404
  * unknown book → 404
"""
from __future__ import annotations

import httpx
import pytest
import respx
from fastapi import FastAPI

from app.discovery.routers.covers import router as covers_router


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(covers_router)
    return app


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    )


@pytest.fixture
async def library_db(tmp_path, monkeypatch):
    """Per-library discovery DB with schema + one well-known book row.

    The book is inserted without `cover_path` / `audiobookshelf_id` so
    each test can UPDATE in whichever combination it needs.
    """
    from app import config as app_config
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    disco_db.set_active_library("testlib")
    await disco_db.init_db("testlib")

    db = await disco_db.get_db("testlib")
    try:
        # Minimal author row.
        await db.execute(
            "INSERT INTO authors (id, name, sort_name) VALUES (1, 'A', 'A')"
        )
        await db.execute(
            "INSERT INTO books (id, title) VALUES (42, 'Test')"
        )
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
            "VALUES (42, 1, 0)"
        )
        await db.commit()
    finally:
        await db.close()

    yield tmp_path
    disco_db.set_active_library(None)


async def _set_book(slug: str, **cols):
    from app.discovery.database import get_db
    db = await get_db(slug)
    try:
        sets = ", ".join(f"{k}=?" for k in cols)
        await db.execute(
            f"UPDATE books SET {sets} WHERE id=42", list(cols.values()),
        )
        await db.commit()
    finally:
        await db.close()


class TestLocalCover:
    async def test_existing_file_streams_with_image_jpeg(
        self, library_db, tmp_path,
    ):
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(b"\xff\xd8\xff" + b"hello-jpeg")
        await _set_book("testlib", cover_path=str(cover))

        app = _make_app()
        async with await _client(app) as c:
            r = await c.get("/api/discovery/covers/testlib/42")
            assert r.status_code == 200
            assert r.headers["content-type"] == "image/jpeg"
            assert r.content == b"\xff\xd8\xff" + b"hello-jpeg"

    async def test_missing_file_and_no_abs_id_is_404(self, library_db):
        await _set_book("testlib", cover_path="/does/not/exist.jpg")
        app = _make_app()
        async with await _client(app) as c:
            r = await c.get("/api/discovery/covers/testlib/42")
            assert r.status_code == 404

    async def test_unknown_book_id_is_404(self, library_db):
        app = _make_app()
        async with await _client(app) as c:
            r = await c.get("/api/discovery/covers/testlib/9999")
            assert r.status_code == 404


class TestAbsProxy:
    @pytest.fixture
    def patch_abs_creds(self, monkeypatch):
        """Load a fake abs_url + api key into the resolver."""
        monkeypatch.setattr(
            "app.config.load_settings",
            lambda: {"abs_url": "http://abs.test"},
        )

        async def fake_secret(key):
            if key == "abs_api_key":
                return "fake-key"
            return ""

        monkeypatch.setattr("app.secrets.get_secret", fake_secret)

    async def test_abs_id_only_proxies_and_preserves_content_type(
        self, library_db, patch_abs_creds,
    ):
        await _set_book("testlib", audiobookshelf_id="abs-123")

        with respx.mock(assert_all_called=True) as mock:
            route = mock.get("http://abs.test/api/items/abs-123/cover").mock(
                return_value=httpx.Response(
                    200, content=b"RIFFwebpbytes",
                    headers={"content-type": "image/webp"},
                )
            )

            app = _make_app()
            async with await _client(app) as c:
                r = await c.get("/api/discovery/covers/testlib/42")
                assert r.status_code == 200
                assert r.headers["content-type"] == "image/webp"
                assert r.content == b"RIFFwebpbytes"

            assert route.called
            assert route.calls.last.request.headers["Authorization"] == "Bearer fake-key"

    async def test_local_missing_falls_back_to_abs(
        self, library_db, patch_abs_creds,
    ):
        await _set_book(
            "testlib",
            cover_path="/does/not/exist.jpg",
            audiobookshelf_id="abs-x",
        )

        with respx.mock() as mock:
            mock.get("http://abs.test/api/items/abs-x/cover").mock(
                return_value=httpx.Response(
                    200, content=b"from-abs",
                    headers={"content-type": "image/jpeg"},
                )
            )

            app = _make_app()
            async with await _client(app) as c:
                r = await c.get("/api/discovery/covers/testlib/42")
                assert r.status_code == 200
                assert r.content == b"from-abs"

    async def test_proxy_404_becomes_our_404(
        self, library_db, patch_abs_creds,
    ):
        await _set_book("testlib", audiobookshelf_id="missing")

        with respx.mock() as mock:
            mock.get("http://abs.test/api/items/missing/cover").mock(
                return_value=httpx.Response(404),
            )

            app = _make_app()
            async with await _client(app) as c:
                r = await c.get("/api/discovery/covers/testlib/42")
                assert r.status_code == 404

    async def test_proxy_connect_error_becomes_404(
        self, library_db, patch_abs_creds,
    ):
        await _set_book("testlib", audiobookshelf_id="unreachable")

        with respx.mock() as mock:
            mock.get("http://abs.test/api/items/unreachable/cover").mock(
                side_effect=httpx.ConnectError("nope"),
            )

            app = _make_app()
            async with await _client(app) as c:
                r = await c.get("/api/discovery/covers/testlib/42")
                assert r.status_code == 404

    async def test_missing_abs_url_or_key_is_404(
        self, library_db, monkeypatch,
    ):
        await _set_book("testlib", audiobookshelf_id="abs-x")
        # Blank abs_url → should short-circuit to 404 with no HTTP call.
        monkeypatch.setattr(
            "app.config.load_settings", lambda: {"abs_url": ""},
        )

        async def fake_secret(_k):
            return ""

        monkeypatch.setattr("app.secrets.get_secret", fake_secret)

        app = _make_app()
        async with await _client(app) as c:
            r = await c.get("/api/discovery/covers/testlib/42")
            assert r.status_code == 404


class TestCoverUrlProxy:
    """Fallback path for books discovered via Goodreads / Hardcover /
    Amazon / ibdb — they have a `cover_url` but no local `cover_path`.
    Without this fallback the `/covers/{bid}` endpoint used to return
    404 for the majority of Seshat's library (every non-Calibre/ABS
    book), and the frontend rendered placeholder glyphs everywhere.
    """

    async def test_cover_url_streams_remote_image(self, library_db):
        await _set_book(
            "testlib", cover_url="https://cdn.example.com/book42.jpg",
        )
        with respx.mock(assert_all_called=True) as mock:
            route = mock.get("https://cdn.example.com/book42.jpg").mock(
                return_value=httpx.Response(
                    200, content=b"\xff\xd8\xffgoodreads-bytes",
                    headers={"content-type": "image/jpeg"},
                )
            )
            app = _make_app()
            async with await _client(app) as c:
                r = await c.get("/api/discovery/covers/testlib/42")
                assert r.status_code == 200
                assert r.content == b"\xff\xd8\xffgoodreads-bytes"
                assert r.headers["content-type"] == "image/jpeg"
            assert route.called

    async def test_cover_url_preserves_upstream_content_type(self, library_db):
        # Hardcover serves webp, Amazon serves jpeg, etc — we must
        # pass the upstream content-type through so the browser picks
        # the right decoder.
        await _set_book(
            "testlib", cover_url="https://cdn.example.com/hc.webp",
        )
        with respx.mock() as mock:
            mock.get("https://cdn.example.com/hc.webp").mock(
                return_value=httpx.Response(
                    200, content=b"RIFFwebpbytes",
                    headers={"content-type": "image/webp"},
                )
            )
            app = _make_app()
            async with await _client(app) as c:
                r = await c.get("/api/discovery/covers/testlib/42")
                assert r.status_code == 200
                assert r.headers["content-type"] == "image/webp"

    async def test_cover_path_takes_precedence_over_url(
        self, library_db, tmp_path,
    ):
        """When both are set (rare but possible after Calibre sync),
        the local file wins — no remote fetch happens."""
        cover = tmp_path / "local.jpg"
        cover.write_bytes(b"local-bytes")
        await _set_book(
            "testlib",
            cover_path=str(cover),
            cover_url="https://cdn.example.com/never-called.jpg",
        )
        # `assert_all_called=False` — we're registering this route
        # specifically to assert it does NOT get called.
        with respx.mock(assert_all_called=False) as mock:
            remote = mock.get("https://cdn.example.com/never-called.jpg")
            app = _make_app()
            async with await _client(app) as c:
                r = await c.get("/api/discovery/covers/testlib/42")
                assert r.content == b"local-bytes"
            assert not remote.called

    async def test_non_http_cover_url_is_404(self, library_db):
        # A bad value in the DB (e.g. a relative path snuck in) must
        # not try to fetch — the url-scheme check rejects it safely.
        await _set_book("testlib", cover_url="/relative/path.jpg")
        app = _make_app()
        async with await _client(app) as c:
            r = await c.get("/api/discovery/covers/testlib/42")
            assert r.status_code == 404

    async def test_upstream_non_image_content_type_is_404(self, library_db):
        # If the CDN serves an HTML error page or CAPTCHA, the
        # content-type won't start with "image/". Reject so the UI
        # doesn't embed garbage into an <img> tag.
        await _set_book(
            "testlib", cover_url="https://cdn.example.com/captcha.html",
        )
        with respx.mock() as mock:
            mock.get("https://cdn.example.com/captcha.html").mock(
                return_value=httpx.Response(
                    200, content=b"<html>captcha</html>",
                    headers={"content-type": "text/html"},
                )
            )
            app = _make_app()
            async with await _client(app) as c:
                r = await c.get("/api/discovery/covers/testlib/42")
                assert r.status_code == 404

    async def test_upstream_4xx_becomes_404(self, library_db):
        await _set_book(
            "testlib", cover_url="https://cdn.example.com/gone.jpg",
        )
        with respx.mock() as mock:
            mock.get("https://cdn.example.com/gone.jpg").mock(
                return_value=httpx.Response(404, content=b""),
            )
            app = _make_app()
            async with await _client(app) as c:
                r = await c.get("/api/discovery/covers/testlib/42")
                assert r.status_code == 404

    async def test_upstream_connect_error_becomes_404(self, library_db):
        await _set_book(
            "testlib", cover_url="https://cdn.example.com/dead.jpg",
        )
        with respx.mock() as mock:
            mock.get("https://cdn.example.com/dead.jpg").mock(
                side_effect=httpx.ConnectError("simulated"),
            )
            app = _make_app()
            async with await _client(app) as c:
                r = await c.get("/api/discovery/covers/testlib/42")
                assert r.status_code == 404


class TestLegacyActiveLibraryRoute:
    async def test_active_library_path_works_without_slug(
        self, library_db, tmp_path,
    ):
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(b"active-lib-cover")
        await _set_book("testlib", cover_path=str(cover))

        app = _make_app()
        async with await _client(app) as c:
            # Legacy route — no slug in path, resolves against active library.
            r = await c.get("/api/discovery/covers/42")
            assert r.status_code == 200
            assert r.content == b"active-lib-cover"
