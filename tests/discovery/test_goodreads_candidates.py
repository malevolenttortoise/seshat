"""Goodreads candidate worker + detail store (2026-10 audit wave 4b, S4,
ADR-0026; G85–G103).

Every Goodreads request here goes through the real `GoodreadsSession`
(backoff, source gate) with a fake curl underneath, so "no request" is
asserted on what reached the wire."""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from app import config as app_config
from app import state
from app.discovery import (
    goodreads_candidates as gc,
    goodreads_filters as gf,
    goodreads_store,
    metadata_cache,
)
from app.discovery.database import set_active_library
from app.metadata import goodreads_session
from app.discovery import lookup as _lookup

_ORIGINAL_MERGE = _lookup.merge_goodreads_book

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "goodreads"
NEUROPATH = (FIXTURES / "book_page_neuropath.html").read_text()
NFL = (FIXTURES / "book_page_nfl_guide_no_genres.html").read_text()

GR_AUTHOR = "555"


def _with_genres(page: str, genres: list[str]) -> str:
    """The fixture page with its `__NEXT_DATA__` genres replaced."""
    import re as _re
    m = _re.search(r'(<script id="__NEXT_DATA__" type="application/json">)(.*?)(</script>)', page, _re.S)
    nd = json.loads(m.group(2))
    for k, v in nd["props"]["pageProps"]["apolloState"].items():
        if k.startswith("Book:"):
            v["bookGenres"] = [{"genre": {"name": g}} for g in genres]
    return page[:m.start(2)] + json.dumps(nd) + page[m.end(2):]


def test_with_genres_rewrites_the_fixture():
    from app.discovery.sources.goodreads import parse_book_page
    assert parse_book_page(_with_genres(NEUROPATH, ["Nonfiction", "Memoir"]))["genres"] == [
        "Nonfiction", "Memoir"]


# ─── Fakes ───────────────────────────────────────────────────


class FakeResp:
    def __init__(self, status: int, text: str = "", data=None):
        self.status_code = status
        self.text = text if data is None else json.dumps(data)
        self.content = self.text.encode()
        self.headers = {}
        self.url = ""

    def json(self):
        return json.loads(self.text)


class FakeGoodreads:
    """What goodreads.com answers, per request kind; records every URL."""

    def __init__(self):
        self.urls: list[str] = []
        self.autocomplete: dict[str, list] = {}   # query → hits
        self.pages: dict[str, FakeResp] = {}       # book id → response
        self.block_autocomplete = False

    async def get(self, url, headers=None, **kw):
        self.urls.append(url)
        if "/book/auto_complete" in url:
            if self.block_autocomplete:
                return FakeResp(202, "x")
            q = unquote(parse_qs(urlparse(url).query)["q"][0])
            return FakeResp(200, data=self.autocomplete.get(q, []))
        if "/book/show/" in url:
            bid = url.rsplit("/", 1)[1]
            return self.pages.get(bid, FakeResp(404, "not found"))
        return FakeResp(404, "")

    def kinds(self):
        return [
            "ac" if "/auto_complete" in u else "page" if "/book/show/" in u else "other"
            for u in self.urls
        ]


def _hit(book_id, title, *, author=GR_AUTHOR, pages=300, snippet="", name="Ann Author"):
    return {
        "bookId": str(book_id), "workId": "w" + str(book_id), "title": title,
        "bookTitleBare": title, "numPages": pages, "ratingsCount": 3,
        "avgRating": "4.1", "author": {"id": int(author), "name": name},
        "description": {"html": snippet, "truncated": True},
        "kcrPreviewUrl": "https://read.amazon.com/kp/embed?asin=B0TESTASIN&x=1",
    }


