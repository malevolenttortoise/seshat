"""
HTTP-level tests for `/api/discovery/persons/{person_id}` (v2.20.0 Phase 2).

Also covers:
  - `/api/discovery/authors/{aid}` now returns `person_id` additively.
  - `/api/discovery/authors/link-pen-names` dual-writes to
    `pen_name_links_v2`.
  - `/api/discovery/authors/pen-name-link/{id}` dual-deletes from
    `pen_name_links_v2`.

These tests build a two-library environment (calibre-library +
abs-audio-library) with shared persons via the v2.20.0 identity
migration, then exercise the union endpoint end-to-end through the
real FastAPI router.
"""
from __future__ import annotations

import aiosqlite
import httpx
import pytest
from fastapi import FastAPI


@pytest.fixture
async def cross_lib_http_env(tmp_path, monkeypatch):
    """Two-library + global identity DB, exposed through an httpx client.

    Yields a dict with:
      - `client`: httpx AsyncClient bound to a FastAPI app with the
         authors router mounted.
      - `add_author(slug, name, **source_ids)`: seed an author row in a
         per-library DB.
      - `add_book(slug, title, author_id, owned=0, hidden=0)`: seed a
         standalone book.
      - `migrate()`: run the v2.20.0 identity migration to populate
         `persons` + `author_links`.
      - `person_id_of(slug, author_id)`: lookup helper.
    """
    from app import config as app_config
    from app import database, state
    from app.discovery import author_identity
    from app.discovery import database as disco_db
    from app.discovery.author_identity import migrate_to_cross_library_identity
    from app.discovery.author_identity import person_id_for
    from app.discovery.routers.authors import router

    # Point all path constants at the test tmp dir.
    global_path = tmp_path / "seshat.db"
    monkeypatch.setattr(app_config, "APP_DB_PATH", global_path)
    monkeypatch.setattr(database, "APP_DB_PATH", global_path)
    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(author_identity, "DATA_DIR", tmp_path)

    # Init both per-library DBs through the production initializer so
    # they get the full schema (authors, books, series, hidden flag,
    # pen_name_links, etc).
    slugs = ["calibre-library", "abs-audio-library"]
    for slug in slugs:
        await disco_db.init_db(slug)
    disco_db.set_active_library("calibre-library")

    # Global identity DB.
    await database.init_db()

    # Register libraries on app state so `_author_detail_for_slug`
    # resolves content_type metadata.
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "calibre-library", "content_type": "ebook",
         "name": "Calibre", "display_name": "Calibre Library"},
        {"slug": "abs-audio-library", "content_type": "audiobook",
         "name": "ABS", "display_name": "Audiobookshelf"},
    ])

    async def add_author(
        slug: str,
        name: str,
        *,
        amazon_id: str | None = None,
        goodreads_id: str | None = None,
        hardcover_id: str | None = None,
    ) -> int:
        from app.metadata.author_names import normalize_author_name
        db = await disco_db.get_db(slug)
        try:
            cur = await db.execute(
                "INSERT INTO authors "
                "(name, sort_name, normalized_name, "
                " amazon_id, goodreads_id, hardcover_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, name, normalize_author_name(name),
                 amazon_id, goodreads_id, hardcover_id),
            )
            await db.commit()
            return cur.lastrowid
        finally:
            await db.close()

    async def add_book(
        slug: str, title: str, author_id: int,
        *, owned: int = 0, hidden: int = 0,
    ) -> int:
        db = await disco_db.get_db(slug)
        try:
            cur = await db.execute(
                "INSERT INTO books "
                "(title, author_id, hidden, owned) "
                "VALUES (?, ?, ?, ?)",
                (title, author_id, hidden, owned),
            )
            # v3.0.0 Phase 4 (ADR-0008): author/person detail reads via
            # book_authors — link the seeded book to its author at pos 0.
            await db.execute(
                "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
                "VALUES (?, ?, 0)",
                (cur.lastrowid, author_id),
            )
            await db.commit()
            return cur.lastrowid
        finally:
            await db.close()

    async def add_pen_link_legacy(
        slug: str, canonical_aid: int, alias_aid: int,
        link_type: str = "pen_name",
    ) -> int:
        db = await disco_db.get_db(slug)
        try:
            cur = await db.execute(
                "INSERT INTO pen_name_links "
                "(canonical_author_id, alias_author_id, link_type) "
                "VALUES (?, ?, ?)",
                (canonical_aid, alias_aid, link_type),
            )
            await db.commit()
            return cur.lastrowid
        finally:
            await db.close()

    async def migrate():
        await migrate_to_cross_library_identity(slugs)

    async def person_id_of(slug: str, author_id: int) -> int | None:
        return await person_id_for(slug, author_id)

    async def read_v2_pen_links() -> list[dict]:
        db = await aiosqlite.connect(str(global_path))
        db.row_factory = aiosqlite.Row
        try:
            rows = await (await db.execute(
                "SELECT canonical_person_id, alias_person_id, link_type "
                "FROM pen_name_links_v2"
            )).fetchall()
            return [dict(r) for r in rows]
        finally:
            await db.close()

    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield {
            "client": client,
            "slugs": slugs,
            "add_author": add_author,
            "add_book": add_book,
            "add_pen_link_legacy": add_pen_link_legacy,
            "migrate": migrate,
            "person_id_of": person_id_of,
            "read_v2_pen_links": read_v2_pen_links,
        }
    disco_db.set_active_library(None)


