"""Goodreads scan safety (2026-10 audit wave 4 S3): a scan stops
requesting book pages once they back off (G51); a new book whose page
didn't load is skipped, never created with a default "English" (G82);
"(Series #N)" / "(Series Book N)" list titles parse (live: "Griffin
Academy 2 (Knights Of War #2)" was stored with its suffix)."""
import httpx
import pytest
from bs4 import BeautifulSoup

from app.discovery.sources import goodreads as gr_source
from app.metadata import goodreads_session as gr


class _Curl:
    def __init__(self, status_for):
        self.status_for = status_for
        self.urls: list[str] = []

    async def get(self, url, **_):
        self.urls.append(url)
        status = self.status_for(url)
        body = b"" if status == 202 else (
            b'<html><script type="application/ld+json">'
            b'{"inLanguage": "English", "bookFormat": "Kindle Edition"}'
            b"</script></html>"
        )
        return httpx.Response(status, content=body, request=httpx.Request("GET", url))


@pytest.fixture
def session(monkeypatch):
    s = gr.GoodreadsSession()
    s._curl = _Curl(lambda url: 200)

    async def get_session():
        return s
    monkeypatch.setattr(gr, "get_session", get_session)
    return s


def _rb(title, book_id):
    return {"title": title, "book_id": book_id, "list_series": None,
            "list_series_idx": None, "list_cover": None, "is_audio_list": False}


def _titles(result):
    return {b.title for b in result.books} | {
        b.title for s in result.series for b in s.books
    }


async def test_a_scan_stops_requesting_book_pages_at_the_first_block(session, monkeypatch):
    session._curl.status_for = lambda url: 202 if url.endswith("/2") else 200
    src = gr_source.GoodreadsSource(rate_limit=0)
    detail_calls: list[str] = []
    real_details = src._get_book_details

    async def spy(book_id, title):
        detail_calls.append(book_id)
        return await real_details(book_id, title)
    monkeypatch.setattr(src, "_get_book_details", spy)
    result = await src.get_author_books(
        "123",
        existing_titles={"known one", "known two"},
        cached_raw_books=[
            _rb("Known One", "1"), _rb("New A", "2"),
            _rb("New B", "3"), _rb("New C", "5"), _rb("Known Two", "4"),
        ],
    )
    # One book page went out; it was blocked; nothing after it, not even
    # an attempt the session would refuse.
    assert [u.rsplit("/", 1)[1] for u in session._curl.urls] == ["2"]
    assert detail_calls == ["2"]
    # Known books still take their list-page data; no new book is made.
    assert _titles(result) == {"Known One", "Known Two"}


async def test_a_new_book_whose_page_failed_is_skipped(session):
    session._curl.status_for = lambda url: 500 if url.endswith("/2") else 200
    src = gr_source.GoodreadsSource(rate_limit=0)
    result = await src.get_author_books(
        "123", existing_titles=set(),
        cached_raw_books=[_rb("New A", "2"), _rb("New B", "3")],
    )
    assert _titles(result) == {"New B"}
    langs = {b.title: b.language for b in result.books}
    assert langs == {"New B": "English"}      # from the page that loaded


_LIST_HTML = """<table class="tableList">
<tr itemtype="http://schema.org/Book">
  <td><a class="bookTitle" href="/book/show/11"><span>Griffin Academy 2 (Knights Of War #2)</span></a></td></tr>
<tr itemtype="http://schema.org/Book">
  <td><a class="bookTitle" href="/book/show/12"><span>Splashdown (Pinwheel Book 2)</span></a></td></tr>
<tr itemtype="http://schema.org/Book">
  <td><a class="bookTitle" href="/book/show/13"><span>Leviathan Wakes (The Expanse, #1)</span></a></td></tr>
<tr itemtype="http://schema.org/Book">
  <td><a class="bookTitle" href="/book/show/14"><span>Omnibus (Saga #1-3)</span></a></td></tr>
</table>"""


def test_list_page_series_suffixes_parse_with_or_without_a_comma():
    recs = gr_source._parse_list_page_records(BeautifulSoup(_LIST_HTML, "lxml"))
    got = [(r["title"], r["list_series"], r["list_series_idx"]) for r in recs]
    assert got == [
        ("Griffin Academy 2", "Knights Of War", 2.0),
        ("Splashdown", "Pinwheel", 2.0),
        ("Leviathan Wakes", "The Expanse", 1.0),
        ("Omnibus (Saga #1-3)", None, None),       # a range stays (set filter)
    ]


def test_the_detail_page_title_fallback_reads_book_n():
    assert gr_source._series_from_title_paren("Splashdown (Pinwheel Book 2)") == ("Pinwheel", 2.0)


async def test_a_backoff_refusal_is_not_retried(session, monkeypatch):
    """`_get` retries transport errors 3s / 6s; a request refused because
    its kind backs off must surface at once."""
    gr._record_block("book_page", 202)
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)
    monkeypatch.setattr(gr_source.asyncio, "sleep", fake_sleep)
    src = gr_source.GoodreadsSource(rate_limit=0)
    with pytest.raises(gr.GoodreadsBackingOff):
        await src._get("https://www.goodreads.com/book/show/9")
    assert slept == [] and session._curl.urls == []

