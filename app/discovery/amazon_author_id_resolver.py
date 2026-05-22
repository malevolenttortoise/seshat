"""
Amazon Author Store ID resolver — maps {author_name} → {author_id}
(v2.11.0 Stage 5++).

The Author Store ID is the stable identifier Amazon assigns to each
author's branded store page. Looks like ``B001IGFHW6`` (Sanderson)
and appears in URLs:

    /stores/author/B001IGFHW6/allbooks
    /Brandon-Sanderson/e/B001IGFHW6
    /-/e/B001IGFHW6
    /marketplaces/ATVPDKIKX0DER/contributors/authors/B001IGFHW6

AmazonAuthorStoreSource (discovery) needs this ID to drive the
``/stores/author/{id}/allbooks`` GET. We cache the result on
``authors.amazon_id`` once resolved.

Two-tier resolution, first hit wins:

Tier 1 — Existing-book pivot
    If we already have any book for this author with ``books.amazon_id``
    set (e.g. URL-paste import set it), GET that book's ``/dp/{asin}``
    detail page and extract the byLine contributor link. Cheap + exact.

Tier 2 — Search fallback
    GET ``/s?k={author_name}&i=stripbooks``. Parse author-byline
    anchors carrying ``/-/e/{id}`` or ``/Author-Slug/e/{id}`` patterns
    out of every book card. Group by ID, pick the one whose slug
    decodes to the closest match to the queried name. Common-name
    collisions (multiple authors named "John Smith") fall back to a
    best-effort first match with a WARNING log.

Both tiers run behind curl_cffi Chrome-120 impersonation, the same
Akamai bypass the rest of AmazonSource uses (shipped Stage 5+).
"""
from __future__ import annotations

import logging
import re
import time
import urllib.parse
from typing import Any

logger = logging.getLogger("seshat.discovery.amazon.author_id_resolver")


# ─── IP-level soft-block penalty box (shared with AmazonSource) ──
#
# When amazon.com returns 429, an Akamai sensor challenge (HTTP 202),
# or a CAPTCHA-shaped thin body, Akamai has put our IP in penalty box
# at the IP level — every subsequent amazon.com request will fail
# until the box clears. Without this, the failure cascades across
# author scans: on 2026-05-20, Hanako Arashi 429'd at 16:17, then
# Mark Arrows 429'd 2 min later with an identical 2296-byte CAPTCHA
# body, because nothing told the next scan the IP was jailed. The
# penalty box stops the cascade by short-circuiting further
# amazon.com calls until the cooldown expires (or until Amazon's
# Retry-After header says to retry).
#
# v2.20.3 — persisted to settings.json (runtime-state keys, protected
# from PATCH) so container restarts don't wipe the cooldown and let
# the next scan walk straight into a fresh penalty. The earlier "wall-
# clock timestamp is enough" comment was wrong: zeroing `_blocked_until`
# on import means `is_amazon_blocked()` returns False until the next
# block is recorded, which is exactly the bug we're closing.

_BLOCK_COOLDOWN_DEFAULT_S = 600.0  # 10 min — matches Akamai's typical CAPTCHA TTL
_BLOCK_COOLDOWN_MIN_S = 60.0       # never less than 1 min
_BLOCK_COOLDOWN_MAX_S = 3600.0     # never more than 1 hour

# Below this body size on a 2xx response, Akamai is almost certainly
# returning a CAPTCHA / sensor-challenge interstitial rather than a real
# Amazon page (real /dp, /s, /author pages weigh 200KB+). Used by the
# 202-detection and thin-body branches; pulled out here so the v2.21.0
# worker classifier can read the same constant.
_AMAZON_SOFT_BLOCK_THIN_BODY_BYTES = 50_000

_blocked_until: float = 0.0  # wall-clock epoch seconds; 0.0 = not blocked
_block_reason: str = ""
_block_count: int = 0