@pytest.fixture
async def env(tmp_path, monkeypatch):
    from app.discovery import database as disco_db
    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(metadata_cache, "DATA_DIR", tmp_path)
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "SETTINGS_PATH", tmp_path / "settings.json")
    app_config._settings_cache["data"] = None
    app_config._settings_cache["mtime"] = None
    app_config.save_settings({
        "metadata_sources": {"goodreads": {"rate_limit": 0.0}},
        "metadata_cache": {"goodreads": {"enabled": True, "mode": "continuous"}},
    })
    monkeypatch.setattr(state, "_discovered_libraries", [
        {"slug": "books-lib", "name": "Books", "content_type": "ebook"},
        {"slug": "audio-lib", "name": "Audio", "content_type": "audiobook"},
    ])
    monkeypatch.setattr(state, "_source_scan_refs", 0)
    monkeypatch.setattr(state, "_library_sync_in_progress", False)
    monkeypatch.setattr(state, "_hygiene_task", None)
    monkeypatch.setattr(gc, "_phase2_seeded_at", 0.0)
    await metadata_cache.init_db(metadata_cache.SOURCE_GOODREADS)
    await disco_db.init_db("books-lib")
    await disco_db.init_db("audio-lib")
    prev = disco_db.get_active_library()
    set_active_library("books-lib")

    fake = FakeGoodreads()
    goodreads_session.reset_session_for_tests()
    monkeypatch.setattr(goodreads_session.GoodreadsSession, "_get_curl", lambda self: fake)
    merged: list = []

    async def _merge(author_id, slug, gr_author_id, book):
        merged.append((author_id, slug, gr_author_id, book))
        return 1, 0

    from app.discovery import lookup
    monkeypatch.setattr(lookup, "merge_goodreads_book", _merge)
    yield {"gr": fake, "merged": merged, "tmp": tmp_path}
    goodreads_session.reset_session_for_tests()
    set_active_library(prev)


def _set(**gr_source):
    s = dict(app_config.load_settings())
    ms = dict(s.get("metadata_sources") or {})
    ms["goodreads"] = {**(ms.get("goodreads") or {}), **gr_source}
    s["metadata_sources"] = ms
    app_config.save_settings(s)


def _phase2(on: bool):
    s = dict(app_config.load_settings())
    mc = dict(s.get("metadata_cache") or {})
    mc["goodreads"] = {**(mc.get("goodreads") or {}), "phase2_enabled": on}
    s["metadata_cache"] = mc
    app_config.save_settings(s)


async def _author(*, gr=GR_AUTHOR, name="Ann Author", scanned_at=None, slug="books-lib",
                  books=()):
    """A discovery author (+ books) holding Goodreads author ID `gr`."""
    from app.discovery.database import get_db
    db = await get_db(slug=slug)
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, normalized_name, goodreads_id, "
            "last_lookup_at) VALUES (?,?,?,?,?)",
            (name, name, name.lower(), gr, scanned_at),
        )
        aid = cur.lastrowid
        for title, gr_book in books:
            cur = await db.execute(
                "INSERT INTO books (title, goodreads_id, owned, source) "
                "VALUES (?,?,1,'calibre')", (title, gr_book),
            )
            await db.execute(
                "INSERT INTO book_authors (book_id, author_id, position) VALUES (?,?,0)",
                (cur.lastrowid, aid),
            )
        await db.commit()
        return aid
    finally:
        await db.close()


async def _list(records, *, gr=GR_AUTHOR, slug="books-lib"):
    db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
    try:
        await db.execute(
            f"INSERT OR REPLACE INTO {metadata_cache.state_table('goodreads')} "
            f"(author_id, library_slug, last_scanned_at, last_outcome, book_count) "
            f"VALUES (?,?,?,?,?)", (gr, slug, time.time(), "ok", len(records)),
        )
        await db.execute(
            f"DELETE FROM {metadata_cache.list_pages_table('goodreads')} "
            f"WHERE author_id = ? AND library_slug = ?", (gr, slug),
        )
        await db.execute(
            f"INSERT INTO {metadata_cache.list_pages_table('goodreads')} "
            f"(author_id, library_slug, page_num, fetched_at, book_ids_json) "
            f"VALUES (?,?,?,?,?)", (gr, slug, 1, time.time(), json.dumps(records)),
        )
        await db.commit()
    finally:
        await db.close()


def _rec(book_id, title, **kw):
    return {"book_id": str(book_id), "title": title, "list_series": None,
            "list_series_idx": None, "list_cover": None, "is_audio_list": False, **kw}


async def _rows(gr=GR_AUTHOR, slug="books-lib"):
    db = await metadata_cache.get_db(metadata_cache.SOURCE_GOODREADS)
    try:
        cur = await db.execute(
            f"SELECT book_id, state, reason, origin, attempts FROM "
            f"{metadata_cache.candidates_table('goodreads')} "
            f"WHERE author_id = ? AND library_slug = ? ORDER BY position", (gr, slug),
        )
        return {r[0]: dict(r) for r in await cur.fetchall()}
    finally:
        await db.close()


# ─── Filters (pure) ──────────────────────────────────────────


