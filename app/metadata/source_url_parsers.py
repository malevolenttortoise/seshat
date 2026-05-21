"""
Per-source author-identifier parsers + canonical URL builders (v2.20.0
Phase 3).

The source-ID badge UI on the author detail page lets the user paste
either a bare ID or a full URL into an edit modal; the backend
canonicalizes both forms into the stable ID Seshat stores in the
per-library `authors.{source}_id` column. This module owns that
parsing for every web source Seshat tracks at the author level.

Parser shape
------------
Each `parse_{source}(value: str) -> str | None` accepts either a bare
ID or a URL containing the ID, and returns the canonical ID string.
Whitespace is trimmed, casing normalized where the source's IDs are
case-stable, garbage rejected with None.

URL-builder shape
-----------------
`canonical_author_url(source, value)` returns the user-facing URL for
an ID — used by the badge component to render the "open author page"
link. None when the source doesn't expose a stable author URL (Google
Books, Kobo, IBDb, AudiobookShelf — these all lack first-class author
pages and our ID for them is either the name itself or a library-
local UUID).

Sources covered
---------------
Web sources with parseable URLs:
  - amazon       (B0XXXXXXXX ASIN)
  - goodreads    (numeric)
  - openlibrary  (OLxxxxxxA)
  - hardcover    (integer; URL pattern slug-based, ID is API-side)
  - audible      (B0XXXXXXXX ASIN)

Web sources without parseable author URLs (accept raw values):
  - kobo         (slug or name)
  - ibdb         (numeric, no public author page)
  - google_books (no first-class author page)

Library-local (NOT mirrorable, but parser exists for completeness):
  - audiobookshelf (UUID)
"""
from __future__ import annotations

import re
from typing import Callable, Optional


# ─── Amazon ──────────────────────────────────────────────────


_AMAZON_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_AMAZON_URL_RES: list[re.Pattern] = [
    # /stores/<anything>/author/B0XXXXXXXX (canonical author landing)
    re.compile(
        r"/stores/[^/]+/author/(?P<id>[A-Z0-9]{10})",
        re.IGNORECASE,
    ),
    # /<slug>/e/B0XXXXXXXX or /-/e/B0XXXXXXXX (legacy author link)
    re.compile(
        r"/[^/]+/e/(?P<id>[A-Z0-9]{10})",
        re.IGNORECASE,
    ),
    # /marketplaces/<m>/contributors/authors/B0XXXXXXXX (SSR JSON path)
    re.compile(
        r"/marketplaces/[A-Z0-9]+/contributors/authors/(?P<id>[A-Z0-9]{10})",
        re.IGNORECASE,
    ),
    # /dp/B0XXXXXXXX (book DP — defensive; user may paste a book URL
    # by mistake. We accept it and let the UI confirm before saving.)
    re.compile(
        r"/(?:dp|gp/product)/(?P<id>[A-Z0-9]{10})",
        re.IGNORECASE,
    ),
    # /author/B0XXXXXXXX (some vanity link variants)
    re.compile(
        r"/author/(?P<id>[A-Z0-9]{10})",
        re.IGNORECASE,
    ),
]


def parse_amazon(value: str) -> Optional[str]:
    """Accept a B0XXXXXXXX ASIN or any Amazon URL containing one.
    Returns the upper-case ASIN, None on no match."""
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    # Bare ID first (no slash → not a URL).
    if "/" not in cleaned and _AMAZON_ASIN_RE.match(cleaned.upper()):
        return cleaned.upper()
    for pat in _AMAZON_URL_RES:
        m = pat.search(cleaned)
        if m:
            return m.group("id").upper()
    return None


# ─── Goodreads ───────────────────────────────────────────────


_GOODREADS_AUTHOR_URL_RE = re.compile(
    r"/author/show/(?P<id>\d+)",
    re.IGNORECASE,
)


def parse_goodreads(value: str) -> Optional[str]:
    """Accept a numeric Goodreads author ID, or a URL like
    `/author/show/14905104` (optionally followed by `.Display_Name`).
    Returns the numeric ID string, None on no match."""
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.isdigit():
        return cleaned
    m = _GOODREADS_AUTHOR_URL_RE.search(cleaned)
    if m:
        return m.group("id")
    return None


# ─── Open Library ────────────────────────────────────────────


_OPENLIBRARY_ID_RE = re.compile(r"^OL\d+A$", re.IGNORECASE)
_OPENLIBRARY_URL_RE = re.compile(
    r"/authors/(?P<id>OL\d+A)",
    re.IGNORECASE,
)


def parse_openlibrary(value: str) -> Optional[str]:
    """Accept an OL...A author key, or a URL like
    `openlibrary.org/authors/OL26320A` (optionally followed by
    `/Display_Name`). Returns the upper-case key, None on no match."""
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if _OPENLIBRARY_ID_RE.match(cleaned):
        return cleaned.upper()
    m = _OPENLIBRARY_URL_RE.search(cleaned)
    if m:
        return m.group("id").upper()
    return None


# ─── Hardcover ───────────────────────────────────────────────


_HARDCOVER_ID_RE = re.compile(r"^\d+$")
_HARDCOVER_URL_RE = re.compile(
    r"/authors/(?P<slug>[^/?#]+)",
    re.IGNORECASE,
)


def parse_hardcover(value: str) -> Optional[str]:
    """Accept a numeric Hardcover author ID, or a hardcover.app URL.
    Hardcover URLs use a slug, not the numeric ID — so URL paste only
    works when the user pastes the ID. We still accept the slug as a
    fallback string so users who paste `/authors/brandon-sanderson`
    can save SOMETHING; backend can later resolve the slug to numeric
    via the Hardcover GraphQL API.

    Returns the numeric ID when value is digits, or the slug when value
    is a URL. None on no match."""
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if _HARDCOVER_ID_RE.match(cleaned):
        return cleaned
    m = _HARDCOVER_URL_RE.search(cleaned)
    if m:
        return m.group("slug")
    return None


