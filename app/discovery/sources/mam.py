"""
MAM integration.

Looks up each book in the user's library against MAM's torrent catalog so the
UI can show what's already available, what's missing, and what would be a
useful upload.

Authentication:
  MAM session tokens are IP- (or ASN-)locked. The user generates one from
  MAM → Preferences → Security and pastes it into Seshat's settings.
  Before each scan we ping the dynamic-seedbox endpoint to register the
  current IP (skipped for ASN-locked sessions), then run searches with the
  token in a `mam_id` cookie.

Search strategy — five-pass cascade:
  Pass 1 — author + full title
  Pass 2 — author + core title         (volume/series prefix stripped)
  Pass 3 — author + subtitle-right     (part after the colon)
  Pass 4 — author + short title        (part before the colon)
  Pass 5 — title words only            (no author, loose cleaning)
  The cascade short-circuits as soon as a high-confidence match is found;
  the best "possible" across all passes is kept as a fallback.

Format preference:
  When several MAM results match the same book, each is scored by:
    1. Highest-priority ebook format present (user-configurable)
    2. Number of formats available (more = more choice)
  The winner's torrent page is linked. If multiple distinct uploads exist
  for the same book, a flag is set so the UI can show a "multiple" badge.
"""

import asyncio
import json
import logging
import re
import time
from typing import Callable, Optional
from urllib.parse import urlencode

import httpx

from app import state
from app.metadata.scoring import score_match_with_breakdown

logger = logging.getLogger("seshat.discovery.mam")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAM_SEARCH_URL = "https://www.myanonamouse.net/tor/js/loadSearchJSONbasic.php"
MAM_BROWSE_BASE = "https://www.myanonamouse.net/tor/browse.php"
MAM_TORRENT_BASE = "https://www.myanonamouse.net/t"
MAM_DYNIP_URL = "https://t.myanonamouse.net/json/dynamicSeedbox.php"
EBOOK_CATEGORY = "14"
AUDIOBOOK_CATEGORY = "13"


def _cat_for(content_type: str) -> str:
    """MAM main_cat id for the given content_type.

    Every scan entry point flows through here so flipping ebook ↔
    audiobook is a single string swap. Unknown content_type falls
    back to ebook — safer default since the historical pipeline was
    ebook-only and most code that doesn't pass a content_type explicitly
    is ebook-era.
    """
    return AUDIOBOOK_CATEGORY if content_type == "audiobook" else EBOOK_CATEGORY

# ─── SQL predicates for "books needing MAM scan" ─────────────
# A book "needs scan" if it was never scanned (mam_status IS NULL) OR if
# its prior result was inconclusive ('possible' or 'not_found') — those
# get retried on every scan because catalog churn on MAM means a search
# that came up empty last week may hit today.
#
# Two flavors: BASIC checks only mam_status, STRICT also requires that
# never-scanned rows have mam_url IS NULL (defensive against any future
# code path that writes one column without the other). Rescannable rows
# (possible/not_found) bypass the mam_url guard because 'possible' rows
# legitimately have a mam_url set.
#
# Each flavor has a _BARE and an _ALIASED form for queries that JOIN
# authors and need a `b.` prefix to disambiguate.

_NEEDS_SCAN_BASIC_BARE = (
    "(mam_status IS NULL OR mam_status IN ('possible','not_found')) "
    "AND is_unreleased = 0 AND hidden = 0"
)
_NEEDS_SCAN_BASIC_ALIASED = (
    "(b.mam_status IS NULL OR b.mam_status IN ('possible','not_found')) "
    "AND b.is_unreleased = 0 AND b.hidden = 0"
)

_NEEDS_SCAN_STRICT_BARE = (
    "((mam_url IS NULL AND mam_status IS NULL) "
    "OR mam_status IN ('possible','not_found')) "
    "AND is_unreleased = 0 AND hidden = 0"
)
_NEEDS_SCAN_STRICT_ALIASED = (
    "((b.mam_url IS NULL AND b.mam_status IS NULL) "
    "OR b.mam_status IN ('possible','not_found')) "
    "AND b.is_unreleased = 0 AND b.hidden = 0"
)

# Match quality thresholds (0-1 scale, uses scoring.score_match)
# The combined score blends 70% title similarity + 30% author overlap,
# so a threshold of 0.65 means moderate title + good author, or
# excellent title + no author info.
MATCH_MIN_SCORE = 0.20     # below this → junk, skip
MATCH_PROMOTE_SCORE = 0.70 # at or above → promote to "found"
# Note: MAM is #1 priority for merge conflicts (SOURCE_PRIORITY in
# lookup.py), but the found threshold stays moderate because MAM
# commonly has series bundles where individual title matching naturally
# scores lower (e.g., "Kingdom's Dawn" vs "The Kingdom Series Bundle").

# Legacy thresholds kept for the _word_match_pct fallback paths
MATCH_MIN_PCT = 25.0
MATCH_PROMOTE_PCT = 50.0

# Status constants
STATUS_FOUND = "found"
STATUS_POSSIBLE = "possible"
STATUS_NOT_FOUND = "not_found"
STATUS_AUTH_ERROR = "auth_error"
STATUS_ERROR = "error"

# Bundle detection — multi-signal heuristic for spotting series
# collections / box sets / omnibuses that lump multiple individual
# books into one torrent. Used to keep low-title-similarity bundle
# matches out of the Found tier (the URL points at a collection,
# not at the searched-for book) and to surface a badge in the UI.
_BUNDLE_TITLE_KEYWORDS_RX = re.compile(
    r"\b(?:series|collection|boxset|box\s*set|omnibus|trilogy|saga|anthology|complete\s+works)\b",
    re.I,
)
# numfiles ≥ this triggers the bundle flag on its own. 5 is a deliberate
# floor: a single book with 4 formats (azw3+epub+mobi+pdf) is the
# largest non-bundle case observed in the wild; 5+ is conservatively
# bundle territory.
_BUNDLE_NUMFILES_FLOOR = 5

# Below this title-similarity, an auto-promote on a bundle result is
# blocked and the result is capped at "possible". Rationale: when the
# bundle's title strongly matches the user's calibre title (e.g. user
# explicitly catalogued the bundle), normal promote logic should fire.
# But when only the author matches and the title is a different book
# inside the bundle, the URL is misleading — keep as possible until
# the (Part B2) filelist-verification path can promote with confidence.
_BUNDLE_PROMOTE_TS_FLOOR = 0.85

# Default delay between MAM API requests (seconds)
DEFAULT_DELAY = 2.0

# How many results to request per search. The MAM API allows 5–1000.
# 100 is deliberate: for prolific authors with many torrents in a series,
# the exact match can get pushed off the first page by bundles and series-
# sibling torrents that MAM ranks higher. A 25-result page once missed
# Robert Jordan's "The Eye of the World" entirely because Wheel of Time
# bundles took every top slot. Don't drop this without re-verifying.
RESULTS_PER_PAGE = 100

# MAM language ID mapping. The MAM API uses numeric language IDs both for
# the request payload (`tor.browse_lang`) and for the per-result `language`
# field. We send the IDs corresponding to the user's selected languages so
# foreign editions don't consume our perpage budget or pass the title match
# threshold via shared filler words.
#
# IDs below were captured from real MAM responses during testing — DO NOT
# guess at IDs you haven't verified, because a wrong ID will silently pull
# results in an unrelated language. To add a new language: open MAM's
# torrent search, filter by that language, inspect the network request
# payload's `browse_lang` array, and add the entry here.
MAM_LANGUAGES: dict[str, int] = {
    "English": 1,
    "Spanish": 4,
    "Dutch": 22,
    "Hungarian": 28,
    "French": 36,
    "Italian": 43,
    "Portuguese": 52,
}

# Default English language ID — used when nothing in the user's language
# selection resolves to a known MAM ID, so we never accidentally send an
# empty browse_lang (which would un-filter the search entirely).
_ENGLISH_LANG_ID = MAM_LANGUAGES["English"]


def _resolve_mam_languages(language_names: list[str]) -> list[int]:
    """Convert human-readable language names to MAM browse_lang IDs.

    Names not in MAM_LANGUAGES are silently dropped (debug-logged) — we
    deliberately don't guess at IDs we haven't verified. If nothing
    resolves we fall back to English-only so the search remains filtered.
    """
    if not language_names:
        return [_ENGLISH_LANG_ID]
    ids: list[int] = []
    unknown: list[str] = []
    for name in language_names:
        mid = MAM_LANGUAGES.get(name)
        if mid is None:
            unknown.append(name)
        elif mid not in ids:
            ids.append(mid)
    if unknown:
        logger.debug(
            f"MAM language(s) not yet mapped, ignoring: {unknown}. "
            f"To add: inspect MAM's browse_lang request payload for that language "
            f"and add the numeric ID to MAM_LANGUAGES in app/sources/mam.py."
        )
    if not ids:
        logger.debug("No selected languages map to MAM IDs — defaulting to English")
        return [_ENGLISH_LANG_ID]
    return ids

# Default format priority (user can override in settings)
DEFAULT_FORMAT_PRIORITY = ["epub", "azw3", "mobi", "kfx", "pdf", "html", "lit", "rtf", "doc"]
DEFAULT_AUDIOBOOK_FORMAT_PRIORITY = ["m4b", "m4a", "mp3", "aax", "aa"]

