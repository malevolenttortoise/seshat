"""v3.0.0 Phase 3.3 — Hardcover book-page contributor parsing.

`_parse_hardcover_contributors` reads the BookData fragment's book-level
`contributions` relation into ordered `Contributor`s (name + role +
Hardcover author id). The dict payloads below mirror the exact GraphQL
shape observed in the 2026-05-26 recon (Galaxy's Edge: Legionnaire, The
Sandman Vol.1, The Three-Body Problem) so the parser stays pinned to
real Hardcover shape:

  - authors carry `contribution: null`                 → role None
  - non-authors carry free-text `contribution`          → role kept
    (e.g. "Translator", "Illustrator", localized variants)
  - id comes from `author.id` (capture-now for the v3.x author-ID
    enrichment arc; Phase 3 only writes book_authors author_id ints)

Combined with lookup's `contributor_is_author` allowlist, only the
author-role contributors survive into `book_authors`.
"""
from __future__ import annotations

from app.discovery.sources.hardcover import _parse_hardcover_contributors
from app.discovery.sources.base import contributor_is_author


def _contrib(name, cid, role=None):
    return {"contribution": role, "author": {"name": name, "id": cid}}


# Galaxy's Edge: Legionnaire — two co-authors, neither role-tagged.
LEGIONNAIRE = {
    "contributions": [
        _contrib("Jason Anspach", 14168090),
        _contrib("Nick Cole", 199737),
    ]
}

# The Sandman Vol.1 — author + two illustrators (one localized).
SANDMAN = {
    "contributions": [
        _contrib("Neil Gaiman", 1221698),
        _contrib("Sam Kieth", 13359, "Illustrator"),
        _contrib("Mike Dringenberg", 7271, "Illustratore"),  # localized — still dropped
    ]
}

# The Three-Body Problem — author (null) + translator. (recon ground truth)
THREE_BODY = {
    "contributions": [
        _contrib("Liu Cixin", 5780686),
        _contrib("Ken Liu", 2917920, "Translator"),
    ]
}


class TestParseContributors:
    def test_two_authors_no_roles(self):
        cs = _parse_hardcover_contributors(LEGIONNAIRE)
        assert [(c.name, c.role, c.source_author_id) for c in cs] == [
            ("Jason Anspach", None, "14168090"),
            ("Nick Cole", None, "199737"),
        ]

    def test_author_plus_illustrators(self):
        cs = _parse_hardcover_contributors(SANDMAN)
        assert [(c.name, c.role) for c in cs] == [
            ("Neil Gaiman", None),
            ("Sam Kieth", "Illustrator"),
            ("Mike Dringenberg", "Illustratore"),
        ]
        assert cs[1].source_author_id == "13359"

    def test_author_plus_translator(self):
        cs = _parse_hardcover_contributors(THREE_BODY)
        assert [(c.name, c.role) for c in cs] == [
            ("Liu Cixin", None),
            ("Ken Liu", "Translator"),
        ]

    def test_empty_string_role_treated_as_author(self):
        cs = _parse_hardcover_contributors({"contributions": [_contrib("X", 1, "")]})
        assert cs[0].role is None  # "" -> None via `role or None`

    def test_missing_contributions_returns_empty(self):
        """No `contributions` key (older fragment / null relation) degrades
        to [] so callers fall back to single-author behavior."""
        assert _parse_hardcover_contributors({"title": "x"}) == []
        assert _parse_hardcover_contributors({"contributions": None}) == []

    def test_missing_name_skipped(self):
        book = {
            "contributions": [
                {"contribution": None, "author": {"name": "", "id": 1}},
                {"contribution": None, "author": None},
                {"contribution": None},  # no author key at all
                _contrib("Real Author", 2),
            ]
        }
        cs = _parse_hardcover_contributors(book)
        assert [c.name for c in cs] == ["Real Author"]

    def test_null_author_id_yields_none_source_id(self):
        cs = _parse_hardcover_contributors(
            {"contributions": [{"contribution": None, "author": {"name": "Y", "id": None}}]}
        )
        assert cs[0].source_author_id is None


class TestRoleFilterIntegration:
    def test_only_authors_survive_filter(self):
        """The Sandman byline through the role-filter keeps only the
        author (Gaiman); both illustrators drop (incl. the localized one)."""
        cs = _parse_hardcover_contributors(SANDMAN)
        kept = [c.name for c in cs if contributor_is_author(c.role)]
        assert kept == ["Neil Gaiman"]

    def test_co_authors_both_survive(self):
        cs = _parse_hardcover_contributors(LEGIONNAIRE)
        kept = [c.name for c in cs if contributor_is_author(c.role)]
        assert kept == ["Jason Anspach", "Nick Cole"]

    def test_three_body_drops_translator(self):
        cs = _parse_hardcover_contributors(THREE_BODY)
        kept = [c.name for c in cs if contributor_is_author(c.role)]
        assert kept == ["Liu Cixin"]
