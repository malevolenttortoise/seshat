"""
v2.3.5 push-back endpoints.

Two layers of coverage:

  - **Dispatch tests** stub out the three push-back helpers
    (`push_abs`, `push_calibre_full`, `push_cwa`) so we exercise
    `POST /books/{bid}/push` routing + user_edited_fields clearing
    + bulk verb behavior without standing up real ABS/calibredb/CWA.

  - **Translation tests** exercise the pure helpers
    (`_build_abs_metadata`, `_format_calibredb_value`,
    `_format_cwa_value`) directly — they're side-effect free.

The actual HTTP/subprocess dispatch is covered at integration time
(Mark's UAT against the live container).
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI


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


@pytest.fixture
async def client(discovery_db):
    from app.discovery.routers import metadata as metadata_router

    app = FastAPI()
    app.include_router(metadata_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ── seed helpers ─────────────────────────────────────────────────────


async def _seed_book(
    book_id: int = 1,
    title: str = "My Book",
    description: str | None = None,
    audiobookshelf_id: str | None = None,
    calibre_id: int | None = None,
    user_edited: list[str] | None = None,
    series_id: int | None = None,
    series_name: str | None = None,
):
    from app.discovery.database import get_db
    from app.metadata.author_names import normalize_author_name
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO authors (id, name, sort_name, "
            "normalized_name) VALUES (101, 'A', 'A', ?)",
            (normalize_author_name("A"),),
        )
        if series_name and series_id:
            await db.execute(
                "INSERT OR IGNORE INTO series (id, name, author_id) "
                "VALUES (?, ?, 101)",
                (series_id, series_name),
            )
        await db.execute(
            "INSERT INTO books (id, title, description, "
            "audiobookshelf_id, calibre_id, series_id, source, owned, "
            "user_edited_fields) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                book_id, title, description, audiobookshelf_id, calibre_id,
                series_id,
                "audiobookshelf" if audiobookshelf_id else
                "calibre" if calibre_id else "goodreads",
                1 if (audiobookshelf_id or calibre_id) else 0,
                json.dumps(user_edited or []),
            ),
        )
        await db.execute(
            "INSERT OR IGNORE INTO book_authors (book_id, author_id, position) "
            "VALUES (?, 101, 0)", (book_id,),
        )
        await db.commit()
    finally:
        await db.close()


async def _book_row(book_id: int) -> dict:
    from app.discovery.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT * FROM books WHERE id = ?", (book_id,),
        )).fetchone()
        return dict(row) if row else {}
    finally:
        await db.close()


def _stub_helpers(monkeypatch, *, abs_calls=None, cal_calls=None,
                  cwa_calls=None,
                  cal_unavailable: bool = False,
                  cwa_unavailable: bool = False,
                  abs_fails: bool = False):
    """Replace the three push-back helpers with deterministic stubs.

    Each call list (`abs_calls`/`cal_calls`/`cwa_calls`) gets appended
    `(book_id, fields)` so tests can assert dispatch routed correctly.
    """
    from app.discovery import push_back

    async def _abs(db, book, fields):
        if abs_calls is not None:
            abs_calls.append((book["id"], list(fields)))
        if abs_fails:
            raise push_back.PushFailed("ABS rejected the push")
        return {"applied": list(fields), "failed": []}

    async def _cal(db, book, fields):
        if cal_calls is not None:
            cal_calls.append((book["id"], list(fields)))
        if cal_unavailable:
            raise push_back.PushUnavailable(
                "calibredb not found in this image",
            )
        return {"applied": list(fields), "failed": []}

    async def _cwa(db, book, fields):
        if cwa_calls is not None:
            cwa_calls.append((book["id"], list(fields)))
        if cwa_unavailable:
            raise push_back.PushUnavailable("CWA push not configured")
        return {"applied": list(fields), "failed": []}

    monkeypatch.setattr(push_back, "push_abs", _abs)
    monkeypatch.setattr(push_back, "push_calibre_full", _cal)
    monkeypatch.setattr(push_back, "push_cwa", _cwa)


# ── Dispatch + clearing tests ────────────────────────────────────────


class TestPushDispatch:
    async def test_abs_push_routes_to_abs_helper(self, client, monkeypatch):
        abs_calls: list = []
        _stub_helpers(monkeypatch, abs_calls=abs_calls)
        await _seed_book(audiobookshelf_id="abs-1",
                         user_edited=["description"])
        r = await client.post(
            "/api/discovery/books/1/push",
            json={"source": "abs", "fields": ["description"]},
        )
        assert r.status_code == 200
        assert abs_calls == [(1, ["description"])]

    async def test_calibre_push_prefers_calibredb(self, client, monkeypatch):
        cal_calls: list = []
        cwa_calls: list = []
        _stub_helpers(monkeypatch, cal_calls=cal_calls, cwa_calls=cwa_calls)
        await _seed_book(calibre_id=42, user_edited=["title"])
        r = await client.post(
            "/api/discovery/books/1/push",
            json={"source": "calibre", "fields": ["title"]},
        )
        assert r.status_code == 200
        assert cal_calls == [(1, ["title"])]
        assert cwa_calls == []  # CWA never reached

    async def test_calibre_push_falls_back_to_cwa(self, client, monkeypatch):
        cal_calls: list = []
        cwa_calls: list = []
        _stub_helpers(
            monkeypatch, cal_calls=cal_calls, cwa_calls=cwa_calls,
            cal_unavailable=True,
        )
        await _seed_book(calibre_id=42, user_edited=["title"])
        r = await client.post(
            "/api/discovery/books/1/push",
            json={"source": "calibre", "fields": ["title"]},
        )
        assert r.status_code == 200
        assert cal_calls == [(1, ["title"])]
        assert cwa_calls == [(1, ["title"])]

    async def test_calibre_push_409_when_neither_configured(
        self, client, monkeypatch,
    ):
        _stub_helpers(
            monkeypatch, cal_unavailable=True, cwa_unavailable=True,
        )
        await _seed_book(calibre_id=42, user_edited=["title"])
        r = await client.post(
            "/api/discovery/books/1/push",
            json={"source": "calibre", "fields": ["title"]},
        )
        assert r.status_code == 409
        assert "CWA" in r.json()["detail"]

    async def test_push_502_on_upstream_failure(self, client, monkeypatch):
        _stub_helpers(monkeypatch, abs_fails=True)
        await _seed_book(audiobookshelf_id="abs-1")
        r = await client.post(
            "/api/discovery/books/1/push",
            json={"source": "abs", "fields": ["description"]},
        )
        assert r.status_code == 502

    async def test_push_404_on_unknown_book(self, client, monkeypatch):
        _stub_helpers(monkeypatch)
        r = await client.post(
            "/api/discovery/books/99/push",
            json={"source": "abs", "fields": ["description"]},
        )
        assert r.status_code == 404

    async def test_push_400_on_invalid_source(self, client, monkeypatch):
        _stub_helpers(monkeypatch)
        await _seed_book(audiobookshelf_id="abs-1")
        r = await client.post(
            "/api/discovery/books/1/push",
            json={"source": "wrong", "fields": ["description"]},
        )
        assert r.status_code == 400

    async def test_push_400_on_missing_fields(self, client, monkeypatch):
        _stub_helpers(monkeypatch)
        await _seed_book(audiobookshelf_id="abs-1")
        r = await client.post(
            "/api/discovery/books/1/push",
            json={"source": "abs"},
        )
        assert r.status_code == 400


class TestPushClearsUserEdited:
    async def test_successful_push_clears_field(self, client, monkeypatch):
        _stub_helpers(monkeypatch)
        await _seed_book(
            audiobookshelf_id="abs-1",
            user_edited=["description", "title"],
        )
        await client.post(
            "/api/discovery/books/1/push",
            json={"source": "abs", "fields": ["description"]},
        )
        uef = json.loads((await _book_row(1))["user_edited_fields"])
        assert "description" not in uef
        assert "title" in uef                  # untouched

    async def test_failed_push_leaves_uef_intact(self, client, monkeypatch):
        _stub_helpers(monkeypatch, abs_fails=True)
        await _seed_book(
            audiobookshelf_id="abs-1",
            user_edited=["description"],
        )
        await client.post(
            "/api/discovery/books/1/push",
            json={"source": "abs", "fields": ["description"]},
        )
        uef = json.loads((await _book_row(1))["user_edited_fields"])
        assert "description" in uef


class TestPushBulk:
    async def test_all_user_edited_iterates_intersection(
        self, client, monkeypatch,
    ):
        # `narrator` is ABS-only, `description` is shared, `cover_path`
        # is shared. The bulk filter pushes only fields ABS can write.
        abs_calls: list = []
        _stub_helpers(monkeypatch, abs_calls=abs_calls)
        await _seed_book(
            audiobookshelf_id="abs-1",
            user_edited=["description", "narrator", "cover_path"],
        )
        r = await client.post(
            "/api/discovery/books/1/push",
            json={"source": "abs", "all_user_edited": True},
        )
        assert r.status_code == 200
        # All three are in the abs col_map. The dispatcher passes them
        # through; the helper would filter unsupported ones internally.
        called_with = abs_calls[0][1]
        assert "description" in called_with
        assert "narrator" in called_with
        # cover_path is in the abs col_map too (per COMPARE_FIELDS),
        # though we deferred its push. The dispatcher doesn't know
        # that; that's the helper's responsibility.
        assert "cover_path" in called_with

    async def test_bulk_with_empty_uef_is_noop(self, client, monkeypatch):
        abs_calls: list = []
        _stub_helpers(monkeypatch, abs_calls=abs_calls)
        await _seed_book(audiobookshelf_id="abs-1", user_edited=[])
        r = await client.post(
            "/api/discovery/books/1/push",
            json={"source": "abs", "all_user_edited": True},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] == []
        # Helper never invoked — nothing to push.
        assert abs_calls == []


# ── Translation helper unit tests ───────────────────────────────────


class TestBuildAbsMetadata:
    def test_simple_scalar_fields(self):
        from app.discovery.push_back import _build_abs_metadata
        book = {
            "title": "T", "description": "D",
            "asin": "B0X", "isbn": "9780",
            "language": "eng", "publisher": "Pub",
            "abridged": 1,
            "narrator": "Alice, Bob",
            "pub_date": "2024-05-01",
        }
        md = _build_abs_metadata(
            book,
            ["title", "description", "asin", "isbn", "language",
             "publisher", "abridged", "narrator", "pub_date"],
        )
        assert md["title"] == "T"
        assert md["description"] == "D"
        assert md["narrators"] == ["Alice", "Bob"]
        assert md["abridged"] is True
        assert md["publishedDate"] == "2024-05-01"

    def test_series_collapses(self):
        from app.discovery.push_back import _build_abs_metadata
        md = _build_abs_metadata(
            {"series_name": "Halo", "series_index": 3.0},
            ["series_name", "series_index"],
        )
        assert md["series"] == [{"name": "Halo", "sequence": "3"}]

    def test_series_index_fractional_kept_as_string(self):
        from app.discovery.push_back import _build_abs_metadata
        md = _build_abs_metadata(
            {"series_name": "Foo", "series_index": 1.5},
            ["series_name", "series_index"],
        )
        assert md["series"][0]["sequence"] == "1.5"

    def test_empty_series_clears(self):
        from app.discovery.push_back import _build_abs_metadata
        md = _build_abs_metadata(
            {"series_name": None, "series_index": None},
            ["series_name"],
        )
        assert md["series"] == []

    def test_unknown_field_dropped(self):
        from app.discovery.push_back import _build_abs_metadata
        md = _build_abs_metadata(
            {"title": "T", "tags": "x,y"},
            ["title", "tags"],
        )
        assert "title" in md
        assert "tags" not in md          # tags not in ABS map (yet)


class TestFormatCalibredbValue:
    def test_scalar_string(self):
        from app.discovery.push_back import _format_calibredb_value
        assert _format_calibredb_value("title", "  Hello  ") == "Hello"

    def test_blank_string_returns_none(self):
        from app.discovery.push_back import _format_calibredb_value
        assert _format_calibredb_value("title", "") is None
        assert _format_calibredb_value("title", "   ") is None

    def test_rating_rounds_to_int(self):
        from app.discovery.push_back import _format_calibredb_value
        # books.rating is REAL (e.g. 8.5). Calibre stores int 0-10.
        assert _format_calibredb_value("rating", 8.5) == "8"
        assert _format_calibredb_value("rating", 10) == "10"

    def test_series_index_integral_drops_decimal(self):
        from app.discovery.push_back import _format_calibredb_value
        assert _format_calibredb_value("series_index", 3.0) == "3"
        assert _format_calibredb_value("series_index", 3.5) == "3.5"

    def test_none_returns_none(self):
        from app.discovery.push_back import _format_calibredb_value
        assert _format_calibredb_value("title", None) is None


class TestFormatCwaValue:
    def test_scalar_string_strips(self):
        from app.discovery.push_back import _format_cwa_value
        assert _format_cwa_value("book_title", "  X  ") == "X"

    def test_empty_returns_none(self):
        from app.discovery.push_back import _format_cwa_value
        assert _format_cwa_value("book_title", "") is None

    def test_rating_int_string(self):
        from app.discovery.push_back import _format_cwa_value
        assert _format_cwa_value("rating", 9.5) == "10"


# ─── _parse_cwa_edit_form ──────────────────────────────────────
#
# CWA's `/admin/book/<id>` edit form is the source of truth our
# CWAClient.push needs to mirror. UAT 2026-05-11 found that posting
# a partial form (only changed fields + csrf) returns 200 silently
# without persisting anything — CWA re-renders the form with
# validation errors. The fix: scrape the entire current form, merge
# our changes, POST the complete form, then verify by re-fetching.

# Minimal sample mirroring CWA's actual edit-form structure.
_SAMPLE_CWA_FORM = """
<html><body>
  <form id="login-snippet"><input name="csrf_token" value="login-csrf"/></form>
  <form id="edit-form" action="/admin/book/123" method="post" enctype="multipart/form-data">
    <input type="hidden" name="csrf_token" value="abc-edit-csrf-xyz"/>
    <input type="text" name="title" value="Old Title"/>
    <input type="text" name="authors" value="A. Author"/>
    <input type="text" name="series" value="Old Series"/>
    <input type="text" name="series_index" value="1"/>
    <input type="text" name="pubdate" value="2024-01-01"/>
    <input type="text" name="publisher" value=""/>
    <input type="text" name="cover_url" value=""/>
    <input type="file" name="btn-upload-cover"/>
    <input type="checkbox" name="detail_view" value="on" checked/>
    <input type="checkbox" name="checkA" value="on"/>
    <input type="submit" name="submit" value="Save"/>
    <select name="languages"><option value="eng" selected>English</option><option value="fra">French</option></select>
    <textarea name="comments">&lt;p&gt;Old body&lt;/p&gt;</textarea>
    <input type="text" name="identifier-type-100" value="isbn"/>
    <input type="text" name="identifier-val-100" value="9781234567890"/>
  </form>