# All known ebook format tokens MAM might return in filetypes
KNOWN_EBOOK_FORMATS = {
    "epub", "mobi", "azw", "azw3", "kfx", "pdf", "html", "htm",
    "lit", "rtf", "doc", "docx", "djvu", "fb2", "txt", "cbr", "cbz",
}
KNOWN_AUDIOBOOK_FORMATS = {
    "m4b", "m4a", "mp3", "aax", "aa", "flac", "ogg", "wav",
}


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
HONORIFICS = re.compile(
    r'\b(Mr|Mrs|Ms|Miss|Dr|PhD|Professor|Prof)\.?\s?\b', re.IGNORECASE
)
RE_ADD_SPACE = re.compile(r'(?<=\S)[;:,.\-\u2014](?=\S)')
RE_PUNCT = re.compile(r'[;:,.\-\u2014]')
RE_SPECIAL = re.compile(r'[^a-zA-Z0-9\s]')
RE_SPECIAL_KEEP_HYPHEN = re.compile(r'[^a-zA-Z0-9\s\-]')

SUBTITLE_DELIMITERS = [':', ' - ', '|']

RE_VOL_PREFIX = re.compile(
    r'^.{2,}?'
    r'(?:[,\s]+)'
    r'(?:Vol(?:ume)?|Book|Part|Bk|Pt)'
    r'[\s.]*'
    r'(?:\d+(?:\.\d+)?|[IVXLCDM]+)'
    r'(?:\s*[:\-]\s*|\s+)',
    re.IGNORECASE,
)
RE_NUM_PREFIX = re.compile(
    r'^.{2,}?[,\s]+#\d+(?:\s*[:\-]\s*|\s+)',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def _clean_title(title: str) -> str:
    """Normalise a title for MAM search (strips hyphens and punctuation)."""
    t = RE_ADD_SPACE.sub(' ', title)
    t = RE_PUNCT.sub('', t)
    t = RE_SPECIAL.sub('', t)
    return ' '.join(t.split())


def _clean_title_loose(title: str) -> str:
    """Minimal cleaning for title-only searches (keeps hyphens)."""
    t = RE_SPECIAL_KEEP_HYPHEN.sub('', title)
    return ' '.join(t.split())


def _clean_authors(authors: str) -> str:
    """Strip honorifics and periods from initials/abbreviations."""
    a = HONORIFICS.sub('', authors)
    a = re.sub(r'\.', '', a)
    return ' '.join(a.split())


def _strip_subtitle(title: str) -> Optional[str]:
    for delim in SUBTITLE_DELIMITERS:
        if delim in title:
            return title.split(delim)[0].strip()
    return None


def _extract_subtitle_part(title: str) -> Optional[str]:
    for delim in SUBTITLE_DELIMITERS:
        if delim in title:
            right = title.split(delim, 1)[1].strip()
            if len(right) >= 3:
                return right
    return None


def _extract_core_title(title: str) -> Optional[str]:
    for pattern in (RE_VOL_PREFIX, RE_NUM_PREFIX):
        m = pattern.match(title)
        if m:
            core = title[m.end():].strip()
            if len(core) >= 3:
                return core
    return None


def _build_query(authors: str, title: str) -> str:
    return f"{_clean_authors(authors)} {_clean_title(title)}"


def build_search_link(authors: str, title: str) -> str:
    """Build a clickable MAM browse URL for manual searching."""
    params = {
        "tor[text]": _build_query(authors, title),
        "tor[srchIn][author]": "true",
        "tor[srchIn][title]": "true",
        "tor[srchIn][series]": "true",
        "tor[srchIn][description]": "true",
        "tor[srchIn][filenames]": "true",
        "tor[srchIn][narrator]": "true",
        "tor[srchIn][tags]": "true",
        "tor[searchIn]": "torrents",
        "tor[searchType]": "active",
        "tor[main_cat]": EBOOK_CATEGORY,
    }
    return f"{MAM_BROWSE_BASE}?{urlencode(params)}"


def _torrent_url(torrent_id) -> str:
    """Build a direct link to a MAM torrent page."""
    return f"{MAM_TORRENT_BASE}/{torrent_id}"


_RE_PUNCT_TOKEN = re.compile(r"[^\w\s]+")


def _word_match_pct(text1: str, text2: str) -> float:
    """Sorted-token word overlap percentage.

    Strips punctuation before tokenizing so "Reach:" matches "Reach". Without
    this, attached colons / apostrophes / commas dragged real exact matches
    below the promote threshold, which silently mis-linked subtitled series
    titles like "Halo: Shadows of Reach: A Master Chief Story".
    """
    def _tokens(t: str) -> list[str]:
        return sorted(_RE_PUNCT_TOKEN.sub(" ", t.lower()).split())
    w1 = _tokens(text1)
    w2 = _tokens(text2)
    i = j = m = 0
    while i < len(w1) and j < len(w2):
        if w1[i] == w2[j]:
            m += 1; i += 1; j += 1
        elif w1[i] < w2[j]:
            i += 1
        else:
            j += 1
    return round(m / max(len(w1), len(w2), 1) * 100, 1)


def _parse_author_info(raw) -> list[str]:
    """Parse MAM's author_info field into a list of author names.

    MAM returns author_info as a JSON-encoded string mapping author IDs to
    names, e.g. '{"12345":"Brandon Sanderson","6789":"Janci Patterson"}'.
    Falls back to treating the input as a plain string if JSON parsing fails.
    """
    if not raw:
        return []
    if isinstance(raw, dict):
        return [str(v) for v in raw.values() if v]
    if isinstance(raw, list):
        return [str(v) for v in raw if v]
    s = str(raw).strip()
    if not s:
        return []
    try:
        parsed = json.loads(s)
    except (ValueError, TypeError):
        return [s]
    if isinstance(parsed, dict):
        return [str(v) for v in parsed.values() if v]
    if isinstance(parsed, list):
        return [str(v) for v in parsed if v]
    return [str(parsed)]


def _author_match(calibre_authors: str, mam_result: dict) -> bool:
    """Check if MAM result author plausibly matches our author string."""
    mam_authors = _parse_author_info(mam_result.get("author_info"))
    if not mam_authors:
        return True

    def tokens(s: str) -> set:
        s = re.sub(r'\.', '', s.lower())
        return set(re.findall(r'[a-z]+', s))

    cal_tok = tokens(calibre_authors)
    mam_tok = set()
    for name in mam_authors:
        mam_tok |= tokens(name)
    overlap = {t for t in cal_tok & mam_tok if len(t) > 1}
    return bool(overlap)


# ---------------------------------------------------------------------------
# Format preference scoring
# ---------------------------------------------------------------------------

def _parse_formats(filetypes_str: str, content_type: str = "ebook") -> list[str]:
    """
    Parse MAM filetypes string into a list of known formats for the
    target content type.

    For ebook scans, audio formats (mp3, m4a) are filtered out so a
    torrent tagged "epub mp3" returns just ["epub"]. For audiobook
    scans the filter inverts — "mp3 m4a aa" returns all three and
    an ebook-only torrent returns [].
    """
    if not filetypes_str:
        return []
    all_tokens = set(f.strip().lower() for f in filetypes_str.split() if f.strip())
    allowed = KNOWN_AUDIOBOOK_FORMATS if content_type == "audiobook" else KNOWN_EBOOK_FORMATS
    return sorted(t for t in all_tokens if t in allowed)


def _format_score(formats: list[str], priority: list[str]) -> tuple[int, int, str]:
    """
    Score a torrent's formats against user's priority list.

    Returns (priority_rank, format_count, best_format):
      priority_rank: 0 = user's #1 format found, 1 = #2, etc. 999 = none found
      format_count:  total ebook formats in this torrent (more = more choice)
      best_format:   name of the highest-priority format found

    Comparison logic:
      - Lower priority_rank is always better (user's preferred format wins)
      - Among same rank, higher format_count wins (more choice for user)
    """
    fmt_set = set(f.lower() for f in formats)
    for rank, pref in enumerate(priority):
        if pref.lower() in fmt_set:
            return (rank, len(formats), pref.lower())
    # No preferred format found — still return format info
    return (999, len(formats), formats[0] if formats else "unknown")


# Books at or above this match_pct are treated as the "same book" with high
# confidence. The format-aware sort that prefers more formats is only
# meaningful WITHIN this set — letting low-confidence matches into the
# format comparison once let a wrong-but-multi-format result beat the
# right-but-single-format match.
HIGH_CONFIDENCE_PCT = 80.0


def _pick_best_result(
    matches: list[dict],
    format_priority: list[str],
) -> dict:
    """
    From a list of scored MAM matches, pick the best one.

    Each match dict has: torrent_id, mam_title, formats, match_pct,
    author_matched, seeders, plus per-result fields.

    Selection logic — order matters:
      1. Filter to high-confidence title matches (>= HIGH_CONFIDENCE_PCT)
         when any exist, falling back to the full list otherwise. This is
         what stops a wrong-book-with-more-formats from beating a
         right-book-with-fewer-formats.
      2. Among the candidates, score by user's format preference rank.
      3. Within the same format rank, prefer higher match_pct, then more
         formats, then more seeders.

    Match quality MUST come before format count in the tiebreak — sorting
    formats first silently mis-matches any series where one torrent
    bundles extra formats.
    """
    if not matches:
        return None

    # ── Filter to high-confidence matches when possible ────────
    high = [m for m in matches if m["match_pct"] >= HIGH_CONFIDENCE_PCT]
    candidates = high if high else matches

    scored = []
    for m in candidates:
        rank, count, best_fmt = _format_score(m["formats"], format_priority)
        scored.append({
            **m,
            "fmt_rank": rank,
            "fmt_count": count,
            "best_format": best_fmt,
        })

    # Sort: lowest fmt_rank, highest match_pct, highest fmt_count, highest seeders
    scored.sort(key=lambda x: (
        x["fmt_rank"],
        -x["match_pct"],
        -x["fmt_count"],
        -x.get("seeders", 0),
    ))
    return scored[0]


# ---------------------------------------------------------------------------
# HTTP layer (sync helpers + Session + auth flow)
# ---------------------------------------------------------------------------

def _build_headers(token: str) -> dict:
    """Build headers for MAM API requests.

    The `curl/8.0` User-Agent is intentional and load-bearing — it's the UA
    we know works against MAM end-to-end. Don't change it without running a
    full scan first; subtle UA-based rejection has bitten us before.

    The IP- (or ASN-)locked `mam_id` cookie is the ONLY auth mechanism;
    the same token will be rejected if the requesting IP differs from the
    one that generated it. See register_ip() and the `skip_ip_update`
    setting for the network-binding workflow.
    """
    return {
        "Content-Type": "application/json",
        "User-Agent": "curl/8.0",
        "Cookie": f"mam_id={token}",
    }


# ---------------------------------------------------------------------------
# HTTP layer (async — httpx.AsyncClient)
# ---------------------------------------------------------------------------
# Native async HTTP via a single process-wide httpx.AsyncClient.
#
# Two non-obvious choices, both load-bearing — do NOT change without running
# a full MAM scan end-to-end first:
#
#   1. Search POSTs use `content=<string>` (raw body), NOT `data=<dict>`
#      (form-url-encoded) and NOT `json=<dict>` (re-serialized by httpx).
#      MAM will happily accept the request and return HTTP 200 with a
#      zero-byte body when the search payload is form-encoded — looks like
#      auth failure but isn't. The fix is sending the exact JSON bytes
#      produced by json.dumps at the call site.
#
#   2. http2=False is explicit. httpx can speak HTTP/2 when `h2` is
#      installed; we pin HTTP/1.1 because that's what's been verified
#      against MAM and we don't want variable transport behavior.
#
# Connection reuse matters for throughput: a 100-book scan with the 5-pass
# cascade fires hundreds of requests, and each fresh TCP+TLS handshake
# costs 50-150ms. Sharing one client across the process drops that to a
# single handshake per batch.

_client: Optional[httpx.AsyncClient] = None

# ── Cookie auto-rotation state ──────────────────────────────
# MAM rotates the mam_id session cookie on every response via Set-Cookie.
# Clients that capture and reuse the new cookie get indefinite session
# lifetime; clients that ignore it eventually expire (~30 days).
#
# The pattern: intercept Set-Cookie after every _do_get/_do_post, compare
# to the in-memory token, and if different, update + fire a callback that
# debounce-persists to settings.json.
_current_token: Optional[str] = None
_rotation_callback: Optional[Callable] = None
_last_rotation_save: float = 0.0
_MAM_ID_RX = re.compile(r"mam_id=([^;\s]+)")


def set_current_token(token: str) -> None:
    """Seed the in-memory token from settings at startup."""
    global _current_token
    _current_token = token


def get_current_token() -> Optional[str]:
    """Return the most recently rotated token."""
    return _current_token


def set_rotation_callback(callback: Callable) -> None:
    """Register a callback for when the token rotates.

    The callback receives the new token string and should persist it
    to settings.json. Called inline after each response, so it should
    be fast (the caller handles debouncing).
    """
    global _rotation_callback
    _rotation_callback = callback


def _extract_mam_id(response: httpx.Response) -> Optional[str]:
    """Extract mam_id from a MAM response's Set-Cookie header."""
    # Primary: httpx cookie jar (handles standard Set-Cookie parsing)
    jar_val = response.cookies.get("mam_id")
    if jar_val:
        return jar_val
    # Fallback: regex against raw Set-Cookie headers (handles edge cases
    # where httpx doesn't parse the cookie due to missing attributes)
    for val in response.headers.get_list("set-cookie"):
        m = _MAM_ID_RX.search(val)
        if m:
            return m.group(1)
    return None


async def _handle_response_cookie(response: httpx.Response) -> None:
    """Check response for a rotated mam_id and update state if changed."""
    global _current_token, _last_rotation_save
    new_token = _extract_mam_id(response)
    if not new_token or new_token == _current_token:
        return
    _current_token = new_token
    # Don't log token bytes (even a prefix) — an 8-char prefix is enough
    # entropy to correlate sessions across log files / log aggregators,
    # and anyone with `docker logs` access is a wider audience than the
    # people authorized to see the MAM session token. The fact-of-rotation
    # is the only diagnostic that matters here.
    logger.debug("MAM cookie rotated")
    # Debounced persistence: only save if 60+ seconds since last save
    now = time.time()
    if _rotation_callback and (now - _last_rotation_save) >= 60:
        _last_rotation_save = now
        try:
            await _rotation_callback(new_token)
        except Exception as e:
            logger.warning(f"Cookie rotation callback failed: {e}")


def _get_client() -> httpx.AsyncClient:
    """Lazy-initialized module-level httpx.AsyncClient for connection reuse.

    MUST be called from within a running asyncio event loop — the client
    binds to whichever loop is active at creation time. Seshat runs one
    uvicorn loop for the whole process lifetime, so this is safe.
    """
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            http2=False,
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
        )
        logger.debug("MAM httpx.AsyncClient created")
    return _client


