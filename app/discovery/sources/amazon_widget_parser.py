"""
Amazon Author-Store widget parser — pure parsing, no I/O.

Extracts structured book data from two surfaces:

  1. The HTML response of ``GET /stores/author/{author_id}/allbooks``
     (the SSR page Amazon serves on the initial author-store visit).

     The page embeds a `"content":{...}` JSON blob (~825 KB) inside a
     larger JavaScript bootstrap, keyed off ``"widgetType":"ProductGrid"``
     and ``"sectionType":"AuthorAllBooksProductGrid"``. We extract:

       - The 85ish *fully populated* products on page 1 (each carries
         a `mediaMatrix.items[]` cross-reference to every format
         variant ASIN for the same canonical work)
       - The 112-entry `ASINList` (page 1's full ASIN slice, including
         the ~27 unpopulated entries the client backfills via /juvec)
       - `totalResultCount` (filtered) and `totalCount` (overall)
       - CSRF + session tokens (`slateToken`, `freshCartCsrfToken`,
         `amazonApiCsrfToken`, `visitId`) needed to POST /juvec
       - pageContext UUIDs (`storeId`, `rootPageId`, `version`,
         `authorId`) echoed into every /juvec request body

  2. The JSON response of ``POST /juvec`` (the AJAX endpoint that
     hydrates remaining ASINs as the client scrolls, and re-runs the
     query under different filters).

     Two request shapes are observed (see Stage 5++ memory notes):

       - **Filter-application**: body has ``authorSearch`` (with
         ``page``, ``pageSize``, ``sort``) + ``authorFilters`` (format
         / language) and NO ASINList. The server returns a fresh,
         filtered ASINList + the first batch of populated products.
       - **Detail-fetch**: body has ``ASINList`` (≤16 ASINs from a
         previously-returned page) + ``authorFilters`` context. The
         server returns only the populated products for those ASINs.

     The response shape was inferred from the embedded content JSON
     and validated by the Stage 5++ live probe in commit 4. Both
     shapes return a ``content`` block matching the SSR widget's
     schema.

This module knows nothing about HTTP, BookResult, or the discovery
pipeline. The AmazonAuthorStoreSource (commit 5) drives I/O and
converts the structured Product dataclasses into BookResult rows.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

_log = logging.getLogger("seshat.discovery.amazon.parser")


# ─── Public dataclasses ──────────────────────────────────────────


@dataclass(frozen=True)
class FormatVariant:
    """One entry from a product's mediaMatrix — a sibling-format
    variant of the same canonical work."""
    binding_symbol: str  # "kindle_edition" / "hardcover" / "paperback" / etc.
    binding_display: str  # human-readable, e.g. "Audiobook, Unabridged"
    asin: str  # the format variant's own ASIN


@dataclass(frozen=True)
class ContributorInfo:
    """One byLine contributor with its role + identity, parsed from the
    Author-Store product-grid widget (v3.0.0 Phase 3.4).

    The widget byLine carries everything the role-filter needs WITHOUT a
    detail-page fetch: a `roles` array (`{displayString,type}`), the
    contributor's Amazon author ASIN (in the `contributor.author` path),
    and an author-media image link. ``role`` follows the `Contributor`
    convention — None for the plain author role, the non-author role's
    displayString (e.g. "Illustrator", "Translator") otherwise — so the
    downstream `contributor_is_author` allowlist drops non-authors. The
    ASIN + image are captured for the v3.x author-ID enrichment arc.
    """
    name: str
    role: str | None = None
    author_id: str | None = None  # Amazon author ASIN, e.g. B001IGFHW6
    image_url: str | None = None


@dataclass(frozen=True)
class Product:
    """One book entry parsed from Amazon's product-grid widget."""
    asin: str
    title: str
    contributors: tuple[str, ...]  # author names from byLine (legacy: names only)
    binding_symbol: str  # e.g. "kindle_edition"
    binding_display: str  # e.g. "Kindle Edition"
    series_title: str | None
    series_position: int | None
    series_total: int | None
    detail_page_link: str  # relative URL like /Mistborn-…/dp/B002GYI9C4
    cover_url: str | None  # hi-res image URL on m.media-amazon.com
    media_matrix: tuple[FormatVariant, ...]  # sibling formats
    genres: tuple[str, ...]
    # v3.0.0 Phase 3.4 — byLine contributors with role + Amazon author
    # ASIN + image. Trailing default so existing Product() construction
    # sites (and test helpers) stay valid; the live parser always sets it.
    contributor_details: tuple[ContributorInfo, ...] = ()


