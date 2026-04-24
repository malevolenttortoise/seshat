"""
Regression test for Hardcover's `_pick_hardcover_cover` helper.

Hardcover's edition-level `cached_image` is inconsistently populated —
older or print-only editions lack a URL even when the book itself
has a canonical cover image. Before this fix, the source only read
edition.image, so those books (e.g. "Right of Retribution 3",
"Otherlife Dreams: The Selfless Hero Trilogy" in Mark's UAT) landed
in Seshat with cover_url=None and rendered as placeholder glyphs
even though Hardcover had the data one level up.

The fix falls back to book.image (added to the `BookData` fragment)
when edition.image is null.
"""
from __future__ import annotations

from app.discovery.sources.hardcover import _pick_hardcover_cover


class TestPickHardcoverCover:
    def test_edition_cover_wins_when_present(self):
        book = {"image": {"url": "https://hc.cdn/book.jpg"}}
        edition = {"image": {"url": "https://hc.cdn/edition.jpg"}}
        assert _pick_hardcover_cover(book, edition) == "https://hc.cdn/edition.jpg"

    def test_falls_back_to_book_level_when_edition_missing(self):
        book = {"image": {"url": "https://hc.cdn/book.jpg"}}
        edition = {"image": None}
        assert _pick_hardcover_cover(book, edition) == "https://hc.cdn/book.jpg"

    def test_falls_back_to_book_level_when_edition_has_no_image_key(self):
        book = {"image": {"url": "https://hc.cdn/book.jpg"}}
        edition = {}
        assert _pick_hardcover_cover(book, edition) == "https://hc.cdn/book.jpg"

    def test_no_cover_available_returns_none(self):
        assert _pick_hardcover_cover({}, {}) is None
        assert _pick_hardcover_cover(
            {"image": None}, {"image": None},
        ) is None

    def test_accepts_bare_string_image_format(self):
        # Defensive — Hardcover's current schema returns dicts but
        # older API revisions returned bare URL strings.
        book = {"image": "https://hc.cdn/bare.jpg"}
        assert _pick_hardcover_cover(book, {}) == "https://hc.cdn/bare.jpg"

    def test_empty_string_treated_as_absent(self):
        # A `{"url": ""}` edition image falls through to the book
        # level rather than returning an empty string.
        book = {"image": {"url": "https://hc.cdn/book.jpg"}}
        edition = {"image": {"url": ""}}
        assert _pick_hardcover_cover(book, edition) == "https://hc.cdn/book.jpg"

    def test_dict_without_url_key_falls_through(self):
        # Malformed dict (missing "url") shouldn't crash — fall
        # through to the next candidate.
        book = {"image": {"url": "https://hc.cdn/book.jpg"}}
        edition = {"image": {"width": 100}}
        assert _pick_hardcover_cover(book, edition) == "https://hc.cdn/book.jpg"