# ─── /persons/{person_id} ────────────────────────────────────


class TestPersonsDetail:
    async def test_returns_404_for_unknown_person(self, cross_lib_http_env):
        client = cross_lib_http_env["client"]
        r = await client.get("/api/discovery/persons/999999")
        assert r.status_code == 404

    async def test_single_library_person_view(self, cross_lib_http_env):
        env = cross_lib_http_env
        aid = await env["add_author"](
            "calibre-library", "Brandon Sanderson",
            amazon_id="B001IGFHW6", goodreads_id="38550",
        )
        await env["add_book"]("calibre-library", "Mistborn", aid, owned=1)
        await env["migrate"]()
        pid = await env["person_id_of"]("calibre-library", aid)

        r = await env["client"].get(f"/api/discovery/persons/{pid}")
        assert r.status_code == 200
        body = r.json()

        assert body["person_id"] == pid
        assert body["canonical_name"] == "Brandon Sanderson"
        assert body["display_name"] == "Brandon Sanderson"
        assert body["source_ids"] == {
            "amazon": "B001IGFHW6",
            "goodreads": "38550",
        }
        assert len(body["libraries"]) == 1
        lib = body["libraries"][0]
        assert lib["library_slug"] == "calibre-library"
        assert lib["content_type"] == "ebook"
        assert lib["author_id"] == aid
        assert lib["author"]["name"] == "Brandon Sanderson"
        assert len(lib["author"]["standalone_books"]) == 1
        assert body["global_stats"]["owned"] == 1
        assert body["global_stats"]["total"] == 1
        assert body["pen_names"] == []
        assert body["low_confidence"] is False

    async def test_cross_library_union_view(self, cross_lib_http_env):
        env = cross_lib_http_env
        # Same author in both libraries → linked to one person.
        c_aid = await env["add_author"](
            "calibre-library", "William D. Arand",
            amazon_id="B01AY7PSG4",
        )
        a_aid = await env["add_author"](
            "abs-audio-library", "William D. Arand",
            goodreads_id="14905104",
        )
        await env["add_book"]("calibre-library", "Half Way Home", c_aid, owned=1)
        await env["add_book"]("calibre-library", "Phoenix Rising", c_aid, owned=0)
        await env["add_book"]("abs-audio-library", "Phoenix Rising", a_aid, owned=1)
        await env["migrate"]()
        pid = await env["person_id_of"]("calibre-library", c_aid)
        assert pid == await env["person_id_of"]("abs-audio-library", a_aid)

        r = await env["client"].get(f"/api/discovery/persons/{pid}")
        assert r.status_code == 200
        body = r.json()

        assert body["canonical_name"] == "William D. Arand"
        # Source IDs unioned across libraries (migration didn't mirror
        # them in our seed, so each came from its own row).
        assert body["source_ids"] == {
            "amazon": "B01AY7PSG4",
            "goodreads": "14905104",
        }
        # Two library entries, one ebook + one audiobook.
        assert len(body["libraries"]) == 2
        libs_by_type = {l["content_type"]: l for l in body["libraries"]}
        assert set(libs_by_type) == {"ebook", "audiobook"}
        assert libs_by_type["ebook"]["library_slug"] == "calibre-library"
        assert libs_by_type["audiobook"]["library_slug"] == "abs-audio-library"
        # global_stats sums across libraries.
        assert body["global_stats"]["total"] == 3
        assert body["global_stats"]["owned"] == 2

    async def test_low_confidence_flagged(self, cross_lib_http_env):
        env = cross_lib_http_env
        # Two unrelated authors with the same normalized name, zero
        # shared source IDs → migration flags both links as low.
        c_aid = await env["add_author"](
            "calibre-library", "John Smith",
            amazon_id="B0CALIBRESMITH",
        )
        a_aid = await env["add_author"](
            "abs-audio-library", "John Smith",
            amazon_id="B0ABSSMITH",  # disjoint!
        )
        await env["add_book"]("calibre-library", "Ebook 1", c_aid)
        await env["add_book"]("abs-audio-library", "Audio 1", a_aid)
        await env["migrate"]()
        pid = await env["person_id_of"]("calibre-library", c_aid)

        r = await env["client"].get(f"/api/discovery/persons/{pid}")
        body = r.json()
        assert body["low_confidence"] is True

    async def test_pen_names_surface_from_v2(self, cross_lib_http_env):
        env = cross_lib_http_env
        canonical_aid = await env["add_author"](
            "calibre-library", "Charles Lamb", amazon_id="B00QE0X49O",
        )
        alias_aid = await env["add_author"](
            "calibre-library", "C.W. Lamb",
        )
        await env["add_pen_link_legacy"](
            "calibre-library", canonical_aid, alias_aid,
        )
        await env["migrate"]()
        canonical_pid = await env["person_id_of"]("calibre-library", canonical_aid)
        alias_pid = await env["person_id_of"]("calibre-library", alias_aid)

        # Canonical view: alias appears under direction=alias_of_this.
        r = await env["client"].get(f"/api/discovery/persons/{canonical_pid}")
        body = r.json()
        assert len(body["pen_names"]) == 1
        pn = body["pen_names"][0]
        assert pn["person_id"] == alias_pid
        assert pn["canonical_name"] == "C.W. Lamb"
        assert pn["direction"] == "alias_of_this"

        # Alias view: canonical appears under direction=this_is_alias_of.
        r = await env["client"].get(f"/api/discovery/persons/{alias_pid}")
        body = r.json()
        assert len(body["pen_names"]) == 1
        pn = body["pen_names"][0]
        assert pn["person_id"] == canonical_pid
        assert pn["direction"] == "this_is_alias_of"

    async def test_orphan_link_skipped_gracefully(self, cross_lib_http_env):
        """If an author_link points at an author_id that no longer
        exists in the per-library DB, the endpoint should skip that
        library block rather than 500."""
        env = cross_lib_http_env
        aid = await env["add_author"]("calibre-library", "Stub Author")
        await env["migrate"]()
        pid = await env["person_id_of"]("calibre-library", aid)

        # Delete the per-library author row without cleaning up the link.
        from app.discovery.database import get_db
        db = await get_db("calibre-library")
        try:
            await db.execute("DELETE FROM authors WHERE id=?", (aid,))
            await db.commit()
        finally:
            await db.close()

        r = await env["client"].get(f"/api/discovery/persons/{pid}")
        assert r.status_code == 200
        # The orphan link is skipped; persons row still returns its
        # canonical identity with an empty libraries list.
        body = r.json()
        assert body["libraries"] == []


