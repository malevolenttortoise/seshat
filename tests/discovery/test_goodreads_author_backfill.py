"""
Tests for `app.discovery.goodreads_author_backfill` — the v2.13.0
reverse-lookup that resolves an author's goodreads_id from one of
their books.

Scope:
  - `_parse_author_id_from_html` — JSON-LD path + anchor fallback +
    empty cases
  - `_pick_seed_book` — ranking order across owned/unowned + each
    identifier kind
  - `_persist_author_goodreads_id` — idempotent writes
  - `resolve_author_goodreads_id` — full flow end-to-end against a
    stubbed `goodreads_session.get_session()`
  - `backfill_missing_author_ids` — selects only authors that need
    resolution + aborts early on soft-block
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture
async def discovery_db(tmp_path, monkeypatch):
    """Per-test discovery DB with the full schema initialized.

    Also patches SETTINGS_PATH (since the goodreads_session module
    reads/writes the runtime-state flag through settings.json) so
    state writes don't leak.
    """
    from app import config as app_config
    from app.discovery import database as disco_db

    monkeypatch.setattr(app_config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_config, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(disco_db, "DATA_DIR", tmp_path)
    disco_db.set_active_library("test")
    await disco_db.init_db("test")
    # Reset cached settings + goodreads_session singleton.
    app_config._settings_cache["data"] = None
    app_config._settings_cache["mtime"] = object()
    import app.metadata.goodreads_session as gs
    gs.reset_session_for_tests()
    yield tmp_path
    disco_db.set_active_library(None)


async def _insert_author(name: str, *, goodreads_id: str = "") -> int:
    from app.discovery.database import get_db
    from app.metadata.author_names import normalize_author_name
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO authors (name, sort_name, normalized_name, goodreads_id) "
            "VALUES (?, ?, ?, ?)",
            (name, name, normalize_author_name(name), goodreads_id or None),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def _insert_book(
    title: str, author_id: int, *,
    goodreads_id: str = "", isbn: str = "", asin: str = "",
    amazon_id: str = "", owned: int = 1, hidden: int = 0,
) -> int:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO books (title, source, owned, hidden, "
            "goodreads_id, isbn, asin, amazon_id) "
            "VALUES (?, 'calibre', ?, ?, ?, ?, ?, ?)",
            (title, owned, hidden,
             goodreads_id or None, isbn or None,
             asin or None, amazon_id or None),
        )
        book_id = cur.lastrowid
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) VALUES (?, ?, 0)",
            (book_id, author_id),
        )
        await db.commit()
        return book_id
    finally:
        await db.close()


class TestParseAuthorIdFromHTML:
    """The HTML parser must extract /author/show/{id} from JSON-LD or
    fall back to anchor hrefs. Empty / malformed → None."""

    def test_json_ld_url_field(self):
        from app.discovery.goodreads_author_backfill import _parse_author_id_from_html
        html = """
        <html><head>
        <script type="application/ld+json">
        {"@type":"Book","name":"Mistborn",
         "author":[{"@type":"Person","name":"Brandon Sanderson",
                    "url":"https://www.goodreads.com/author/show/38550.Brandon_Sanderson"}]}
        </script>
        </head></html>
        """
        assert _parse_author_id_from_html(html) == "38550"

    def test_json_ld_sameAs_field(self):
        from app.discovery.goodreads_author_backfill import _parse_author_id_from_html
        html = """
        <script type="application/ld+json">
        {"author":{"@type":"Person",
                   "sameAs":"https://www.goodreads.com/author/show/12345"}}
        </script>
        """
        assert _parse_author_id_from_html(html) == "12345"

    def test_anchor_fallback_when_no_jsonld(self):
        from app.discovery.goodreads_author_backfill import _parse_author_id_from_html
        html = """
        <a href="/author/show/99999.Some_Author">Some Author</a>
        """
        assert _parse_author_id_from_html(html) == "99999"

    def test_no_author_info_returns_none(self):
        from app.discovery.goodreads_author_backfill import _parse_author_id_from_html
        html = "<html><body>nothing here</body></html>"
        assert _parse_author_id_from_html(html) is None

    def test_empty_returns_none(self):
        from app.discovery.goodreads_author_backfill import _parse_author_id_from_html
        assert _parse_author_id_from_html("") is None
        assert _parse_author_id_from_html(None) is None  # type: ignore[arg-type]

    def test_malformed_jsonld_falls_back_to_anchor(self):
        from app.discovery.goodreads_author_backfill import _parse_author_id_from_html
        html = """
        <script type="application/ld+json">{not valid json}</script>
        <a href="/author/show/777">link</a>
        """
        assert _parse_author_id_from_html(html) == "777"


class TestPickSeedBook:
    """Ranking: owned+goodreads_id beats everything else; owned+isbn
    beats owned+asin; any+goodreads_id beats any+isbn; books with no
    resolvable identifier return None."""

    async def test_owned_goodreads_id_wins(self, discovery_db):
        from app.discovery.goodreads_author_backfill import _pick_seed_book
        author_id = await _insert_author("Test")
        await _insert_book("A", author_id, isbn="9780000000001", owned=1)
        await _insert_book("B", author_id, goodreads_id="888", owned=1)
        await _insert_book("C", author_id, asin="B07XYZ", owned=1)

        book = await _pick_seed_book(author_id)
        assert book is not None
        assert book["title"] == "B"
        assert book["goodreads_id"] == "888"

    async def test_owned_isbn_beats_unowned_goodreads(self, discovery_db):
        from app.discovery.goodreads_author_backfill import _pick_seed_book
        author_id = await _insert_author("Test")
        await _insert_book("Owned-ISBN", author_id, isbn="9780000000001", owned=1)
        await _insert_book("Unowned-GR", author_id, goodreads_id="999", owned=0)

        book = await _pick_seed_book(author_id)
        assert book["title"] == "Owned-ISBN"

    async def test_no_resolvable_books_returns_none(self, discovery_db):
        from app.discovery.goodreads_author_backfill import _pick_seed_book
        author_id = await _insert_author("Test")
        # Book with no identifiers at all.
        await _insert_book("Naked", author_id, owned=1)
        assert await _pick_seed_book(author_id) is None

    async def test_hidden_books_excluded(self, discovery_db):
        from app.discovery.goodreads_author_backfill import _pick_seed_book
        author_id = await _insert_author("Test")
        await _insert_book("Hidden", author_id, goodreads_id="999",
                           owned=1, hidden=1)
        # Hidden book is the only one — seed picker should return None.
        assert await _pick_seed_book(author_id) is None


class TestPersist:
    async def test_writes_and_is_idempotent(self, discovery_db):
        from app.discovery.goodreads_author_backfill import (
            _persist_author_goodreads_id,
        )
        from app.discovery.database import get_db

        author_id = await _insert_author("Test")
        await _persist_author_goodreads_id(author_id, "12345")
        db = await get_db()
        try:
            row = await (await db.execute(
                "SELECT goodreads_id FROM authors WHERE id = ?",
                (author_id,),
            )).fetchone()
        finally:
            await db.close()
        assert row[0] == "12345"

        # Second call with same value — should be a no-op write.
        await _persist_author_goodreads_id(author_id, "12345")
        # Different value should overwrite.
        await _persist_author_goodreads_id(author_id, "67890")
        db = await get_db()
        try:
            row = await (await db.execute(
                "SELECT goodreads_id FROM authors WHERE id = ?",
                (author_id,),
            )).fetchone()
        finally:
            await db.close()
        assert row[0] == "67890"


class TestResolveAuthorGoodreadsId:
    """End-to-end with a stubbed `goodreads_session.get_session()`."""

    async def _stub_session(self, monkeypatch, *, html: str, status: int = 200):
        """Patch goodreads_session.get_session to return a stub that
        delivers canned responses without real HTTP."""
        from app.metadata import goodreads_session as gs

        class StubSession:
            calls: list[str] = []

            async def get(self, url, **kwargs):
                self.__class__.calls.append(url)
                body = html.encode("utf-8")
                return SimpleNamespace(
                    status_code=status, content=body,
                    text=html,
                )

        async def _get_session(rate_limit=None):
            return StubSession()

        monkeypatch.setattr(gs, "get_session", _get_session)
        return StubSession

    async def test_direct_goodreads_id_path(self, discovery_db, monkeypatch):
        """Book has stored goodreads_id → /book/show fetched directly,
        author parsed, persisted."""
        from app.discovery.goodreads_author_backfill import (
            resolve_author_goodreads_id,
        )
        html = """
        <script type="application/ld+json">
        {"author":{"@type":"Person",
                   "url":"https://www.goodreads.com/author/show/55555"}}
        </script>
        """
        stub = await self._stub_session(monkeypatch, html=html)

        author_id = await _insert_author("Test")
        await _insert_book("Seed", author_id, goodreads_id="42")

        resolved = await resolve_author_goodreads_id(author_id)

        assert resolved == "55555"
        # Verify exactly ONE /book/show fetch happened, with book_id=42.
        assert len(stub.calls) == 1
        assert "/book/show/42" in stub.calls[0]

    async def test_isbn_resolver_path(self, discovery_db, monkeypatch):
        """Book has only ISBN → resolver chain converts to goodreads_book_id
        → /book/show fetched → author parsed."""
        from app.discovery.goodreads_author_backfill import (
            resolve_author_goodreads_id,
        )
        # Patch the resolver to return a fixed result so we don't make
        # real HTTP from the resolver itself.
        import app.discovery.goodreads_author_backfill as backfill_mod
        from app.metadata.goodreads_id_resolver import ResolveResult

        async def fake_resolve(q):
            return ResolveResult(
                goodreads_book_id="777", tier="auto_complete", soft_blocked=False,
            )
        monkeypatch.setattr(backfill_mod, "resolve_goodreads_id", fake_resolve)

        html = """
        <a href="/author/show/12321.Some_Author">Some Author</a>
        """
        stub = await self._stub_session(monkeypatch, html=html)

        author_id = await _insert_author("Test")
        await _insert_book("Seed", author_id, isbn="9780000000001")

        resolved = await resolve_author_goodreads_id(author_id)
        assert resolved == "12321"
        assert "/book/show/777" in stub.calls[0]

    async def test_no_resolvable_book_returns_none(self, discovery_db, monkeypatch):
        """Author has only books with no identifiers → graceful None."""
        from app.discovery.goodreads_author_backfill import (
            resolve_author_goodreads_id,
        )
        stub = await self._stub_session(monkeypatch, html="")
        author_id = await _insert_author("Test")
        await _insert_book("Naked", author_id)  # no IDs at all

        resolved = await resolve_author_goodreads_id(author_id)
        assert resolved is None
        assert stub.calls == []  # no HTTP fired

    async def test_soft_block_response_returns_none(self, discovery_db, monkeypatch):
        """/book/show returns 202 → return None, don't persist."""
        from app.discovery.goodreads_author_backfill import (
            resolve_author_goodreads_id,
        )
        # 202 with empty body → soft-block detection.
        await self._stub_session(monkeypatch, html="", status=202)

        author_id = await _insert_author("Test")
        await _insert_book("Seed", author_id, goodreads_id="42")

        resolved = await resolve_author_goodreads_id(author_id)
        assert resolved is None


