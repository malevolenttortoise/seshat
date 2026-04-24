"""
Tests for Amazon's author-byline gate — the search-card filter and
the detail-page verifier.

Mark's UAT showed two failure modes:

  1. Legitimate Arand books rejected by the search filter because
     Amazon's card text didn't carry the exact "William D. Arand"
     string the old all-parts-substring check required.
  2. Books by completely different authors (Dirty Like Me, Kingdom
     Revival) slipped past the search filter and the detail page
     never verified authorship, so they landed in the user's library.

The fix extracts contributor anchors / byline text from each card,
extracts #bylineInfo authors from detail pages, and compares via
the shared `authors_match` helper. Pen-name aliases widen the
accepted set so Randi Darren scans accept books bylined under
William D. Arand (the real author).
"""
from __future__ import annotations

from bs4 import BeautifulSoup

from app.discovery.sources.amazon import (
    _extract_card_authors,
    _extract_detail_authors,
    _extract_search_results,
    _parse_detail_page,
)


# ─── _extract_card_authors ─────────────────────────────────────

def _card_html(inner: str) -> str:
    return f'<div data-asin="B01ABCDEFG">{inner}</div>'


def _card(inner: str):
    return BeautifulSoup(_card_html(inner), "lxml").select_one("[data-asin]")


class TestExtractCardAuthors:
    def test_contributor_anchor_returns_name(self):
        card = _card(
            '<h2><a><span>The Book</span></a></h2>'
            '<a class="a-link-normal" href="/Author/e/B00EXAMPLE/">'
            'William D. Arand</a>'
        )
        assert _extract_card_authors(card) == ["William D. Arand"]

    def test_field_author_url_recognized(self):
        card = _card(
            '<a class="a-link-normal" href="/s?field-author=Randi+Darren">'
            'Randi Darren</a>'
        )
        assert _extract_card_authors(card) == ["Randi Darren"]

    def test_byline_text_fallback_when_no_anchor(self):
        # Compact cards without contributor anchors still carry
        # "by Name" in the card text.
        card = _card('<h2><span>The Book</span></h2><div>by William D. Arand | Aug 12, 2020</div>')
        authors = _extract_card_authors(card)
        assert "William D. Arand" in authors

    def test_multiple_authors_split_on_comma(self):
        card = _card(
            '<a class="a-link-normal" href="/e/B001/">William D. Arand</a>'
            '<a class="a-link-normal" href="/e/B002/">Co Author</a>'
        )
        authors = _extract_card_authors(card)
        assert "William D. Arand" in authors
        assert "Co Author" in authors

    def test_no_attribution_returns_empty(self):
        # Compact layout — card has title + price only, no byline.
        card = _card('<h2><span>Title Only</span></h2><span>$9.99</span>')
        assert _extract_card_authors(card) == []

    def test_non_author_anchors_ignored(self):
        # The title anchor itself is `a.a-link-normal` too — we must
        # not mistake it for an author.
        card = _card(
            '<h2><a class="a-link-normal" href="/dp/B01X"><span>Title</span></a></h2>'
        )
        assert _extract_card_authors(card) == []


# ─── _extract_detail_authors ──────────────────────────────────

def _detail(byline_html: str):
    html = f"<html><body><div id='bylineInfo'>{byline_html}</div></body></html>"
    return BeautifulSoup(html, "lxml")