async def aclose_session() -> None:
    """Tear down the module-level AsyncClient.

    Called from main.py's lifespan() during app shutdown. Safe to call
    multiple times — subsequent calls are no-ops. The `a` prefix is
    deliberate: callers must `await` this to actually close the
    underlying transport.
    """
    global _client
    if _client is not None:
        try:
            await _client.aclose()
            logger.debug("MAM httpx.AsyncClient closed")
        except Exception as e:
            logger.warning(f"Error closing MAM client: {e}")
        finally:
            _client = None


async def _do_get(url: str, token: str, timeout: int = 15) -> httpx.Response:
    """Async GET to a MAM endpoint with standard headers + cookie rotation."""
    # Use the rotated token if available, fall back to explicit token
    effective = _current_token or token
    resp = await _get_client().get(
        url, headers=_build_headers(effective), timeout=timeout
    )
    await _handle_response_cookie(resp)
    return resp


async def _do_post(url: str, token: str, payload: str, timeout: int = 20) -> httpx.Response:
    """Async POST to a MAM endpoint with standard headers + cookie rotation.

    `payload` MUST be a pre-serialized JSON string. Sent via httpx
    `content=` so the body bytes go on the wire untouched. See the module
    header for why `data=<dict>` and `json=<dict>` both break the search
    API in subtle ways.
    """
    effective = _current_token or token
    resp = await _get_client().post(
        url, headers=_build_headers(effective), content=payload, timeout=timeout
    )
    await _handle_response_cookie(resp)
    return resp


async def register_ip(session_id: str, skip_ip_update: bool = True) -> dict:
    """
    Ping MAM's dynamic seedbox endpoint to register this server's IP.
    Returns {"success": bool, "message": str}

    skip_ip_update defaults to True because IP registration is only needed
    for non-ASN-locked sessions, and the rest of the codebase always passes
    True. The default exists so any new caller that forgets to specify gets
    the safer behavior automatically.
    """
    if skip_ip_update:
        return {"success": True, "message": "Skipped IP registration (ASN-locked session)"}

    logger.info("Registering server IP with MAM...")

    try:
        resp = await _do_get(MAM_DYNIP_URL, session_id)
        body = resp.text.strip()
        logger.debug(f"IP registration response: {body}")

        # MAM dynamicSeedbox.php returns JSON like:
        #   {"Success": true, "msg": "Completed", "ip": "...", "ASN": 12345, "AS": "..."}
        # On failure msg may be "No Session Cookie", "Incorrect session type - ...",
        # "Invalid session - IP mismatch", "Last Change too recent", etc.
        try:
            data = resp.json()
        except Exception:
            if "<html" in body.lower():
                return {"success": False, "message": "Got HTML login page — token wrong or expired"}
            return {"success": False, "message": f"Non-JSON response: {body[:200]}"}

        msg = str(data.get("msg", "")).strip()
        if data.get("Success"):
            logger.info(f"IP registration OK ({msg or 'no message'})")
            return {
                "success": True,
                "message": msg or "OK",
                "ip": data.get("ip"),
                "asn": data.get("ASN"),
                "as_org": data.get("AS"),
            }

        # Success=false branch — interpret known msg values
        msg_l = msg.lower()
        if "incorrect session type" in msg_l:
            logger.warning("ASN-locked session — IP registration not needed")
            return {"success": True, "message": "ASN-locked session — IP registration not needed"}
        if "no session cookie" in msg_l or "invalid cookie" in msg_l:
            return {"success": False, "message": "Token not recognised by MAM"}
        if "ip mismatch" in msg_l or "asn mismatch" in msg_l:
            return {"success": False, "message": f"Session locked to a different network: {msg}"}
        if "too recent" in msg_l:
            return {"success": False, "message": f"IP change rate-limited by MAM: {msg}"}
        return {"success": False, "message": msg or f"Unexpected response: {body[:200]}"}
    except asyncio.TimeoutError:
        return {"success": False, "message": "Timeout connecting to MAM"}
    except Exception:
        # Log the full traceback server-side but return a generic message:
        # exception details can leak library versions, internal hostnames,
        # or stack frame paths through the API response body.
        logger.exception("MAM IP-registration network error")
        return {
            "success": False,
            "message": "Network error connecting to MAM dynamic seedbox endpoint",
        }