# ─── /authors/{aid} now returns person_id ────────────────────


class TestAuthorsDetailPersonId:
    async def test_person_id_surfaced_when_linked(self, cross_lib_http_env):
        env = cross_lib_http_env
        aid = await env["add_author"]("calibre-library", "Brandon Sanderson")
        await env["migrate"]()
        pid = await env["person_id_of"]("calibre-library", aid)

        r = await env["client"].get(
            f"/api/discovery/authors/{aid}?slug=calibre-library"
        )
        assert r.status_code == 200
        assert r.json()["person_id"] == pid

    async def test_person_id_none_when_unlinked(self, cross_lib_http_env):
        """Author row inserted without running the migration → no link
        yet → response includes person_id=null rather than crashing."""
        env = cross_lib_http_env
        aid = await env["add_author"]("calibre-library", "Unlinked Author")
        # No migrate() call.
        r = await env["client"].get(
            f"/api/discovery/authors/{aid}?slug=calibre-library"
        )
        assert r.status_code == 200
        assert r.json()["person_id"] is None

    async def test_cross_library_uses_author_links_for_punctuation_drift(
        self, cross_lib_http_env,
    ):
        """v2.20.0 — the cross_library fanout in /authors/{aid} prefers
        author_links over normalized_name matching. Even when two
        libraries have differently-punctuated names (C.W. Lamb vs
        Charles W. Lamb), they merge correctly as long as the migration
        linked them (via shared source IDs)."""
        env = cross_lib_http_env
        # Same author across libraries, distinct punctuation. Shared
        # amazon_id makes the migration consolidate them under one
        # person despite the differing normalized_name.
        c_aid = await env["add_author"](
            "calibre-library", "Charles W. Lamb",
            amazon_id="B00QE0X49O",
        )
        a_aid = await env["add_author"](
            "abs-audio-library", "Charles Lamb",
            amazon_id="B00QE0X49O",
        )
        # Manually pin both per-library rows to the same person via
        # get_or_create_person — the production audit flow / Phase 5
        # triage UI eventually does this for punctuation-drift cases.
        from app.discovery.author_identity import get_or_create_person
        pid = await get_or_create_person("calibre-library", c_aid)
        # For abs-audio, the names normalize differently so the auto-
        # link path would create a new person. Override that by
        # writing the link directly.
        from app import database
        gdb = await database.get_db()
        try:
            await gdb.execute(
                "INSERT INTO author_links "
                "(person_id, library_slug, author_id, link_source) "
                "VALUES (?, 'abs-audio-library', ?, 'manual')",
                (pid, a_aid),
            )
            await gdb.commit()
        finally:
            await gdb.close()

        # Fetch via /authors/{c_aid} with include_cross_library — the
        # response should include the abs-audio block via author_links,
        # not via the lossy normalized_name match (which would miss
        # this case).
        r = await env["client"].get(
            f"/api/discovery/authors/{c_aid}?"
            f"slug=calibre-library&include_cross_library=1"
        )
        assert r.status_code == 200
        body = r.json()
        assert "abs-audio-library" in body["cross_library"]
        assert body["cross_library"]["abs-audio-library"]["author"]["name"] == "Charles Lamb"


# ─── Pen-name dual-write to v2 ───────────────────────────────