@pytest.mark.parametrize("title,reason", [
    ("Księga 2", None),
    ("ゼロ", "non_latin"),
    ("86 T06 (French Edition)", "edition_marker"),
    ("The Children of Aegis (Die Kinder von Aegis) German Edition: Ein Roman", "edition_marker"),
    ("ZERO (1996) ISBN: 4087030504 [Japanese Import]", "edition_marker"),
    ("Bob's Book of Killer Crosswords", "non_book_title"),
    ("My Journal...: It's all about me!", "non_book_title"),
    ("A Mostly Accurate Guide to Magic, Mayhem & Other Poor Decisions", None),
    ("The Kindle Edition Saga (Special Edition)", None),
])
def test_title_filters(title, reason):
    assert gf.title_skip_reason(title, include_nonfiction=False) == reason


def test_non_book_titles_pass_while_nonfiction_is_allowed():
    assert gf.title_skip_reason("Killer Crosswords", include_nonfiction=True) is None


@pytest.mark.parametrize("snippet,lang", [
    ("Wesoła Banda Piratów ocaliła świat już kilkukrotnie, teraz jednak musi "
     "pogodzić się z tym, co nieuniknione. Ziemię czeka zagłada.", "pl"),
    ("Pour freiner la progression de la Légion, la Fédération envoie, pour une "
     "mission conjointe, la 86e Force de frappe mobile au royaume uni.", "fr"),
    ("Tom's life is not what it once was. His marriage to the beautiful Nora is "
     "on the rocks and he now sees his two young children only on her say-so.", "en"),
    ("Short one.", None),
])
def test_snippet_language(snippet, lang):
    assert gf.snippet_language(snippet) == lang


def test_a_hit_with_zero_pages_is_rejected():
    assert gf.hit_reject_reason("Zero", 0, "", include_nonfiction=False) == "zero_pages"
    assert gf.hit_reject_reason("Unknown pages", None, "", include_nonfiction=False) is None


@pytest.mark.parametrize("genres,hit", [
    (["Nonfiction", "Sports"], "Nonfiction"),
    (["Historical Fiction", "Nonfiction", "Fiction"], None),
    (["Sports", "Football"], "Sports"),
    (["Romance", "Sports", "Contemporary"], None),
    (["Science Fiction", "Psychology", "Fiction"], None),
    ([], None),
])
def test_nonfiction_genres(genres, hit):
    assert gf.nonfiction_genre(genres) == hit


def test_page_filters():
    ok = {"language": "English", "genres": ["Fantasy"]}
    assert gf.page_reject_reason(ok, include_nonfiction=False) is None
    assert gf.page_reject_reason({"language": "Polish"}, include_nonfiction=False) == "language:polish"
    assert gf.page_reject_reason({"language": "English", "is_audiobook": True},
                                 include_nonfiction=False) == "audiobook"
    assert gf.page_reject_reason({"language": "English", "is_set": True},
                                 include_nonfiction=False) == "set"
    nf = {"language": "English", "genres": ["Nonfiction", "Self Help"]}
    assert gf.page_reject_reason(nf, include_nonfiction=False) == "nonfiction_genre:Nonfiction"
    assert gf.page_reject_reason(nf, include_nonfiction=True) is None


# ─── The book-page parser (G99 fixtures) ─────────────────────


def test_parse_book_page_reads_next_data():
    from app.discovery.sources.goodreads import parse_book_page
    d = parse_book_page(NEUROPATH, book_id="3170310")
    assert d["loaded"] is True
    assert d["language"] == "English"
    assert d["genres"][:3] == ["Science Fiction", "Horror", "Fiction"]
    assert d["book_format"] == "Mass Market Paperback"
    assert d["isbn13"] == "9780752882796"
    assert d["page_count"] == 375
    assert [c.name for c in d["contributors"]] == ["R. Scott Bakker"]


def test_a_book_few_people_shelved_has_no_genres():
    from app.discovery.sources.goodreads import parse_book_page
    d = parse_book_page(NFL, book_id="25244619")
    assert d["genres"] == []
    assert d["asin"] == "B00VD6XNQI"
    assert d["book_format"] == "Kindle Edition"


# ─── Known titles ────────────────────────────────────────────


def test_a_series_prefix_title_is_known_by_its_tail():
    from app.discovery.sources.goodreads import known_title_keys
    keys = known_title_keys(["Calamity", "Magic's Toll: Fatebound"])
    assert gc.title_is_known("Knights of Eternity : Calamity", keys)
    assert not gc.title_is_known("Magic's Toll: Cursebound", keys)


# ─── Seeding (G86 / G94) ─────────────────────────────────────


async def test_a_never_scanned_author_gets_no_candidates(env):
    await _author(scanned_at=None)
    await _list([_rec(1, "One")])
    assert await gc.refresh_author(GR_AUTHOR, "books-lib") is None
    assert await _rows() == {}


