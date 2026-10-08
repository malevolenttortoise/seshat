"""Goodreads backs off per request kind after an AWS WAF block (2026-10
audit wave 4 S3, G66): book pages, list pages and autocomplete each
escalate 2 → 5 → 10 → 30 → 60 min on repeated blocks and clear on the
next success; a book-page block no longer switches Goodreads off."""
import httpx
import pytest

from app.metadata import goodreads_session as gr


class _Curl:
    """curl_cffi stand-in: answers each URL from `status_for(url)`."""

    def __init__(self, status_for=lambda url: 200):
        self.status_for = status_for
        self.urls: list[str] = []

    async def get(self, url, **_):
        self.urls.append(url)
        status = self.status_for(url)
        body = b"" if status == 202 else b"<html>ok</html>"
        return httpx.Response(status, content=body)


@pytest.fixture
def clock(monkeypatch):
    box = {"now": 1_000_000.0}
    monkeypatch.setattr(gr, "_now", lambda: box["now"])
    return box


def _session(status_for=lambda url: 200) -> gr.GoodreadsSession:
    s = gr.GoodreadsSession()
    s._curl = _Curl(status_for)
    return s


BOOK = "https://www.goodreads.com/book/show/241980410"
LIST = "https://www.goodreads.com/author/list/123?page=1"
AUTO = "https://www.goodreads.com/book/auto_complete?format=json&q=9780000000001"


async def test_a_block_backs_that_kind_off_on_a_rising_ladder(clock):
    s = _session(lambda url: 202)
    waits = []
    for _ in range(6):
        await s.get(BOOK, ignore_backoff=True)
        waits.append(gr.backoff_until("book_page") - clock["now"])
    assert waits == [120.0, 300.0, 600.0, 1800.0, 3600.0, 3600.0]


async def test_a_book_page_block_leaves_list_pages_and_autocomplete_alone(clock):
    s = _session(lambda url: 202 if "/book/show/" in url else 200)
    await s.get(BOOK)
    assert gr.is_backing_off("book_page")
    assert not gr.is_backing_off("list_page")
    assert not gr.is_backing_off("autocomplete")
    await s.get(LIST)                      # still goes out
    await s.get(AUTO)
    assert s._curl.urls == [BOOK, LIST, AUTO]


async def test_a_kind_backing_off_is_refused_without_sending(clock):
    """The dangerous call: no request to Goodreads while its kind backs off."""
    s = _session(lambda url: 202)
    await s.get(BOOK)
    with pytest.raises(gr.GoodreadsBackingOff) as exc:
        await s.get("https://www.goodreads.com/book/show/1")
    assert exc.value.kind == "book_page"
    assert s._curl.urls == [BOOK]


async def test_the_probe_goes_out_while_backing_off(clock):
    s = _session(lambda url: 202)
    await s.get(BOOK)
    s._curl.status_for = lambda url: 200
    resp = await s.get(BOOK, ignore_backoff=True)
    assert resp.status_code == 200
    assert not gr.is_backing_off("book_page")     # its success cleared it


async def test_a_success_clears_the_ladder(clock):
    s = _session(lambda url: 202)
    await s.get(BOOK)
    clock["now"] += 121
    s._curl.status_for = lambda url: 200
    await s.get(BOOK)
    s._curl.status_for = lambda url: 202
    await s.get(BOOK)
    assert gr.backoff_until("book_page") - clock["now"] == 120.0   # back to the first step


async def test_a_backoff_ends_by_itself(clock):
    s = _session(lambda url: 429)
    await s.get(AUTO)
    assert gr.is_backing_off("autocomplete")
    clock["now"] += 121
    assert not gr.is_backing_off("autocomplete")
    await s.get(AUTO)                      # allowed again (and blocked again)
    assert len(s._curl.urls) == 2


async def test_the_overall_state_follows_the_backoffs(clock):
    s = _session(lambda url: 202)
    await s.get(BOOK)
    state = gr.get_session_state()
    assert state["state"] == "soft_blocked"
    assert state["backoff"]["book_page"]["backing_off"] is True
    clock["now"] += 121
    assert gr.get_session_state()["state"] == "active"   # ran out; not a latch


async def test_mark_active_ends_every_backoff(clock):
    s = _session(lambda url: 202)
    await s.get(BOOK)
    await s.get(LIST)
    gr.clear_all()
    assert not gr.is_soft_blocked()
    assert gr.get_session_state()["state"] == "active"


async def test_a_404_is_an_answer_not_a_block(clock):
    s = _session(lambda url: 404)
    await s.get(BOOK)
    assert not gr.is_backing_off("book_page")


async def test_the_resolver_skips_autocomplete_while_it_backs_off(clock, monkeypatch):
    from app.metadata import goodreads_id_resolver as resolver

    s = _session(lambda url: 429)

    async def get_session():
        return s
    monkeypatch.setattr(gr, "get_session", get_session)
    assert await resolver._tier1_auto_complete("9780000000001") == "_soft_blocked"
    assert await resolver._tier1_auto_complete("9780000000002") == "_soft_blocked"
    assert await resolver._tier4_auto_complete_title("Banished", "123") == "_soft_blocked"
    assert len(s._curl.urls) == 1          # only the first went out


async def test_a_backoff_refusal_never_marks_a_bibliography_complete(clock, monkeypatch):
    """A refused list page used to read as "no more pages", caching the
    walk as fully indexed for a week."""
    from app.metadata import goodreads_bibliography as bib

    s = _session(lambda url: 202)
    await s.get(LIST)                      # list pages now backing off

    async def get_session():
        return s
    monkeypatch.setattr(gr, "get_session", get_session)
    entries, has_more, blocked = await bib._fetch_page("123", 1)
    assert blocked is True