class TestPenNameDualWrite:
    async def test_link_writes_to_v2(self, cross_lib_http_env):
        env = cross_lib_http_env
        canonical_aid = await env["add_author"]("calibre-library", "Charles Lamb")
        alias_aid = await env["add_author"]("calibre-library", "C.W. Lamb")
        await env["migrate"]()
        canonical_pid = await env["person_id_of"]("calibre-library", canonical_aid)
        alias_pid = await env["person_id_of"]("calibre-library", alias_aid)

        r = await env["client"].post(
            "/api/discovery/authors/link-pen-names",
            json={
                "canonical_author_id": canonical_aid,
                "alias_author_id": alias_aid,
                "link_type": "pen_name",
            },
        )
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

        v2 = await env["read_v2_pen_links"]()
        assert any(
            row["canonical_person_id"] == canonical_pid
            and row["alias_person_id"] == alias_pid
            and row["link_type"] == "pen_name"
            for row in v2
        )

    async def test_unlink_removes_from_v2(self, cross_lib_http_env):
        env = cross_lib_http_env
        canonical_aid = await env["add_author"]("calibre-library", "Charles Lamb")
        alias_aid = await env["add_author"]("calibre-library", "C.W. Lamb")
        await env["migrate"]()

        # Link first.
        r = await env["client"].post(
            "/api/discovery/authors/link-pen-names",
            json={
                "canonical_author_id": canonical_aid,
                "alias_author_id": alias_aid,
            },
        )
        link_id = r.json()["link_id"]
        assert len(await env["read_v2_pen_links"]()) == 1

        # Unlink — v2 row should also disappear.
        r = await env["client"].delete(
            f"/api/discovery/authors/pen-name-link/{link_id}"
        )
        assert r.status_code == 200
        assert await env["read_v2_pen_links"]() == []

    async def test_link_skips_v2_when_authors_unlinked(self, cross_lib_http_env):
        """If the migration hasn't linked the authors yet, the v2 write
        is best-effort and silently skipped — the legacy write still
        succeeds."""
        env = cross_lib_http_env
        canonical_aid = await env["add_author"]("calibre-library", "Foo")
        alias_aid = await env["add_author"]("calibre-library", "Bar")
        # NO migrate() call → no author_links rows.

        r = await env["client"].post(
            "/api/discovery/authors/link-pen-names",
            json={
                "canonical_author_id": canonical_aid,
                "alias_author_id": alias_aid,
            },
        )
        assert r.status_code == 200
        # Legacy row landed but v2 row didn't (no person_ids to resolve).
        assert await env["read_v2_pen_links"]() == []


# ─── Phase 3 — Source-ID badge PATCH ──────────────────────────