</body></html>
"""


class TestParseCwaEditForm:
    def test_returns_csrf_and_field_dict(self):
        from app.discovery.push_back import _parse_cwa_edit_form
        csrf, fields = _parse_cwa_edit_form(_SAMPLE_CWA_FORM)
        # Edit form's csrf — NOT the login snippet's.
        assert csrf == "abc-edit-csrf-xyz"
        assert fields["csrf_token"] == "abc-edit-csrf-xyz"

    def test_extracts_text_inputs(self):
        from app.discovery.push_back import _parse_cwa_edit_form
        _, fields = _parse_cwa_edit_form(_SAMPLE_CWA_FORM)
        assert fields["title"] == "Old Title"
        assert fields["authors"] == "A. Author"
        assert fields["series"] == "Old Series"
        assert fields["series_index"] == "1"
        # Empty-value inputs preserved as empty string (not omitted).
        assert fields["publisher"] == ""

    def test_skips_file_and_submit_inputs(self):
        from app.discovery.push_back import _parse_cwa_edit_form
        _, fields = _parse_cwa_edit_form(_SAMPLE_CWA_FORM)
        # File inputs can't roundtrip as form data — must be skipped.
        assert "btn-upload-cover" not in fields
        # Submit buttons aren't form data either.
        assert "submit" not in fields

    def test_checkbox_only_when_checked(self):
        from app.discovery.push_back import _parse_cwa_edit_form
        _, fields = _parse_cwa_edit_form(_SAMPLE_CWA_FORM)
        # detail_view is checked → included (mirrors browser POST behavior)
        assert fields["detail_view"] == "on"
        # checkA is unchecked → omitted
        assert "checkA" not in fields

    def test_select_takes_selected_option(self):
        from app.discovery.push_back import _parse_cwa_edit_form
        _, fields = _parse_cwa_edit_form(_SAMPLE_CWA_FORM)
        assert fields["languages"] == "eng"

    def test_textarea_preserves_inner_html(self):
        from app.discovery.push_back import _parse_cwa_edit_form
        _, fields = _parse_cwa_edit_form(_SAMPLE_CWA_FORM)
        # CWA's `comments` field carries rich HTML — must roundtrip
        # verbatim (BeautifulSoup unescapes entities to literal markup).
        assert fields["comments"] == "<p>Old body</p>"

    def test_identifier_pairs_carry_through(self):
        from app.discovery.push_back import _parse_cwa_edit_form
        _, fields = _parse_cwa_edit_form(_SAMPLE_CWA_FORM)
        # Existing identifiers (encoded with their DB row IDs) must
        # carry through unchanged so the POST doesn't accidentally
        # drop them.
        assert fields["identifier-type-100"] == "isbn"
        assert fields["identifier-val-100"] == "9781234567890"

    def test_raises_when_no_form_found(self):
        from app.discovery.push_back import _parse_cwa_edit_form, PushFailed
        # No form at all → can't locate edit form → PushFailed.
        with pytest.raises(PushFailed, match="could not locate"):
            _parse_cwa_edit_form("<html><body><p>404</p></body></html>")

    def test_raises_when_no_csrf_in_form(self):
        from app.discovery.push_back import _parse_cwa_edit_form, PushFailed
        # Form has title input but no csrf_token — fails to identify
        # the edit form (since the discriminator requires both).
        html = """
        <html><body>
          <form><input name="title" value="X"/></form>
        </body></html>
        """
        with pytest.raises(PushFailed, match="could not locate"):
            _parse_cwa_edit_form(html)


# ─── CWAClient.push three-phase flow ───────────────────────────


class _StubResponse:
    """Minimal httpx-shaped response stub for monkeypatching."""

    def __init__(self, status_code: int, text: str, cookies: dict | None = None):
        self.status_code = status_code
        self.text = text
        self.cookies = cookies or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _StubHttpClient:
    """Httpx AsyncClient stand-in. Records calls; serves canned responses
    keyed by (method, url-substring) FIFO. Only mirrors the surface
    CWAClient actually uses (`get`, `post`, `cookies`)."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.cookies = {"session": "logged-in"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def get(self, url, **_kw):
        self.calls.append(("GET", url, None))
        return self._consume("GET", url)

    async def post(self, url, *, data=None, headers=None, **_kw):
        self.calls.append(("POST", url, data))
        return self._consume("POST", url)

    def _consume(self, method, url):
        for i, (m, u_part, resp) in enumerate(self._responses):
            if m == method and u_part in url:
                return self._responses.pop(i)[2]
        raise AssertionError(f"unexpected {method} {url} (no canned response)")