async def test_an_audiobook_library_gets_no_candidates(env):
    await _author(scanned_at=time.time(), slug="audio-lib")
    await _list([_rec(1, "One")], slug="audio-lib")
    assert await gc.refresh_author(GR_AUTHOR, "audio-lib") is None


async def test_first_fill_classifies_every_entry(env):
    await _author(scanned_at=time.time() - 30 * 86400,
                  books=[("Owned Book", "10"), ("Calamity", None)])
    await _list([
        _rec(10, "Owned Book Retitled"),                 # known by Goodreads ID
        _rec(11, "Knights of Eternity : Calamity"),      # known by title tail
        _rec(12, "Box Set Books 1-3"),                   # set title
        _rec(13, "Audio One", is_audio_list=True),       # audio
        _rec(14, "Ein Roman (German Edition)"),          # edition marker
        _rec(15, "New Book"),                            # candidate
    ])
    counts = await gc.refresh_author(GR_AUTHOR, "books-lib")
    rows = await _rows()
    assert {b: (r["state"], r["reason"]) for b, r in rows.items()} == {
        "10": ("known", None), "11": ("known", None),
        "12": ("skipped", "set_title"), "13": ("skipped", "audio"),
        "14": ("skipped", "edition_marker"), "15": ("pending", None),
    }
    assert rows["15"]["origin"] == "first_fill"
    assert counts["pending"] == 1


async def test_a_big_catalogue_is_baseline_then_its_new_ids_are_candidates(env):
    await _author(scanned_at=time.time())
    big = [_rec(i, f"Book {i}") for i in range(1, 102)]          # 101 listed
    await _list(big)
    await gc.refresh_author(GR_AUTHOR, "books-lib")
    assert {r["state"] for r in (await _rows()).values()} == {"baseline"}
    await _list(big + [_rec(500, "Brand New Release")])          # weekly refresh
    await gc.refresh_author(GR_AUTHOR, "books-lib")
    row = (await _rows())["500"]
    assert (row["state"], row["origin"]) == ("pending", "weekly")


async def test_undecided_rows_another_source_created_become_known(env):
    aid = await _author(scanned_at=time.time())
    await _list([_rec(1, "Will Appear")])
    await gc.refresh_author(GR_AUTHOR, "books-lib")
    from app.discovery.database import get_db
    db = await get_db(slug="books-lib")
    try:
        cur = await db.execute("INSERT INTO books (title) VALUES ('Will Appear')")
        await db.execute("INSERT INTO book_authors (book_id, author_id, position) VALUES (?,?,0)",
                         (cur.lastrowid, aid))
        await db.commit()
    finally:
        await db.close()
    await gc.refresh_author(GR_AUTHOR, "books-lib")
    assert (await _rows())["1"]["state"] == "known"


async def test_seed_new_authors_seeds_only_scanned_ones(env):
    await _author(gr="1", name="Scanned", scanned_at=time.time())
    await _author(gr="2", name="Never", scanned_at=None)
    await _list([_rec(1, "A")], gr="1")
    await _list([_rec(2, "B")], gr="2")
    assert await gc.seed_new_authors() == 1
    assert await _rows(gr="1") and not await _rows(gr="2")


# ─── Steps (G95 / G97) ───────────────────────────────────────


async def _seeded(env, records=None, **author_kw):
    await _author(scanned_at=time.time() - 30 * 86400, **author_kw)
    await _list(records or [_rec(100, "New Book")])
    await gc.refresh_author(GR_AUTHOR, "books-lib")


async def test_an_autocomplete_match_waits_for_its_page(env):
    await _seeded(env)
    env["gr"].autocomplete["New Book"] = [_hit(100, "New Book", snippet="The story of a man who was not what he was.")]
    res = await gc.step()
    assert res.outcome == "autocomplete"
    assert (await _rows())["100"]["state"] == "awaiting_page"
    stored = await goodreads_store.get_book("100")
    assert stored["asin"] == "B0TESTASIN" and stored["ac_num_pages"] == 300


async def test_no_match_retries_once_with_the_author_then_rejects(env):
    await _seeded(env)
    env["gr"].autocomplete["New Book"] = [_hit(9, "New Book", author="999")]
    res = await gc.step()
    queries = [unquote(parse_qs(urlparse(u).query)["q"][0]) for u in env["gr"].urls]
    assert queries == ["New Book", "New Book Ann Author"]
    assert (await _rows())["100"]["reason"] == "no_autocomplete_match"
    assert res.outcome == "autocomplete"