# Runtime-state keys in settings.json mirror `goodreads_session_state*`.
# `_RUNTIME_STATE_KEYS` in `app/routers/settings.py` lists these so a
# user PATCH can't accidentally clear an active cooldown.
_SETTINGS_KEY_BLOCKED_UNTIL = "amazon_blocked_until"
_SETTINGS_KEY_BLOCK_REASON = "amazon_block_reason"
_SETTINGS_KEY_BLOCKED_SINCE = "amazon_blocked_since"


def _persist_block_state(*, was_already_blocked: bool) -> None:
    """Mirror `_blocked_until` / `_block_reason` into settings.json.

    Called from `record_amazon_soft_block` after the module globals are
    updated. Writes the wall-clock cooldown expiry, the reason string,
    and (on a fresh arm only) the "since" timestamp. Failure is logged
    but never raised: the in-memory cooldown still applies this process,
    so a write failure only compromises restart-survival.
    """
    try:
        from app.config import load_settings, save_settings
        s = dict(load_settings())
        s[_SETTINGS_KEY_BLOCKED_UNTIL] = _blocked_until
        s[_SETTINGS_KEY_BLOCK_REASON] = _block_reason
        if not was_already_blocked:
            # Fresh arm — stamp the start time. Extensions preserve the
            # original arm time so "blocked for Xs" reads naturally in
            # the UI.
            s[_SETTINGS_KEY_BLOCKED_SINCE] = time.time()
        save_settings(s)
    except Exception as exc:
        logger.warning(
            "amazon: failed to persist cooldown state to settings (%s) — "
            "cooldown applies this process but a restart will clear it",
            exc,
        )


def _load_persisted_block_state() -> None:
    """Restore the cooldown from settings.json at module import.

    If the persisted `amazon_blocked_until` is still in the future,
    re-arm the in-memory state so `is_amazon_blocked()` keeps the
    cascade off. Expired persisted state is ignored silently. Safe
    to call multiple times (idempotent — only extends a cooldown).
    """
    global _blocked_until, _block_reason
    try:
        from app.config import load_settings
        s = load_settings()
    except Exception:
        return
    persisted_until = s.get(_SETTINGS_KEY_BLOCKED_UNTIL)
    if not isinstance(persisted_until, (int, float)):
        return
    if persisted_until <= time.time():
        return  # cooldown expired during the downtime — nothing to restore
    if persisted_until > _blocked_until:
        _blocked_until = float(persisted_until)
        _block_reason = str(s.get(_SETTINGS_KEY_BLOCK_REASON) or "")
        logger.info(
            "amazon: restored soft-block cooldown from settings — "
            "%.0fs remaining (reason: %s)",
            amazon_block_remaining_s(),
            _block_reason or "<unknown>",
        )


# Restore any persisted cooldown on import so a container restart in
# the middle of an Akamai penalty doesn't bypass it. Wrapped so a
# settings.json read failure during startup never blocks import.
try:
    _load_persisted_block_state()
except Exception:
    pass


def is_amazon_blocked() -> bool:
    """True if amazon.com requests are currently in soft-block cooldown."""
    return _blocked_until > time.time()


def amazon_block_remaining_s() -> float:
    """Seconds remaining in the current penalty box (0 if not blocked)."""
    remaining = _blocked_until - time.time()
    return max(0.0, remaining)