def _form_html_for(values, csrf="csrf-x"):
    """Build a CWA-shaped edit form with the given values."""
    extras = "\n".join(
        f'<input type="text" name="{k}" value="{v}"/>'
        for k, v in values.items() if k not in ("csrf_token", "title")
    )
    return f"""
    <html><body>
      <form>
        <input type="hidden" name="csrf_token" value="{csrf}"/>
        <input type="text" name="title" value="{values.get("title", "T")}"/>
        {extras}
      </form>
    </body></html>
    """


class TestCWAClientPush:
    @pytest.mark.asyncio
    async def test_merges_partial_into_full_form_and_verifies(self, monkeypatch):
        """Happy path: GET full form, POST merged payload, GET verifies."""
        from app.discovery import push_back

        responses = [
            ("GET", "/login", _StubResponse(200, _form_html_for(
                {"title": "Old Title", "series": "", "series_index": "1"}, csrf="login-csrf",
            ))),
            ("POST", "/login", _StubResponse(200, "ok", cookies={"session": "x"})),
            ("GET", "/admin/book/55", _StubResponse(200, _form_html_for(
                {"title": "Old Title", "series": "", "series_index": "1"}, csrf="edit-csrf",
            ))),
            ("POST", "/admin/book/55", _StubResponse(200, "ok")),
            # After save, refetched form shows new series (success).
            ("GET", "/admin/book/55", _StubResponse(200, _form_html_for(
                {"title": "Old Title", "series": "New Series", "series_index": "1"},
                csrf="edit-csrf-2",
            ))),
        ]
        stub = _StubHttpClient(responses)
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: stub)

        client = push_back.CWAClient("http://cwa.local", "u", "p")
        await client.push(55, {"series": "New Series"})

        post_body = next(
            d for (m, u, d) in stub.calls
            if m == "POST" and "/admin/book/55" in u
        )
        # Our pushed value wins over scraped state.
        assert post_body["series"] == "New Series"
        # Scraped state carries through (full-form replacement).
        assert post_body["title"] == "Old Title"
        # detail_view forced to "on" so CWA processes as full edit.
        assert post_body["detail_view"] == "on"
        # checkA/checkT explicitly disabled.
        assert post_body["checkA"] == "false"
        assert post_body["checkT"] == "false"
        # csrf comes from the scraped EDIT form, not the caller.
        assert post_body["csrf_token"] == "edit-csrf"

    @pytest.mark.asyncio
    async def test_raises_when_field_doesnt_persist(self, monkeypatch):
        """Verification phase catches CWA's silent-failure mode."""
        from app.discovery import push_back

        responses = [
            ("GET", "/login", _StubResponse(200, _form_html_for({"title": "T"}))),
            ("POST", "/login", _StubResponse(200, "ok", cookies={"session": "x"})),
            ("GET", "/admin/book/77", _StubResponse(200, _form_html_for(
                {"title": "T", "series": ""}, csrf="csrf1",
            ))),
            ("POST", "/admin/book/77", _StubResponse(200, "ok")),
            # Phase 3 — series STILL empty. CWA silently rejected (UAT canary).
            ("GET", "/admin/book/77", _StubResponse(200, _form_html_for(
                {"title": "T", "series": ""}, csrf="csrf2",
            ))),
        ]
        stub = _StubHttpClient(responses)
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: stub)

        client = push_back.CWAClient("http://cwa.local", "u", "p")
        with pytest.raises(push_back.PushFailed, match="did not.*persist"):
            await client.push(77, {"series": "Should Have Persisted"})

    @pytest.mark.asyncio
    async def test_raises_on_4xx_post(self, monkeypatch):
        """HTTP-level failure still raises (existing behavior preserved)."""
        from app.discovery import push_back

        responses = [
            ("GET", "/login", _StubResponse(200, _form_html_for({"title": "T"}))),
            ("POST", "/login", _StubResponse(200, "ok", cookies={"session": "x"})),
            ("GET", "/admin/book/99", _StubResponse(200, _form_html_for(
                {"title": "T"}, csrf="c1",
            ))),
            ("POST", "/admin/book/99", _StubResponse(500, "server error")),
        ]
        stub = _StubHttpClient(responses)
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: stub)

        client = push_back.CWAClient("http://cwa.local", "u", "p")
        with pytest.raises(push_back.PushFailed, match="HTTP 500"):
            await client.push(99, {"series": "X"})


