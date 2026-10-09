"""Provenance (2026-10 audit wave 4b, S8 / G63): which source created a
discovered book (`discovered_by`, kept when it becomes owned), which source
wrote each of its fields (`field_source_map`), and which source won each
field of a grab's enrichment (`enriched.field_sources`)."""
from __future__ import annotations

import json

import pytest

from app.discovery.sources.base import AuthorResult, BookResult, SeriesResult


@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    from app import config as app_config
    from app.discovery import database as disco_db
    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    yield tmp_path
    disco_db.set_active_library(None)


async def _author(name="Ann Author") -> int:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, normalized_name) VALUES (?, ?, ?)",
            (name, name, name.lower()),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _row(title: str) -> dict:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        r = await (await db.execute(
            "SELECT id, source, discovered_by, field_source_map, description, language "
            "FROM books WHERE title = ?", (title,),
        )).fetchone()
        return dict(r) if r else None
    finally:
        await db.close()


def _map(row) -> dict:
    return json.loads(row["field_source_map"] or "{}")


async def _merge(author_id, source, *, books=(), series=(), full_scan=False):
    from app.discovery.lookup import _merge_result
    result = AuthorResult(name="Ann Author", books=list(books), series=list(series))
    return await _merge_result(author_id, result, source, ["English"], full_scan=full_scan)


# ─── The discovery merge ─────────────────────────────────────


async def test_a_new_standalone_book_records_its_source(discovery_db):
    aid = await _author()
    await _merge(aid, "amazon", books=[BookResult(
        title="Lone Book", description="A story.", page_count=300,
        external_id="B0X", source="amazon",
    )])
    row = await _row("Lone Book")
    assert row["source"] == "amazon" and row["discovered_by"] == "amazon"
    assert _map(row) == {"title": "amazon", "description": "amazon", "page_count": "amazon"}


async def test_a_new_series_book_records_its_series_source(discovery_db):
    aid = await _author()
    bk = BookResult(title="Series One", series_name="Saga", series_index=1.0,
                    language="English", external_id="7", source="goodreads")
    await _merge(aid, "goodreads", series=[SeriesResult(name="Saga", books=[bk])])
    row = await _row("Series One")
    assert row["discovered_by"] == "goodreads"
    assert _map(row) == {"title": "goodreads", "series": "goodreads",
                         "series_index": "goodreads", "language": "goodreads"}


async def test_a_higher_priority_series_takes_the_series_credit(discovery_db):
    aid = await _author()
    hc = BookResult(title="Book", series_name="Hardcover's Saga", series_index=1.0, external_id="h1")
    await _merge(aid, "hardcover", series=[SeriesResult(name="Hardcover's Saga", books=[hc])])
    gr = BookResult(title="Book", series_name="Goodreads Saga", series_index=1.0, external_id="g1")
    await _merge(aid, "goodreads", series=[SeriesResult(name="Goodreads Saga", books=[gr])])
    m = _map(await _row("Book"))
    assert (m["series"], m["series_index"], m["title"]) == ("goodreads", "goodreads", "hardcover")


async def test_a_later_source_is_credited_with_the_fields_it_fills(discovery_db):
    aid = await _author()
    await _merge(aid, "hardcover", books=[BookResult(title="Shared", external_id="h1")])
    await _merge(aid, "kobo", full_scan=True, books=[BookResult(
        title="Shared", description="Kobo's blurb.", language="English", external_id="k1",
    )])
    row = await _row("Shared")
    assert row["discovered_by"] == "hardcover"
    assert _map(row) == {"title": "hardcover", "description": "kobo", "language": "kobo"}


async def test_a_queued_difference_doesnt_move_the_credit(discovery_db):
    aid = await _author()
    await _merge(aid, "hardcover", books=[BookResult(
        title="Shared", description="First.", external_id="h1")])
    await _merge(aid, "kobo", full_scan=True, books=[BookResult(
        title="Shared", description="Different.", external_id="k1")])
    assert _map(await _row("Shared"))["description"] == "hardcover"   # queued, not written