async def verify_search_auth(session_id: str) -> dict:
    """Verify MAM search API access with a test query."""
    logger.info("Verifying MAM search API access...")

    # Auth probe only — always English, regardless of user language settings.
    test_payload = json.dumps({
        "tor": {
            "text": "test",
            "srchIn": {"title": "true"},
            "searchType": "active",
            "searchIn": "torrents",
            "main_cat": [EBOOK_CATEGORY],
            "browse_lang": [_ENGLISH_LANG_ID],
            "startNumber": "0",
        },
        "perpage": 5,
    })

    try:
        resp = await _do_post(MAM_SEARCH_URL, session_id, test_payload, 15)
        if resp.status_code == 200 and len(resp.text) > 0:
            logger.info("MAM search auth OK")
            return {"success": True, "message": "Connection successful"}
        elif resp.status_code == 200 and len(resp.text) == 0:
            return {"success": False, "message": "HTTP 200 but empty response — token may be invalid or expired"}
        elif resp.status_code == 403:
            return {"success": False,
                    "message": "HTTP 403 — session rejected. Check token is valid for this server's IP/ASN."}
        else:
            return {"success": False, "message": f"Unexpected HTTP {resp.status_code}"}
    except Exception:
        # Same rationale as register_ip's handler: full traceback to logs,
        # generic message in the API response.
        logger.exception("MAM search-auth network error")
        return {
            "success": False,
            "message": "Network error verifying MAM search access",
        }


async def validate_connection(session_id: str, skip_ip_update: bool = True) -> dict:
    """Full validation: IP registration + search auth test.

    See register_ip() for why skip_ip_update defaults to True.
    """
    ip_result = await register_ip(session_id, skip_ip_update)
    if not ip_result["success"]:
        return {
            "success": False,
            "message": f"IP registration failed: {ip_result['message']}",
            "ip_result": ip_result, "search_result": None,
        }
    search_result = await verify_search_auth(session_id)
    return {
        "success": search_result["success"],
        "message": search_result["message"] if search_result["success"]
                   else f"Search auth failed: {search_result['message']}",
        "ip_result": ip_result, "search_result": search_result,
    }


# ---------------------------------------------------------------------------
# MAM search (async)
# ---------------------------------------------------------------------------

class _AuthError(Exception):
    pass


async def _mam_search(
    token: str,
    authors: Optional[str],
    title: str,
    perpage: int = RESULTS_PER_PAGE,
    lang_ids: Optional[list[int]] = None,
    content_type: str = "ebook",
) -> Optional[dict]:
    """
    Search MAM natively (httpx.AsyncClient). Pass authors=None for
    title-only search (pass 5). Returns parsed JSON response or None on
    error. Raises _AuthError on 401/403.

    `content_type` routes the `main_cat` filter: "ebook" (default)
    scopes to E-Books, "audiobook" scopes to AudioBooks. Callers that
    want both categories aren't currently supported — scan flows
    know the book's library and pass exactly one.
    """
    if authors is None:
        query = _clean_title_loose(title)
    else:
        query = _build_query(authors, title)

    if not lang_ids:
        lang_ids = [_ENGLISH_LANG_ID]

    payload = json.dumps({
        "tor": {
            "text": query,
            "srchIn": {
                "author": "true",
                "description": "true",
                "filenames": "true",
                "narrator": "true",
                "series": "true",
                "tags": "true",
                "title": "true",
            },
            "searchType": "active",
            "searchIn": "torrents",
            "main_cat": [_cat_for(content_type)],
            "browse_lang": lang_ids,
            "browseFlagsHideVsShow": "0",
            "startDate": "", "endDate": "", "hash": "",
            "sortType": "default",
            "startNumber": "0",
        },
        "perpage": perpage,
    })

    try:
        resp = await _do_post(MAM_SEARCH_URL, token, payload)
        if resp.status_code in (401, 403):
            raise _AuthError(f"HTTP {resp.status_code}")
        resp.raise_for_status()
        if not resp.text or len(resp.text) == 0:
            return None
        return resp.json()
    except _AuthError:
        raise
    except Exception as e:
        logger.debug(f"Search error for '{query[:60]}': {e}")
        return None


# ---------------------------------------------------------------------------
# Result evaluation — scores all results from a search
# ---------------------------------------------------------------------------

def _is_bundle(item: dict) -> bool:
    """Detect whether a MAM result is a multi-book bundle / collection.

    Three signals; ANY one is sufficient (false-positives are mostly
    harmless — a tagged single book just gets a "Series Bundle" badge,
    while a missed bundle could let a wrong URL silently look correct):

      1. ``numfiles`` ≥ 5  — large file counts are bundles in practice
      2. Title contains bundle keywords (series, collection, omnibus, …)
      3. ``series_info`` has a range index like "1-12" or "1-4"
         (single books always have a numeric index like "5")
    """
    numfiles = item.get("numfiles") or 0
    try:
        if int(numfiles) >= _BUNDLE_NUMFILES_FLOOR:
            return True
    except (ValueError, TypeError):
        pass

    title = str(item.get("title") or item.get("name") or "")
    if title and _BUNDLE_TITLE_KEYWORDS_RX.search(title):
        return True

    raw_series = item.get("series_info")
    if raw_series:
        try:
            parsed = (
                json.loads(raw_series)
                if isinstance(raw_series, str)
                else raw_series
            )
            entries = (
                parsed.values() if isinstance(parsed, dict)
                else parsed if isinstance(parsed, list) else []
            )
            for entry in entries:
                # MAM format: ["Series Name", "<index>", numeric_index]
                # where <index> is "5" for single books and "1-12" / "1, 3, 5"
                # for bundles.
                if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    idx = str(entry[1])
                    if "-" in idx or "," in idx:
                        return True
        except (ValueError, TypeError, AttributeError):
            pass

    return False


def _evaluate_results(
    data: list[dict],
    calibre_title: str,
    search_title: str,
    authors: str,
    lang_ids: Optional[list[int]] = None,
    known_series: str = "",
    content_type: str = "ebook",
) -> list[dict]:
    """
    Evaluate all MAM search results for a book. Returns a list of viable
    matches, each with scoring info. Empty list = no viable matches.

    Each returned match dict:
      torrent_id, mam_title, formats, format_str, match_pct,
      confidence, author_matched, seeders, my_snatched
    """
    if not lang_ids:
        lang_ids = [_ENGLISH_LANG_ID]
    allowed_lang_set = set(lang_ids)

    matches = []
    for item in data:
        # MAM normally returns title/name as strings, but some catalog rows
        # store numeric-looking titles (e.g. "1984") as JSON numbers, and the
        # `or` chain happily returns those ints if they're truthy. Coerce to
        # str so downstream string ops (.lower(), .split(), .strip()) don't
        # explode partway through a 2000-book scan with a generic AttributeError.
        mam_title = str(item.get("title") or item.get("name") or "")
        torrent_id = item.get("id", "")

        # Belt-and-suspenders language filter. browse_lang in the request body
        # already restricts to the user's selected languages, but if MAM ever
        # returns a row in a different language (e.g. cataloging glitches) we
        # don't want it slipping through. We check the numeric `language`
        # field first because it's the same vocabulary as browse_lang; falls
        # back to the 3-letter `lang_code` only if the numeric field is missing.
        result_lang = item.get("language")
        if isinstance(result_lang, int):
            if result_lang not in allowed_lang_set:
                logger.debug(f"  Eval: SKIP '{mam_title[:50]}' — language={result_lang} not in {sorted(allowed_lang_set)}")
                continue
        else:
            # No numeric language — fall back to 3-letter code (rare).
            lang_code = str(item.get("lang_code") or "").strip().lower()
            if lang_code and lang_code not in ("eng", "en", "english"):
                # We only know how to fall-back-match English. Anything else
                # gets a free pass since we can't safely correlate.
                logger.debug(f"  Eval: SKIP '{mam_title[:50]}' — non-English lang_code={lang_code} (no numeric language field)")
                continue

        # Parse ebook formats from filetypes field. Same defensive coercion
        # as mam_title above — MAM occasionally returns numeric values here
        # for malformed catalog entries.
        filetypes_raw = str(item.get("filetype") or item.get("filetypes") or "")
        formats = _parse_formats(filetypes_raw, content_type=content_type)

        # Format-based rejection: inverted by content_type. An ebook scan
        # skips audio-only torrents (mp3/m4a/etc.) since _parse_formats
        # returned nothing under the ebook allowlist; an audiobook scan
        # skips ebook-only torrents the same way.
        if not formats and filetypes_raw.strip():
            other = "audio" if content_type == "ebook" else "ebook"
            logger.debug(f"  Eval: SKIP '{mam_title[:50]}' — {other}-only formats ({filetypes_raw.strip()})")
            continue

        # Category-based rejection: MAM categories like "AudioBooks -
        # Fantasy" vs "Ebooks - Fantasy". For ebook scans we drop
        # audiobook-prefixed categories; for audiobook scans we drop
        # ebook-prefixed ones. The main_cat filter on the search
        # request already narrows this, but a handful of older
        # cross-category listings still slip through.
        category = str(item.get("category") or "").strip()
        cat_lower = category.lower()
        if content_type == "ebook" and cat_lower.startswith("audiobook"):
            logger.debug(f"  Eval: SKIP '{mam_title[:50]}' — audiobook category ({category})")
            continue
        if content_type == "audiobook" and cat_lower.startswith("ebook"):
            logger.debug(f"  Eval: SKIP '{mam_title[:50]}' — ebook category ({category})")
            continue

        # ── Improved scoring via scoring.py ──
        # Extract MAM author names for overlap scoring
        mam_authors = _parse_author_info(item.get("author_info"))

        # Combined score: 70% title similarity + 30% author overlap
        # + series boost when known_series matches in the MAM title.
        # Use the breakdown variant so we can capture the individual
        # title-similarity signal — needed for the bundle promote gate
        # below, which compares ts to a floor independent of confidence.
        bd_full = score_match_with_breakdown(
            record_title=mam_title, record_authors=mam_authors,
            search_title=calibre_title, search_authors=authors,
            known_series=known_series,
        )
        bd_search = score_match_with_breakdown(
            record_title=mam_title, record_authors=mam_authors,
            search_title=search_title, search_authors=authors,
            known_series=known_series,
        )
        confidence = max(bd_full["confidence"], bd_search["confidence"])
        title_similarity_max = max(
            bd_full["title_similarity"], bd_search["title_similarity"]
        )

        # Legacy compatibility: also compute the old percentage for the
        # match_pct field (used by _pick_best_result sorting)
        pct_full = _word_match_pct(calibre_title, mam_title)
        pct_search = _word_match_pct(search_title, mam_title)
        pct = max(pct_full, pct_search)

        if confidence < MATCH_MIN_SCORE:
            logger.debug(f"  Eval: SKIP '{mam_title[:50]}' — confidence {confidence:.2f} < {MATCH_MIN_SCORE} min")
            continue  # junk result

        # Legacy author match (kept for diagnostic logging)
        author_ok = _author_match(authors, item)

        # MAM marks torrents the user has already snatched via "my_snatched"
        # (truthy when present). Capture so we can show a badge in the UI.
        my_snatched = bool(item.get("my_snatched"))

        matches.append({
            "torrent_id": str(torrent_id),
            "mam_title": mam_title,
            "formats": formats,
            "format_str": ",".join(formats) if formats else filetypes_raw.strip(),
            "match_pct": pct,
            "confidence": confidence,
            "title_similarity": title_similarity_max,
            "author_matched": author_ok,
            "seeders": int(item.get("seeders", 0) or 0),
            "my_snatched": my_snatched,
            # Bundle/collection torrent flag — set when numfiles, title
            # keywords, or series-range marker indicate this is a multi-
            # book torrent rather than a single-book release.
            "is_bundle": _is_bundle(item),
            # Passed through to the books row so Send-to-Pipeline can
            # forward it as the grab category. MAM returns values like
            # "Ebooks - Fantasy" here — passed along as-is.
            "category": category,
        })

    return matches