class TestSourceIdBadge:
    async def test_preview_parses_url(self, cross_lib_http_env):
        env = cross_lib_http_env
        aid = await env["add_author"]("calibre-library", "Brandon Sanderson")
        await env["migrate"]()
        pid = await env["person_id_of"]("calibre-library", aid)

        r = await env["client"].get(
            f"/api/discovery/persons/{pid}/source-id/preview"
            f"?source=amazon&value=https://www.amazon.com/stores/author/B001IGFHW6/allbooks"
        )
        assert r.status_code == 200
        body = r.json()
        assert body["parsed"] == "B001IGFHW6"
        assert body["url"] == "https://www.amazon.com/stores/author/B001IGFHW6/allbooks"

    async def test_preview_empty_value_clears(self, cross_lib_http_env):
        env = cross_lib_http_env
        aid = await env["add_author"]("calibre-library", "Brandon Sanderson")
        await env["migrate"]()
        pid = await env["person_id_of"]("calibre-library", aid)

        r = await env["client"].get(
            f"/api/discovery/persons/{pid}/source-id/preview"
            f"?source=amazon&value="
        )
        body = r.json()
        assert body["parsed"] is None
        assert body["url"] is None

    async def test_preview_rejects_unknown_source(self, cross_lib_http_env):
        env = cross_lib_http_env
        aid = await env["add_author"]("calibre-library", "Brandon Sanderson")
        await env["migrate"]()
        pid = await env["person_id_of"]("calibre-library", aid)

        r = await env["client"].get(
            f"/api/discovery/persons/{pid}/source-id/preview"
            f"?source=myspace&value=foo"
        )
        assert r.status_code == 400

    async def test_patch_writes_through_to_all_linked_libraries(
        self, cross_lib_http_env,
    ):
        env = cross_lib_http_env
        c_aid = await env["add_author"]("calibre-library", "Brandon Sanderson")
        a_aid = await env["add_author"]("abs-audio-library", "Brandon Sanderson")
        await env["migrate"]()
        pid = await env["person_id_of"]("calibre-library", c_aid)
        assert pid == await env["person_id_of"]("abs-audio-library", a_aid)

        # PATCH the amazon_id via a URL — should canonicalize and mirror.
        r = await env["client"].patch(
            f"/api/discovery/persons/{pid}/source-id",
            json={
                "source": "amazon",
                "value": "https://www.amazon.com/stores/Brandon-Sanderson/author/B001IGFHW6",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["parsed"] == "B001IGFHW6"
        assert body["url"] == "https://www.amazon.com/stores/author/B001IGFHW6/allbooks"
        assert body["mirrored_rows"] == 2

        # Both per-library rows should now carry the canonical ID.
        from app.discovery.database import get_db
        for slug, aid in (("calibre-library", c_aid), ("abs-audio-library", a_aid)):
            db = await get_db(slug)
            try:
                row = await (await db.execute(
                    "SELECT amazon_id FROM authors WHERE id=?", (aid,),
                )).fetchone()
                assert row["amazon_id"] == "B001IGFHW6"
            finally:
                await db.close()

    async def test_patch_clears_when_value_empty(self, cross_lib_http_env):
        env = cross_lib_http_env
        aid = await env["add_author"](
            "calibre-library", "Brandon Sanderson",
            amazon_id="B001IGFHW6",
        )
        await env["migrate"]()
        pid = await env["person_id_of"]("calibre-library", aid)

        r = await env["client"].patch(
            f"/api/discovery/persons/{pid}/source-id",
            json={"source": "amazon", "value": ""},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["parsed"] is None
        assert body["old_value"] == "B001IGFHW6"

        from app.discovery.database import get_db
        db = await get_db("calibre-library")
        try:
            row = await (await db.execute(
                "SELECT amazon_id FROM authors WHERE id=?", (aid,),
            )).fetchone()
            assert row["amazon_id"] is None
        finally:
            await db.close()

    async def test_patch_rejects_unparseable(self, cross_lib_http_env):
        env = cross_lib_http_env
        aid = await env["add_author"]("calibre-library", "Brandon Sanderson")
        await env["migrate"]()
        pid = await env["person_id_of"]("calibre-library", aid)

        r = await env["client"].patch(
            f"/api/discovery/persons/{pid}/source-id",
            json={"source": "amazon", "value": "not-an-asin"},
        )
        assert r.status_code == 400
        assert "unrecognized" in r.json()["detail"].lower()

    async def test_patch_rejects_library_local_columns(self, cross_lib_http_env):
        env = cross_lib_http_env
        aid = await env["add_author"]("calibre-library", "Brandon Sanderson")
        await env["migrate"]()
        pid = await env["person_id_of"]("calibre-library", aid)

        r = await env["client"].patch(
            f"/api/discovery/persons/{pid}/source-id",
            json={"source": "audiobookshelf", "value": "uuid-here"},
        )
        assert r.status_code == 400
        assert "library-local" in r.json()["detail"].lower()

    async def test_patch_accepts_column_name_with_id_suffix(
        self, cross_lib_http_env,
    ):
        """Client may pass `source='amazon_id'` (the column name)
        instead of `'amazon'` — we accept both."""
        env = cross_lib_http_env
        aid = await env["add_author"]("calibre-library", "Brandon Sanderson")
        await env["migrate"]()
        pid = await env["person_id_of"]("calibre-library", aid)

        r = await env["client"].patch(
            f"/api/discovery/persons/{pid}/source-id",
            json={"source": "amazon_id", "value": "B001IGFHW6"},
        )
        assert r.status_code == 200
        assert r.json()["parsed"] == "B001IGFHW6"

    async def test_history_endpoint(self, cross_lib_http_env):
        env = cross_lib_http_env
        aid = await env["add_author"]("calibre-library", "Brandon Sanderson")
        await env["migrate"]()
        pid = await env["person_id_of"]("calibre-library", aid)

        # Make two edits.
        await env["client"].patch(
            f"/api/discovery/persons/{pid}/source-id",
            json={"source": "amazon", "value": "B001IGFHW6"},
        )
        await env["client"].patch(
            f"/api/discovery/persons/{pid}/source-id",
            json={"source": "goodreads", "value": "38550"},
        )

        r = await env["client"].get(
            f"/api/discovery/persons/{pid}/source-id/history"
        )
        assert r.status_code == 200
        history = r.json()["history"]
        assert len(history) == 2
        # Newest first.
        assert history[0]["source_name"] == "goodreads"
        assert history[0]["new_value"] == "38550"
        assert history[1]["source_name"] == "amazon"


# ─── Phase 4 — Cross-library search dedup ─────────────────────


class TestPersonsSearch:
    async def test_dedupes_cross_library_hits(self, cross_lib_http_env):
        env = cross_lib_http_env
        c_aid = await env["add_author"]("calibre-library", "William D. Arand")
        a_aid = await env["add_author"]("abs-audio-library", "William D. Arand")
        await env["migrate"]()

        r = await env["client"].get(
            "/api/discovery/persons/search?q=Arand"
        )
        assert r.status_code == 200
        persons = r.json()["persons"]
        # Two per-library rows, ONE person hit.
        assert len(persons) == 1
        p = persons[0]
        assert p["canonical_name"] == "William D. Arand"
        assert set(p["library_slugs"]) == {"calibre-library", "abs-audio-library"}
        assert set(p["content_types"]) == {"ebook", "audiobook"}
        assert p["author_ids_by_slug"]["calibre-library"] == c_aid
        assert p["author_ids_by_slug"]["abs-audio-library"] == a_aid

    async def test_normalized_name_matches_punctuation_drift(
        self, cross_lib_http_env,
    ):
        """Typing `D. L. Bacon` matches a person with normalized_name
        of `D.L. Bacon` (both normalize identically)."""
        env = cross_lib_http_env
        await env["add_author"]("calibre-library", "D.L. Bacon")
        await env["migrate"]()

        r = await env["client"].get(
            "/api/discovery/persons/search?q=D.+L.+Bacon"
        )
        assert r.status_code == 200
        persons = r.json()["persons"]
        assert any(p["canonical_name"] == "D.L. Bacon" for p in persons)

    async def test_substring_match(self, cross_lib_http_env):
        env = cross_lib_http_env
        await env["add_author"]("calibre-library", "Brandon Sanderson")
        await env["add_author"]("calibre-library", "Brandon Mull")
        await env["migrate"]()

        r = await env["client"].get(
            "/api/discovery/persons/search?q=Brandon"
        )
        persons = r.json()["persons"]
        names = [p["canonical_name"] for p in persons]
        assert "Brandon Sanderson" in names
        assert "Brandon Mull" in names

    async def test_empty_query_rejected(self, cross_lib_http_env):
        r = await cross_lib_http_env["client"].get(
            "/api/discovery/persons/search?q="
        )
        assert r.status_code == 422

    async def test_limit_respected(self, cross_lib_http_env):
        env = cross_lib_http_env
        for i in range(5):
            await env["add_author"]("calibre-library", f"Test Author {i}")
        await env["migrate"]()

        r = await env["client"].get(
            "/api/discovery/persons/search?q=Test&limit=3"
        )
        assert r.status_code == 200
        assert len(r.json()["persons"]) == 3


class TestPersonsLinkPenNames:
    async def test_creates_v2_link_and_legacy_when_shared_library(
        self, cross_lib_http_env,
    ):
        env = cross_lib_http_env
        canonical_aid = await env["add_author"](
            "calibre-library", "Charles Lamb",
        )
        alias_aid = await env["add_author"](
            "calibre-library", "C.W. Lamb",
        )
        await env["migrate"]()
        canonical_pid = await env["person_id_of"]("calibre-library", canonical_aid)
        alias_pid = await env["person_id_of"]("calibre-library", alias_aid)

        r = await env["client"].post(
            "/api/discovery/persons/link-pen-names",
            json={
                "canonical_person_id": canonical_pid,
                "alias_person_id": alias_pid,
                "link_type": "pen_name",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body["v2_link_id"] is not None
        assert body["legacy_link_id"] is not None

        # v2 row landed.
        assert len(await env["read_v2_pen_links"]()) == 1

    async def test_v2_only_when_no_shared_library(self, cross_lib_http_env):
        """When the two persons have no library overlap, the link
        lives only in pen_name_links_v2 (legacy schema can't represent
        cross-library pen names)."""
        env = cross_lib_http_env
        c_aid = await env["add_author"]("calibre-library", "Person A")
        a_aid = await env["add_author"]("abs-audio-library", "Person B")
        await env["migrate"]()
        pid_a = await env["person_id_of"]("calibre-library", c_aid)
        pid_b = await env["person_id_of"]("abs-audio-library", a_aid)

        r = await env["client"].post(
            "/api/discovery/persons/link-pen-names",
            json={
                "canonical_person_id": pid_a,
                "alias_person_id": pid_b,
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["v2_link_id"] is not None
        assert body["legacy_link_id"] is None
        assert len(await env["read_v2_pen_links"]()) == 1

    async def test_unlink_drops_both_v2_and_legacy(self, cross_lib_http_env):
        env = cross_lib_http_env
        canonical_aid = await env["add_author"]("calibre-library", "Charles Lamb")
        alias_aid = await env["add_author"]("calibre-library", "C.W. Lamb")
        await env["migrate"]()
        canonical_pid = await env["person_id_of"]("calibre-library", canonical_aid)
        alias_pid = await env["person_id_of"]("calibre-library", alias_aid)

        # Link first.
        r = await env["client"].post(
            "/api/discovery/persons/link-pen-names",
            json={
                "canonical_person_id": canonical_pid,
                "alias_person_id": alias_pid,
            },
        )
        v2_id = r.json()["v2_link_id"]

        # Confirm both v2 and legacy rows exist.
        assert len(await env["read_v2_pen_links"]()) == 1

        # Unlink.
        r = await env["client"].delete(
            f"/api/discovery/persons/pen-name-link/{v2_id}"
        )
        assert r.status_code == 200
        assert await env["read_v2_pen_links"]() == []

        # Legacy row also gone.
        from app.discovery.database import get_db
        db = await get_db("calibre-library")
        try:
            row = await (await db.execute(
                "SELECT COUNT(*) AS n FROM pen_name_links"
            )).fetchone()
            assert row["n"] == 0
        finally:
            await db.close()

    async def test_reclassify_link_type(self, cross_lib_http_env):
        env = cross_lib_http_env
        a_aid = await env["add_author"]("calibre-library", "A")
        b_aid = await env["add_author"]("calibre-library", "B")
        await env["migrate"]()
        a_pid = await env["person_id_of"]("calibre-library", a_aid)
        b_pid = await env["person_id_of"]("calibre-library", b_aid)

        # Initial as pen_name.
        await env["client"].post(
            "/api/discovery/persons/link-pen-names",
            json={
                "canonical_person_id": a_pid,
                "alias_person_id": b_pid,
                "link_type": "pen_name",
            },
        )
        # Re-link as co_author — should update, not duplicate.
        r = await env["client"].post(
            "/api/discovery/persons/link-pen-names",
            json={
                "canonical_person_id": a_pid,
                "alias_person_id": b_pid,
                "link_type": "co_author",
            },
        )
        assert r.json()["status"] == "updated"
        v2 = await env["read_v2_pen_links"]()
        assert len(v2) == 1
        assert v2[0]["link_type"] == "co_author"

    async def test_self_link_rejected(self, cross_lib_http_env):
        env = cross_lib_http_env
        aid = await env["add_author"]("calibre-library", "Solo")
        await env["migrate"]()
        pid = await env["person_id_of"]("calibre-library", aid)

        r = await env["client"].post(
            "/api/discovery/persons/link-pen-names",
            json={
                "canonical_person_id": pid,
                "alias_person_id": pid,
            },
        )
        assert r.status_code == 400


# ─── Phase 5 — Link triage ────────────────────────────────────


class TestTriage:
    async def test_triage_lists_low_confidence(self, cross_lib_http_env):
        env = cross_lib_http_env
        # Two "John Smith"s with disjoint source IDs → migration flags
        # the links as low confidence.
        await env["add_author"](
            "calibre-library", "John Smith", amazon_id="B0CAL",
        )
        await env["add_author"](
            "abs-audio-library", "John Smith", amazon_id="B0ABS",
        )
        await env["migrate"]()

        r = await env["client"].get("/api/discovery/persons/triage")
        assert r.status_code == 200
        body = r.json()
        assert len(body["low_confidence"]) == 1
        lc = body["low_confidence"][0]
        assert lc["canonical_name"] == "John Smith"
        assert len(lc["links"]) == 2
        assert all(l["link_confidence"] == "low" for l in lc["links"])

    async def test_triage_lists_unlinked_authors(self, cross_lib_http_env):
        env = cross_lib_http_env
        await env["migrate"]()
        # Insert an author DIRECTLY into a per-library DB WITHOUT
        # going through the identity hooks — simulates a row that the
        # migration didn't see.
        from app.discovery.database import get_db
        db = await get_db("calibre-library")
        try:
            cur = await db.execute(
                "INSERT INTO authors (name, sort_name, normalized_name) "
                "VALUES ('Sneaky Author', 'Sneaky', 'sneaky author')",
            )
            await db.commit()
            sneaky_id = cur.lastrowid
        finally:
            await db.close()

        r = await env["client"].get("/api/discovery/persons/triage")
        body = r.json()
        names = [a["name"] for a in body["unlinked_authors"]]
        assert "Sneaky Author" in names
        sneaky = next(
            a for a in body["unlinked_authors"]
            if a["author_id"] == sneaky_id
        )
        assert sneaky["library_slug"] == "calibre-library"

    async def test_unlink_creates_new_person(self, cross_lib_http_env):
        env = cross_lib_http_env
        c_aid = await env["add_author"]("calibre-library", "Charles Lamb")
        a_aid = await env["add_author"]("abs-audio-library", "Charles Lamb")
        await env["migrate"]()
        pid = await env["person_id_of"]("calibre-library", c_aid)
        assert pid == await env["person_id_of"]("abs-audio-library", a_aid)

        # Unlink the abs-audio side — should create a new person.
        r = await env["client"].post(
            f"/api/discovery/persons/{pid}/unlink-author",
            json={"library_slug": "abs-audio-library", "author_id": a_aid},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body["new_person_id"] != pid
        assert body["old_person_dropped"] is False

        # Calibre side still on old person.
        assert await env["person_id_of"]("calibre-library", c_aid) == pid
        # Abs side on the new person.
        new_pid = await env["person_id_of"]("abs-audio-library", a_aid)
        assert new_pid == body["new_person_id"]

    async def test_unlink_drops_orphan_person(self, cross_lib_http_env):
        """Unlinking the ONLY linked row should leave the old person
        with zero links — we drop it."""
        env = cross_lib_http_env
        aid = await env["add_author"]("calibre-library", "Solo Author")
        await env["migrate"]()
        pid = await env["person_id_of"]("calibre-library", aid)

        r = await env["client"].post(
            f"/api/discovery/persons/{pid}/unlink-author",
            json={"library_slug": "calibre-library", "author_id": aid},
        )
        body = r.json()
        assert body["old_person_dropped"] is True

        # Old person should be gone.
        r = await env["client"].get(f"/api/discovery/persons/{pid}")
        assert r.status_code == 404

    async def test_link_author_moves_link(self, cross_lib_http_env):
        env = cross_lib_http_env
        c_aid = await env["add_author"]("calibre-library", "Person A")
        a_aid = await env["add_author"]("abs-audio-library", "Person B")
        await env["migrate"]()
        pid_a = await env["person_id_of"]("calibre-library", c_aid)
        pid_b = await env["person_id_of"]("abs-audio-library", a_aid)

        # Move the abs author from person_b to person_a (manual merge).
        r = await env["client"].post(
            f"/api/discovery/persons/{pid_a}/link-author",
            json={"library_slug": "abs-audio-library", "author_id": a_aid},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok"
        assert body["old_person_dropped"] is True  # B had only one link

        # Abs author now points at person A.
        assert await env["person_id_of"]("abs-audio-library", a_aid) == pid_a
        # Person B is gone.
        r = await env["client"].get(f"/api/discovery/persons/{pid_b}")
        assert r.status_code == 404

    async def test_link_already_linked_noop(self, cross_lib_http_env):
        env = cross_lib_http_env
        aid = await env["add_author"]("calibre-library", "Author")
        await env["migrate"]()
        pid = await env["person_id_of"]("calibre-library", aid)

        r = await env["client"].post(
            f"/api/discovery/persons/{pid}/link-author",
            json={"library_slug": "calibre-library", "author_id": aid},
        )
        assert r.json()["status"] == "already_linked"

    async def test_approve_flips_links_and_survives_recompute(
        self, cross_lib_http_env,
    ):
        """v2.20.0 Phase 5 — `POST /persons/{pid}/approve-links` flips
        the person's links to high+manual and exempts them from future
        re-flagging by `recompute-consolidation`."""
        env = cross_lib_http_env
        # Two "John Smith"s with disjoint source IDs → low confidence.
        c_aid = await env["add_author"](
            "calibre-library", "John Smith", amazon_id="B0CAL",
        )
        a_aid = await env["add_author"](
            "abs-audio-library", "John Smith", amazon_id="B0ABS",
        )
        await env["migrate"]()
        pid = await env["person_id_of"]("calibre-library", c_aid)

        # Confirm low-confidence.
        r = await env["client"].get("/api/discovery/persons/triage")
        assert any(p["person_id"] == pid for p in r.json()["low_confidence"])

        # Approve.
        r = await env["client"].post(
            f"/api/discovery/persons/{pid}/approve-links",
        )
        assert r.status_code == 200, r.text
        assert r.json()["approved"] == 2

        # Triage no longer lists the person.
        r = await env["client"].get("/api/discovery/persons/triage")
        assert not any(
            p["person_id"] == pid for p in r.json()["low_confidence"]
        )

        # Re-run consolidation — the approved person must NOT get
        # re-flagged (the source IDs are still disjoint, but the
        # manual link_source exempts the person from flagging).
        r = await env["client"].post(
            "/api/discovery/persons/recompute-consolidation"
        )
        assert r.status_code == 200
        # Triage still empty for the approved person.
        r = await env["client"].get("/api/discovery/persons/triage")
        assert not any(
            p["person_id"] == pid for p in r.json()["low_confidence"]
        )

        # Verify link_source rows directly.
        from app import database
        gdb = await database.get_db()
        try:
            rows = await (await gdb.execute(
                "SELECT link_source, link_confidence FROM author_links "
                "WHERE person_id = ?",
                (pid,),
            )).fetchall()
            assert all(r["link_source"] == "manual" for r in rows)
            assert all(r["link_confidence"] == "high" for r in rows)
        finally:
            await gdb.close()

    async def test_approve_404_for_unknown_person(self, cross_lib_http_env):
        r = await cross_lib_http_env["client"].post(
            "/api/discovery/persons/99999999/approve-links",
        )
        assert r.status_code == 404

    async def test_recompute_consolidation_clears_and_reflags(
        self, cross_lib_http_env,
    ):
        env = cross_lib_http_env
        # Set up a low-confidence pair.
        await env["add_author"](
            "calibre-library", "John Smith", amazon_id="B0CAL",
        )
        await env["add_author"](
            "abs-audio-library", "John Smith", amazon_id="B0ABS",
        )
        await env["migrate"]()
        # First triage: 1 low-confidence person.
        r = await env["client"].get("/api/discovery/persons/triage")
        assert len(r.json()["low_confidence"]) == 1

        # Manually fix one row's amazon_id to match the other.
        from app.discovery.database import get_db
        db = await get_db("abs-audio-library")
        try:
            await db.execute(
                "UPDATE authors SET amazon_id='B0CAL' "
                "WHERE amazon_id='B0ABS'"
            )
            await db.commit()
        finally:
            await db.close()

        # Re-run consolidation — both rows now share amazon_id, so
        # the link should be re-flagged as high confidence.
        r = await env["client"].post(
            "/api/discovery/persons/recompute-consolidation"
        )
        assert r.status_code == 200
        assert r.json()["flagged"] == 0

        # Triage now shows zero low-confidence persons.
        r = await env["client"].get("/api/discovery/persons/triage")
        assert len(r.json()["low_confidence"]) == 0
