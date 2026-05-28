"""v3.0.0 Phase 3.2 — Goodreads book-page contributor parsing.

`_parse_book_contributors` reads the scoped `div.ContributorLinksList`
byline into ordered `Contributor`s (name + role + Goodreads author id).
The HTML fixtures below mirror the exact markup observed in the
2026-05-26 recon (Galaxy's Edge: Legionnaire, The Sandman Vol.1, The
Three-Body Problem) so the parser stays pinned to real Goodreads shape:

  - authors carry NO `span.ContributorLink__role`  → role None
  - non-authors carry `(Illustrator)` / `(Translator)` → role w/o parens
  - id comes from the `/author/show/<id>` href (capture-now for the
    v3.x author-ID enrichment arc)

Combined with lookup's `contributor_is_author` allowlist, only the
author-role contributors survive into `book_authors`.
"""
from __future__ import annotations

from bs4 import BeautifulSoup

from app.discovery.sources.goodreads import _parse_book_contributors
from app.discovery.sources.base import contributor_is_author


def _soup(inner: str) -> BeautifulSoup:
    return BeautifulSoup(f"<html><body>{inner}</body></html>", "lxml")


def _contrib_link(href, name, role=None):
    role_html = (
        f'<span class="ContributorLink__role">{role}</span>' if role else ""
    )
    return (
        f'<a class="ContributorLink" href="{href}">'
        f'<span class="ContributorLink__name">{name}</span>{role_html}</a>'
    )


# Galaxy's Edge: Legionnaire — two co-authors, neither role-tagged.
LEGIONNAIRE = _soup(
    '<div class="ContributorLinksList">'
    + _contrib_link("https://www.goodreads.com/author/show/14168090.Jason_Anspach", "Jason Anspach")
    + _contrib_link("https://www.goodreads.com/author/show/199737.Nick_Cole", "Nick Cole")
    + "</div>"
)

# The Sandman Vol.1 — author + two illustrators.
SANDMAN = _soup(
    '<div class="ContributorLinksList">'
    + _contrib_link("https://www.goodreads.com/author/show/1221698.Neil_Gaiman", "Neil Gaiman")
    + _contrib_link("https://www.goodreads.com/author/show/13359.Sam_Kieth", "Sam Kieth", "(Illustrator)")
    + _contrib_link("https://www.goodreads.com/author/show/7271.Mike_Dringenberg", "Mike Dringenberg", "(Illustrator)")
    + "</div>"
)

# The Three-Body Problem — author + translator.
THREE_BODY = _soup(
    '<div class="ContributorLinksList">'
    + _contrib_link("https://www.goodreads.com/author/show/5780686.Liu_Cixin", "Liu Cixin")
    + _contrib_link("https://www.goodreads.com/author/show/2917920.Ken_Liu", "Ken Liu", "(Translator)")
    + "</div>"
)


class TestParseContributors:
    def test_two_authors_no_roles(self):
        cs = _parse_book_contributors(LEGIONNAIRE)
        assert [(c.name, c.role, c.source_author_id) for c in cs] == [
            ("Jason Anspach", None, "14168090"),
            ("Nick Cole", None, "199737"),
        ]

    def test_author_plus_illustrators_roles_stripped(self):
        cs = _parse_book_contributors(SANDMAN)
        assert [(c.name, c.role) for c in cs] == [
            ("Neil Gaiman", None),
            ("Sam Kieth", "Illustrator"),     # parens stripped
            ("Mike Dringenberg", "Illustrator"),
        ]
        assert cs[1].source_author_id == "13359"

    def test_author_plus_translator(self):
        cs = _parse_book_contributors(THREE_BODY)
        assert [(c.name, c.role) for c in cs] == [
            ("Liu Cixin", None),
            ("Ken Liu", "Translator"),
        ]

    def test_goodreads_author_badge_normalized_to_author(self):
        """A "(Goodreads Author)" role badge is an author, not a
        contributor role — must not be dropped by the filter."""
        soup = _soup(
            '<div class="ContributorLinksList">'
            + _contrib_link("https://www.goodreads.com/author/show/42.X", "Some Author", "(Goodreads Author)")
            + "</div>"
        )
        cs = _parse_book_contributors(soup)
        assert cs[0].role is None

    def test_no_contributor_list_returns_empty(self):
        """Older / unexpected layouts (no ContributorLinksList) degrade
        to [] so callers fall back to single-author behavior."""
        assert _parse_book_contributors(_soup("<div>no byline here</div>")) == []

    def test_missing_name_skipped(self):
        soup = _soup(
            '<div class="ContributorLinksList">'
            '<a class="ContributorLink" href="/author/show/1.X"></a>'
            + _contrib_link("/author/show/2.Y", "Real Author")
            + "</div>"
        )
        cs = _parse_book_contributors(soup)
        assert [c.name for c in cs] == ["Real Author"]


class TestRoleFilterIntegration:
    def test_only_authors_survive_filter(self):
        """The Sandman byline through the role-filter keeps only the
        author (Gaiman); illustrators drop."""
        cs = _parse_book_contributors(SANDMAN)
        kept = [c.name for c in cs if contributor_is_author(c.role)]
        assert kept == ["Neil Gaiman"]

    def test_co_authors_both_survive(self):
        cs = _parse_book_contributors(LEGIONNAIRE)
        kept = [c.name for c in cs if contributor_is_author(c.role)]
        assert kept == ["Jason Anspach", "Nick Cole"]