class TestExtractDetailAuthors:
    def test_single_author_span(self):
        soup = _detail(
            '<span class="author">'
            '<a>William D. Arand</a>'
            '<span class="contribution">(Author)</span>'
            '</span>'
        )
        assert _extract_detail_authors(soup) == ["William D. Arand"]

    def test_translator_excluded(self):
        soup = _detail(
            '<span class="author">'
            '<a>William D. Arand</a>'
            '<span class="contribution">(Author)</span>'
            '</span>'
            '<span class="author">'
            '<a>Someone Else</a>'
            '<span class="contribution">(Translator)</span>'
            '</span>'
        )
        authors = _extract_detail_authors(soup)
        assert authors == ["William D. Arand"]

    def test_foreword_excluded(self):
        # The "Kingdom Revival: Forward by Randy Clark" case — Randy
        # Clark is the FOREWORD author, not the book author. Must not
        # pass the gate when the query is for a different person.
        soup = _detail(
            '<span class="author">'
            '<a>Actual Author</a>'
            '<span class="contribution">(Author)</span>'
            '</span>'
            '<span class="author">'
            '<a>Randy Clark</a>'
            '<span class="contribution">(Foreword)</span>'
            '</span>'
        )
        assert _extract_detail_authors(soup) == ["Actual Author"]

    def test_co_authors_returned(self):
        soup = _detail(
            '<span class="author">'
            '<a>First Author</a>'
            '<span class="contribution">(Author)</span>'
            '</span>'
            '<span class="author">'
            '<a>Second Author</a>'
            '<span class="contribution">(Author)</span>'
            '</span>'
        )
        authors = _extract_detail_authors(soup)
        assert authors == ["First Author", "Second Author"]

    def test_plain_byline_fallback(self):
        # Some layouts skip the span markup entirely and just render
        # "by Author Name (Author)" as flat text.
        soup = _detail("by William D. Arand (Author)")
        assert "William D. Arand" in _extract_detail_authors(soup)

    def test_missing_byline_returns_empty(self):
        soup = BeautifulSoup("<html><body></body></html>", "lxml")
        assert _extract_detail_authors(soup) == []


# ─── _extract_search_results (integration) ────────────────────

def _search_html(cards: list[str]) -> str:
    return (
        "<html><body><div id='search'>"
        + "".join(cards)
        + "</div></body></html>"
    )


def _asin_card(asin: str, title: str, author: str = "") -> str:
    byline = (
        f'<a class="a-link-normal" href="/e/B00/">{author}</a>'
        if author else ""
    )
    return (
        f'<div data-asin="{asin}">'
        f'<h2><a><span>{title}</span></a></h2>'
        f'{byline}'
        f'</div>'
    )


class TestExtractSearchResults:
    def test_matching_author_accepted(self):
        html = _search_html([
            _asin_card("B0ARAND001", "Super Sales", "William D. Arand"),
        ])
        results = _extract_search_results(html, "William D. Arand")
        assert results == [("B0ARAND001", "Super Sales")]

    def test_wrong_author_rejected(self):
        html = _search_html([
            _asin_card("B0WRONG001", "Dirty Like Me", "Jaine Diamond"),
        ])
        results = _extract_search_results(html, "Randi Darren")
        assert results == []

    def test_author_name_normalization_accepts_punctuation_variant(self):
        # Queried as "A. K. Duboff", card shows "A.K. DuBoff" — matches
        # via the shared authors_match fuzzy/normalized comparator.
        html = _search_html([
            _asin_card("B0DUBOFF01", "Stranded", "A.K. DuBoff"),
        ])
        results = _extract_search_results(html, "A. K. Duboff")
        assert len(results) == 1
        assert results[0][0] == "B0DUBOFF01"

    def test_pen_name_alias_accepted(self):
        # Query is for pen-name "Randi Darren"; card is bylined under
        # the real author "William D. Arand". accept_authors widens
        # the gate so this specific case is accepted.
        html = _search_html([
            _asin_card("B0INCUBUS1", "Incubus Inc.", "William D. Arand"),
        ])
        results = _extract_search_results(
            html, "Randi Darren",
            accept_authors=["Randi Darren", "William D. Arand"],
        )
        assert len(results) == 1
        assert results[0][0] == "B0INCUBUS1"

    def test_card_without_attribution_is_accepted_for_detail_verification(self):
        # Compact card with no visible byline — defer rejection to the
        # detail-page gate rather than dropping potentially-valid ASINs.
        html = _search_html([
            f'<div data-asin="B0NOAUTH01">'
            f'<h2><a><span>Some Book</span></a></h2>'
            f'</div>'
        ])
        results = _extract_search_results(html, "William D. Arand")
        assert len(results) == 1
        assert results[0][0] == "B0NOAUTH01"


