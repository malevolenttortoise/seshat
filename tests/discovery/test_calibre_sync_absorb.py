"""Wave B S6 — Calibre sync absorbs a missing row by contributor overlap or
a linked author + the title identity (F10), and keeps its author rows
clean (C10: Calibre's `|` read back as a comma; author rows whose Calibre
author is gone retired once they list nothing).
"""
from __future__ import annotations

import pytest

from app import state


def _book(book_id, title, authors, *, series=None, index=1.0):
    """`_read_calibre_db`'s shape; `authors` = [(calibre id, name)]."""
    return {
        "book_id": book_id, "title": title, "pubdate": "2024-01-01",
        "series_index": index, "book_path": f"x/{title}", "cover_path": None,
        "isbn": None,
        "authors": [{"id": i, "name": n, "sort": n} for i, n in authors],
        "series": [{"id": 900, "name": series}] if series else [],
        "tags": None, "rating": None, "description": None, "language": "eng",
        "publisher": None, "formats": "epub",
    }


@pytest.fixture
async def env(tmp_path, monkeypatch):
    from app import config as app_config
    from app import database as pipeline_database
    from app.discovery import database as disco_db
    from app.discovery import roster

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(pipeline_database, "APP_DB_PATH", tmp_path / "seshat.db")
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "test", "name": "Books", "content_type": "ebook"},
        {"slug": "audio", "name": "Audio", "content_type": "audiobook"},
    ])
    await pipeline_database.init_db()
    await disco_db.init_db("test")
    await disco_db.init_db("audio")
    prev = disco_db.get_active_library()
    disco_db.set_active_library("test")
    roster.invalidate()
    yield tmp_path
    roster.invalidate()
    disco_db.set_active_library(prev)


async def _q(sql, args=(), slug="test"):
    from app.discovery.database import get_db
    db = await get_db(slug)
    try:
        cur = await db.execute(sql, args)
        if sql.lstrip().upper().startswith("SELECT"):
            return [tuple(r) for r in await cur.fetchall()]
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _author(name, calibre_id=None, slug="test"):
    from app.metadata.author_names import normalize_author_name
    return await _q(
        "INSERT INTO authors (name, sort_name, normalized_name, calibre_id) VALUES (?,?,?,?)",
        (name, name, normalize_author_name(name), calibre_id), slug)


async def _missing(title, author_ids, *, series=None, index=None, slug="test", source="hardcover"):
    sid = None
    if series:
        sid = await _q("INSERT INTO series (name, author_id) VALUES (?, ?)", (series, author_ids[0]), slug)
    bid = await _q(
        "INSERT INTO books (title, source, owned, series_id, series_index) VALUES (?,?,0,?,?)",
        (title, source, sid, index), slug)
    for pos, aid in enumerate(author_ids):
        await _q("INSERT INTO book_authors (book_id, author_id, position) VALUES (?,?,?)",
                 (bid, aid, pos), slug)
    return bid


async def _sync(monkeypatch, books):
    from app.discovery import calibre_sync
    monkeypatch.setattr(calibre_sync, "_read_calibre_db", lambda *a, **k: {"books": books})
    await calibre_sync.sync_calibre("x", "y")


async def _row(bid):
    rows = await _q("SELECT owned, calibre_id, source FROM books WHERE id = ?", (bid,))
    return rows[0] if rows else None


async def _byline(calibre_id):
    return [r[0] for r in await _q(
        "SELECT a.name FROM books b JOIN book_authors ba ON ba.book_id = b.id "
        "JOIN authors a ON a.id = ba.author_id WHERE b.calibre_id = ? ORDER BY ba.position",
        (calibre_id,))]


# ─── Absorb (F10) ────────────────────────────────────────────


async def test_a_co_author_the_calibre_book_lists_second_absorbs_the_missing_row(env, monkeypatch):
    """Atlantis University: owned under the pen name first, the missing row
    under the co-author Calibre lists second."""
    todd = await _author("Michael Todd", calibre_id=1)
    anderle = await _author("Michael Anderle", calibre_id=2)
    bid = await _missing("Through the Gate (Atlantis University Book 1)", [anderle])
    await _sync(monkeypatch, [_book(10, "Through the Gate (Atlantis University Book 1)",
                                    [(1, "Michael Todd"), (2, "Michael Anderle")])])
    assert await _row(bid) == (1, 10, "calibre")
    assert await _byline(10) == ["Michael Todd", "Michael Anderle"]
    assert todd