class TestBackfillSweep:
    async def test_only_picks_unfilled_authors_with_resolvable_books(
        self, discovery_db, monkeypatch,
    ):
        """The sweep query excludes:
          - Authors already having goodreads_id
          - Authors with NO resolvable books
        """
        from app.discovery.goodreads_author_backfill import (
            backfill_missing_author_ids,
        )
        # Author A — already has goodreads_id, skip.
        a = await _insert_author("Already-Filled", goodreads_id="111")
        await _insert_book("X", a, isbn="9780000000001")

        # Author B — missing goodreads_id, has resolvable book → INCLUDE.
        b = await _insert_author("Needs-Resolution")
        await _insert_book("Y", b, goodreads_id="222")

        # Author C — missing goodreads_id, no resolvable books → SKIP.
        c = await _insert_author("No-Books")
        await _insert_book("Z", c)  # no IDs

        # Patch both backfill paths to no-op (we're validating the
        # Phase-1 candidate selection here; Phase 2 has its own test).
        called_phase1: list[int] = []
        called_phase2: list[int] = []
        import app.discovery.goodreads_author_backfill as backfill_mod

        async def fake_p1(aid):
            called_phase1.append(aid)
            return None

        async def fake_p2(aid, name):
            called_phase2.append(aid)
            return None

        monkeypatch.setattr(
            backfill_mod, "resolve_author_goodreads_id", fake_p1,
        )
        monkeypatch.setattr(
            backfill_mod, "resolve_author_via_calibre_coauthor", fake_p2,
        )

        await backfill_missing_author_ids()
        # Phase 1 considers only authors with resolvable book rows.
        assert called_phase1 == [b]
        # Phase 2 considers EVERY remaining author missing goodreads_id
        # (it doesn't care about Seshat's books table — it queries
        # Calibre directly).
        assert set(called_phase2) == {b, c}

    async def test_phase2_resolves_via_calibre_coauthor(
        self, discovery_db, monkeypatch, tmp_path,
    ):
        """The Phase-2 sweep should fire for authors with NO Seshat
        books-table rows when they have a Calibre book."""
        from app.discovery.goodreads_author_backfill import (
            backfill_missing_author_ids,
        )
        # Author exists in Seshat but has zero books rowed to them
        # (the co-author case).
        a = await _insert_author("Co-Author")
        # No _insert_book call — exactly the empty-books-table scenario.

        # Build a minimal Calibre metadata.db at a known path.
        cal_db = tmp_path / "metadata.db"
        conn = __import__("sqlite3").connect(str(cal_db))
        conn.executescript("""
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE books (id INTEGER PRIMARY KEY);
            CREATE TABLE books_authors_link (book INTEGER, author INTEGER);
            CREATE TABLE identifiers (book INTEGER, type TEXT, val TEXT);
            INSERT INTO authors (id, name) VALUES (1, 'Co-Author');
            INSERT INTO books (id) VALUES (42);
            INSERT INTO books_authors_link VALUES (42, 1);
            INSERT INTO identifiers VALUES (42, 'goodreads', '12345');
        """)
        conn.commit()
        conn.close()

        # Point CALIBRE_DB_PATH at our fake DB.
        from app import config as app_config
        monkeypatch.setattr(app_config, "CALIBRE_DB_PATH", str(cal_db))
        import app.discovery.goodreads_author_backfill as backfill_mod
        monkeypatch.setattr(backfill_mod, "CALIBRE_DB_PATH", str(cal_db))

        # Stub /book/show to return a JSON-LD with matching author.
        from app.metadata import goodreads_session as gs

        calls: list[str] = []

        class StubSession:
            async def get(self, url, **kwargs):
                calls.append(url)
                html = """
                <script type="application/ld+json">
                {"author":[{"@type":"Person","name":"Primary Author",
                            "url":"https://www.goodreads.com/author/show/111"},
                           {"@type":"Person","name":"Co-Author",
                            "url":"https://www.goodreads.com/author/show/222"}]}
                </script>
                """
                return SimpleNamespace(
                    status_code=200, content=html.encode("utf-8"), text=html,
                )

        stub_instance = StubSession()

        async def _get_session(rate_limit=None):
            return stub_instance
        monkeypatch.setattr(gs, "get_session", _get_session)

        stats = await backfill_missing_author_ids()
        assert stats["phase2_resolved"] == 1
        # And the author's goodreads_id is the CO-AUTHOR's, not the
        # primary's — name-matching worked.
        from app.discovery.database import get_db
        db = await get_db()
        try:
            row = await (await db.execute(
                "SELECT goodreads_id FROM authors WHERE id = ?", (a,),
            )).fetchone()
        finally:
            await db.close()
        assert row[0] == "222"
        # And we hit the right book. Calibre stores the GOODREADS-side
        # book id in identifiers.val, so the URL uses 12345 (not the
        # local Calibre book row id 42).
        assert any("/book/show/12345" in u for u in calls), \
            f"expected /book/show/12345 in calls, got {calls}"

    async def test_phase2_skips_when_no_calibre_book_for_author(
        self, discovery_db, monkeypatch, tmp_path,
    ):
        """If Calibre doesn't have the author at all, Phase 2 returns
        None without firing any HTTP."""
        from app.discovery.goodreads_author_backfill import (
            backfill_missing_author_ids,
        )
        await _insert_author("Nobody")

        cal_db = tmp_path / "metadata.db"
        conn = __import__("sqlite3").connect(str(cal_db))
        conn.executescript("""
            CREATE TABLE authors (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE books (id INTEGER PRIMARY KEY);
            CREATE TABLE books_authors_link (book INTEGER, author INTEGER);
            CREATE TABLE identifiers (book INTEGER, type TEXT, val TEXT);
        """)
        conn.commit()
        conn.close()

        from app import config as app_config
        monkeypatch.setattr(app_config, "CALIBRE_DB_PATH", str(cal_db))
        import app.discovery.goodreads_author_backfill as backfill_mod
        monkeypatch.setattr(backfill_mod, "CALIBRE_DB_PATH", str(cal_db))

        # Track that the bypass was NOT called.
        from app.metadata import goodreads_session as gs

        class TrackingSession:
            calls: list[str] = []

            async def get(self, url, **kwargs):
                self.__class__.calls.append(url)
                return SimpleNamespace(status_code=200, content=b"", text="")

        async def _get_session(rate_limit=None):
            return TrackingSession()
        monkeypatch.setattr(gs, "get_session", _get_session)

        stats = await backfill_missing_author_ids()
        # Phase 2 considered the author but didn't find a Calibre book.
        assert stats["phase2_resolved"] == 0
        # No HTTP fired.
        assert TrackingSession.calls == []

    async def test_aborts_on_soft_block(self, discovery_db, monkeypatch):
        """If the session flips to soft_blocked mid-sweep, the rest
        of the authors are deferred to next sync."""
        from app.discovery.goodreads_author_backfill import (
            backfill_missing_author_ids,
        )
        from app.metadata import goodreads_session as gs
        # Two authors needing resolution.
        a1 = await _insert_author("A1")
        await _insert_book("X", a1, goodreads_id="1")
        a2 = await _insert_author("A2")
        await _insert_book("Y", a2, goodreads_id="2")

        # First call to resolve flips state; second iteration must
        # bail before doing anything.
        import app.discovery.goodreads_author_backfill as backfill_mod

        async def fake_resolve(aid):
            if aid == a1:
                gs.mark_soft_blocked(last_status=202)
                return None
            return "should-not-be-called"
        monkeypatch.setattr(
            backfill_mod, "resolve_author_goodreads_id", fake_resolve,
        )

        stats = await backfill_missing_author_ids()
        assert stats["considered"] == 1
        assert stats["skipped_soft_blocked"] >= 1