def record_amazon_soft_block(
    reason: str,
    *,
    retry_after_s: float | None = None,
) -> None:
    """Activate the amazon.com soft-block cooldown.

    `retry_after_s` honors Amazon's Retry-After header when present
    (clamped to [60, 3600]). Falls back to the 10-min default when
    no header is provided or it doesn't parse cleanly.

    Idempotent: re-calling while already blocked refreshes the
    timestamp only if the new cooldown extends past the current one.
    """
    global _blocked_until, _block_reason, _block_count
    cooldown = (
        retry_after_s if retry_after_s is not None
        else _BLOCK_COOLDOWN_DEFAULT_S
    )
    cooldown = max(_BLOCK_COOLDOWN_MIN_S, min(_BLOCK_COOLDOWN_MAX_S, cooldown))
    new_until = time.time() + cooldown
    was_already_blocked = _blocked_until > time.time()
    # Only extend; never shorten a longer cooldown that's already in flight.
    if new_until > _blocked_until:
        _blocked_until = new_until
        _block_reason = reason
        _block_count += 1
        logger.warning(
            "amazon: soft-block cooldown activated (%.0fs) — reason: %s "
            "[block #%d this process]",
            cooldown, reason, _block_count,
        )
        _persist_block_state(was_already_blocked=was_already_blocked)


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header into seconds.

    Supports both forms RFC 7231 allows:
      - delta-seconds: ``"60"`` → 60.0
      - HTTP-date:     ``"Wed, 21 Oct 2026 07:28:00 GMT"`` → seconds-until

    Returns None on parse failure (caller falls back to the default
    cooldown)."""
    if not value:
        return None
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None


def _get_response_retry_after(resp: Any) -> float | None:
    """Pull the Retry-After header off a curl_cffi / httpx response.

    Returns parsed seconds, or None when the header is absent /
    unparseable. Defensive — different HTTP clients expose headers
    via slightly different attribute shapes."""
    headers = getattr(resp, "headers", None)
    if headers is None:
        return None
    # Both curl_cffi and httpx headers support case-insensitive .get().
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:
        raw = None
    return parse_retry_after(raw)


# ─── URL endpoints ───────────────────────────────────────────────


_DP_URL_TEMPLATE = "https://www.amazon.com/dp/{asin}"
_SEARCH_URL = "https://www.amazon.com/s"
# Amazon's author vanity-URL: `/author/{normalized_name}` 301-redirects
# to `/stores/{Display-Name}/author/{author_id}` when the normalized
# name matches an indexed author. Works for any author (Kindle-only,
# print, mainstream, indie) — single request, no result-page parsing,
# no disambiguation needed because the answer is in the redirect URL
# itself. Validated 2026-05-13 for B01AY7PSG4 (Arand), B001IGFHW6
# (Sanderson). 404 when no match — falls through to the search tier.
_VANITY_URL_TEMPLATE = "https://www.amazon.com/author/{slug}"
_VANITY_REDIRECT_RE = __import__("re").compile(
    r'/stores/[^/]+/author/(?P<id>[A-Z0-9]{10})'
)


# ─── Author-ID extraction patterns ───────────────────────────────


# Match `/<slug>/e/<id>` or `/-/e/<id>` URL paths. Captures both the
# slug and the ID. The ID is the 10-char Amazon Standard Identifier
# (Author flavour). The slug is "-" for canonical short-form links
# (`/-/e/B001IGFHW6`) and a name-derived slug for the long form
# (`/Brandon-Sanderson/e/B001IGFHW6`). Accepts trailing `?` or `"` or
# whitespace so we don't over-greedy-match.
_AUTHOR_LINK_RE = re.compile(
    r'/(?P<slug>[^/"\s]+)/e/(?P<id>[A-Z0-9]{10})(?:[?"/\s&]|$)'
)

# Match the JSON-embedded author path in byLine.contributor.author:
#     /marketplaces/ATVPDKIKX0DER/contributors/authors/B001IGFHW6
# This is the most authoritative source — appears in the SSR JSON
# payload on /dp/{asin} pages with the productGrid widget loaded.
_CONTRIBUTOR_PATH_RE = re.compile(
    r'/marketplaces/[A-Z0-9]+/contributors/authors/(?P<id>[A-Z0-9]{10})'
)


# ─── Tier 1: existing-book pivot ─────────────────────────────────


async def _tier1_book_pivot(
    asin: str,
    *,
    session: Any,
    timeout: float,
) -> str | None:
    """GET /dp/{asin}, extract the author ID from byLine markup.

    Returns the author ID on success, None on any failure (network
    error, page parse miss, Akamai soft-block). Caller falls through
    to Tier 2.
    """
    if is_amazon_blocked():
        logger.info(
            "tier1: SKIP %s — amazon.com is in soft-block cooldown "
            "(%.0fs remaining)", asin, amazon_block_remaining_s(),
        )
        return None
    url = _DP_URL_TEMPLATE.format(asin=asin)
    try:
        resp = await session.get(url, timeout=timeout)
    except Exception as exc:  # network, TLS, etc. — log + fall through
        logger.debug("tier1: GET %s failed: %s", url, exc)
        return None

    status = getattr(resp, "status_code", None)
    body = getattr(resp, "text", None) or ""
    # v2.19.0 — explicit 429 → trip the IP-level penalty box so the
    # next author scan doesn't walk straight into the same wall.
    if status == 429:
        record_amazon_soft_block(
            f"tier1 GET {url} returned HTTP 429",
            retry_after_s=_get_response_retry_after(resp),
        )
        return None
    # v2.20.3 — HTTP 202 is the Akamai sensor-challenge signature
    # (silent failure pre-v2.20.3: status != 200 logged but no cooldown
    # tripped, so a 202 cascade looked identical to "page not found").
    if status == 202:
        record_amazon_soft_block(
            f"tier1 GET {url} returned HTTP 202 with {len(body)}-byte "
            f"body (Akamai sensor challenge)",
            retry_after_s=_get_response_retry_after(resp),
        )
        return None
    if status != 200 or not body:
        logger.debug(
            "tier1: GET %s returned status=%s body_len=%d (no extract)",
            url, status, len(body),
        )
        return None
    # Akamai thin-body soft-block guard — real /dp pages are 200KB+
    if len(body) < _AMAZON_SOFT_BLOCK_THIN_BODY_BYTES:
        logger.warning(
            "tier1: GET %s thin body (%d bytes) — likely Akamai soft-block",
            url, len(body),
        )
        record_amazon_soft_block(
            f"tier1 GET {url} returned 200 with {len(body)}-byte body"
        )
        return None

    return _extract_author_id_from_html(body)


def _extract_author_id_from_html(html: str) -> str | None:
    """Try the JSON contributor path first (most authoritative), then
    fall back to anchor URLs (slug/e/id). Returns the first match."""
    m = _CONTRIBUTOR_PATH_RE.search(html)
    if m:
        return m.group("id")
    m = _AUTHOR_LINK_RE.search(html)
    if m:
        return m.group("id")
    return None


# ─── Tier 2: search fallback ─────────────────────────────────────


def _normalize_name(name: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace. Same
    normalization as the metadata sources use for cross-source name
    matching (matches the Kobo `_kobo_author_matches` pattern shipped
    in v2.10.6)."""
    s = name.lower()
    # Strip every char that isn't alphanumeric (drops periods,
    # commas, hyphens, apostrophes — "J. N. Chaney" / "J.N. Chaney"
    # both collapse to "jnchaney").
    return re.sub(r"[^a-z0-9]", "", s)