async def test_the_title_identity_absorbs_another_title_shape(env, monkeypatch):
    a = await _author("Jim Butcher", calibre_id=1)
    bid = await _missing("Book 06.0 - Blood Rites", [a])
    await _sync(monkeypatch, [_book(10, "Blood Rites", [(1, "Jim Butcher")],
                                    series="The Dresden Files", index=6.0)])
    assert await _row(bid) == (1, 10, "calibre")


async def test_a_linked_pen_name_absorbs(env, monkeypatch):
    """Shirtaloon / Travis Deverell: absorbed once Mark links them."""
    shirt = await _author("Shirtaloon", calibre_id=1)
    travis = await _author("Travis Deverell")
    bid = await _missing("He Who Fights With Monsters, Book 2: Adventurer", [travis])
    await _q("INSERT INTO pen_name_links (canonical_author_id, alias_author_id, link_type) "
             "VALUES (?, ?, 'pen_name')", (travis, shirt))
    await _sync(monkeypatch, [_book(10, "He Who Fights With Monsters 2", [(1, "Shirtaloon")],
                                    series="He Who Fights with Monsters", index=2.0)])
    assert await _row(bid) == (1, 10, "calibre")
    assert await _byline(10) == ["Shirtaloon"]


async def test_without_the_link_a_pen_name_split_stays(env, monkeypatch):
    await _author("Shirtaloon", calibre_id=1)
    travis = await _author("Travis Deverell")
    bid = await _missing("He Who Fights With Monsters, Book 2: Adventurer", [travis])
    await _sync(monkeypatch, [_book(10, "He Who Fights With Monsters 2", [(1, "Shirtaloon")],
                                    series="He Who Fights with Monsters", index=2.0)])
    assert await _row(bid) == (0, None, "hardcover")


async def test_another_volume_is_not_absorbed(env, monkeypatch):
    a = await _author("William D. Arand", calibre_id=1)
    bid = await _missing("Right of Retribution 3", [a])
    await _sync(monkeypatch, [_book(10, "Right of Retribution: Book 2", [(1, "William D. Arand")])])
    assert await _row(bid) == (0, None, "hardcover")


async def test_a_book_of_another_filter_family_is_not_absorbed(env, monkeypatch):
    """G197: a comic of the same name is never the owned novel."""
    a = await _author("Frank Herbert", calibre_id=1)
    bid = await _missing("Dune: The Graphic Novel, Book 1", [a])
    await _sync(monkeypatch, [_book(10, "Dune", [(1, "Frank Herbert")])])
    assert await _row(bid) == (0, None, "hardcover")


async def test_two_matches_are_flipped_owned_not_merged(env, monkeypatch):
    a = await _author("Susan Dennard", calibre_id=1)
    b1 = await _missing("Truthwitch: The Witchlands", [a])
    b2 = await _missing("Truthwitch: A Witchlands Novel", [a])
    await _sync(monkeypatch, [_book(10, "Truthwitch", [(1, "Susan Dennard")],
                                    series="The Witchlands", index=1.0)])
    assert await _row(b1) == (1, None, "hardcover")
    assert await _row(b2) == (1, None, "hardcover")


async def test_two_matches_that_are_different_books_flip_nothing(env, monkeypatch):
    """G211 (2026-10-22): a graphic novel's Vol. 1 and Vol. 2 both read as the
    owned novel; flipping them owned hid two real missing books (and the owned
    check would stop their announces). Which, if any, is the book is a guess."""
    a = await _author("Kevin J. Anderson", calibre_id=1)
    v1 = await _missing("Dune: House Corrino, Vol. 1", [a], series="Dune: House Corrino", index=1.0)
    v2 = await _missing("Dune: House Corrino, Vol. 2", [a])
    await _q("UPDATE books SET series_id = (SELECT series_id FROM books WHERE id = ?), "
             "series_index = 2.0 WHERE id = ?", (v1, v2))
    await _sync(monkeypatch, [_book(10, "Dune: House Corrino", [(1, "Kevin J. Anderson")],
                                    series="Prelude to Dune", index=3.0)])
    assert await _row(v1) == (0, None, "hardcover")
    assert await _row(v2) == (0, None, "hardcover")


