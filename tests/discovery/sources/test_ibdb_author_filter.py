"""
Tests for ibdb's author-byline gate.

Pre-fix: `score_match` was called with `search_title == record_title`,
making the title term trivially 1.0 and letting the 0.3 floor pass
effectively anything. A scan of "Randi Darren" brought back a
baseball biography ("Joueur des Astros de Houston") because
"Darren Oliver" appeared in its author list, and religious books
like "Kingdom Revival: Forward by Randy Clark" because Randy is a
substring of Randi.

Post-fix: `authors_match` (normalized + fuzzy comparator) gates each
item by comparing the queried author name against the item's
listed authors. Pen-name aliases are accepted via the
`_linked_author_names` attribute.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.discovery.sources.ibdb import IbdbSource


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def source():
    return IbdbSource()


def _item(title: str, authors: list[str], **kwargs) -> dict:
    return {"title": title, "authors": authors, **kwargs}


class TestIbdbAuthorGate:
    async def test_matching_author_accepted(self, source, monkeypatch):
        async def fake_get(self, url, params=None, **kwargs):
            return _FakeResp({"results": [
                _item("Wild Wastes", ["Randi Darren"]),
            ]})
        monkeypatch.setattr(IbdbSource, "_get", fake_get)

        result = await source.get_author_books("Randi Darren")
        assert result is not None
        titles = [b.title for b in result.books] + [
            b.title for sr in result.series for b in sr.books
        ]
        assert "Wild Wastes" in titles

    async def test_joueur_des_astros_rejected(self, source, monkeypatch):
        # The regression: ibdb returns a baseball biography because
        # "Darren Oliver" is in the author list, and the old filter
        # lets it through. authors_match should reject it outright.
        async def fake_get(self, url, params=None, **kwargs):
            return _FakeResp({"results": [
                _item(
                    "Joueur des Astros de Houston",
                    ["Randy Johnson", "Jeff Kent", "Darren Oliver"],
                ),
            ]})
        monkeypatch.setattr(IbdbSource, "_get", fake_get)

        result = await source.get_author_books("Randi Darren")
        assert result is not None
        titles = [b.title for b in result.books] + [
            b.title for sr in result.series for b in sr.books
        ]
        assert "Joueur des Astros de Houston" not in titles

    async def test_foreword_contributor_rejected(self, source, monkeypatch):
        # Similar to the Kingdom Revival case: "Randy Clark" is a
        # foreword contributor, not the book's author. If ibdb lists
        # him in its authors array, authors_match("Randi Darren",
        # "Randy Clark") should return False.
        async def fake_get(self, url, params=None, **kwargs):
            return _FakeResp({"results": [
                _item(
                    "Kingdom Revival: Forward by Randy Clark",
                    ["Randy Clark"],
                ),
            ]})
        monkeypatch.setattr(IbdbSource, "_get", fake_get)

        result = await source.get_author_books("Randi Darren")
        assert result is not None
        titles = [b.title for b in result.books] + [
            b.title for sr in result.series for b in sr.books
        ]
        assert all("Kingdom Revival" not in t for t in titles)

    async def test_pen_name_alias_accepted(self, source, monkeypatch):
        # Queried under pen-name Randi Darren; item is bylined to
        # the real author William D. Arand. Accept via injected
        # `_linked_author_names`.
        async def fake_get(self, url, params=None, **kwargs):
            return _FakeResp({"results": [
                _item("Incubus Inc.", ["William D. Arand"]),
            ]})
        monkeypatch.setattr(IbdbSource, "_get", fake_get)

        source._linked_author_names = ["William D. Arand"]
        result = await source.get_author_books("Randi Darren")
        titles = [b.title for b in result.books] + [
            b.title for sr in result.series for b in sr.books
        ]
        assert "Incubus Inc." in titles

    async def test_punctuation_variant_accepted(self, source, monkeypatch):
        # Item's listed author is "A.K. DuBoff" (compact), query is
        # "A. K. Duboff" (spaced + lowercase-b). Normalized match.
        async def fake_get(self, url, params=None, **kwargs):
            return _FakeResp({"results": [
                _item("Stranded", ["A.K. DuBoff"]),
            ]})
        monkeypatch.setattr(IbdbSource, "_get", fake_get)

        result = await source.get_author_books("A. K. Duboff")
        titles = [b.title for b in result.books] + [
            b.title for sr in result.series for b in sr.books
        ]
        assert "Stranded" in titles

    async def test_empty_author_list_rejected(self, source, monkeypatch):
        # No author info on the item — can't prove authorship, reject.
        # Safer than accepting and letting junk through.
        async def fake_get(self, url, params=None, **kwargs):
            return _FakeResp({"results": [
                _item("Mystery Book", []),
            ]})
        monkeypatch.setattr(IbdbSource, "_get", fake_get)

        result = await source.get_author_books("Randi Darren")
        titles = [b.title for b in result.books] + [
            b.title for sr in result.series for b in sr.books
        ]
        assert titles == []

    async def test_mixed_results_keeps_only_matches(self, source, monkeypatch):
        # Sanity: multiple items, only matching ones land in the
        # output.
        async def fake_get(self, url, params=None, **kwargs):
            return _FakeResp({"results": [
                _item("Wild Wastes", ["Randi Darren"]),
                _item("Unrelated Book", ["Randy Johnson"]),
                _item("Fostering Faust", ["Randi Darren"]),
            ]})
        monkeypatch.setattr(IbdbSource, "_get", fake_get)

        result = await source.get_author_books("Randi Darren")
        titles = [b.title for b in result.books] + [
            b.title for sr in result.series for b in sr.books
        ]
        assert "Wild Wastes" in titles
        assert "Fostering Faust" in titles
        assert "Unrelated Book" not in titles


class TestRoleSuffix:
    async def test_a_role_suffix_still_matches(self, source, monkeypatch):
        # 2026-10 audit L2-09: a live scan showed `['Snekguy (author)']
        # don't match ['Snekguy']` and IBDB found nothing.
        async def fake_get(self, url, params=None, **kwargs):
            return _FakeResp({"results": [
                _item("Pinwheel", ["Snekguy (author)"]),
                _item("Edited Volume", [{"name": "Snekguy (Editor)"}]),
            ]})
        monkeypatch.setattr(IbdbSource, "_get", fake_get)

        result = await source.get_author_books("Snekguy")
        assert result is not None
        titles = [b.title for b in result.books] + [
            b.title for sr in result.series for b in sr.books
        ]
        assert {"Pinwheel", "Edited Volume"} <= set(titles)
