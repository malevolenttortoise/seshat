"""Kobo's search + detail parsing (G188, 2026-10-20).

Until G188 the record carried no authors, so the enricher's score (title
70%, author 30%) topped out at 0.70 under its 0.80 bar and Kobo never
merged a field; and most of its near-misses were other books. The pages
below copy the markup of live pages fetched 2026-10-20 (a search for
"Heaven Sent an Inquisitor, So I Took Her Soul" by Jax A River, and the
detail pages of Claire Broad's "Heaven Sent" and "My Dungeon Life …
Volume 16"), cut to the parts the parser reads. No request is sent.
"""
from __future__ import annotations

import pytest

from app.metadata.sources.kobo import KoboSource


def _card(title: str, href: str, authors: list[str] | None) -> str:
    who = "" if authors is None else (
        '<dl><dd data-testid="authors">'
        + ", ".join(f'<a data-testid="book-attribute-link" href="/a">{a}</a>' for a in authors)
        + "</dd></dl>"
    )
    return (
        '<div class="flex flex-col pl-0 w-full min-w-0 gap-2xsmall p-me">'
        '<div class="flex flex-col w-full min-w-0 gap-4xsmall">'
        f'<h2 class="all-unset"><a data-testid="title" href="{href}">{title}</a></h2>'
        f"</div>{who}</div>"
    )


def _search(*cards: str) -> str:
    return f"<html><body><div class='results'>{''.join(cards)}</div></body></html>"


def _detail(title: str, *, subtitle: str = "", authors: tuple[str, ...] = (), series: str = "",
            index: str = "") -> str:
    sub = f'<span class="subtitle product-field">{subtitle}</span>' if subtitle else ""
    who = "".join(
        f'<span class="visible-contributors">Author <a class="contributor-name" href="/x">{a}</a></span>'
        for a in authors
    )
    ser = (
        f'<span class="series product-field"><span class="sequenced-name-prefix">Book {index} - </span>'
        f'<a href="/s">{series}</a></span>' if series else ""
    )
    return (
        f'<html><body><h1 class="title product-field">{title}</h1>{sub}'
        f'<span class="authors product-field contributor-list">by {who}</span>{ser}</body></html>'
    )


def _kobo(pages: dict[str, str]) -> tuple[KoboSource, list[str]]:
    src = KoboSource()
    asked: list[str] = []

    async def fetch(url: str):
        asked.append(url)
        for key, page in pages.items():
            if key in url:
                return page
        return None

    src._fetch = fetch  # type: ignore[method-assign]
    return src, asked


class TestSearchPicksTheRightBook:
    async def test_the_searched_author_wins_over_a_namesake(self):
        """Two "Heaven Sent"s: the one by the searched author is fetched."""
        src, asked = _kobo({
            "/search?": _search(
                _card("Heaven Sent", "/us/en/ebook/heaven-sent-74", ["Claire Broad"]),
                _card("Heaven Sent", "/us/en/ebook/heaven-sent-99", ["Jax A River"]),
            ),
            "heaven-sent-99": _detail("Heaven Sent", authors=("Jax A River",)),
        })
        rec = await src.search_book("Heaven Sent", "Jax A River")
        assert asked[1].endswith("heaven-sent-99")
        assert rec.authors == ["Jax A River"]

    async def test_another_volume_is_never_picked(self):
        """Live 2026-10-17: "… Volume 16" came back for volumes 1–9."""
        src, asked = _kobo({
            "/search?": _search(
                _card("My Dungeon Life : Rise of the Slave Harem Volume 16", "/us/en/ebook/mdl-16",
                      ["Whatsawhizzer"]),
                _card("My Dungeon Life : Rise of the Slave Harem Volume 2", "/us/en/ebook/mdl-2",
                      ["Whatsawhizzer"]),
            ),
            "mdl-2": _detail("My Dungeon Life : Rise of the Slave Harem Volume 2",
                             authors=("Whatsawhizzer",)),
        })
        rec = await src.search_book("My Dungeon Life Vol 2", "Whatsawhizzer")
        assert asked[1].endswith("mdl-2")
        assert rec.title.endswith("Volume 2")

    async def test_only_other_volumes_means_no_record(self):
        src, asked = _kobo({
            "/search?": _search(
                _card("My Dungeon Life : Rise of the Slave Harem Volume 16", "/us/en/ebook/mdl-16",
                      ["Whatsawhizzer"]),
            ),
        })
        assert await src.search_book("My Dungeon Life Vol 1", "Whatsawhizzer") is None
        assert len(asked) == 1            # no detail page fetched

    async def test_a_card_without_authors_borrows_none(self):
        """A card with no authors line never takes its neighbour's."""
        src, _ = _kobo({
            "/search?": _search(
                _card("Golden Flight", "/us/en/ebook/golden-flight", None),
                _card("Other Book", "/us/en/ebook/other", ["Someone Else"]),
            ),
            "golden-flight": _detail("Golden Flight"),
        })
        rec = await src.search_book("Golden Flight", "Nobody")
        assert rec is not None and rec.authors == []


class TestDetailPage:
    async def test_title_subtitle_and_authors(self):
        """Claire Broad's book, not Jax A River's: the subtitle and the
        author now say so."""
        src, _ = _kobo({"x": _detail(
            "Heaven Sent", subtitle="Soul Lessons from the Afterlife", authors=("Claire Broad",),
        )})
        d = await src._get_book_details("x")
        assert d["title"] == "Heaven Sent: Soul Lessons from the Afterlife"
        assert d["authors"] == ["Claire Broad"]

    async def test_a_series_reference_is_no_subtitle(self):
        src, _ = _kobo({"x": _detail(
            "My Dungeon Life : Rise of the Slave Harem Volume 16", subtitle="My Dungeon Life, #16",
            authors=("Whatsawhizzer",), series="My Dungeon Life", index="16",
        )})
        d = await src._get_book_details("x")
        assert d["title"] == "My Dungeon Life : Rise of the Slave Harem Volume 16"
        assert (d["series_name"], d["series_index"]) == ("My Dungeon Life", 16.0)

    @pytest.mark.parametrize("authors", [("A. Rivers", "Alexa Rivers"), ("Same", "Same")])
    async def test_every_author_once(self, authors):
        src, _ = _kobo({"x": _detail("The King", authors=authors)})
        d = await src._get_book_details("x")
        assert d["authors"] == list(dict.fromkeys(authors))