async def test_a_row_the_filters_hid_as_a_comic_is_not_absorbed(env, monkeypatch):
    """G197 as ruled: a row's stored filter reason is its family here, so a
    graphic novel hidden from its series name ("The Dresden Files Graphic
    Novels") never becomes the owned novel though its title reads as one."""
    a = await _author("Jim Butcher", calibre_id=1)
    bid = await _missing("The Dresden Files: Storm Front, Vol. 1", [a],
                         series="The Dresden Files Graphic Novels", index=1.0)
    await _q("UPDATE books SET hidden = 1, hidden_reason = 'comic' WHERE id = ?", (bid,))
    await _sync(monkeypatch, [_book(10, "Storm Front", [(1, "Jim Butcher")])])
    assert await _row(bid) == (0, None, "hardcover")


async def test_the_update_sweep_keeps_calibres_byline(env, monkeypatch):
    """The merge unions the absorbed row's contributors; Calibre's own list
    stays the byline (Honor Among Thieves and Rachel Caine)."""
    aguirre = await _author("Ann Aguirre", calibre_id=1)
    caine = await _author("Rachel Caine")
    await _sync(monkeypatch, [_book(10, "Honor Among Thieves", [(1, "Ann Aguirre")])])
    bid = await _missing("Honor Among Thieves", [aguirre, caine])
    await _sync(monkeypatch, [_book(10, "Honor Among Thieves", [(1, "Ann Aguirre")])])
    assert await _row(bid) is None                       # merged into the Calibre row
    assert await _byline(10) == ["Ann Aguirre"]


async def test_the_legacy_heal_keeps_calibres_byline(env, monkeypatch):
    from app.discovery import calibre_sync
    aguirre = await _author("Ann Aguirre", calibre_id=1)
    caine = await _author("Rachel Caine")
    await _sync(monkeypatch, [_book(10, "Honor Among Thieves", [(1, "Ann Aguirre")])])
    bid = await _missing("Honor Among Thieves", [caine, aguirre])
    monkeypatch.setattr(calibre_sync, "_read_calibre_ids", lambda *a, **k: [10])
    from app.discovery import sync_state
    monkeypatch.setattr(sync_state, "resolve_threshold", lambda *a, **k: (1.0, "incremental"))
    monkeypatch.setattr(calibre_sync, "_read_calibre_series_authors", lambda *a, **k: [])
    await _sync(monkeypatch, [])
    assert await _row(bid) is None
    assert await _byline(10) == ["Ann Aguirre"]


# ─── Author hygiene (C10) ────────────────────────────────────


async def test_a_comma_calibre_stores_as_a_pipe_reads_back(env, monkeypatch):
    from app.discovery import calibre_sync
    assert calibre_sync._calibre_name("Harrow| Alix E.") == "Harrow, Alix E."
    assert calibre_sync._calibre_name(None) is None


