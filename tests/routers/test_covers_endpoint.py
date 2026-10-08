"""`GET /api/v1/covers/{path}` serves a stored cover by its absolute path,
with or without the path's leading "/" (2026-10 audit L4-04: the UI now
drops it so the URL has no `//`)."""
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI

from app.routers import covers


@pytest.fixture
def cover_file(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    (staging / "tentative-covers").mkdir(parents=True)
    f = staging / "tentative-covers" / "1.jpg"
    f.write_bytes(b"\xff\xd8\xff fake jpeg")
    monkeypatch.setattr(covers, "load_settings", lambda: {"staging_path": str(staging)})
    return f


def _client():
    app = FastAPI()
    app.include_router(covers.router)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_without_the_leading_slash(cover_file):
    async with _client() as c:
        r = await c.get("/api/v1/covers/" + quote(str(cover_file).lstrip("/"), safe=""))
    assert r.status_code == 200
    assert r.content.startswith(b"\xff\xd8")


async def test_the_old_double_slash_shape_still_works(cover_file):
    async with _client() as c:
        r = await c.get("/api/v1/covers/" + quote(str(cover_file), safe=""))
    assert r.status_code == 200


async def test_outside_the_allowed_roots_is_refused(cover_file):
    # Restoring the "/" can't turn a relative path into an escape.
    async with _client() as c:
        r = await c.get("/api/v1/covers/" + quote("etc/passwd", safe=""))
    assert r.status_code == 403