async def _tier2_vanity_url(
    author_name: str,
    *,
    session: Any,
    timeout: float,
) -> str | None:
    """GET /author/{normalized_name} and harvest the author_id from
    Amazon's 301-redirect target.

    The vanity URL redirects to `/stores/{Display-Name}/author/{id}`
    when Amazon's index has a matching author. Most reliable single-
    request resolution path; works for Kindle-only indies that the
    `/s?k=...&i=stripbooks` search doesn't surface.

    Returns the author ID on success, None on 404 / no redirect /
    no extractable ID. Caller falls through to /s search variants.
    """
    if is_amazon_blocked():
        logger.info(
            "tier2 vanity: SKIP %r — amazon.com is in soft-block cooldown "
            "(%.0fs remaining)", author_name, amazon_block_remaining_s(),
        )
        return None
    slug = _normalize_name(author_name)
    if not slug:
        return None
    url = _VANITY_URL_TEMPLATE.format(slug=slug)
    try:
        resp = await session.get(url, timeout=timeout, allow_redirects=True)
    except Exception as exc:
        logger.debug("tier2 vanity: GET %s failed: %s", url, exc)
        return None
    status = getattr(resp, "status_code", None)
    # v2.19.0 — explicit 429 → trip the penalty box. 404 stays a quiet
    # "slug not indexed" miss (no block recorded).
    if status == 429:
        record_amazon_soft_block(
            f"tier2 vanity GET {url} returned HTTP 429",
            retry_after_s=_get_response_retry_after(resp),
        )
        return None
    # v2.20.3 — HTTP 202 Akamai sensor challenge. The vanity endpoint
    # 301-redirects on hit and 404s on miss; a 202 reply has no
    # legitimate interpretation, so trip the cooldown.
    if status == 202:
        body = getattr(resp, "text", "") or ""
        record_amazon_soft_block(
            f"tier2 vanity GET {url} returned HTTP 202 with "
            f"{len(body)}-byte body (Akamai sensor challenge)",
            retry_after_s=_get_response_retry_after(resp),
        )
        return None
    if status != 200:
        # 404 expected when the slug isn't indexed; fall through quietly.
        logger.debug("tier2 vanity: %s returned status=%s", url, status)
        return None
    # The final URL (after redirects) is what carries the author ID.
    # curl_cffi exposes it via `resp.url`. Match against the
    # `/stores/.../author/{id}` portion.
    final_url = str(getattr(resp, "url", "") or "")
    m = _VANITY_REDIRECT_RE.search(final_url)
    if m:
        return m.group("id")
    # Belt-and-suspenders: the body may also contain the ID even if
    # the URL didn't redirect cleanly.
    body = getattr(resp, "text", "") or ""
    m = _VANITY_REDIRECT_RE.search(body)
    if m:
        return m.group("id")
    return None