# ─── Audible ─────────────────────────────────────────────────


_AUDIBLE_AUTHOR_URL_RE = re.compile(
    r"/author/[^/]+/(?P<id>[A-Z0-9]{10})",
    re.IGNORECASE,
)


def parse_audible(value: str) -> Optional[str]:
    """Accept a B0XXXXXXXX Audible author ASIN, or an
    `audible.com/author/<slug>/B0XXXXXXXX` URL. Same 10-char ASIN
    format as Amazon. Returns the upper-case ASIN, None on no match."""
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if "/" not in cleaned and _AMAZON_ASIN_RE.match(cleaned.upper()):
        return cleaned.upper()
    m = _AUDIBLE_AUTHOR_URL_RE.search(cleaned)
    if m:
        return m.group("id").upper()
    # Fall through: a paste of `https://audible.com/.../<asin>` without
    # the `/author/` prefix can still surface the trailing ASIN.
    m = re.search(r"/(?P<id>[A-Z0-9]{10})(?:[/?#]|$)", cleaned)
    if m and _AMAZON_ASIN_RE.match(m.group("id").upper()):
        return m.group("id").upper()
    return None


# ─── Sources without a parseable canonical URL ───────────────


def parse_kobo(value: str) -> Optional[str]:
    """Kobo doesn't expose a stable author page; the ID Seshat stores
    is either a slug or the author's name. We accept anything non-
    empty and trim it. Returns the trimmed value, None when empty."""
    if not value:
        return None
    cleaned = value.strip()
    return cleaned or None


def parse_ibdb(value: str) -> Optional[str]:
    """IBDb (iblist.com) doesn't expose a clean public author page.
    Accept numeric IDs primarily; also tolerate `?id=<n>` URL fragments
    in case a user pastes a result URL."""
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if _HARDCOVER_ID_RE.match(cleaned):  # any digit string
        return cleaned
    m = re.search(r"[?&]id=(?P<id>\d+)", cleaned)
    if m:
        return m.group("id")
    return None


def parse_google_books(value: str) -> Optional[str]:
    """Google Books has no first-class author page; the ID Seshat
    stores tends to be the author's name or a Books search slug.
    Accept anything non-empty trimmed."""
    if not value:
        return None
    return value.strip() or None


def parse_fictiondb(value: str) -> Optional[str]:
    """FictionDB IDs are numeric. Public author URL is
    `fictiondb.com/author/<slug>~<id>.htm` — we extract the trailing
    numeric ID from URL form, or accept a bare number."""
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.isdigit():
        return cleaned
    m = re.search(r"~(?P<id>\d+)\.htm", cleaned, re.IGNORECASE)
    if m:
        return m.group("id")
    return None


def parse_audiobookshelf(value: str) -> Optional[str]:
    """AudiobookShelf author IDs are library-local UUIDs. Defensive
    parser provided for completeness; in practice this column should
    never be edited through the source-ID badge UI (it's not in
    MIRRORABLE_SOURCE_ID_COLUMNS). Accept UUID-shaped strings."""
    if not value:
        return None
    cleaned = value.strip().lower()
    if re.match(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        cleaned,
    ):
        return cleaned
    return None


# ─── Source registry + canonical URL builder ─────────────────


# (source_key, parser, url_builder_or_none)
_REGISTRY: dict[str, tuple[Callable[[str], Optional[str]],
                            Optional[Callable[[str], str]]]] = {
    "amazon": (
        parse_amazon,
        lambda v: f"https://www.amazon.com/stores/author/{v}/allbooks",
    ),
    "goodreads": (
        parse_goodreads,
        lambda v: f"https://www.goodreads.com/author/show/{v}",
    ),
    "openlibrary": (
        parse_openlibrary,
        lambda v: f"https://openlibrary.org/authors/{v}",
    ),
    "hardcover": (
        parse_hardcover,
        # Numeric ID has no canonical URL (Hardcover routes by slug);
        # slug-form gets a URL. Detect by whether the value is all
        # digits.
        lambda v: (
            f"https://hardcover.app/authors/{v}" if not v.isdigit() else ""
        ),
    ),
    "audible": (
        parse_audible,
        lambda v: f"https://www.audible.com/author/-/{v}",
    ),
    "kobo": (parse_kobo, None),
    "ibdb": (parse_ibdb, None),
    "google_books": (parse_google_books, None),
    "fictiondb": (parse_fictiondb, None),
    "audiobookshelf": (parse_audiobookshelf, None),
}


def parse_source_id(source: str, value: str) -> Optional[str]:
    """Dispatch parser by source name. Returns canonical ID or None.

    Raises ValueError when `source` is unknown — caller's mistake;
    the badge UI only ever submits known source keys.
    """
    entry = _REGISTRY.get(source)
    if entry is None:
        raise ValueError(f"unknown source: {source!r}")
    parser, _ = entry
    return parser(value)


def canonical_author_url(source: str, value: str) -> Optional[str]:
    """Build the user-facing author URL for an ID. Returns None when
    the source doesn't expose a canonical author page, or the value
    doesn't have one (e.g., Hardcover with a bare numeric ID)."""
    entry = _REGISTRY.get(source)
    if entry is None:
        return None
    _, builder = entry
    if builder is None or not value:
        return None
    url = builder(value)
    return url or None


def known_sources() -> list[str]:
    """List of all source keys this module supports."""
    return list(_REGISTRY.keys())
