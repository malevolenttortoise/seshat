"""Kobo on curl_cffi, no challenge solving (2026-10 audit wave 4 S7, G60).

Both Kobo sources used `cloudscraper`, a Cloudflare challenge solver
(access tier 3, ADR-0025). Now a challenge is a counted block and a
backoff, and the dirty session is dropped."""
import httpx
import pytest

from app.metadata import kobo_session, source_gate


class _Session:
    """curl_cffi AsyncSession stand-in."""

    made = 0

    def __init__(self, answer):
        type(self).made += 1
        self.answer = answer
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    async def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers or {}))
        return self.answer(url)

    async def close(self):
        self.closed = True


@pytest.fixture
def kobo(monkeypatch):
    """Point kobo_session at fake sessions answering through `box["answer"]`."""
    box = {"answer": lambda url: httpx.Response(200, text="<html>book</html>"),
           "sessions": [], "now": 1_000_000.0}

    def make():
        s = _Session(lambda url: box["answer"](url))
        box["sessions"].append(s)
        return s

    monkeypatch.setattr(kobo_session, "_create_session", make)
    monkeypatch.setattr(kobo_session, "_now", lambda: box["now"])
    return box


URL = "https://www.kobo.com/us/en/ebook/some-book"


def _pending(caller="scan"):
    return source_gate._pending.get((source_gate._today(), "kobo", caller, "")) or {}


async def test_a_page_comes_back_with_a_chrome_profile(kobo):
    with source_gate.caller("scan"):
        assert await kobo_session.fetch(URL) == "<html>book</html>"
    url, headers = kobo["sessions"][0].calls[0]
    assert url == URL and "Chrome/" in headers["User-Agent"]
    assert _pending()["ok"] == 1


@pytest.mark.parametrize("resp", [
    httpx.Response(403, text="<html>Attention Required! | Cloudflare</html>"),
    httpx.Response(503, text="<html><title>Just a moment...</title>challenge-platform</html>"),
    httpx.Response(200, headers={"cf-mitigated": "challenge"}, text="<html></html>"),
    httpx.Response(429, text="slow down"),
])
async def test_a_challenge_is_a_block_never_solved(kobo, resp):
    """The dangerous call: no further request to kobo.com while it backs off."""
    kobo["answer"] = lambda url: resp
    with source_gate.caller("scan"):
        assert await kobo_session.fetch(URL) is None
        assert await kobo_session.fetch(URL) is None     # refused, not sent
    assert sum(len(s.calls) for s in kobo["sessions"]) == 1
    assert kobo["sessions"][0].closed                     # dirty state dropped
    assert kobo_session.backoff_until() - kobo["now"] == 120.0
    assert _pending()["blocks"] == 1


async def test_the_backoff_climbs_and_a_success_clears_it(kobo):
    kobo["answer"] = lambda url: httpx.Response(403, text="blocked")
    waits = []
    for _ in range(3):
        await kobo_session.fetch(URL)
        waits.append(kobo_session.backoff_until() - kobo["now"])
        kobo["now"] += waits[-1] + 1
    assert waits == [120.0, 300.0, 600.0]
    kobo["answer"] = lambda url: httpx.Response(200, text="ok")
    assert await kobo_session.fetch(URL) == "ok"
    assert kobo_session.backoff_state()["level"] == 0
    assert len(kobo["sessions"]) == 4                     # a fresh one after each block


async def test_a_plain_error_is_not_a_block(kobo):
    kobo["answer"] = lambda url: httpx.Response(404, text="<html>not found</html>")
    assert await kobo_session.fetch(URL) is None
    assert kobo_session.backoff_until() == 0.0


async def test_both_kobo_sources_go_through_it(kobo):
    from app.discovery.sources.kobo import KoboSource as DiscoKobo
    from app.metadata.sources.kobo import KoboSource as MetaKobo

    assert await DiscoKobo()._fetch(URL) == "<html>book</html>"
    assert await MetaKobo()._fetch(URL) == "<html>book</html>"
    assert len(kobo["sessions"]) == 1                     # one shared session
    assert len(kobo["sessions"][0].calls) == 2