async def _tier2_search(
    author_name: str,
    *,
    session: Any,
    timeout: float,
) -> str | None:
    """GET `/s?k={author}` against multiple category filters, parse
    author-byline anchors out of the first non-empty result, pick
    the most-matching ID.

    Tries in order:
      1. `i=digital-text` (Kindle store) — best for Kindle-only
         indies; the print-store fallback misses them entirely.
      2. unfiltered — broader coverage if Kindle store had no chip.
      3. `i=stripbooks` (print) — last resort.

    Returns the author ID on the first variant that produces an
    anchor match, None if all three are empty.
    """
    if is_amazon_blocked():
        logger.info(
            "tier2 search: SKIP %r — amazon.com is in soft-block cooldown "
            "(%.0fs remaining)", author_name, amazon_block_remaining_s(),
        )
        return None
    variants = [
        {"k": author_name, "i": "digital-text"},
        {"k": author_name},
        {"k": author_name, "i": "stripbooks"},
    ]
    for params in variants:
        url = f"{_SEARCH_URL}?{urllib.parse.urlencode(params)}"
        try:
            resp = await session.get(url, timeout=timeout)
        except Exception as exc:
            logger.debug("tier2 search: GET %s failed: %s", url, exc)
            continue

        status = getattr(resp, "status_code", None)
        body = getattr(resp, "text", None) or ""
        # v2.19.0 — explicit 429 → trip the penalty box and bail the
        # whole variant loop; we know the IP is jailed.
        if status == 429:
            record_amazon_soft_block(
                f"tier2 search GET {url} returned HTTP 429",
                retry_after_s=_get_response_retry_after(resp),
            )
            return None
        # v2.20.3 — HTTP 202 Akamai sensor challenge. Bail all variants
        # for the same reason 429 does: trying another /s URL against
        # a jailed IP just produces another 202.
        if status == 202:
            record_amazon_soft_block(
                f"tier2 search GET {url} returned HTTP 202 with "
                f"{len(body)}-byte body (Akamai sensor challenge)",
                retry_after_s=_get_response_retry_after(resp),
            )
            return None
        if status != 200 or not body:
            logger.debug(
                "tier2 search: %s returned status=%s body_len=%d (skipping)",
                url, status, len(body),
            )
            continue
        if len(body) < _AMAZON_SOFT_BLOCK_THIN_BODY_BYTES:
            # v2.19.0 — thin body is the CAPTCHA-interstitial signature.
            # Record once and bail all variants; trying the others would
            # just generate two more thin-body responses against the same
            # IP-level jail. Caller falls through to Tier 2c (DDG).
            logger.warning(
                "tier2 search: %s thin body (%d bytes) — likely Akamai "
                "soft-block; bailing all variants",
                url, len(body),
            )
            record_amazon_soft_block(
                f"tier2 search GET {url} returned 200 with {len(body)}-byte body"
            )
            return None

        result = _pick_best_author_id_from_search(body, author_name)
        if result:
            return result
        logger.debug(
            "tier2 search: %s parsed 0 author anchors; trying next variant",
            url,
        )
    return None