# ---------------------------------------------------------------------------
# Per-book check — five-pass cascade with format-aware scoring
# ---------------------------------------------------------------------------

async def check_book(
    token: str,
    title: str,
    authors: str,
    format_priority: list[str] = None,
    delay: float = DEFAULT_DELAY,
    lang_ids: Optional[list[int]] = None,
    series_name: str = "",
    content_type: str = "ebook",
) -> dict:
    """
    Five-pass search cascade for a single book, with format preference scoring.

    `content_type` routes the whole cascade through the ebook or
    audiobook variants — search main_cat, format filtering, category
    rejection, default priority list. Callers that don't pass
    content_type get the ebook path (historical behavior).

    Returns dict with:
      status, mam_url, mam_torrent_id, mam_title, mam_formats, mam_has_multiple,
      mam_is_bundle, match_pct, best_format, passes_tried, search_link, error
    """
    if format_priority is None:
        format_priority = (
            DEFAULT_AUDIOBOOK_FORMAT_PRIORITY if content_type == "audiobook"
            else DEFAULT_FORMAT_PRIORITY
        )
    if not lang_ids:
        lang_ids = [_ENGLISH_LANG_ID]

    # Default result — search link as fallback URL
    fallback_search_link = build_search_link(authors, title)
    result = {
        "status": STATUS_NOT_FOUND,
        "mam_url": fallback_search_link,
        "mam_torrent_id": None,
        "mam_title": None,
        "mam_formats": None,
        "mam_has_multiple": False,
        "mam_my_snatched": False,
        "mam_is_bundle": False,
        "match_pct": None,
        "best_format": None,
        "passes_tried": [],
        "search_link": fallback_search_link,
        "error": None,
    }

    # Track best "possible" across all passes
    best_possible = None

    def _try_evaluate(pass_num: int, resp: dict, search_title: str) -> bool:
        """
        Evaluate all results from a search pass. Returns True if cascade should stop.
        Updates result dict and best_possible as side effects.
        """
        nonlocal best_possible

        if not resp or not resp.get("data"):
            logger.debug(f"  Pass {pass_num}: no data in response")
            return False

        data = resp["data"]
        # Log total_found vs returned so we can spot truncated result sets.
        total_found = resp.get("found") or resp.get("total_found") or resp.get("total")
        if total_found is not None and isinstance(total_found, (int, str)):
            try:
                tf = int(total_found)
                if tf > len(data):
                    logger.debug(
                        f"  Pass {pass_num}: MAM returned {len(data)} of {tf} total — "
                        f"results may be truncated by perpage limit"
                    )
            except (ValueError, TypeError):
                pass
        matches = _evaluate_results(data, title, search_title, authors, lang_ids, known_series=series_name, content_type=content_type)

        if not matches:
            return False

        # Separate into author-confirmed and author-unconfirmed
        confirmed = [m for m in matches if m["author_matched"]]
        all_viable = confirmed if confirmed else matches

        # Check if multiple distinct uploads exist (different torrent IDs)
        unique_ids = set(m["torrent_id"] for m in all_viable)
        has_multiple = len(unique_ids) > 1

        # Pick best result by format preference
        best = _pick_best_result(all_viable, format_priority)
        if not best:
            return False

        pct = best["match_pct"]
        conf = best.get("confidence", pct / 100.0)
        ts = best.get("title_similarity", 0.0)
        is_bundle = bool(best.get("is_bundle", False))

        # Build candidate info
        candidate = {
            "pass": pass_num,
            "torrent_id": best["torrent_id"],
            "mam_title": best["mam_title"],
            "formats": best["format_str"],
            "has_multiple": has_multiple,
            "match_pct": pct,
            "confidence": conf,
            "best_format": best.get("best_format", ""),
            "author_matched": best["author_matched"],
            "my_snatched": best.get("my_snatched", False),
            "category": best.get("category", "") or "",
            "is_bundle": is_bundle,
        }

        # Bundle promote-gate: confidence alone isn't enough to elevate a
        # collection torrent to "Found" — the URL would point at a multi-
        # book torrent rather than the searched-for single book. Require
        # that title-similarity (not just blended confidence) clear a
        # higher floor, OR that the user's calibre title strongly matches
        # the bundle's own title (intentional bundle catalog entry). Part
        # B2 will add a filelist-verification override that re-promotes
        # a low-ts bundle when the search title appears as a filename.
        promote_blocked_by_bundle = (
            is_bundle
            and conf >= MATCH_PROMOTE_SCORE
            and ts < _BUNDLE_PROMOTE_TS_FLOOR
        )
        if promote_blocked_by_bundle:
            logger.debug(
                f"  Pass {pass_num}: BUNDLE-CAP '{best['mam_title'][:50]}' — "
                f"conf={conf:.2f} would promote, but ts={ts:.2f} < "
                f"{_BUNDLE_PROMOTE_TS_FLOOR} for a bundle; held as possible"
            )

        # Promote to FOUND using the combined confidence score
        # (70% title similarity + 30% author overlap from scoring.py).
        # The old threshold was: pct >= 50 AND author_matched (boolean).
        # The new threshold uses the blended score which already accounts
        # for both title and author quality in one number.
        if conf >= MATCH_PROMOTE_SCORE and not promote_blocked_by_bundle:
            result["status"] = STATUS_FOUND
            result["passes_tried"].append(pass_num)
            result["mam_url"] = _torrent_url(best["torrent_id"])
            result["mam_torrent_id"] = best["torrent_id"]
            result["mam_title"] = best["mam_title"]
            result["mam_formats"] = best["format_str"]
            result["mam_category"] = best.get("category", "") or ""
            result["mam_has_multiple"] = has_multiple
            result["mam_my_snatched"] = best.get("my_snatched", False)
            result["mam_is_bundle"] = is_bundle
            result["match_pct"] = pct
            result["confidence"] = conf
            result["best_format"] = best.get("best_format", "")
            return True  # stop cascade

        # Otherwise save as best possible so far
        if best_possible is None or conf > best_possible.get("confidence", 0):
            best_possible = candidate
        return False

    try:
        # --- Pass 1: author + full title ---
        r = await _mam_search(token, authors, title, lang_ids=lang_ids, content_type=content_type)
        await asyncio.sleep(delay)
        result["passes_tried"].append(1)
        if _try_evaluate(1, r, title):
            return result

        # --- Pass 2: author + core title (volume prefix stripped) ---
        core = _extract_core_title(title)
        if core:
            r = await _mam_search(token, authors, core, lang_ids=lang_ids, content_type=content_type)
            await asyncio.sleep(delay)
            if _try_evaluate(2, r, core):
                return result

        # --- Pass 3: author + subtitle right (part after colon) ---
        sub_right = _extract_subtitle_part(title)
        if sub_right and sub_right != core:
            r = await _mam_search(token, authors, sub_right, lang_ids=lang_ids, content_type=content_type)
            await asyncio.sleep(delay)
            if _try_evaluate(3, r, sub_right):
                return result

        # --- Pass 4: author + short title (part before colon) ---
        short = _strip_subtitle(title)
        if short and short != title and short != core:
            r = await _mam_search(token, authors, short, lang_ids=lang_ids, content_type=content_type)
            await asyncio.sleep(delay)
            if _try_evaluate(4, r, short):
                return result

        # --- Pass 5: title only (no author), loose cleaning ---
        title_only = core or sub_right or short or title
        r = await _mam_search(token, None, title_only, lang_ids=lang_ids, content_type=content_type)
        await asyncio.sleep(delay)
        if _try_evaluate(5, r, title_only):
            return result

    except _AuthError as e:
        result["status"] = STATUS_AUTH_ERROR
        result["error"] = str(e)
        return result

    # No pass hit promotion — use best possible if we have one
    if best_possible:
        result["status"] = STATUS_POSSIBLE
        result["mam_url"] = _torrent_url(best_possible["torrent_id"])
        result["mam_torrent_id"] = best_possible["torrent_id"]
        result["mam_title"] = best_possible["mam_title"]
        result["mam_formats"] = best_possible["formats"]
        result["mam_category"] = best_possible.get("category", "") or ""
        result["mam_has_multiple"] = best_possible["has_multiple"]
        result["mam_my_snatched"] = best_possible.get("my_snatched", False)
        result["mam_is_bundle"] = best_possible.get("is_bundle", False)
        result["match_pct"] = best_possible["match_pct"]
        result["best_format"] = best_possible.get("best_format", "")
        result["passes_tried"] = [best_possible["pass"]]

    return result