async def test_the_reader_decodes_the_pipe(env, tmp_path):
    import sqlite3
    from app.discovery import calibre_sync
    p = tmp_path / "metadata.db"
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, pubdate TEXT, series_index REAL,
            path TEXT, last_modified TEXT, has_cover INTEGER DEFAULT 0, isbn TEXT DEFAULT '');
        CREATE TABLE comments (book INTEGER, text TEXT);
        CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT, sort TEXT);
        CREATE TABLE books_authors_link (book INTEGER, author INTEGER);
        CREATE TABLE series (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE books_series_link (book INTEGER, series INTEGER);
        CREATE TABLE identifiers (book INTEGER, type TEXT, val TEXT);
        CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE books_tags_link (book INTEGER, tag INTEGER);
        CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating INTEGER);
        CREATE TABLE books_ratings_link (book INTEGER, rating INTEGER);
        CREATE TABLE languages (id INTEGER PRIMARY KEY, lang_code TEXT);
        CREATE TABLE books_languages_link (book INTEGER, lang_code INTEGER);
        CREATE TABLE publishers (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE books_publishers_link (book INTEGER, publisher INTEGER);
        CREATE TABLE data (book INTEGER, format TEXT, name TEXT);
        INSERT INTO books (id, title, path) VALUES (1, 'The Ten Thousand Doors of January', 'x');
        INSERT INTO authors VALUES (5, 'Harrow| Alix E.', 'Harrow| Alix E.');
        INSERT INTO books_authors_link VALUES (1, 5);
    """)
    c.commit()
    c.close()
    try:
        data = calibre_sync._read_calibre_db(str(p), str(tmp_path))
    except sqlite3.OperationalError as e:          # a column this stub lacks
        pytest.skip(f"stub metadata.db too thin for the reader: {e}")
    assert data["books"][0]["authors"][0]["name"] == "Harrow, Alix E."


async def _link(slug, author_id, person_name):
    from app.database import get_db
    g = await get_db()
    try:
        row = await (await g.execute("SELECT id FROM persons WHERE canonical_name = ?",
                                     (person_name,))).fetchone()
        pid = row[0] if row else (await g.execute(
            "INSERT INTO persons (canonical_name, normalized_name) VALUES (?, ?)",
            (person_name, person_name.lower()))).lastrowid
        await g.execute("INSERT INTO author_links (person_id, library_slug, author_id) VALUES (?,?,?)",
                        (pid, slug, author_id))
        await g.commit()
        return pid
    finally:
        await g.close()


async def _persons():
    from app.database import get_db
    g = await get_db()
    try:
        return {r[0] for r in await (await g.execute("SELECT canonical_name FROM persons")).fetchall()}
    finally:
        await g.close()


async def test_an_author_whose_calibre_author_is_gone_is_retired_with_its_mirror(env, monkeypatch):
    """The ten `Last| First` rows of 2026-09-21: corrected in Calibre under
    new ids, the old rows kept, mirrored empty into the ABS library."""
    stale = await _author("Quinn| Montgomery", calibre_id=1401)
    mirror = await _author("Quinn| Montgomery", slug="audio")
    await _link("test", stale, "Quinn| Montgomery")
    await _link("audio", mirror, "Quinn| Montgomery")
    await _author("Montgomery Quinn", calibre_id=1500)
    await _sync(monkeypatch, [_book(10, "A Book", [(1500, "Montgomery Quinn")])])
    assert await _q("SELECT id FROM authors WHERE name LIKE '%|%'") == []
    assert await _q("SELECT id FROM authors WHERE name LIKE '%|%'", slug="audio") == []
    assert "Quinn| Montgomery" not in await _persons()
    assert await _q("SELECT name FROM authors WHERE calibre_id = 1500") == [("Montgomery Quinn",)]


async def test_a_gone_author_still_listing_books_stays(env, monkeypatch):
    """Harrow's old row (36 missing) and Tom Clancy: wave C's cleanup goes first."""
    clancy = await _author("Tom Clancy", calibre_id=1416)
    await _missing("Red Storm Rising", [clancy])
    await _author("Someone Else", calibre_id=1)
    await _sync(monkeypatch, [_book(10, "A Book", [(1, "Someone Else")])])
    assert await _q("SELECT name FROM authors WHERE id = ?", (clancy,)) == [("Tom Clancy",)]


async def test_an_allow_listed_or_elsewhere_owning_name_stays(env, monkeypatch):
    from app.database import get_db
    picked = await _author("Picked Author", calibre_id=1401)
    gone_here = await _author("Audio Owner", calibre_id=1402)
    abs_row = await _author("Audio Owner", slug="audio")
    await _missing("Audio Book", [abs_row], slug="audio")
    g = await get_db()
    try:
        await g.execute("INSERT INTO authors_allowed (name, normalized, source) "
                        "VALUES ('Picked Author', 'picked author', 'manual')")
        await g.commit()
    finally:
        await g.close()
    await _author("Someone Else", calibre_id=1)
    await _sync(monkeypatch, [_book(10, "A Book", [(1, "Someone Else")])])
    left = {r[0] for r in await _q("SELECT id FROM authors")}
    assert {picked, gone_here} <= left


async def test_an_incremental_sync_retires_nothing(env, monkeypatch):
    from app.discovery import calibre_sync, sync_state
    await _author("Quinn| Montgomery", calibre_id=1401)
    monkeypatch.setattr(sync_state, "resolve_threshold", lambda *a, **k: (1.0, "incremental"))
    monkeypatch.setattr(calibre_sync, "_read_calibre_ids", lambda *a, **k: [10])
    monkeypatch.setattr(calibre_sync, "_read_calibre_series_authors", lambda *a, **k: [])
    await _author("Someone Else", calibre_id=1)
    await _sync(monkeypatch, [_book(10, "A Book", [(1, "Someone Else")])])
    assert await _q("SELECT name FROM authors WHERE name LIKE '%|%'") == [("Quinn| Montgomery",)]


async def test_a_read_with_no_authors_retires_nothing(env):
    from app.discovery import calibre_sync
    from app.discovery.database import get_db
    await _author("Quinn| Montgomery", calibre_id=1401)
    db = await get_db("test")
    try:
        assert await calibre_sync._retire_gone_authors(db, "test", set(), {}) == 0
    finally:
        await db.close()


async def test_a_second_calibre_copy_is_never_absorbed_into_the_first(env, monkeypatch):
    await _author("Jim Butcher", calibre_id=1)
    await _sync(monkeypatch, [
        _book(10, "Blood Rites", [(1, "Jim Butcher")], series="The Dresden Files", index=6.0),
        _book(11, "Blood Rites", [(1, "Jim Butcher")], series="The Dresden Files", index=6.0),
    ])
    assert await _q("SELECT calibre_id FROM books WHERE source = 'calibre' ORDER BY calibre_id") == [
        (10,), (11,)]


async def test_a_signed_copy_listing_is_not_absorbed(env, monkeypatch):
    """G197 on real data: the identity calls the listing the same book; its
    filter family keeps it apart."""
    a = await _author("James S. A. Corey", calibre_id=1)
    bid = await _missing("Leviathan Falls - Carton of 10 Signed Copies", [a])
    await _sync(monkeypatch, [_book(10, "Leviathan Falls", [(1, "James S. A. Corey")],
                                    series="The Expanse", index=9.0)])
    assert await _row(bid) == (0, None, "hardcover")


async def test_a_row_calibres_author_no_longer_maps_to_is_retired(env, monkeypatch):
    """Calibre's author 5 is now read as "Harrow, Alix E." (the decoded
    comma): the old "Harrow| Alix E." row holding id 5 is superseded."""
    old = await _author("Harrow| Alix E.", calibre_id=5)
    await _sync(monkeypatch, [_book(10, "A Book", [(5, "Harrow, Alix E.")])])
    names = [r[0] for r in await _q("SELECT name FROM authors WHERE calibre_id = 5")]
    assert names == ["Harrow, Alix E."]
    assert await _q("SELECT id FROM authors WHERE id = ?", (old,)) == []


async def test_an_abs_author_of_the_same_name_with_its_own_id_stays(env, monkeypatch):
    await _author("Quinn| Montgomery", calibre_id=1401)
    abs_row = await _author("Quinn| Montgomery", slug="audio")
    await _q("UPDATE authors SET audiobookshelf_id = 'abs-1' WHERE id = ?", (abs_row,), slug="audio")
    await _author("Someone Else", calibre_id=1)
    await _sync(monkeypatch, [_book(10, "A Book", [(1, "Someone Else")])])
    assert await _q("SELECT id FROM authors WHERE name LIKE '%|%'") == []
    assert await _q("SELECT id FROM authors WHERE id = ?", (abs_row,), slug="audio") == [(abs_row,)]


async def test_a_gone_author_with_books_and_no_normalized_name_stays(env, monkeypatch):
    old = await _author("Legacy Row", calibre_id=1401)
    await _q("UPDATE authors SET normalized_name = NULL WHERE id = ?", (old,))
    await _missing("A Missing Book", [old])
    await _author("Someone Else", calibre_id=1)
    await _sync(monkeypatch, [_book(10, "A Book", [(1, "Someone Else")])])
    assert await _q("SELECT id FROM authors WHERE id = ?", (old,)) == [(old,)]