def _pick_best_author_id_from_search(
    html: str, queried_name: str,
) -> str | None:
    """Parse all `/{slug}/e/{id}` anchors, group by ID, and pick the
    ID whose canonical slug best matches the queried name.

    Strategy:
      1. Collect all `_AUTHOR_LINK_RE` matches → list of (slug, id).
      2. Group by `id`. Each ID may have several occurrences with
         different slugs (Amazon's HTML inlines both the short and
         long form on the same card).
      3. For each ID, take the *longest* observed slug (the long
         form usually = decoded name; the short form is "-").
      4. Normalize each long-slug and the queried name; pick the
         ID whose normalized slug == normalized queried name.
      5. If no exact match, fall back to the most-frequent ID with
         a WARNING log noting imprecision.
    """
    matches = _AUTHOR_LINK_RE.findall(html)
    if not matches:
        return None

    # _AUTHOR_LINK_RE.findall returns a list of (slug, id) tuples.
    by_id: dict[str, list[str]] = {}
    for slug, author_id in matches:
        by_id.setdefault(author_id, []).append(slug)

    target_norm = _normalize_name(queried_name)

    # Score each ID: prefer exact normalized-name match.
    exact: list[str] = []
    near: list[tuple[int, str]] = []  # (frequency, id) for tie-break
    for author_id, slugs in by_id.items():
        # The long-form slug is the one with hyphens decoded → a name.
        # The short-form is "-" (the `/-/e/{id}` shape). Pick the
        # longest non-"-" slug; if all are "-", keep "-".
        candidate_slugs = [s for s in slugs if s != "-"]
        long_slug = max(candidate_slugs, key=len) if candidate_slugs else "-"
        slug_norm = _normalize_name(long_slug.replace("-", " "))
        if slug_norm == target_norm:
            exact.append(author_id)
        else:
            near.append((len(slugs), author_id))

    if exact:
        if len(exact) > 1:
            logger.info(
                "tier2: multiple IDs for %r normalize-exact; using first %r",
                queried_name, exact[0],
            )
        return exact[0]

    # No exact match — most-frequent fallback with explicit warn.
    if near:
        near.sort(reverse=True)
        chosen = near[0][1]
        logger.warning(
            "tier2: no exact-name match for %r in %d candidate IDs; "
            "falling back to most-frequent %r",
            queried_name, len(by_id), chosen,
        )
        return chosen
    return None


# ─── Tier 2c: DuckDuckGo search-engine fallback (opt-in) ─────────


_DDG_HTML_URL = "https://duckduckgo.com/html/"

# Default UA mirrors AmazonSource's. DDG accepts most desktop UAs.
_DDG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:143.0) "
        "Gecko/20100101 Firefox/143.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}


def _extract_amazon_author_id_from_ddg_html(html: str) -> str | None:
    """Find the first /stores/.../author/{id} URL in a DDG result page.

    Handles two shapes:
      1. Direct anchor hrefs that contain the canonical Amazon URL.
      2. DDG's tracking-redirect shape (``/l/?uddg=<encoded-url>&kh=-1``)
         where the real Amazon URL is URL-encoded inside `uddg`.
    """
    if not html:
        return None
    m = _VANITY_REDIRECT_RE.search(html)
    if m:
        return m.group("id")
    # DDG sometimes wraps result links through `/l/?uddg=...`. Pull all
    # `uddg=` values and decode them in case the canonical URL is there.
    for raw in re.findall(r'uddg=([^&"\'<>\s]+)', html):
        decoded = urllib.parse.unquote(raw)
        m = _VANITY_REDIRECT_RE.search(decoded)
        if m:
            return m.group("id")
    return None