async def test_the_retry_with_the_author_can_confirm(env):
    await _seeded(env)
    env["gr"].autocomplete["New Book Ann Author"] = [_hit(100, "New Book")]
    await gc.step()
    assert (await _rows())["100"]["state"] == "awaiting_page"


async def test_a_foreign_snippet_rejects_before_the_page(env):
    await _seeded(env)
    env["gr"].autocomplete["New Book"] = [_hit(100, "New Book", snippet=(
        "Wesoła Banda Piratów ocaliła świat już kilkukrotnie, teraz jednak musi "
        "pogodzić się z tym, co nieuniknione."))]
    await gc.step()
    assert (await _rows())["100"]["reason"] == "snippet_language:pl"
    assert "page" not in env["gr"].kinds()


async def test_a_blocked_autocomplete_leaves_the_candidate_alone(env):
    await _seeded(env)
    env["gr"].block_autocomplete = True
    res = await gc.step()
    assert res.outcome == "blocked"
    row = (await _rows())["100"]
    assert (row["state"], row["attempts"]) == ("pending", 0)
    # Backing off now: the next step sends nothing.
    n = len(env["gr"].urls)
    res = await gc.step()
    assert len(env["gr"].urls) == n
    assert res.outcome == "idle" and res.retry_in_s > 0


async def _awaiting(env, book_id="100", page=None):
    await _seeded(env, [_rec(book_id, "Neuropath")])
    env["gr"].autocomplete["Neuropath"] = [_hit(book_id, "Neuropath")]
    await gc.step()
    if page is not None:
        env["gr"].pages[book_id] = page


async def test_a_page_that_passes_is_created_at_once(env):
    await _awaiting(env, page=FakeResp(200, NEUROPATH))
    res = await gc.step()
    assert res.outcome == "page"
    row = (await _rows())["100"]
    assert (row["state"], row["reason"]) == ("created", "new")
    (author_id, slug, gr_author, book), = env["merged"]
    assert (slug, gr_author, book.external_id, book.source) == ("books-lib", GR_AUTHOR, "100", "goodreads")
    assert book.language == "English" and book.isbn == "9780752882796"
    assert (await goodreads_store.get_book("100"))["page_fetched_at"]


async def test_a_nonfiction_page_is_rejected_unless_allowed(env):
    nf_page = _with_genres(NEUROPATH, ["Nonfiction", "Memoir"])
    await _awaiting(env, page=FakeResp(200, nf_page))
    await gc.step()
    assert (await _rows())["100"]["reason"] == "nonfiction_genre:Nonfiction"
    assert env["merged"] == []


async def test_nonfiction_passes_when_the_option_is_on(env):
    _set(include_nonfiction=True)
    nf_page = _with_genres(NEUROPATH, ["Nonfiction", "Memoir"])
    await _awaiting(env, page=FakeResp(200, nf_page))
    await gc.step()
    assert (await _rows())["100"]["state"] == "created"


async def test_a_blocked_page_waits(env):
    await _awaiting(env, page=FakeResp(202, "x"))
    res = await gc.step()
    row = (await _rows())["100"]
    assert (row["state"], row["attempts"]) == ("awaiting_page", 0)
    assert res.outcome == "blocked"
    assert goodreads_session.is_backing_off("book_page")


async def test_a_gone_page_is_rejected(env):
    await _awaiting(env, page=FakeResp(404, "gone"))
    await gc.step()
    assert (await _rows())["100"]["reason"] == "page_404"


async def test_while_the_page_gap_runs_autocomplete_carries_on(env, monkeypatch):
    await _seeded(env, [_rec(100, "Neuropath"), _rec(101, "Second")])
    env["gr"].autocomplete["Neuropath"] = [_hit(100, "Neuropath")]
    env["gr"].autocomplete["Second"] = [_hit(101, "Second")]
    await gc.step()                                    # 100 → awaiting_page
    from app.metadata import source_gate
    monkeypatch.setattr(source_gate, "kind_wait_seconds", lambda s, k: 90.0)
    env["gr"].urls.clear()
    res = await gc.step()
    assert env["gr"].kinds() == ["ac"]                 # no page during the gap
    assert (await _rows())["101"]["state"] == "awaiting_page"
    res = await gc.step()                              # nothing but the page left
    assert res.outcome == "idle" and res.retry_in_s == pytest.approx(90.0)