@dataclass(frozen=True)
class AllBooksPageData:
    """Everything we need from one GET of /stores/author/.../allbooks."""
    author_id: str
    store_id: str
    root_page_id: str
    version: str
    slate_token: str
    fresh_cart_csrf_token: str
    amazon_api_csrf_token: str
    visit_id: str
    obfuscated_marketplace_id: str
    asin_list: tuple[str, ...]  # full page-1 ASIN slice (~112)
    products: tuple[Product, ...]  # ~85 populated on page 1
    total_result_count: int
    total_count: int
    available_languages: tuple[str, ...]
    sort_options: tuple[dict, ...] = field(default_factory=tuple)
    # Raw content blob retained for debugging + future field additions
    # without forcing a parser rev. Not part of stable contract.
    raw_content: dict = field(default_factory=dict)


@dataclass(frozen=True)
class JuvecResponse:
    """Parsed /juvec POST response.

    Field semantics depend on which request shape was sent:
      - Filter-application: `asin_list` carries the filtered ASIN set;
        `products` is the first batch of populated entries.
      - Detail-fetch: `products` carries only the requested ASINs;
        `asin_list` may be empty or unchanged.
    """
    products: tuple[Product, ...]
    asin_list: tuple[str, ...]
    total_result_count: int | None
    raw_content: dict = field(default_factory=dict)


# ─── Public entry points ─────────────────────────────────────────


class ParseError(ValueError):
    """The HTML / response JSON didn't contain the expected widget
    structure. Caller should log and treat as empty result."""


class SoftBlockSuspectedError(ParseError):
    """A v2.20.2 narrower subclass: the page didn't contain the
    ProductGrid marker AT ALL, which means we weren't served an
    author-store page in the first place — most likely a CAPTCHA
    interstitial or other Akamai soft-block response that came
    through with a 200 OK + non-trivial body (so the size-based
    thin-body check in the source layer didn't catch it).

    The source layer (`_fetch_allbooks` in `sources/amazon.py`)
    upgrades this signal to a soft-block cooldown — same treatment
    as a 429 or sub-50KB response — so subsequent author scans
    short-circuit instead of cascading into the same wall.

    A plain `ParseError` (marker present but `content` malformed)
    is treated as a one-off parser failure, not a soft-block, since
    it suggests Amazon shipped an HTML schema change rather than
    blocked us."""