async def test_a_url_only_merge_leaves_the_map_alone(discovery_db):
    aid = await _author()
    await _merge(aid, "hardcover", books=[BookResult(title="Shared", external_id="h1")])
    before = (await _row("Shared"))["field_source_map"]
    await _merge(aid, "ibdb", books=[BookResult(title="Shared", external_id="i1")])
    assert (await _row("Shared"))["field_source_map"] == before


# ─── Book merge, queue apply, migration ──────────────────────


async def test_merging_two_rows_keeps_the_discoverer(discovery_db):
    from app.discovery.book_merge import _resolve_fields
    w = {"title": "T", "source": "calibre", "owned": 1, "discovered_by": None}
    l = {"title": "T", "source": "amazon", "owned": 0, "discovered_by": "amazon"}
    assert _resolve_fields(w, l)["discovered_by"] == "amazon"


async def test_accepting_a_queued_change_credits_its_source(discovery_db):
    from app.discovery.database import get_db
    from app.discovery.routers.metadata import queue_apply
    aid = await _author()
    await _merge(aid, "hardcover", books=[BookResult(
        title="Shared", description="First.", external_id="h1")])
    await _merge(aid, "kobo", full_scan=True, books=[BookResult(
        title="Shared", description="Kobo's longer blurb.", external_id="k1")])
    db = await get_db()
    try:
        (qid,) = await (await db.execute(
            "SELECT id FROM metadata_review_queue WHERE field = 'description'")).fetchone()
    finally:
        await db.close()
    await queue_apply(qid)
    row = await _row("Shared")
    assert row["description"] == "Kobo's longer blurb."
    assert _map(row)["description"] == "kobo"


async def test_the_migration_backfills_discovered_books_only(tmp_path, monkeypatch):
    """`discovered_by` = `source` for rows no library owns; an owned row's
    discoverer was already overwritten."""
    import aiosqlite
    from app.discovery import database as disco_db
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    await disco_db.init_db("mig")
    db = await aiosqlite.connect(tmp_path / "seshat_mig.db")
    try:
        await db.execute("INSERT INTO books (title, source, owned) VALUES ('A', 'amazon', 0)")
        await db.execute("INSERT INTO books (title, source, owned) VALUES ('C', 'calibre', 1)")
        await db.execute("INSERT INTO books (title, source, owned) VALUES ('S', 'audiobookshelf', 1)")
        await db.execute("UPDATE books SET discovered_by = NULL")
        (v,) = await (await db.execute("PRAGMA user_version")).fetchone()
        await db.execute(f"PRAGMA user_version = {v - 2}")   # replay the two S8 entries
        await db.commit()
    finally:
        await db.close()
    await disco_db.init_db("mig")
    db = await aiosqlite.connect(tmp_path / "seshat_mig.db")
    try:
        rows = dict(await (await db.execute("SELECT title, discovered_by FROM books")).fetchall())
    finally:
        await db.close()
    assert rows == {"A": "amazon", "C": None, "S": None}


# ─── Enrichment's per-field winners ──────────────────────────


def test_enrichment_records_which_source_won_each_field():
    from app.metadata.enricher import _merge_records
    from app.metadata.record import MetaRecord
    mam = MetaRecord(title="T", authors=["A"], description="short", source="mam")
    # Goodreads' title equals MAM's: the credit stays with the first.
    gr = MetaRecord(title="".join(["T"]), authors=["A"], description="a much longer description",
                    page_count=300, cover_url="gr.jpg", source="goodreads")
    hc = MetaRecord(title="T3", page_count=999, isbn="978", language="en", source="hardcover")
    merged = _merge_records(_merge_records(_merge_records(None, mam), gr), hc)
    assert merged.to_dict()["field_sources"] == {
        "title": "mam", "authors": "mam", "description": "goodreads",
        "page_count": "goodreads", "cover_url": "goodreads",
        "isbn": "hardcover", "language": "hardcover",
    }