async def test_the_merge_waits_while_a_scan_runs(env, monkeypatch):
    await _awaiting(env, page=FakeResp(200, NEUROPATH))
    monkeypatch.setattr(state, "_source_scan_refs", 1)
    await gc.step()
    assert (await _rows())["100"]["state"] == "accepted"
    assert env["merged"] == []
    monkeypatch.setattr(state, "_source_scan_refs", 0)
    env["gr"].urls.clear()
    res = await gc.step()
    assert res.outcome == "merged" and env["gr"].urls == []    # from the store
    assert (await _rows())["100"]["state"] == "created"


async def test_a_candidate_a_scan_created_meanwhile_costs_no_request(env):
    aid = await _author(scanned_at=time.time() - 30 * 86400)
    await _list([_rec(100, "Late Arrival")])
    await gc.refresh_author(GR_AUTHOR, "books-lib")
    from app.discovery.database import get_db
    db = await get_db(slug="books-lib")
    try:
        cur = await db.execute("INSERT INTO books (title) VALUES ('Late Arrival')")
        await db.execute("INSERT INTO book_authors (book_id, author_id, position) VALUES (?,?,0)",
                         (cur.lastrowid, aid))
        await db.commit()
    finally:
        await db.close()
    await gc.step()
    assert env["gr"].urls == []
    assert (await _rows())["100"]["state"] == "known"


# ─── Order (G73 / G75 / G86) ─────────────────────────────────


async def test_order_bumped_then_weekly_then_smallest_first(env):
    now = time.time()
    await _author(gr="1", name="Big", scanned_at=now - 30 * 86400)
    await _author(gr="2", name="Small", scanned_at=now - 30 * 86400)
    await _author(gr="3", name="Bumped", scanned_at=now - 30 * 86400)
    await _list([_rec(10, "Big One"), _rec(11, "Big Two"), _rec(12, "Big Three")], gr="1")
    await _list([_rec(20, "Small One")], gr="2")
    await _list([_rec(30, "Bumped One"), _rec(31, "Bumped Two")], gr="3")
    for g in ("1", "2", "3"):
        await gc.refresh_author(g, "books-lib", now=now)
    # A weekly new ID for the big author, and a scan covering author 3.
    await _list([_rec(10, "Big One"), _rec(11, "Big Two"), _rec(12, "Big Three"),
                 _rec(13, "Big New")], gr="1")
    await gc.refresh_author("1", "books-lib", now=now)
    await gc.refresh_author("3", "books-lib", now=now, scanned_now=True)
    order = []
    db = await metadata_cache.get_db("goodreads")
    try:
        for _ in range(7):
            row = await gc._next_candidate(db, "pending")
            order.append(row["book_id"])
            await db.execute(
                f"UPDATE {metadata_cache.candidates_table('goodreads')} SET state='rejected' "
                f"WHERE book_id = ?", (row["book_id"],),
            )
            await db.commit()
    finally:
        await db.close()
    assert order == ["30", "31", "13", "20", "10", "11", "12"]


async def test_the_author_last_worked_on_comes_first(env):
    now = time.time()
    await _author(gr="1", name="First", scanned_at=now - 30 * 86400)
    await _author(gr="2", name="Second", scanned_at=now - 30 * 86400)
    await _list([_rec(20, "S1")], gr="2")
    await _list([_rec(10, "F1"), _rec(11, "F2"), _rec(12, "F3")], gr="1")
    for g in ("1", "2"):
        await gc.refresh_author(g, "books-lib", now=now)
    db = await metadata_cache.get_db("goodreads")
    try:
        row = await gc._next_candidate(db, "pending")
    finally:
        await db.close()
    assert row["book_id"] == "20"                     # smallest catalogue first
    await gc._set_state({**row, "book_id": "10", "author_id": "1"}, "rejected")
    db = await metadata_cache.get_db("goodreads")
    try:
        row = await gc._next_candidate(db, "pending")
    finally:
        await db.close()
    assert row["book_id"] == "11"                     # the interrupted author first


# ─── Phase 2 (G88) ───────────────────────────────────────────


async def test_phase2_does_nothing_while_off(env):
    await _list([_rec(1, "One")])
    assert await gc.phase2_step() is None
    assert env["gr"].urls == []


async def test_phase2_fetches_smallest_catalogue_first_and_skips_stored_pages(env):
    _phase2(True)
    await _list([_rec(1, "One"), _rec(2, "Two")], gr="7")
    await _list([_rec(3, "Solo")], gr="8")
    await goodreads_store.save_page("3", NEUROPATH)   # already stored
    env["gr"].pages["1"] = FakeResp(200, NEUROPATH)
    res = await gc.phase2_step()
    assert env["gr"].urls[-1].endswith("/book/show/1")
    assert res.outcome == "phase2"
    status = await gc.status()
    assert status["phase2"]["enabled"] is True and status["phase2"]["fetched"] == 1