async def _tier2c_ddg_search(
    author_name: str,
    *,
    timeout: float,
) -> str | None:
    """DuckDuckGo search-engine fallback for Amazon Author Store ID.

    When amazon.com puts our IP in the soft-block penalty box, DDG
    still surfaces the canonical /stores/.../author/{id} URL because
    its crawler hits amazon.com at much lower density than we do.
    Single GET, no Akamai-bypass dance — DDG doesn't gate scrapers
    the way Amazon does.

    Borrows Calibre's `search_engines.py` patterns: html endpoint,
    `kp=-2` safe-search off, URL-unwrap for DDG's tracking-redirect
    shape (``/l/?uddg=...``).

    Returns the resolved 10-char author ID or None on miss / error.
    Opt-in only — the public `resolve_amazon_author_id` entry point
    routes through this when `use_ddg_fallback=True`.
    """
    try:
        import httpx
    except ImportError:
        logger.warning(
            "tier2c ddg: httpx not available — cannot fall back to DDG"
        )
        return None

    # Query shape: `site:amazon.com "{author}" /stores/author` —
    # constrains hits to amazon.com pages that mention the author and
    # carry the /stores/author URL fragment, which catches both the
    # `/stores/{Display-Name}/author/{id}` canonical URL and the older
    # `/{slug}/e/{id}` short form when DDG indexes either one.
    query = f'site:amazon.com "{author_name}" /stores/author'
    params = {"q": query, "kp": "-2"}
    url = f"{_DDG_HTML_URL}?{urllib.parse.urlencode(params)}"

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            headers=_DDG_HEADERS,
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
    except Exception as exc:
        logger.debug("tier2c ddg: GET %s failed: %s", url, exc)
        return None

    if resp.status_code != 200 or not resp.text:
        logger.debug(
            "tier2c ddg: status=%s body_len=%d (no extract)",
            resp.status_code, len(resp.text or ""),
        )
        return None

    return _extract_amazon_author_id_from_ddg_html(resp.text)


# ─── Session factory (mirrors AmazonSource pattern) ─────────────


def _create_impersonating_session() -> Any | None:
    """Build a curl_cffi AsyncSession with Chrome 120 TLS impersonation.

    Mirrors `app/discovery/sources/amazon.py:_create_impersonating_session`.
    Kept duplicated rather than imported to avoid circular dep — the
    resolver is called *before* AmazonAuthorStoreSource initializes
    in the scan workflow.

    Returns None if curl_cffi isn't installed; resolver falls back to
    returning None (graceful degradation; caller logs + skips).
    """
    try:
        from curl_cffi.requests import AsyncSession
        return AsyncSession(impersonate="chrome120", timeout=15.0)
    except ImportError:
        logger.warning(
            "amazon_author_id_resolver: curl_cffi not installed — cannot "
            "resolve author IDs without it (Akamai blocks plain httpx). "
            "Install via `pip install curl_cffi`."
        )
        return None


# ─── Public entry point ──────────────────────────────────────────