class TestCWAClientDelete:
    """v2.27.0 Phase 5b — `POST /delete/<book_id>` against CWA's admin
    route. CSRF scraped from the edit page (or /login fallback);
    success verified by re-fetching the edit page and expecting it
    to no longer render (200 → still there → fail)."""

    @pytest.mark.asyncio
    async def test_happy_path_deletes_and_verifies(self, monkeypatch):
        from app.discovery import push_back

        responses = [
            # _login
            ("GET", "/login", _StubResponse(200, _form_html_for(
                {"title": "T"}, csrf="login-csrf",
            ))),
            ("POST", "/login", _StubResponse(200, "ok", cookies={"session": "x"})),
            # CSRF scrape from edit page
            ("GET", "/admin/book/42", _StubResponse(200, _form_html_for(
                {"title": "Doomed"}, csrf="edit-csrf",
            ))),
            # The actual delete
            ("POST", "/delete/42", _StubResponse(200, '{"success":true}')),
            # Verification re-fetch — non-200 means the book is gone.
            ("GET", "/admin/book/42", _StubResponse(404, "not found")),
        ]
        stub = _StubHttpClient(responses)
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: stub)

        client = push_back.CWAClient("http://cwa.local", "u", "p")
        await client.delete(42)

        # CSRF came from the edit page, not /login.
        post_body = next(
            d for (m, u, d) in stub.calls
            if m == "POST" and "/delete/42" in u
        )
        assert post_body["csrf_token"] == "edit-csrf"

    @pytest.mark.asyncio
    async def test_falls_back_to_login_csrf_when_edit_page_missing(self, monkeypatch):
        """If the edit page returns non-200 (book already deleted by a
        prior partial enact attempt), we still want a CSRF token —
        scrape from /login. This is the idempotency-on-retry path."""
        from app.discovery import push_back

        responses = [
            ("GET", "/login", _StubResponse(200, _form_html_for(
                {"title": "T"}, csrf="initial-login-csrf",
            ))),
            ("POST", "/login", _StubResponse(200, "ok", cookies={"session": "x"})),
            # Edit page already 404 (book gone from a prior attempt).
            ("GET", "/admin/book/42", _StubResponse(404, "not found")),
            # Fallback CSRF scrape from /login.
            ("GET", "/login", _StubResponse(200, _form_html_for(
                {"title": "T"}, csrf="fallback-csrf",
            ))),
            ("POST", "/delete/42", _StubResponse(200, '{"success":true}')),
            ("GET", "/admin/book/42", _StubResponse(404, "still gone")),
        ]
        stub = _StubHttpClient(responses)
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: stub)

        client = push_back.CWAClient("http://cwa.local", "u", "p")
        await client.delete(42)

        post_body = next(
            d for (m, u, d) in stub.calls
            if m == "POST" and "/delete/42" in u
        )
        assert post_body["csrf_token"] == "fallback-csrf"

    @pytest.mark.asyncio
    async def test_treats_302_redirect_as_success(self, monkeypatch):
        """Real-CWA behavior caught in dev-stack UAT 2026-05-24:
        /admin/book/<missing-id> returns 302 → /. With follow_redirects
        enabled, httpx silently follows the redirect and returns 200
        from the home page — which the naive verify-by-200 check
        misreads as "book still there." The fix uses
        follow_redirects=False on the verify GET so a 3xx-redirect-away
        surfaces directly and is recognized as success (the book is
        actually gone)."""
        from app.discovery import push_back

        responses = [
            ("GET", "/login", _StubResponse(200, _form_html_for(
                {"title": "T"}, csrf="login-csrf",
            ))),
            ("POST", "/login", _StubResponse(200, "ok", cookies={"session": "x"})),
            ("GET", "/admin/book/42", _StubResponse(200, _form_html_for(
                {"title": "Doomed"}, csrf="edit-csrf",
            ))),
            ("POST", "/delete/42", _StubResponse(200, '{"success":true}')),
            # Real CWA's behavior after delete: 302 to home (not 404).
            ("GET", "/admin/book/42", _StubResponse(302, "")),
        ]
        stub = _StubHttpClient(responses)
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: stub)

        client = push_back.CWAClient("http://cwa.local", "u", "p")
        # Must NOT raise — the 302 means the book is gone.
        await client.delete(42)

    @pytest.mark.asyncio
    async def test_raises_when_book_still_renders_after_delete(self, monkeypatch):
        """CWA can return 200 on /delete while silently rejecting it
        (CSRF mismatch, permission, etc.). Re-fetching the edit page
        and seeing 200 = the book is still there = the delete didn't
        take. Surface as PushFailed so the orchestrator can rollback
        the soft-delete and audit-log the failure."""
        from app.discovery import push_back

        responses = [
            ("GET", "/login", _StubResponse(200, _form_html_for({"title": "T"}))),
            ("POST", "/login", _StubResponse(200, "ok", cookies={"session": "x"})),
            ("GET", "/admin/book/42", _StubResponse(200, _form_html_for(
                {"title": "Still Here"}, csrf="c1",
            ))),
            ("POST", "/delete/42", _StubResponse(200, '{"success":false}')),
            # Verification — book still renders. Silent rejection.
            ("GET", "/admin/book/42", _StubResponse(200, _form_html_for(
                {"title": "Still Here"}, csrf="c2",
            ))),
        ]
        stub = _StubHttpClient(responses)
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: stub)

        client = push_back.CWAClient("http://cwa.local", "u", "p")
        with pytest.raises(push_back.PushFailed, match="did not remove"):
            await client.delete(42)

    @pytest.mark.asyncio
    async def test_raises_on_4xx_delete(self, monkeypatch):
        from app.discovery import push_back

        responses = [
            ("GET", "/login", _StubResponse(200, _form_html_for({"title": "T"}))),
            ("POST", "/login", _StubResponse(200, "ok", cookies={"session": "x"})),
            ("GET", "/admin/book/42", _StubResponse(200, _form_html_for(
                {"title": "T"}, csrf="c1",
            ))),
            ("POST", "/delete/42", _StubResponse(500, "server error")),
        ]
        stub = _StubHttpClient(responses)
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: stub)

        client = push_back.CWAClient("http://cwa.local", "u", "p")
        with pytest.raises(push_back.PushFailed, match="HTTP 500"):
            await client.delete(42)

    @pytest.mark.asyncio
    async def test_raises_when_no_csrf_obtainable(self, monkeypatch):
        from app.discovery import push_back

        responses = [
            # _login still needs a CSRF (which the login form provides).
            ("GET", "/login", _StubResponse(200, _form_html_for(
                {"title": "T"}, csrf="login-csrf",
            ))),
            ("POST", "/login", _StubResponse(200, "ok", cookies={"session": "x"})),
            # Edit page broken — no CSRF token in HTML.
            ("GET", "/admin/book/42", _StubResponse(200, "<html></html>")),
            # Fallback /login probe also missing CSRF.
            ("GET", "/login", _StubResponse(200, "<html></html>")),
        ]
        stub = _StubHttpClient(responses)
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: stub)

        client = push_back.CWAClient("http://cwa.local", "u", "p")
        with pytest.raises(push_back.PushFailed, match="could not extract CSRF"):
            await client.delete(42)