async def test_phase2_waits_for_candidates(env):
    _phase2(True)
    _set(book_page_gap=0)                               # no 2-min wait here
    await _seeded(env)                                  # one pending candidate
    await _list([_rec(7, "Phase Two Book")], gr="7")
    env["gr"].autocomplete["New Book"] = [_hit(100, "New Book")]
    await gc.step()
    assert env["gr"].kinds() == ["ac"]                  # candidate first
    env["gr"].pages["100"] = FakeResp(200, NEUROPATH)
    await gc.step()
    assert env["gr"].urls[-1].endswith("/book/show/100")
    env["gr"].pages["7"] = FakeResp(200, NEUROPATH)
    await gc.step()                                     # nothing else: phase 2
    assert env["gr"].urls[-1].endswith("/book/show/7")


# ─── Status (G73 rule 5) ─────────────────────────────────────


async def test_status_counts_the_first_fill(env):
    await _seeded(env, [_rec(100, "A"), _rec(101, "B")])
    env["gr"].autocomplete["A"] = [_hit(9, "A", author="999")]
    await gc.step()
    s = await gc.status()
    assert s["first_fill"]["candidates"] == 2
    assert s["first_fill"]["decided"] == 1
    assert s["first_fill"]["authors_left"] == 1
    assert s["rejected_by_reason"] == {"no_autocomplete_match": 1}


async def test_a_scan_puts_the_author_first(env):
    aid = await _author(scanned_at=time.time() - 30 * 86400)
    await _list([_rec(1, "One")])
    await gc.refresh_author(GR_AUTHOR, "books-lib")
    await gc.note_author_scanned(aid, "books-lib")
    db = await metadata_cache.get_db("goodreads")
    try:
        cur = await db.execute(
            f"SELECT scan_bumped_at FROM {metadata_cache.candidate_authors_table('goodreads')}"
        )
        (bumped,), = await cur.fetchall()
    finally:
        await db.close()
    assert bumped and time.time() - bumped < 60


async def test_status_says_when_the_next_page_may_go(env, monkeypatch):
    from app.metadata import source_gate
    monkeypatch.setattr(source_gate, "kind_wait_seconds", lambda s, k: 50.0)
    s = await gc.status()
    assert s["book_page_next_at"] == pytest.approx(time.time() + 50.0, abs=5)


# ─── The worker's merge (real `_merge_result`) ───────────────


async def test_merge_goodreads_book_creates_a_goodreads_row(env, monkeypatch):
    from app.discovery import lookup
    from app.discovery.sources.base import BookResult, Contributor
    monkeypatch.setattr(lookup, "merge_goodreads_book", _ORIGINAL_MERGE)
    aid = await _author(scanned_at=time.time(), name="Ann Author")
    book = BookResult(
        title="Brand New Indie", external_id="4242", source="goodreads",
        source_url="https://www.goodreads.com/book/show/4242", language="English",
        pub_date="2025-01-01", contributors=[Contributor(name="Ann Author", source_author_id=GR_AUTHOR)],
    )
    created, updated = await lookup.merge_goodreads_book(aid, "books-lib", GR_AUTHOR, book)
    assert (created, updated) == (1, 0)
    from app.discovery.database import get_db
    db = await get_db(slug="books-lib")
    try:
        cur = await db.execute(
            "SELECT b.source, b.goodreads_id, b.owned FROM books b JOIN book_authors ba "
            "ON ba.book_id = b.id WHERE ba.author_id = ? AND b.title = 'Brand New Indie'", (aid,),
        )
        rows = await cur.fetchall()
    finally:
        await db.close()
    assert [tuple(r) for r in rows] == [("goodreads", "4242", 0)]
    # A second merge of the same book creates nothing.
    assert (await lookup.merge_goodreads_book(aid, "books-lib", GR_AUTHOR, book))[0] == 0


async def test_merge_goodreads_book_respects_library_only_mode(env, monkeypatch):
    from app.discovery import lookup
    from app.discovery.sources.base import BookResult
    monkeypatch.setattr(lookup, "merge_goodreads_book", _ORIGINAL_MERGE)
    s = dict(app_config.load_settings()); s["author_scan_owned_only"] = True
    app_config.save_settings(s)
    aid = await _author(scanned_at=time.time())
    book = BookResult(title="X", external_id="1", source="goodreads")
    assert await lookup.merge_goodreads_book(aid, "books-lib", GR_AUTHOR, book) == (0, 0)


