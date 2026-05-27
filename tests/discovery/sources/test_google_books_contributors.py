"""v3.0.0 Phase 3.5 — Google Books contributor population.

`volumeInfo.authors` is a flat name list with no role signal, so each
entry becomes a plain-author Contributor (role None). Google Books is
LINK-ONLY (not in TRUSTED_CREATE_SOURCES): the lookup-side
`_link_discovered_contributors` resolves these against EXISTING author
rows and never mints — that gate is the safeguard against the untyped
list pulling in non-authors, so the source just surfaces the names.
"""
from __future__ import annotations

import httpx

from app.discovery.sources.base import contributor_is_author
from app.discovery.sources.google_books import GoogleBooksSource


def _inject_transport(monkeypatch, handler):
    orig = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: orig(
            transport=httpx.MockTransport(handler),
            **{k: v for k, v in kw.items() if k != "transport"},
        ),
    )


def _volumes_handler(items):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": items})
    return handler


def _volume(vid, title, authors):
    return {"id": vid, "volumeInfo": {"title": title, "authors": authors}}


class TestContributors:
    async def test_co_authors_populated(self, monkeypatch):
        _inject_transport(monkeypatch, _volumes_handler([
            _volume("v1", "A Co-Authored Book", ["Jason Anspach", "Nick Cole"]),
        ]))
        result = await GoogleBooksSource(rate_limit=0).get_author_books("Jason Anspach")
        # standalone (no series) book
        bk = result.books[0]
        assert [(c.name, c.role) for c in bk.contributors] == [
            ("Jason Anspach", None),
            ("Nick Cole", None),
        ]
        assert all(c.source_author_id is None for c in bk.contributors)  # GB exposes no IDs
        kept = [c.name for c in bk.contributors if contributor_is_author(c.role)]
        assert kept == ["Jason Anspach", "Nick Cole"]

    async def test_single_author(self, monkeypatch):
        _inject_transport(monkeypatch, _volumes_handler([
            _volume("v2", "Solo Work", ["Brandon Sanderson"]),
        ]))
        result = await GoogleBooksSource(rate_limit=0).get_author_books("Brandon Sanderson")
        assert [c.name for c in result.books[0].contributors] == ["Brandon Sanderson"]

    async def test_missing_authors_field_yields_empty(self, monkeypatch):
        # A volume with no authors fails the author_overlap gate and is
        # skipped — but if the queried name appears it still has [] safely.
        _inject_transport(monkeypatch, _volumes_handler([
            _volume("v3", "Has Author", ["Brandon Sanderson"]),
            {"id": "v4", "volumeInfo": {"title": "No Authors"}},  # dropped by overlap gate
        ]))
        result = await GoogleBooksSource(rate_limit=0).get_author_books("Brandon Sanderson")
        titles = {b.title for b in result.books}
        assert "No Authors" not in titles  # overlap gate dropped it
        assert "Has Author" in titles
