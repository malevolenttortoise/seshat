"""v3.x (ADR-0016 slice 04) — Goodreads author-photo extraction.

The pre-slice-04 selector ``img.authorPhoto, img[alt*='author']`` matched
ZERO images on the current Goodreads DOM (2026-05-30 live recon across
5 well-anchored authors: Sanderson, King, Heinlein, Rothfuss, Robert
Jordan). The replacement selector ``img[src*='/authors/']`` targets
the canonical Goodreads author-photo URL pattern
(``images.gr-assets.com/authors/{ts}p2/{id}.jpg``) which is distinct
from book-cover URLs (``i.gr-assets.com/images/S/.../books/...``).

Fixtures mirror the exact shapes the recon captured, including:
  - book covers ALWAYS present on author-list pages, must not be picked
  - alt = author name; no class (the old ``.authorPhoto`` class is gone)
  - the ``nophoto`` placeholder URL convention
"""
from __future__ import annotations

from bs4 import BeautifulSoup

from app.discovery.sources.goodreads import _extract_author_photo


def _soup(inner: str) -> BeautifulSoup:
    return BeautifulSoup(f"<html><body>{inner}</body></html>", "lxml")


# Real shapes from the 2026-05-30 recon — the author photo wrapper
# (no class, alt=author name, src=/authors/) alongside a book-cover
# img (class=bookCover, alt=book title, src=/books/).
SANDERSON_PAGE = _soup(
    '<div class="leftContainer">'
    '  <img alt="Brandon Sanderson" '
    '       src="https://images.gr-assets.com/authors/1721927489p2/38550.jpg"/>'
    '  <img class="bookCover" alt="Mistborn: The Final Empire" '
    '       src="https://i.gr-assets.com/images/S/compressed.photo.goodreads.com/books/1617768316i/68428._SY75_.jpg"/>'
    '  <img class="bookCover" alt="The Way of Kings" '
    '       src="https://i.gr-assets.com/images/S/compressed.photo.goodreads.com/books/1659905828i/7235533._SY75_.jpg"/>'
    '</div>'
)

# Same shape but the author has no photo — Goodreads serves a nophoto
# placeholder. The src still contains "/authors/" so the selector
# matches, but the nophoto filter must NULL it out.
NOPHOTO_AUTHOR = _soup(
    '<div class="leftContainer">'
    '  <img alt="Unknown Author" '
    '       src="https://images.gr-assets.com/authors/nophoto/u_50x66-...jpg"/>'
    '  <img class="bookCover" alt="Some Book" '
    '       src="https://i.gr-assets.com/images/S/.../books/123/abc.jpg"/>'
    '</div>'
)

# Page with ONLY book covers (no author photo at all — extreme edge,
# but the selector must not pick a book cover by accident).
ONLY_BOOK_COVERS = _soup(
    '<div class="leftContainer">'
    '  <img class="bookCover" alt="Book A" '
    '       src="https://i.gr-assets.com/images/S/.../books/123/a.jpg"/>'
    '  <img class="bookCover" alt="Book B" '
    '       src="https://i.gr-assets.com/images/S/.../books/456/b.jpg"/>'
    '</div>'
)

# Completely empty body — defends against an undocumented "no
# leftContainer" layout.
EMPTY_PAGE = _soup('<div></div>')

# The pre-slice-04 BROKEN shape — img.authorPhoto class. The selector
# must NOT depend on this class (it's gone from the live DOM). This
# fixture proves the new selector still works against a hypothetical
# legacy/cached page that DID carry the class, AND it doesn't break
# if the class reappears later.
LEGACY_AUTHORPHOTO_CLASS = _soup(
    '<div class="leftContainer">'
    '  <img class="authorPhoto" alt="Legacy Layout" '
    '       src="https://images.gr-assets.com/authors/999p2/42.jpg"/>'
    '</div>'
)


# ─── 1. Real DOM shape — extracts the photo, ignores book covers ─


def test_extracts_author_photo_from_real_dom_shape():
    """Sanderson-like fixture: author <img> (no class) + book covers
    (class=bookCover). Selector picks the author photo, not the first
    book cover. Closes the failure mode the broken selector hit."""
    assert _extract_author_photo(SANDERSON_PAGE) == (
        "https://images.gr-assets.com/authors/1721927489p2/38550.jpg"
    )


# ─── 2. nophoto placeholder is filtered out ────────────────────


def test_nophoto_placeholder_returns_none():
    """A nophoto URL still contains '/authors/' but must NOT be written
    to the DB — the historical filter at line 584 of the pre-slice
    code stays as defense."""
    assert _extract_author_photo(NOPHOTO_AUTHOR) is None


# ─── 3. Page without author photo — returns None, never a book ──


def test_only_book_covers_returns_none():
    """Pre-slice the broken selector fell THROUGH to the first book
    cover when no author <img> matched (the visible UAT regression
    that wrote a book-cover URL into John Birmingham's author row).
    The new selector requires '/authors/' in src — book-cover URLs
    don't qualify."""
    assert _extract_author_photo(ONLY_BOOK_COVERS) is None


# ─── 4. Empty page — returns None, no exception ────────────────


def test_empty_page_returns_none():
    assert _extract_author_photo(EMPTY_PAGE) is None


# ─── 5. Legacy `.authorPhoto` class — still extracted via src ───


def test_legacy_authorphoto_class_still_extracted_via_src():
    """If Goodreads ever brings back the `.authorPhoto` class
    (hypothetically), the URL-pattern selector still picks up the
    photo as long as its src is under `/authors/`. Demonstrates the
    selector is class-independent — DOM-reshuffling resistant."""
    assert _extract_author_photo(LEGACY_AUTHORPHOTO_CLASS) == (
        "https://images.gr-assets.com/authors/999p2/42.jpg"
    )


# ─── 6. Src missing entirely — returns None ────────────────────


def test_img_without_src_returns_none():
    """An <img> tag without a src attribute must not crash + must
    return None. Probably never appears in real Goodreads HTML, but
    defensive against future DOM shape drift."""
    soup = _soup('<img alt="No src" data-src="https://.../authors/x.jpg"/>')
    # Selector requires src= for the substring match, so the element
    # isn't matched in the first place.
    assert _extract_author_photo(soup) is None


# ─── 7. Other Goodreads photo namespaces don't false-match ─────


def test_user_photos_namespace_not_picked():
    """Goodreads has user-uploaded photos under `/photos/user/...`. The
    selector requires `/authors/` substring specifically; user photos
    don't qualify, so a stray `<img>` from a sidebar review list etc.
    doesn't get misread as an author photo."""
    soup = _soup(
        '<img alt="Some User" src="https://images.gr-assets.com/photos/user/u_42.jpg"/>'
    )
    assert _extract_author_photo(soup) is None