def parse_allbooks_html(html: str) -> AllBooksPageData:
    """Parse the SSR HTML of /stores/author/{id}/allbooks.

    Raises ParseError if the expected widget JSON isn't found.
    """
    content = _extract_product_grid_content(html)
    # pageContext is embedded as a JSON object; its `storeId` /
    # `rootPageId` / `version` / `authorId` fields are the values the
    # /juvec POST body needs verbatim. NOTE: there's another `storeId`
    # in `nexusLoggingInfo` that refers to what pageContext calls
    # `rootPageId` — Amazon's bootstrap naming is internally
    # inconsistent. Always extract from pageContext for /juvec.
    page_ctx = _extract_page_context(html)

    slate = _extract_string_token(html, "slateToken") or ""
    fresh = _extract_string_token(html, "freshCartCsrfToken") or ""
    api_csrf = _extract_string_token(html, "amazonApiCsrfToken") or ""
    visit_id = _extract_string_token(html, "visitId") or ""
    marketplace = _extract_string_token(html, "obfuscatedMarketplaceId") or ""

    products = _parse_products(content.get("products") or [])
    asin_list = tuple(str(a) for a in (content.get("ASINList") or []))

    return AllBooksPageData(
        author_id=str(page_ctx.get("authorId") or ""),
        store_id=str(page_ctx.get("storeId") or ""),
        root_page_id=str(page_ctx.get("rootPageId") or ""),
        version=str(page_ctx.get("version") or ""),
        slate_token=slate,
        fresh_cart_csrf_token=fresh,
        amazon_api_csrf_token=api_csrf,
        visit_id=visit_id,
        obfuscated_marketplace_id=marketplace,
        asin_list=asin_list,
        products=products,
        total_result_count=int(content.get("totalResultCount") or 0),
        total_count=int(content.get("totalCount") or 0),
        available_languages=tuple(content.get("languageFilter") or []),
        sort_options=tuple(content.get("sortOptions") or []),
        raw_content=content,
    )


def parse_juvec_response(json_body: dict) -> JuvecResponse:
    """Parse a /juvec POST response.

    Validated 2026-05-13 against the live endpoint:
    the response carries `products`, `ASINList`, `totalResultCount`,
    `totalCount`, `isSuccess`, `allProductsReturned` etc. at the
    TOP LEVEL of the JSON body. There IS a `content` key in the
    response but it's just an echo of the request's `content` field
    (`{"includeOutOfStock": true}`) — NOT a wrapper around the data.

    We require at least one of the expected widget-shape fields to
    be present at top level. If a future Amazon rev wraps the data
    under a different key (e.g. `data`, `result`), this will raise
    rather than silently return empty.
    """
    if not isinstance(json_body, dict):
        raise ParseError(
            f"/juvec response was not a JSON object "
            f"(type={type(json_body).__name__})"
        )
    expected = ("products", "ASINList", "totalResultCount", "totalCount")
    if not any(k in json_body for k in expected):
        raise ParseError(
            f"/juvec response has no expected widget fields at top level; "
            f"top-level keys = {sorted(json_body.keys())}"
        )
    # `isSuccess: False` is Amazon's explicit "we processed the request
    # but it failed" signal — surface as a parse error so the caller
    # falls back rather than silently treating an empty result as a
    # legitimate empty catalog.
    if json_body.get("isSuccess") is False:
        raise ParseError(
            f"/juvec response carried isSuccess=False; "
            f"correctedSearchKeywords={json_body.get('correctedSearchKeywords')!r}"
        )
    return JuvecResponse(
        products=_parse_products(json_body.get("products") or []),
        asin_list=tuple(str(a) for a in (json_body.get("ASINList") or [])),
        total_result_count=(
            int(json_body["totalResultCount"])
            if "totalResultCount" in json_body else None
        ),
        raw_content=json_body,
    )


# ─── Helpers ─────────────────────────────────────────────────────


_PRODUCT_GRID_MARKER = '"widgetType":"ProductGrid"'
_CONTENT_PREFIX = '"content":{'
_PAGE_CONTEXT_PREFIX = '"pageContext":{'