# ─── Scans while the worker is on: cache only (G-C / G103) ───


async def test_a_cache_only_scan_sends_nothing_and_leaves_new_books(env):
    from app.discovery.sources.goodreads import GoodreadsSource, known_title_keys
    await goodreads_store.save_page("10", NEUROPATH)
    src = GoodreadsSource()
    src._known_titles = known_title_keys(["Known Book"])
    res = await src.get_author_books(
        GR_AUTHOR, existing_titles=set(), owned_titles=["Known Book"],
        cached_raw_books=[_rec(10, "Known Book"), _rec(11, "Brand New")],
        cache_only=True,
    )
    assert env["gr"].urls == []
    titles = [b.title for b in res.books] + [b.title for s in res.series for b in s.books]
    assert titles == ["Known Book"]
    known = (res.books + [b for s in res.series for b in s.books])[0]
    assert known.page_count == 375 and known.language == "English"   # stored page


async def test_a_live_scan_skips_nonfiction_too(env, monkeypatch):
    from app.discovery.sources import goodreads as gr_mod
    src = gr_mod.GoodreadsSource()

    async def fake_details(book_id, title):
        d = gr_mod._empty_book_details()
        d.update(loaded=True, language="English", genres=["Nonfiction", "Sports"])
        return d

    monkeypatch.setattr(src, "_get_book_details", fake_details)
    res = await src.get_author_books(
        GR_AUTHOR, existing_titles=set(), owned_titles=[],
        cached_raw_books=[_rec(1, "Paul's Guide to the Draft")],
    )
    assert res.books == [] and res.series == []
    _set(include_nonfiction=True)
    res = await src.get_author_books(
        GR_AUTHOR, existing_titles=set(), owned_titles=[],
        cached_raw_books=[_rec(1, "Paul's Guide to the Draft")],
    )
    assert [b.title for b in res.books] == ["Paul's Guide to the Draft"]


# ─── Enrichment and the store (G93) ──────────────────────────


async def test_enrichment_reads_a_stored_page_instead_of_fetching(env):
    from app.metadata.sources.goodreads import _fetch_and_parse_book
    await goodreads_store.save_page("3170310", NEUROPATH)
    rec = await _fetch_and_parse_book("3170310", title="Neuropath", author="R. Scott Bakker")
    assert env["gr"].urls == []
    assert rec.page_count == 375 and rec.language == "English"
    assert rec.title == "Neuropath"


async def test_a_stale_or_unreleased_page_is_fetched_again(env):
    await goodreads_store.save_page("3170310", NEUROPATH, now=time.time() - 91 * 86400)
    assert await goodreads_store.stored_record("3170310") is None


async def test_enrichment_stores_the_pages_it_loads(env):
    from app.metadata.sources.goodreads import _fetch_and_parse_book
    env["gr"].pages["3170310"] = FakeResp(200, NEUROPATH)
    rec = await _fetch_and_parse_book("3170310", title="Neuropath", author="")
    assert rec is not None and env["gr"].kinds() == ["page"]
    assert (await goodreads_store.get_book("3170310"))["page_fetched_at"]


# ─── The worker tick ─────────────────────────────────────────


async def test_the_tick_runs_a_candidate_step_when_no_list_is_due(env, monkeypatch):
    from app.discovery import metadata_cache_worker as w
    monkeypatch.setattr(w, "_gr_candidates_seeded_at", 0.0)
    await _seeded(env)
    env["gr"].autocomplete["New Book"] = [_hit(100, "New Book")]
    res = await w.tick_goodreads()
    assert res.outcome == "candidate_autocomplete"
    assert (await _rows())["100"]["state"] == "awaiting_page"


async def test_the_tick_is_queue_empty_with_nothing_to_do(env, monkeypatch):
    from app.discovery import metadata_cache_worker as w
    monkeypatch.setattr(w, "_gr_candidates_seeded_at", 0.0)
    res = await w.tick_goodreads()
    assert res.outcome == "queue_empty"
    assert env["gr"].urls == []


async def test_phase2_skips_a_page_stored_after_its_seed(env):
    """A page a candidate or a grab stored after phase 2 seeded its list
    isn't fetched again."""
    _phase2(True)
    await _list([_rec(3, "Solo")], gr="8")
    await gc._seed_phase2(time.time())                 # 3 is pending in phase 2
    await goodreads_store.save_page("3", NEUROPATH)    # then stored elsewhere
    res = await gc.phase2_step()
    assert env["gr"].urls == []
    assert res.detail.endswith("fetched")