# ---------------------------------------------------------------------------
# Debug cascade — mirrors check_book but emits a structured trace
# ---------------------------------------------------------------------------
# Used by the (toggle-gated) /api/v1/mam/debug-match endpoint to surface
# everything the production cascade considers when scoring a result:
# raw response shape, per-result field names, both score variants
# (vs. calibre title and vs. per-pass search title) with their full
# breakdowns, and the would-have-been-promote/demote decision per result.
#
# Stays parallel to check_book on purpose — when production scoring
# changes, this function should change with it. Don't share state with
# check_book at the cost of clarity; the debug surface needs to reflect
# real behavior, and a divergent debug view is worse than no debug view.

async def debug_check_book(
    token: str,
    title: str,
    authors: str,
    series_name: str = "",
    content_type: str = "ebook",
    lang_ids: Optional[list[int]] = None,
    delay: float = 0.5,
) -> dict:
    """Run the cascade for one book and return a full structured trace.

    Trace shape:
      {
        "input": {...},
        "passes": [
          {
            "pass_num": 1,
            "search_title": "...",
            "search_author": "..." | None,
            "raw_response_keys": [...],
            "raw_total_found": int | None,
            "result_count_returned": int,
            "first_result_full": {raw item dict},  # for schema discovery
            "results": [
              {
                "torrent_id", "mam_title", "mam_authors",
                "all_keys": [...],
                "category", "filetype", "language", "lang_code",
                "seeders", "my_snatched",
                "numfiles_field", "files_field", "filecount_field",
                "description_field_present", "description_sample",
                "score_vs_calibre_title": {breakdown},
                "score_vs_search_title": {breakdown},
                "confidence_max", "decision",
              }, ...
            ]
          }, ...
        ],
        "thresholds": {"min": ..., "promote": ...},
      }
    """
    if not lang_ids:
        lang_ids = [_ENGLISH_LANG_ID]

    trace: dict = {
        "input": {
            "title": title,
            "authors": authors,
            "series": series_name,
            "content_type": content_type,
            "lang_ids": lang_ids,
        },
        "thresholds": {
            "min": MATCH_MIN_SCORE,
            "promote": MATCH_PROMOTE_SCORE,
        },
        "passes": [],
    }

    # Build the same pass list check_book uses, in order. We always run
    # all five (no short-circuit on promote) because the debug view's
    # whole point is to show every pass, even ones a real scan would
    # have skipped after an early hit.
    core = _extract_core_title(title)
    sub_right = _extract_subtitle_part(title)
    short = _strip_subtitle(title)
    title_only = core or sub_right or short or title

    passes_to_run: list[tuple[int, Optional[str], str]] = [(1, authors, title)]
    if core:
        passes_to_run.append((2, authors, core))
    if sub_right and sub_right != core:
        passes_to_run.append((3, authors, sub_right))
    if short and short != title and short != core:
        passes_to_run.append((4, authors, short))
    passes_to_run.append((5, None, title_only))

    for pass_num, pass_authors, search_title in passes_to_run:
        pass_trace: dict = {
            "pass_num": pass_num,
            "search_title": search_title,
            "search_author": pass_authors,
            "raw_response_keys": [],
            "raw_total_found": None,
            "result_count_returned": 0,
            "first_result_full": None,
            "results": [],
        }

        try:
            resp = await _mam_search(
                token, pass_authors, search_title,
                lang_ids=lang_ids, content_type=content_type,
            )
        except _AuthError as e:
            pass_trace["error"] = f"auth_error: {e}"
            trace["passes"].append(pass_trace)
            break
        except Exception as e:
            pass_trace["error"] = f"exception: {e}"
            trace["passes"].append(pass_trace)
            continue

        await asyncio.sleep(delay)

        if not resp:
            pass_trace["error"] = "empty_response"
            trace["passes"].append(pass_trace)
            continue

        pass_trace["raw_response_keys"] = sorted(resp.keys())
        pass_trace["raw_total_found"] = (
            resp.get("found") or resp.get("total_found") or resp.get("total")
        )
        data = resp.get("data") or []
        pass_trace["result_count_returned"] = len(data)
        if data:
            # Capture the FIRST raw item verbatim so the endpoint user can see
            # MAM's actual schema (field names, value types, presence/absence
            # of description/filelist/numfiles). Subsequent items only get
            # their key list to keep the payload manageable.
            pass_trace["first_result_full"] = data[0]

        for idx, item in enumerate(data):
            mam_title = str(item.get("title") or item.get("name") or "")
            mam_authors = _parse_author_info(item.get("author_info"))

            score_full_breakdown = score_match_with_breakdown(
                record_title=mam_title, record_authors=mam_authors,
                search_title=title, search_authors=authors,
                known_series=series_name,
            )
            score_search_breakdown = score_match_with_breakdown(
                record_title=mam_title, record_authors=mam_authors,
                search_title=search_title, search_authors=authors,
                known_series=series_name,
            )
            confidence = max(
                score_full_breakdown["confidence"],
                score_search_breakdown["confidence"],
            )

            if confidence < MATCH_MIN_SCORE:
                decision = "skipped_below_min"
            elif confidence >= MATCH_PROMOTE_SCORE:
                decision = "would_promote_to_found"
            else:
                decision = "kept_as_possible"

            pass_trace["results"].append({
                "torrent_id": str(item.get("id", "")),
                "mam_title": mam_title,
                "mam_authors": mam_authors,
                "all_keys": sorted(item.keys()) if idx > 0 else None,
                "category": str(item.get("category") or ""),
                "filetype": str(item.get("filetype") or item.get("filetypes") or ""),
                "language": item.get("language"),
                "lang_code": item.get("lang_code"),
                "seeders": item.get("seeders"),
                "my_snatched": bool(item.get("my_snatched")),
                # Probe likely field names for bundle detection — Part B
                # uses whichever of these is present.
                "numfiles_field": item.get("numfiles"),
                "files_field": item.get("files"),
                "filecount_field": item.get("filecount"),
                "description_field_present": "description" in item,
                "description_sample": str(item.get("description") or "")[:300],
                "score_vs_calibre_title": score_full_breakdown,
                "score_vs_search_title": score_search_breakdown,
                "confidence_max": round(confidence, 4),
                "decision": decision,
            })

        trace["passes"].append(pass_trace)

    return trace


# ---------------------------------------------------------------------------
# Batch scanning — processes books from the DB
# ---------------------------------------------------------------------------