# ─── _parse_detail_page (author gate) ─────────────────────────

def _full_detail_html(title: str, byline: str = "") -> str:
    byline_block = f'<div id="bylineInfo">{byline}</div>' if byline else ""
    return (
        f'<html><body>'
        f'<span id="productTitle">{title}</span>'
        f'{byline_block}'
        f'</body></html>'
    )


class TestParseDetailPageAuthorGate:
    def test_matching_author_parses_successfully(self):
        html = _full_detail_html(
            "Super Sales on Super Heroes",
            '<span class="author">'
            '<a>William D. Arand</a>'
            '<span class="contribution">(Author)</span>'
            '</span>',
        )
        book = _parse_detail_page(
            html, "B0ARAND01", expected_authors=["William D. Arand"],
        )
        assert book is not None
        assert book.title == "Super Sales on Super Heroes"

    def test_different_detail_author_rejected(self):
        # "Dirty Like Me" slipped past the search gate somehow; the
        # detail-page author gate catches it because the detail
        # byline attributes it to Jaine Diamond, not Randi Darren.
        html = _full_detail_html(
            "Dirty Like Me",
            '<span class="author">'
            '<a>Jaine Diamond</a>'
            '<span class="contribution">(Author)</span>'
            '</span>',
        )
        book = _parse_detail_page(
            html, "B0WRONG01", expected_authors=["Randi Darren"],
        )
        assert book is None

    def test_kingdom_revival_foreword_case(self):
        # The ibdb/Amazon junk case — "Kingdom Revival" has Randy Clark
        # as foreword author. Query "Randi Darren" must not match via
        # partial-string collision on "Randy" because
        # `_extract_detail_authors` excludes (Foreword) roles and
        # authors_match compares the actual author.
        html = _full_detail_html(
            "Kingdom Revival: Forward by Randy Clark",
            '<span class="author">'
            '<a>Actual Kingdom Author</a>'
            '<span class="contribution">(Author)</span>'
            '</span>'
            '<span class="author">'
            '<a>Randy Clark</a>'
            '<span class="contribution">(Foreword)</span>'
            '</span>',
        )
        book = _parse_detail_page(
            html, "B0KINGDOM01", expected_authors=["Randi Darren"],
        )
        assert book is None

    def test_no_byline_is_conservative_reject(self):
        # Paranoid default — if the detail page has no byline block at
        # all, we can't prove authorship, so we don't claim it.
        html = _full_detail_html("Some Book", byline="")
        book = _parse_detail_page(
            html, "B0NOBYLINE", expected_authors=["William D. Arand"],
        )
        assert book is None

    def test_no_expected_authors_skips_the_gate(self):
        # Backwards-compat: callers that don't pass expected_authors
        # (e.g. older tests, pre-flight parses) get the prior
        # behavior — detail parsing just returns the book.
        html = _full_detail_html(
            "Any Book",
            '<span class="author"><a>Anyone</a></span>',
        )
        book = _parse_detail_page(html, "B0ANY0001")
        assert book is not None

    def test_pen_name_alias_accepted_on_detail(self):
        # Query for pen-name, detail attributes to real author —
        # accepted via expected_authors including both names.
        html = _full_detail_html(
            "Incubus Inc.",
            '<span class="author">'
            '<a>William D. Arand</a>'
            '<span class="contribution">(Author)</span>'
            '</span>',
        )
        book = _parse_detail_page(
            html, "B0INCUBUS1",
            expected_authors=["Randi Darren", "William D. Arand"],
        )
        assert book is not None
