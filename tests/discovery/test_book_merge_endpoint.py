"""
v2.10.0 — POST /api/discovery/books/{bid}/merge HTTP endpoint tests.

Confirms the wire-shape (path bid + body other_id + slug routing),
the winner-policy delegation to `pick_winner_id`, and the error
shapes (400 same-id / missing-other / two-calibre-owned, 404
missing-row). Resolution logic itself lives in
`test_book_merge.py` — these tests just verify the endpoint glue.
"""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI


@pytest.fixture
async def merge_endpoint(tmp_path, monkeypatch):
    """Per-test client with both discovery + pipeline DBs initialized
    and a real router mounted under the books prefix."""
    from app import config as app_config
    from app import database as pipeline_database
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(pipeline_database, "APP_DB_PATH", tmp_path / "seshat.db")
    await pipeline_database.init_db()
    disco_db.set_active_library("testlib")
    await disco_db.init_db("testlib")

    from app.discovery.routers import books as books_router
    app = FastAPI()
    app.include_router(books_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as c:
        yield c
    disco_db.set_active_library(None)


async def _seed_pair(*, winner_kind="calibre_owned", loser_kind="unowned_goodreads"):
    """Insert one author + two books matching the requested shapes.

    Returns (author_id, winner_row_id, loser_row_id) using the
    semantics expected by `pick_winner_id`. The caller uses the
    return ids to POST to the merge endpoint from either side.
    """
    from app.discovery.database import get_db
    from app.metadata.author_names import normalize_author_name

    kind_to_fields = {
        "calibre_owned": dict(source="calibre", owned=1, calibre_id=3897),
        "unowned_goodreads": dict(source="goodreads", owned=0,
                                  mam_torrent_id="713780",
                                  goodreads_id="57332968"),
        "owned_goodreads": dict(source="goodreads", owned=1,
                                mam_torrent_id="713780",
                                goodreads_id="57332968"),
    }
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, normalized_name) "
            "VALUES (?, ?, ?)",
            ("Arand", "Arand", normalize_author_name("Arand")),
        )
        author_id = cur.lastrowid

        async def _insert(fields):
            cols = ["title", "author_id"] + list(fields.keys())
            vals = ["Right of Retribution 2", author_id] + list(fields.values())
            ph = ", ".join("?" * len(cols))
            cur2 = await db.execute(
                f"INSERT INTO books ({', '.join(cols)}) VALUES ({ph})", vals,
            )
            return cur2.lastrowid

        w_id = await _insert(kind_to_fields[winner_kind])
        l_id = await _insert(kind_to_fields[loser_kind])
        await db.commit()
        return author_id, w_id, l_id
    finally:
        await db.close()


class TestMergeEndpoint:
    async def test_calibre_owned_always_wins_regardless_of_initiator(
        self, merge_endpoint,
    ):
        """The user can open the merge from either row's sidebar —
        the calibre+owned row survives in both cases."""
        _, calibre_id, goodreads_id = await _seed_pair()

        # Initiator = the Goodreads (loser) side.
        r = await merge_endpoint.post(
            f"/api/discovery/books/{goodreads_id}/merge?slug=testlib",
            json={"other_id": calibre_id},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["winner_id"] == calibre_id
        assert body["loser_id"] == goodreads_id
        # Surviving row carries identity fields from the absorbed side.
        merged = body["merged_book"]
        assert merged["id"] == calibre_id
        assert merged["mam_torrent_id"] == "713780"
        assert merged["goodreads_id"] == "57332968"

    async def test_same_id_rejected_with_400(self, merge_endpoint):
        _, calibre_id, _ = await _seed_pair()
        r = await merge_endpoint.post(
            f"/api/discovery/books/{calibre_id}/merge?slug=testlib",
            json={"other_id": calibre_id},
        )
        assert r.status_code == 400

    async def test_missing_other_id_rejected_with_400(self, merge_endpoint):
        _, calibre_id, _ = await _seed_pair()
        r = await merge_endpoint.post(
            f"/api/discovery/books/{calibre_id}/merge?slug=testlib",
            json={},
        )
        assert r.status_code == 400

    async def test_missing_row_returns_404(self, merge_endpoint):
        _, calibre_id, _ = await _seed_pair()
        r = await merge_endpoint.post(
            f"/api/discovery/books/{calibre_id}/merge?slug=testlib",
            json={"other_id": 99999},
        )
        assert r.status_code == 404

    async def test_two_owned_calibre_rows_rejected_with_400(self, merge_endpoint):
        from app.discovery.database import get_db
        from app.metadata.author_names import normalize_author_name
        db = await get_db()
        try:
            cur = await db.execute(
                "INSERT INTO authors (name, sort_name, normalized_name) "
                "VALUES (?, ?, ?)",
                ("Arand", "Arand", normalize_author_name("Arand")),
            )
            author_id = cur.lastrowid
            a = await db.execute(
                "INSERT INTO books (title, author_id, source, owned, "
                "calibre_id) VALUES ('X', ?, 'calibre', 1, 3796)",
                (author_id,),
            )
            b = await db.execute(
                "INSERT INTO books (title, author_id, source, owned, "
                "calibre_id) VALUES ('X', ?, 'calibre', 1, 3897)",
                (author_id,),
            )
            await db.commit()
            a_id, b_id = a.lastrowid, b.lastrowid
        finally:
            await db.close()
        r = await merge_endpoint.post(
            f"/api/discovery/books/{a_id}/merge?slug=testlib",
            json={"other_id": b_id},
        )
        assert r.status_code == 400
        assert "Calibre" in r.json()["detail"]