async def scan_books_batch(
    db,
    session_id: str,
    limit: int = 100,
    delay: float = DEFAULT_DELAY,
    skip_ip_update: bool = True,
    format_priority: list[str] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    lang_ids: Optional[list[int]] = None,
    book_ids: Optional[list[int]] = None,
    content_type: str = "ebook",
) -> dict:
    """
    Scan a batch of books that don't yet have MAM data.

    Returns {"scanned": int, "found": int, "possible": int,
             "not_found": int, "errors": int, "error": str|None}.

    Two scan-set modes:
      - `book_ids` provided  → scan exactly that ID set (snapshot mode).
      - `book_ids=None`      → query whatever currently matches the
                                `_NEEDS_SCAN_BASIC_*` predicate (never-scanned
                                rows plus rescannable possible/not_found rows).

    Snapshot mode is what orchestrators use when concurrent author scans
    may be adding new books mid-run: any books added during THIS scan
    won't be picked up — they wait for the next MAM scan, which is what
    the user expects (otherwise the queue would silently grow forever).
    """
    if format_priority is None:
        format_priority = (
            DEFAULT_AUDIOBOOK_FORMAT_PRIORITY if content_type == "audiobook"
            else DEFAULT_FORMAT_PRIORITY
        )

    # Register IP first
    ip_result = await register_ip(session_id, skip_ip_update)
    if not ip_result["success"]:
        return {"scanned": 0, "found": 0, "possible": 0, "not_found": 0,
                "errors": 0, "error": f"IP registration failed: {ip_result['message']}"}

    if book_ids is not None:
        if not book_ids:
            return {"scanned": 0, "found": 0, "possible": 0, "not_found": 0,
                    "errors": 0, "error": None}
        placeholders = ",".join("?" * len(book_ids))
        rows = await db.execute_fetchall(f"""
            SELECT b.id, b.title, a.name as author_name, b.owned, b.is_unreleased,
                   s.name as series_name
            FROM books b
            JOIN authors a ON b.author_id = a.id
            LEFT JOIN series s ON b.series_id = s.id
            WHERE b.id IN ({placeholders})
            ORDER BY b.owned DESC, b.id ASC
        """, tuple(book_ids))
    else:
        rows = await db.execute_fetchall(f"""
            SELECT b.id, b.title, a.name as author_name, b.owned, b.is_unreleased,
                   s.name as series_name
            FROM books b
            JOIN authors a ON b.author_id = a.id
            LEFT JOIN series s ON b.series_id = s.id
            WHERE {_NEEDS_SCAN_BASIC_ALIASED}
            ORDER BY b.owned DESC, b.id ASC
            LIMIT ?
        """, (limit,))

    if not rows:
        logger.info("MAM scan: no books need scanning")
        return {"scanned": 0, "found": 0, "possible": 0, "not_found": 0,
                "errors": 0, "error": None}

    logger.info(f"MAM scan: processing {len(rows)} books (limit={limit})")
    stats = {"scanned": 0, "found": 0, "possible": 0, "not_found": 0, "errors": 0,
             "current_book": "", "error": None}

    for i, row in enumerate(rows):
        book_id, book_title, author_name = row[0], row[1], row[2]
        book_series = row[5] if len(row) > 5 else ""

        # Yield to concurrent source scans. `state._source_scan_refs` is
        # incremented by lookup_author and decremented in its finally.
        # SQLite's single-writer lock means a long MAM batch (tens of
        # UPDATE books per minute) can starve a source scan's merge
        # writes past the 30s busy_timeout — observed in v1.1.9-dev2
        # where Amazon's whole merge lost the race. Pausing here before
        # the HTTP + UPDATE cycle lets the source scan grab the writer
        # lock cleanly; MAM resumes on the next iteration with no lost
        # progress. 20-minute cap is a safety net for a stuck refcount
        # (shouldn't happen — finally block guarantees decrement) so a
        # bug can't strand MAM forever.
        #
        # CRITICAL: commit before the pause-sleep loop. The previous
        # iteration's UPDATE books call started an implicit transaction
        # that only flushes at the per-book `db.commit()` below. Without
        # the explicit commit here, MAM's uncommitted writer transaction
        # would keep the writer lock for however long the source scan
        # runs — which re-creates the exact starvation bug we're trying
        # to prevent. v1.1.9-dev3 testing confirmed: Goodreads spent 30s
        # blocked on UPDATE authors while MAM sat paused with its last
        # UPDATE uncommitted.
        if state._source_scan_refs > 0:
            await db.commit()
            logger.info(
                f"MAM [{i+1}/{len(rows)}] paused — {state._source_scan_refs} "
                f"source scan(s) in progress"
            )
            paused_at = asyncio.get_event_loop().time()
            while state._source_scan_refs > 0:
                if asyncio.get_event_loop().time() - paused_at > 1200:
                    logger.warning(
                        f"MAM [{i+1}/{len(rows)}] paused 20min — refcount "
                        f"stuck at {state._source_scan_refs}, resuming anyway"
                    )
                    break
                await asyncio.sleep(1.0)
            else:
                logger.info(f"MAM [{i+1}/{len(rows)}] resumed — source scan finished")

        logger.debug(f"MAM [{i+1}/{len(rows)}] {book_title[:65]} — {author_name[:35]}")

        # Surface the title BEFORE the network call so the progress widget
        # shows what we're waiting on, not what we just finished. MAM shows
        # every attempt — no filter-noise to hide here.
        stats["current_book"] = book_title
        if on_progress:
            on_progress(dict(stats))

        check = await check_book(session_id, book_title, author_name, format_priority, delay, lang_ids=lang_ids, series_name=book_series or "", content_type=content_type)
        stats["scanned"] += 1

        # Write result to DB
        await db.execute("""
            UPDATE books SET mam_url=?, mam_status=?, mam_formats=?,
                   mam_torrent_id=?, mam_category=?, mam_has_multiple=?, mam_my_snatched=?,
                   mam_is_bundle=?
            WHERE id=?
        """, (
            check["mam_url"],
            check["status"],
            check["mam_formats"],
            check["mam_torrent_id"],
            check.get("mam_category", "") or "",
            1 if check["mam_has_multiple"] else 0,
            1 if check.get("mam_my_snatched") else 0,
            1 if check.get("mam_is_bundle") else 0,
            book_id,
        ))

        if check["status"] == STATUS_FOUND:
            stats["found"] += 1
        elif check["status"] == STATUS_POSSIBLE:
            stats["possible"] += 1
        elif check["status"] == STATUS_AUTH_ERROR:
            stats["errors"] += 1
            stats["error"] = check.get("error", "Auth error")
            logger.error(f"MAM auth error — stopping scan: {check.get('error')}")
            await db.commit()
            return stats
        elif check["status"] == STATUS_ERROR:
            stats["errors"] += 1
        else:
            stats["not_found"] += 1

        if on_progress:
            on_progress(dict(stats))

        # Commit per-book. Was every-10-books, but with rate_mam=2s the
        # writer transaction stayed open for ~20s between commits and
        # user-originated writes (Hide/Dismiss/Delete/Approve MAM from
        # the sidebar, edit Save) queued behind it until the 30s SQLite
        # busy_timeout expired. Per-book commit drops the hold time to
        # just the UPDATE itself (~ms), so user clicks feel instant
        # during an active scan. WAL + synchronous=NORMAL makes the
        # extra commits effectively free.
        await db.commit()

        if cancel_check and cancel_check():
            logger.info(f"MAM scan: pause requested after {stats['scanned']} books")
            return stats

    logger.info(f"MAM scan complete: {stats}")
    return stats


# ---------------------------------------------------------------------------
# Full scan management
# ---------------------------------------------------------------------------

async def start_full_scan(db) -> dict:
    """Start a full MAM scan. Creates a tracking row in mam_scan_log.

    The eligible book IDs are snapshotted up-front and stored as a JSON
    array in `mam_scan_log.book_ids_snapshot`. Subsequent batches consume
    slices of this list rather than re-querying `WHERE mam_status IS NULL`,
    so a concurrent author/source scan that adds new books mid-run does
    NOT inflate the queue — those books wait for the next full scan,
    matching the manual MAM scan's snapshot behavior.

    Batch size is 400; full scans take many batches with a 5-minute pause
    between them (see run_full_scan_batch + the orchestrator loop).
    """
    running = await db.execute_fetchall(
        "SELECT id FROM mam_scan_log WHERE status='running'"
    )
    if running:
        return {"error": "A full scan is already in progress"}

    id_rows = await db.execute_fetchall(f"""
        SELECT id FROM books
        WHERE {_NEEDS_SCAN_STRICT_BARE}
        ORDER BY owned DESC, id ASC
    """)
    snapshot = [r[0] for r in id_rows]
    total = len(snapshot)

    if total == 0:
        return {"error": "No books need scanning — all books already have MAM data"}

    now = time.time()
    cursor = await db.execute(
        """INSERT INTO mam_scan_log (total_books, last_offset, batch_size, started_at, status, book_ids_snapshot)
           VALUES (?, 0, 400, ?, 'running', ?)""",
        (total, now, json.dumps(snapshot))
    )
    scan_id = cursor.lastrowid
    await db.commit()
    logger.info(f"Full MAM scan started: {total} books snapshotted, scan_id={scan_id}")
    return {"id": scan_id, "total_books": total}