async def resolve_amazon_author_id(
    author_name: str,
    *,
    known_book_asin: str | None = None,
    session: Any | None = None,
    timeout: float = 15.0,
    use_ddg_fallback: bool = False,
) -> str | None:
    """Resolve an Amazon Author Store ID for ``author_name``.

    Args:
        author_name: The author's name as Seshat knows it (e.g.
            "Brandon Sanderson" or "J. N. Chaney").
        known_book_asin: If we already have any Amazon ASIN for a
            book by this author, pass it here to enable the cheap
            Tier 1 detail-page pivot.
        session: An async HTTP session with a curl_cffi-style
            ``.get(url, timeout=...)`` interface returning an object
            with ``.status_code`` and ``.text`` attributes. If None,
            a default Chrome-120 impersonating session is built.
        timeout: Per-request timeout in seconds (default 15.0).
        use_ddg_fallback: When True, fall back to a DuckDuckGo
            site-restricted search after the amazon.com tiers fail
            (v2.19.0; opt-in via the ``amazon_use_ddg_fallback``
            source-config setting). Mirrors Calibre's
            search_engines.py pattern for the same purpose.

    Returns:
        The 10-char Amazon Author Store ID (e.g. "B001IGFHW6"), or
        None if every tier failed (caller should log + skip Amazon
        discovery for this author).
    """
    if not author_name or not author_name.strip():
        return None

    # v2.19.0 — if amazon.com is in soft-block cooldown, every tier
    # below would just walk into the same wall. Short-circuit early.
    if is_amazon_blocked():
        logger.info(
            "resolve_amazon_author_id: SKIP %r — amazon.com in soft-block "
            "cooldown (%.0fs remaining)",
            author_name, amazon_block_remaining_s(),
        )
        return None

    owns_session = False
    if session is None:
        session = _create_impersonating_session()
        owns_session = True
    if session is None:
        # curl_cffi missing → cannot proceed
        return None

    try:
        if known_book_asin:
            result = await _tier1_book_pivot(
                known_book_asin, session=session, timeout=timeout,
            )
            if result:
                logger.info(
                    "resolved amazon_author_id %r for %r via tier-1 "
                    "(book pivot on %s)",
                    result, author_name, known_book_asin,
                )
                return result

        # Tier 2a: vanity URL — one request, redirect target carries
        # the author_id. Works for any author the Amazon index can
        # match by normalized name, including Kindle-only indies that
        # the print-store search misses entirely.
        result = await _tier2_vanity_url(
            author_name, session=session, timeout=timeout,
        )
        if result:
            logger.info(
                "resolved amazon_author_id %r for %r via tier-2a (vanity URL)",
                result, author_name,
            )
            return result

        # Tier 2b: /s search across multiple category filters, parse
        # author anchors. Slower + less reliable than the vanity URL
        # but catches authors whose normalized name doesn't match
        # Amazon's vanity index (e.g. very common names where the
        # vanity slug points to someone else).
        result = await _tier2_search(
            author_name, session=session, timeout=timeout,
        )
        if result:
            logger.info(
                "resolved amazon_author_id %r for %r via tier-2b (search)",
                result, author_name,
            )
            return result

        # Tier 2c (v2.19.0): DuckDuckGo site-restricted search. Only
        # fires when `use_ddg_fallback=True` (set by AmazonSource from
        # the `amazon_use_ddg_fallback` source-config setting). Useful
        # when the amazon.com tiers were 429'd / Akamai-blocked OR
        # genuinely returned no anchor matches — DDG's index of
        # /stores/.../author/{id} URLs covers many authors whose
        # vanity slug doesn't match Amazon's normalized name index.
        if use_ddg_fallback:
            # If we're here because the earlier tiers tripped the IP
            # penalty box, the check above already short-circuited and
            # we never got this far. If we're here because the tiers
            # simply found no match, DDG is a worthwhile second look.
            result = await _tier2c_ddg_search(
                author_name, timeout=timeout,
            )
            if result:
                logger.info(
                    "resolved amazon_author_id %r for %r via tier-2c "
                    "(DDG fallback)", result, author_name,
                )
                return result

        logger.info(
            "amazon_author_id resolution FAILED for %r "
            "(tier-1 %s, tier-2a vanity URL miss, "
            "tier-2b search returned no anchor matches%s)",
            author_name,
            "skipped (no known_book_asin)" if not known_book_asin else "miss",
            ", tier-2c DDG miss" if use_ddg_fallback else "",
        )
        return None
    finally:
        if owns_session and hasattr(session, "close"):
            try:
                await session.close()
            except Exception:
                pass