def _extract_page_context(html: str) -> dict:
    """Locate `"pageContext":{...}` in the HTML and JSON-decode it.

    pageContext is the canonical source for the values the /juvec
    POST body needs (`storeId`, `rootPageId`, `version`, `authorId`,
    `pagePath`, `brandName`, etc.). It's embedded as a JSON object
    multiple times in the page bootstrap (~3-4 copies, all identical);
    we take the first.

    Other places in the HTML expose subsets of these fields with
    INCONSISTENT names — e.g. `nexusLoggingInfo` has a `storeId` that
    actually corresponds to pageContext's `rootPageId`. Always extract
    via this helper to avoid that pitfall.

    Raises ParseError if not found or unparseable.
    """
    pos = html.find(_PAGE_CONTEXT_PREFIX)
    if pos < 0:
        raise ParseError("pageContext marker not found in allbooks HTML")
    open_brace = pos + len(_PAGE_CONTEXT_PREFIX) - 1
    end = _scan_balanced_brace(html, open_brace)
    raw = html[open_brace:end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParseError(f"pageContext JSON decode failed: {exc}")


def _extract_product_grid_content(html: str) -> dict:
    """Locate the ProductGrid widget's `content` JSON in the HTML and
    JSON-decode it via balanced-brace walking.

    The widget JSON is embedded inside a larger JavaScript bootstrap;
    we can't blindly regex it because nested braces inside strings
    would mismatch. Walk `{` and `}` from the opening brace, ignoring
    those inside JSON-encoded string literals.
    """
    grid_pos = html.find(_PRODUCT_GRID_MARKER)
    if grid_pos < 0:
        # v2.20.2 — surface a soft-block-suspected subclass so the
        # source layer can trip the IP-level cooldown. The marker is
        # what tells us we're on the author-store page in the first
        # place; its absence almost always means we were served a
        # CAPTCHA / soft-block response that snuck past the size-based
        # thin-body check (Amazon's soft-block pages CAN exceed 50KB
        # when they bundle the full site chrome).
        raise SoftBlockSuspectedError(
            "ProductGrid marker not found in allbooks HTML — page may be "
            "a non-author-store, a Captcha shim, or a thin-body soft-block"
        )
    content_pos = html.find(_CONTENT_PREFIX, grid_pos)
    if content_pos < 0:
        raise ParseError(
            "ProductGrid widget present but no `\"content\":{` follows it"
        )

    open_brace = content_pos + len(_CONTENT_PREFIX) - 1
    end = _scan_balanced_brace(html, open_brace)
    raw = html[open_brace:end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParseError(f"ProductGrid content JSON decode failed: {exc}")


def _scan_balanced_brace(s: str, open_idx: int) -> int:
    """Return the index of the `}` that closes the `{` at `open_idx`.

    Respects JSON string literals: ignores braces inside `"..."` and
    handles `\\"` escape correctly. Raises ParseError if EOF hits
    before depth returns to zero.
    """
    if open_idx >= len(s) or s[open_idx] != "{":
        raise ParseError(f"_scan_balanced_brace: char at {open_idx} not `{{`")
    depth = 0
    in_str = False
    escape = False
    for i in range(open_idx, len(s)):
        c = s[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i
    raise ParseError("ran out of input before finding closing brace")


_TOKEN_PATTERN_CACHE: dict[str, re.Pattern] = {}


def _extract_string_token(html: str, key: str) -> str | None:
    """Find the first occurrence of `"<key>":"<value>"` in the HTML
    and return the unescaped value. Returns None if not found.

    Multiple copies of each token are present (Amazon's bootstrap
    re-emits them per widget); we always take the first. They're
    identical when re-emitted.
    """
    pat = _TOKEN_PATTERN_CACHE.get(key)
    if pat is None:
        pat = re.compile(rf'"{re.escape(key)}":"((?:[^"\\]|\\.)*)"')
        _TOKEN_PATTERN_CACHE[key] = pat
    m = pat.search(html)
    if m is None:
        return None
    raw = m.group(1)
    # JSON-style backslash unescape (handles \", \\, \/, \n, etc.)
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        # Fallback to literal if the token value contains exotic
        # escapes; we still return something usable.
        return raw


def _parse_byline_contributor(c: object) -> ContributorInfo | None:
    """Parse one byLine contributor node into a ``ContributorInfo``.

    v3.0.0 Phase 3.4. The node shape (confirmed against a live allbooks
    payload) is::

        {"name": "Brandon Sanderson",
         "roles": [{"displayString": "Author", "type": "author"}],
         "contributor": {"author": "/marketplaces/.../authors/B001IGFHW6"},
         "links": [..., {"url": ".../amzn-author-media-prod/o1ehb….jpg"}]}

    Role rule (matches the locked allowlist): ``role`` is None when the
    node has no roles or *every* role is type "author"; otherwise it is
    the first non-author role's displayString (so the downstream
    `contributor_is_author` filter drops it). Returns None for a node
    with no usable name.
    """
    if not isinstance(c, dict):
        return None
    name = (c.get("name") or "").strip()
    if not name:
        return None

    roles = c.get("roles") or []
    non_author = [
        (r.get("displayString") or "").strip()
        for r in roles
        if isinstance(r, dict) and (r.get("type") or "").strip().lower() != "author"
    ]
    non_author = [d for d in non_author if d]
    role = non_author[0] if non_author else None

    # Amazon author ASIN — last path segment of the contributor reference.
    author_id = None
    cobj = c.get("contributor")
    if isinstance(cobj, dict):
        ref = next((v for v in cobj.values() if isinstance(v, str) and v), "")
        seg = ref.rstrip("/").rsplit("/", 1)[-1] if ref else ""
        author_id = seg or None

    # Author-media image link, if Amazon included one among the links.
    image_url = None
    for lk in c.get("links") or []:
        u = lk.get("url") if isinstance(lk, dict) else None
        if u and ("author-media" in u or u.lower().rsplit("?", 1)[0].endswith((".jpg", ".jpeg", ".png"))):
            image_url = u
            break

    return ContributorInfo(name=name, role=role, author_id=author_id, image_url=image_url)


def _parse_products(raw_products: list) -> tuple[Product, ...]:
    """Convert raw product dicts to immutable Product tuples.

    Defensive: skips entries missing critical fields (asin, title,
    bindingInformation) with a DEBUG log rather than raising — Amazon
    occasionally serves "placeholder" entries for ASINs the SSR
    rendered but couldn't fully populate, and we want the parse to
    continue past those.
    """
    out: list[Product] = []
    for raw in raw_products:
        if not isinstance(raw, dict):
            continue
        asin = raw.get("asin")
        if not asin:
            continue
        title_obj = raw.get("title") or {}
        title = title_obj.get("displayString") if isinstance(title_obj, dict) else None
        if not title:
            _log.debug("amazon parser: product %r missing title; skipping", asin)
            continue
        binding_info = (raw.get("bindingInformation") or {}).get("binding") or {}
        binding_symbol = binding_info.get("symbol")
        binding_display = binding_info.get("displayString") or ""
        if not binding_symbol:
            _log.debug(
                "amazon parser: product %r missing binding symbol; skipping",
                asin,
            )
            continue

        # ─ contributors (authors + roles + ids + images) ─
        by_line = raw.get("byLine") or {}
        contributors_raw = by_line.get("contributors") or []
        contributor_details: list[ContributorInfo] = []
        for c in contributors_raw:
            ci = _parse_byline_contributor(c)
            if ci is not None:
                contributor_details.append(ci)
        contributors = [ci.name for ci in contributor_details]  # legacy names-only view

        # ─ series info ─
        series_raw = raw.get("bookSeriesInfo")
        series_title: str | None = None
        series_position: int | None = None
        series_total: int | None = None
        if isinstance(series_raw, dict):
            series_title = series_raw.get("seriesTitle") or None
            sp = series_raw.get("position")
            st = series_raw.get("total")
            series_position = int(sp) if isinstance(sp, (int, float)) else None
            series_total = int(st) if isinstance(st, (int, float)) else None

        # ─ cover URL (prefer hi-res, fall back to low-res) ─
        cover_url: str | None = None
        images = (raw.get("productImages") or {}).get("images") or []
        if images and isinstance(images[0], dict):
            hi = images[0].get("hiRes") or {}
            lo = images[0].get("lowRes") or {}
            cover_url = hi.get("url") or lo.get("url") or None

        # ─ mediaMatrix variants ─
        variants: list[FormatVariant] = []
        media = raw.get("mediaMatrix") or {}
        for item in media.get("items") or []:
            if not isinstance(item, dict):
                continue
            ib = item.get("binding") or {}
            sym = ib.get("symbol")
            disp = ib.get("displayString") or ""
            product_path = item.get("product") or ""
            # product looks like /marketplaces/.../products/{ASIN}; strip
            v_asin = product_path.rsplit("/", 1)[-1] if product_path else ""
            if sym and v_asin:
                variants.append(FormatVariant(
                    binding_symbol=sym,
                    binding_display=disp,
                    asin=v_asin,
                ))

        # ─ genres (rare; usually [] in author-store payload) ─
        genres_raw = raw.get("genres") or []
        genres: list[str] = []
        for g in genres_raw:
            if isinstance(g, str):
                genres.append(g)
            elif isinstance(g, dict) and "displayString" in g:
                genres.append(g["displayString"])

        out.append(Product(
            asin=asin,
            title=title,
            contributors=tuple(contributors),
            contributor_details=tuple(contributor_details),
            binding_symbol=binding_symbol,
            binding_display=binding_display,
            series_title=series_title,
            series_position=series_position,
            series_total=series_total,
            detail_page_link=raw.get("detailPageLinkURL") or "",
            cover_url=cover_url,
            media_matrix=tuple(variants),
            genres=tuple(genres),
        ))
    return tuple(out)


# ─── Format ↔ binding mapping ────────────────────────────────────


# Server-side filter input value (lowercase, may differ from output
# binding symbol). Sent in `authorFilters.format` of /juvec POST body.
#
# v2.11.1: audio-binding filters added so AmazonSource can serve
# audiobook discovery scans. `audible_audiobook` is the dominant
# audio format (matches Audible-distributed digital audiobooks
# Amazon sells); `audio_cd` / `mp3_cd` / `preloaded_digital_audio`
# are the niche physical / hardware-bound variants.
#
# The filter input → binding symbol mapping is NOT 1:1 — Amazon's
# /juvec accepts the filter-input value on the request side and
# emits the binding.symbol value on the response side. Examples:
#   filter "kindle" → binding "kindle_edition"
#   filter "audible_audiobook" → binding "audio_download"
#   filter "audio_cd" → binding "audioCD"
FILTER_TO_BINDING: dict[str, str] = {
    # Ebook formats
    "kindle": "kindle_edition",
    "paperback": "paperback",
    "hardcover": "hardcover",
    "mass_market": "mass_market",
    # Audiobook formats (v2.11.1)
    "audible_audiobook": "audio_download",
    "audio_cd": "audioCD",
    "mp3_cd": "mp3_cd",
    "preloaded_digital_audio": "preloaded_digital_audio_player",
}

# Reverse map for converting binding symbol back to filter input value
# when round-tripping through Settings UI / API.
BINDING_TO_FILTER: dict[str, str] = {v: k for k, v in FILTER_TO_BINDING.items()}

# Ebook-vs-audiobook split — the AmazonExtrasRow dropdowns split
# the filter options by tab. The discovery dispatcher also uses
# this to pick which `format_filter` field on the source to read
# based on the active scan's content type.
EBOOK_FILTERS: frozenset[str] = frozenset({
    "kindle", "paperback", "hardcover", "mass_market",
})
AUDIOBOOK_FILTERS: frozenset[str] = frozenset({
    "audible_audiobook", "audio_cd", "mp3_cd", "preloaded_digital_audio",
})


# Languages observed in Sanderson's `content.languageFilter`. Used as
# the static default list in Settings UI when no live snapshot is
# available; the actual list per-author is sourced from
# AllBooksPageData.available_languages on each scan.
DEFAULT_LANGUAGES: tuple[str, ...] = (
    "English", "Spanish", "German", "French", "Italian",
    "ChineseSimplified", "ChineseTraditional", "Japanese", "Portuguese",
    "Russian", "Polish", "Turkish", "Danish", "Greek", "Romanian",
    "Catalan", "Serbian", "MiddleEnglish",
)