async def run_full_scan_batch(
    db,
    session_id: str,
    skip_ip_update: bool = True,
    delay: float = DEFAULT_DELAY,
    format_priority: list[str] = None,
    lang_ids: Optional[list[int]] = None,
    on_book: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
    content_type: str = "ebook",
) -> dict:
    """
    Run one batch of a full scan (400 books per batch).

    Consumes the snapshot stored in `mam_scan_log.book_ids_snapshot`,
    sliced by `last_offset → last_offset + batch_size`. Only those exact
    IDs are processed, so a concurrent author/source scan adding new
    books mid-run does NOT inflate this scan's queue.

    If `book_ids_snapshot` is NULL — possible only for a scan started on
    an older binary before the snapshot column existed — falls back to
    the legacy `WHERE mam_status IS NULL` path so an in-progress scan
    survives an upgrade.

    Returns {"status": "batch_complete"|"scan_complete"|"error"|"no_scan", ...}.
    """
    if format_priority is None:
        format_priority = (
            DEFAULT_AUDIOBOOK_FORMAT_PRIORITY if content_type == "audiobook"
            else DEFAULT_FORMAT_PRIORITY
        )

    rows = await db.execute_fetchall(
        "SELECT id, total_books, last_offset, batch_size, book_ids_snapshot "
        "FROM mam_scan_log WHERE status='running' LIMIT 1"
    )
    if not rows:
        return {"status": "no_scan", "scanned": 0, "remaining": 0, "next_batch_in_seconds": None}

    scan_id, total_books, last_offset, batch_size, snapshot_json = rows[0]

    # Register IP
    ip_result = await register_ip(session_id, skip_ip_update)
    if not ip_result["success"]:
        return {"status": "error", "scanned": 0, "remaining": 0,
                "next_batch_in_seconds": None,
                "error": f"IP registration failed: {ip_result['message']}"}

    # Snapshot path (current) or legacy WHERE mam_status IS NULL path.
    if snapshot_json:
        try:
            snapshot_ids = json.loads(snapshot_json)
        except (ValueError, TypeError):
            snapshot_ids = []
        batch_ids = snapshot_ids[last_offset:last_offset + batch_size]
        if not batch_ids:
            await db.execute(
                "UPDATE mam_scan_log SET status='complete', finished_at=? WHERE id=?",
                (time.time(), scan_id)
            )
            await db.commit()
            logger.info(f"Full MAM scan complete (snapshot exhausted, scan_id={scan_id})")
            return {"status": "scan_complete", "scanned": 0, "remaining": 0, "next_batch_in_seconds": None}
        placeholders = ",".join("?" * len(batch_ids))
        book_rows = await db.execute_fetchall(f"""
            SELECT b.id, b.title, a.name as author_name
            FROM books b
            JOIN authors a ON b.author_id = a.id
            WHERE b.id IN ({placeholders})
            ORDER BY b.owned DESC, b.id ASC
        """, tuple(batch_ids))
    else:
        book_rows = await db.execute_fetchall(f"""
            SELECT b.id, b.title, a.name as author_name
            FROM books b
            JOIN authors a ON b.author_id = a.id
            WHERE {_NEEDS_SCAN_STRICT_ALIASED}
            ORDER BY b.owned DESC, b.id ASC
            LIMIT ?
        """, (batch_size,))

    if not book_rows:
        await db.execute(
            "UPDATE mam_scan_log SET status='complete', finished_at=? WHERE id=?",
            (time.time(), scan_id)
        )
        await db.commit()
        logger.info(f"Full MAM scan complete (scan_id={scan_id})")
        return {"status": "scan_complete", "scanned": 0, "remaining": 0, "next_batch_in_seconds": None}

    logger.info(f"Full scan batch: {len(book_rows)} books (scan_id={scan_id})")
    scanned = 0
    # Running batch-local tallies so on_progress can fire after every
    # book. The caller (router's _full_scan_loop closure) adds these
    # onto baselines carried over from previous batches so the unified
    # Dashboard widget ticks up in real time instead of jumping after
    # each 5-minute batch boundary.
    found = 0
    possible = 0
    not_found = 0
    errors = 0

    for i, row in enumerate(book_rows):
        book_id, book_title, author_name = row

        # Per-book progress hook (same contract as scan_books_batch): fire
        # BEFORE the network call so the widget shows what we're waiting on.
        if on_book:
            on_book(book_title)

        check = await check_book(session_id, book_title, author_name, format_priority, delay, lang_ids=lang_ids, content_type=content_type)
        scanned += 1

        await db.execute("""
            UPDATE books SET mam_url=?, mam_status=?, mam_formats=?,
                   mam_torrent_id=?, mam_has_multiple=?, mam_my_snatched=?,
                   mam_is_bundle=?
            WHERE id=?
        """, (
            check["mam_url"], check["status"], check["mam_formats"],
            check["mam_torrent_id"], 1 if check["mam_has_multiple"] else 0,
            1 if check.get("mam_my_snatched") else 0,
            1 if check.get("mam_is_bundle") else 0,
            book_id,
        ))

        # Tally + fire on_progress. Done AFTER the DB write so a
        # mid-batch crash doesn't leave the widget showing counts
        # that don't match what's persisted.
        status = check["status"]
        if status == "found":
            found += 1
        elif status == "possible":
            possible += 1
        elif status == "not_found":
            not_found += 1
        elif status == STATUS_AUTH_ERROR:
            errors += 1

        if on_progress:
            on_progress({
                "scanned": scanned,
                "found": found,
                "possible": possible,
                "not_found": not_found,
                "errors": errors,
                "current_book": book_title,
            })

        if status == STATUS_AUTH_ERROR:
            logger.error(f"Full scan auth error — pausing")
            await db.execute(
                "UPDATE mam_scan_log SET last_offset=last_offset+?, status='auth_error' WHERE id=?",
                (scanned, scan_id)
            )
            await db.commit()
            return {"status": "error", "scanned": scanned,
                    "found": found, "possible": possible,
                    "not_found": not_found, "errors": errors,
                    "remaining": total_books - last_offset - scanned,
                    "next_batch_in_seconds": None, "error": check.get("error")}

        if (i + 1) % 10 == 0:
            await db.commit()

    # Update progress
    new_offset = last_offset + scanned
    await db.execute(
        "UPDATE mam_scan_log SET last_offset=? WHERE id=?",
        (new_offset, scan_id)
    )
    await db.commit()

    # Remaining: snapshot path uses (total - processed). Legacy path
    # COUNTs `WHERE mam_status IS NULL` because there's no snapshot to
    # diff against.
    if snapshot_json:
        remaining = max(0, total_books - new_offset)
    else:
        remaining_row = await db.execute_fetchall(f"""
            SELECT COUNT(*) FROM books
            WHERE {_NEEDS_SCAN_STRICT_BARE}
        """)
        remaining = remaining_row[0][0] if remaining_row else 0

    if remaining == 0:
        await db.execute(
            "UPDATE mam_scan_log SET status='complete', finished_at=? WHERE id=?",
            (time.time(), scan_id)
        )
        await db.commit()
        logger.info(f"Full MAM scan complete (scan_id={scan_id})")
        return {"status": "scan_complete", "scanned": scanned,
                "found": found, "possible": possible,
                "not_found": not_found, "errors": errors,
                "remaining": 0, "next_batch_in_seconds": None}

    logger.info(f"Full scan batch done: {scanned} scanned, {remaining} remaining "
                f"(found={found}, possible={possible}, not_found={not_found})")
    return {"status": "batch_complete", "scanned": scanned,
            "found": found, "possible": possible,
            "not_found": not_found, "errors": errors,
            "remaining": remaining, "next_batch_in_seconds": 300}


async def cancel_full_scan(db) -> dict:
    rows = await db.execute_fetchall("SELECT id FROM mam_scan_log WHERE status='running'")
    if not rows:
        return {"success": False, "message": "No running scan to cancel"}
    await db.execute(
        "UPDATE mam_scan_log SET status='cancelled', finished_at=? WHERE id=?",
        (time.time(), rows[0][0])
    )
    await db.commit()
    logger.info(f"Full MAM scan cancelled (scan_id={rows[0][0]})")
    return {"success": True, "message": "Full scan cancelled"}


async def get_full_scan_status(db) -> dict:
    rows = await db.execute_fetchall("""
        SELECT id, total_books, last_offset, batch_size, started_at, finished_at, status
        FROM mam_scan_log ORDER BY started_at DESC LIMIT 1
    """)
    if not rows:
        return {"active": False, "status": None}
    scan_id, total, offset, batch, started, finished, status = rows[0]
    return {
        "active": status == "running",
        "scan_id": scan_id, "total_books": total, "scanned": offset,
        "batch_size": batch, "status": status,
        "started_at": started, "finished_at": finished,
        "progress_pct": round(offset / max(total, 1) * 100, 1),
    }


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

async def get_mam_stats(db) -> dict:
    upload_row = await db.execute_fetchall(
        "SELECT COUNT(*) FROM books WHERE owned=1 AND mam_status='not_found' AND hidden=0"
    )
    download_row = await db.execute_fetchall(
        "SELECT COUNT(*) FROM books WHERE owned=0 AND mam_status IN ('found','possible') AND is_unreleased=0 AND hidden=0"
    )
    nowhere_row = await db.execute_fetchall(
        "SELECT COUNT(*) FROM books WHERE owned=0 AND mam_status='not_found' AND is_unreleased=0 AND hidden=0"
    )
    # `total_scanned` excludes `not_applicable` because that status is
    # set by the user via the Skip MAM button — those rows were never
    # actually scanned. v2.3.7.
    scanned_row = await db.execute_fetchall(
        "SELECT COUNT(*) FROM books WHERE mam_status IS NOT NULL "
        "AND mam_status != 'not_applicable' AND hidden=0"
    )
    unscanned_row = await db.execute_fetchall(
        f"SELECT COUNT(*) FROM books WHERE {_NEEDS_SCAN_BASIC_BARE}"
    )
    skipped_row = await db.execute_fetchall(
        "SELECT COUNT(*) FROM books WHERE mam_status='not_applicable' AND hidden=0"
    )
    return {
        "upload_candidates": upload_row[0][0] if upload_row else 0,
        "available_to_download": download_row[0][0] if download_row else 0,
        "missing_everywhere": nowhere_row[0][0] if nowhere_row else 0,
        "total_scanned": scanned_row[0][0] if scanned_row else 0,
        "total_unscanned": unscanned_row[0][0] if unscanned_row else 0,
        "total_skipped": skipped_row[0][0] if skipped_row else 0,
    }
