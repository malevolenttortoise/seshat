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
import sqlite3
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


def _recent_scan_cutoff_seconds(settings: Optional[dict] = None) -> float:
    """Return the cutoff window (seconds) for the recently-scanned skip.

    Reads `mam_recent_scan_skip_days` from settings (default 7). 0 or
    negative disables the skip — every eligible book is in the queue
    regardless of last-scan timestamp. Used by `_recent_scan_skip_clause`
    and `_recent_scan_order_clause` below.
    """
    if settings is None:
        from app.config import load_settings
        settings = load_settings()
    days = settings.get("mam_recent_scan_skip_days", 7)
    try:
        days = float(days)
    except (TypeError, ValueError):
        days = 7.0
    if days <= 0:
        return 0.0
    return days * 86400.0


def _recent_scan_skip_clause(
    cutoff_seconds: float, prefix: str = ""
) -> str:
    """Build the SQL fragment that excludes recently-scanned books.

    Returns either an empty string (cutoff disabled) or
    " AND ({prefix}mam_last_scanned_at IS NULL OR {prefix}mam_last_scanned_at < <unix_ts>)".
    The cutoff timestamp is computed at call time, not stored — every
    query gets a fresh "now - window" value.

    `prefix` is "" for bare queries or "b." for queries that JOIN
    authors and need a table-qualified column reference.
    """
    if cutoff_seconds <= 0:
        return ""
    cutoff_ts = time.time() - cutoff_seconds
    col = f"{prefix}mam_last_scanned_at"
    return f" AND ({col} IS NULL OR {col} < {cutoff_ts})"


def _recent_scan_order_clause(prefix: str = "") -> str:
    """Build the ORDER BY fragment for oldest-first scan ordering.

    Returns "{prefix}owned DESC, COALESCE({prefix}mam_last_scanned_at, 0) ASC, {prefix}id ASC"
    so the eligible set is processed in:
      1. Owned books first (priority by ownership)
      2. Never-scanned (NULL → 0) before any scanned book
      3. Oldest-scanned before newest-scanned
      4. Stable id tiebreaker
    Pairs with `_recent_scan_skip_clause` to give libraries full scan
    coverage over time even with small batch sizes.
    """
    col = f"{prefix}mam_last_scanned_at"
    return (
        f"{prefix}owned DESC, "
        f"COALESCE({col}, 0) ASC, "
        f"{prefix}id ASC"
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
# the description-verification path can promote with confidence.
_BUNDLE_PROMOTE_TS_FLOOR = 0.85

# Master switch for bundle URL verification. Fires when the bundle cap
# path needs a tiebreaker (bundle + author overlap + ts <
# BUNDLE_PROMOTE_TS_FLOOR). The verification path fetches the bundle's
# torrent description via the documented Search JSON API (TOS 1.7
# approved automation list) and checks whether the searched title
# appears as a structured list entry. A match promotes the bundle to
# Found regardless of the blended confidence score.
#
# A previous filelist-based signal was REMOVED in v2.4.0 — MAM staff
# confirmed mbsc browser-tier scraping isn't on the approved automation
# list. See feedback_mam_mbsc_filelist_tos.md +
# project_seshat_filelist_future_reenable.md.
_BUNDLE_VERIFICATION_ENABLED = True

# ── Cover-image verification (Part C) ──────────────────────────
# Multi-candidate cover-pHash ranker for MAM URL verification. When
# the searched book has a cover hash on its books row, fetch covers
# for the top-N text candidates, compute pHash distance against the
# searched book's cover, and:
#   - PROMOTE the candidate if any non-bundle candidate has distance
#     <= _COVER_PROMOTE_DIST_MAX (replacing the text-score winner)
#   - DEMOTE candidates whose distance >= _COVER_DEMOTE_DIST_MIN
#     (filtered out before the format-priority pick)
#
# Validated 2026-05-09 against 16 cover pairs from Mark's library:
# right-Possibles cluster at distance 0-6, wrong-matches at 28-36,
# with a 22-bit empty band between. See project_seshat_mam_url_confidence
# memory for the experiment + threshold derivation.
#
# Bundles are EXCLUDED from cover verification — MAM bundle covers
# show the bundle's art (omnibus/series cover), not the individual
# book's cover, so cover-pHash would always look like "wrong" for
# legitimate bundle matches. Bundle URL verification (description-based,
# the existing path) owns bundle decisions.
#
# The two thresholds are gated SEPARATELY: promotion ships first
# (safe under any Cohort C rate — false-promote requires distance
# <= 10 which only happens for same-image), demotion ships as a
# follow-up after production data accumulates in the deadband.
_COVER_VERIFICATION_ENABLED = True   # production-enabled 2026-05-09 (v2.4.0)
_COVER_DEMOTION_ENABLED = True       # promoter-anchored mode — see below
_COVER_PROMOTE_DIST_MAX = 10         # pHash <= → promote (data: max(right)=6)
_COVER_DEMOTE_DIST_MIN = 22          # pHash >= → demote (data: min(wrong)=22)
_COVER_TOPN_CANDIDATES = 10          # bumped 5→10 after UAT showed Veil/Raw
                                     # cases where the right candidate sat
                                     # outside top-5 due to text-conf ties

# Promoter-anchored demotion: filter cover-distant candidates ONLY when
# at least one candidate also has a cover-promote signal. Rationale: A3
# UAT (2026-05-09) found 3 Cohort C examples (Raw Bk1, Incarceron, MMM)
# at distances 28-34 — squarely inside the wrong band. Aggressive
# demotion would falsely-reject these. Anchoring to a promoter means
# we only filter when cover-pHash has decisive positive evidence to
# anchor the verdict; without a promoter we fall back to text-only
# behavior (no regression on Cohort C). See project_seshat_mam_url_confidence.

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


# Trailing bare-number stripping for series volumes that MAM may
# zero-pad differently. Mark's "Right of Retribution 2" failed to
# match MAM's "Right of Retribution 02" because MAM tokenizes "2"
# and "02" as different search terms; stripping the trailing number
# entirely lets the cascade find both volumes (cover-pHash sorts
# the right one out per Part C). Anchored to title end so we don't
# strip mid-title numbers like "1984" or "Apollo 11".
_RE_TRAILING_VOLUME = re.compile(r"\s+\d+\s*$")


def _alternate_title_forms(title: str) -> list[str]:
    """Return alternate title forms for MAM-search variant passes.

    Currently handles trailing-number stripping (Right of Retribution 2
    → Right of Retribution; Domestic Decay 2 → Domestic Decay). Returns
    only forms that DIFFER from the input — the caller dedupes against
    its already-tried passes.
    """
    if not title:
        return []
    out: list[str] = []
    m = _RE_TRAILING_VOLUME.search(title)
    if m:
        stripped = title[: m.start()].strip()
        if len(stripped) >= 3 and stripped != title:
            out.append(stripped)
    return out


def _alternate_author_forms(author: str) -> list[str]:
    """Return alternate author tokenization forms for MAM-search variant passes.

    Multi-initial authors like "J J Cross" / "P. G. Wodehouse" can be
    indexed on MAM as "JJ Cross" / "P.G. Wodehouse" (or vice versa),
    and MAM's combined-text search treats space-separated initials as
    distinct tokens that don't match the no-space form. Generates:

      "J J Cross"      → ["JJ Cross", "J.J. Cross"]
      "P. G. Wodehouse"→ ["P G Wodehouse", "PG Wodehouse"]
      "JK Rowling"     → ["J K Rowling", "J.K. Rowling"]

    Triggered when the author has 2+ single-letter tokens (with or
    without periods) followed by at least one longer token. Returns only
    forms that DIFFER from the input so the caller can dedupe.
    """
    if not author:
        return []
    tokens = author.split()
    initials: list[str] = []
    rest: list[str] = []
    for t in tokens:
        bare = t.rstrip(".")
        if len(bare) == 1 and bare.isalpha():
            initials.append(bare)
        else:
            rest.append(t)
    if len(initials) < 2 or not rest:
        # Detect concatenated-initials form like "JK Rowling" (single
        # multi-letter all-uppercase token followed by surname).
        if (
            len(tokens) >= 2
            and tokens[0].isalpha()
            and 2 <= len(tokens[0]) <= 4
            and tokens[0].isupper()
            and tokens[0].rstrip(".") == tokens[0]  # no periods
        ):
            split_initials = list(tokens[0])
            spaced = " ".join(split_initials) + " " + " ".join(tokens[1:])
            with_periods = "".join(i + "." for i in split_initials) + " " + " ".join(tokens[1:])
            return [v for v in (spaced, with_periods) if v != author]
        return []
    rest_part = " ".join(rest)
    concat = "".join(initials) + " " + rest_part
    with_periods = "".join(i + "." for i in initials) + " " + rest_part
    out = [v for v in (concat, with_periods) if v != author]
    # Dedupe in case both variants happen to equal each other.
    seen: set[str] = set()
    deduped: list[str] = []
    for v in out:
        if v not in seen:
            seen.add(v)
            deduped.append(v)
    return deduped


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

# mbsc browser-session cookie state was REMOVED in v2.4.0 after MAM
# staff confirmed mbsc-based scraping (filelist.php fetches) is not on
# Section 1.7's approved automation list. See feedback_mam_mbsc_filelist_tos.md
# for the staff exchange. Bundle URL verification now relies solely on
# the description-based path (Phase 4), which uses the documented
# Search JSON API + tor.id + description flag — explicitly TOS-allowed.
# Future re-enable IF MAM exposes filelist via the documented API:
# project_seshat_filelist_future_reenable.md.


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


def _extract_cookie_value(
    response: httpx.Response, name: str
) -> Optional[str]:
    """Extract a cookie value from a MAM response by name.

    Reads through httpx's parsed cookie jar exclusively — the jar
    correctly honors RFC 6265 expiration semantics (`Max-Age=0`,
    `Expires=` in the past), so a deletion sentinel like
    `Set-Cookie: mbsc=deleted; Max-Age=0` does NOT appear here and
    we won't mistake it for a fresh rotation.

    Earlier versions had a regex fallback against raw Set-Cookie
    headers for "edge cases where httpx drops cookies due to missing
    attributes" — defensive code with no real-world trigger. The
    fallback ignored expiration attributes and captured deletion
    sentinels as rotations, which on 2026-05-09 corrupted both
    `_current_token` and `_current_mbsc_token` after MAM served a
    logout response on a rejected filelist fetch (which then poisoned
    every subsequent search with HTTP 403). Don't add it back.
    """
    return response.cookies.get(name)


def _extract_mam_id(response: httpx.Response) -> Optional[str]:
    """Extract mam_id from a MAM response's Set-Cookie header."""
    return _extract_cookie_value(response, "mam_id")


async def _handle_response_cookie(response: httpx.Response) -> None:
    """Check response for a rotated mam_id value and update state.

    mam_id rotates on every JSON API call; we capture from Set-Cookie
    and debounce-persist to settings.json. The mbsc parallel was
    REMOVED in v2.4.0 along with all browser-tier scraping (TOS).
    """
    global _current_token, _last_rotation_save
    now = time.time()

    new_token = _extract_mam_id(response)
    if new_token and new_token != _current_token:
        _current_token = new_token
        # Don't log token bytes (even a prefix) — an 8-char prefix is
        # enough entropy to correlate sessions across log files / log
        # aggregators, and anyone with `docker logs` access is a wider
        # audience than the people authorized to see the MAM session
        # token. The fact-of-rotation is the only diagnostic that
        # matters here.
        logger.debug("MAM cookie rotated")
        # Debounced persistence: only save if 60+ seconds since last save
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

# (filelist HTML scraping helpers were REMOVED in v2.4.0 along with the
# whole mbsc browser-tier path — TOS-disallowed per MAM staff. See
# project_seshat_filelist_future_reenable.md for restoration design IF
# MAM exposes filelist via the documented API in the future.)


# ---------------------------------------------------------------------------
# Description-based bundle verification
# ---------------------------------------------------------------------------
# Sole bundle-content verification signal in v2.4.0+ (the previous
# filelist signal was TOS-disallowed and removed). Uses the documented
# Torrent Search JSON endpoint (on MAM's approved automation list per
# TOS 1.7) with the `id` and `description` parameters to fetch a single
# torrent's description, then checks whether the searched book title
# appears as a structured list entry.
#
# Bundle uploaders typically format their content listings as:
#   <br /><br /><strong>Duel Nature - 4</strong><br />
#   [*] 01. How To Marry a Millionaire Vampire - Narrated by ...
# We strip HTML/BBCode to a list of plain lines, then compare each
# line (with leading numbering and trailing volume/narrator markers
# stripped) against the searched title for an exact match. Naive
# substring would false-positive on prose ("fans of X will love this"),
# negations, and recommendations; the structured-line check rejects
# all three.

# Block-level HTML/BBCode that becomes a line break.
_DESC_BLOCK_RX = re.compile(
    r"<br\s*/?>|</?p\b[^>]*>|</?div\b[^>]*>|</?li\b[^>]*>|\[\*\]",
    re.I,
)

# Inline HTML tags — strip without affecting line structure.
_DESC_HTML_TAG_RX = re.compile(r"<[^>]+>")

# Inline BBCode formatting tags — strip without affecting line structure.
_DESC_BBCODE_RX = re.compile(
    r"\[/?(?:b|i|u|s|size|color|font|center|left|right|quote|url|img|spoiler|hr)"
    r"(?:=[^\]]*)?\]",
    re.I,
)

# HTML entity decode (limited to the ones MAM descriptions actually use).
_DESC_ENTITY_RX = re.compile(r"&(amp|nbsp|quot|apos|lt|gt|#\d+);", re.I)
_DESC_ENTITY_MAP = {
    "amp": "&",
    "nbsp": " ",
    "quot": '"',
    "apos": "'",
    "lt": "<",
    "gt": ">",
}


def _strip_to_lines(text: Optional[str]) -> list[str]:
    """Strip HTML/BBCode from a description and split into trimmed lines.

    Block-level markup (<br>, <p>, <li>, [*]) becomes newlines so list
    items surface as separate lines we can match against. Inline
    formatting (<strong>, [b], [size=4], etc.) is dropped without
    affecting line boundaries. HTML entities are decoded so &nbsp;
    becomes a regular space.
    """
    if not text:
        return []
    s = _DESC_BLOCK_RX.sub("\n", text)
    s = _DESC_HTML_TAG_RX.sub("", s)
    s = _DESC_BBCODE_RX.sub("", s)

    def _decode_entity(m: "re.Match[str]") -> str:
        e = m.group(1)
        if e.startswith("#"):
            try:
                return chr(int(e[1:]))
            except ValueError:
                return ""
        return _DESC_ENTITY_MAP.get(e.lower(), "")

    s = _DESC_ENTITY_RX.sub(_decode_entity, s)
    lines: list[str] = []
    for raw in s.split("\n"):
        line = re.sub(r"\s+", " ", raw).strip()
        if line:
            lines.append(line)
    return lines


# Leading: optional list marker + optional numbering. Examples matched:
#   "[*] ", "* ", "- ", "01. ", "1) ", "1: "
_DESC_LEADING_RX = re.compile(
    r"^[\[\(]?\*[\]\)]?\s*"
    r"|^[*\-•]\s+"
    r"|^\d+(?:\.\d+)?[\.\):]\s+",
)


def _line_match_candidates(line: str) -> list[str]:
    """Generate progressively-stripped variants of a description line.

    A bundle line might look like any of:
      "Duel Nature"
      "Duel Nature - 4"
      "1. Duel Nature"
      "[*] 01. How To Marry a Millionaire Vampire - Narrated by ... - MP3"
      "Duel Nature (Book 4)"
    We yield lowercased candidates progressively stripped of leading
    numbering and trailing dash/parenthetical segments so the matcher
    can compare any variant to the searched title.

    Cap at 5 iterations of trailing strips so a pathologically dash-
    heavy line can't loop forever.
    """
    if not line:
        return []
    s = re.sub(r"\s+", " ", line.strip().lower())
    # Strip leading list markers / numbering — apply twice in case both
    # are present (e.g. "[*] 01. Foo" → strip "[*]" → "01. Foo" → strip "01.").
    for _ in range(2):
        new = _DESC_LEADING_RX.sub("", s).strip()
        if new == s:
            break
        s = new
    if not s:
        return []
    candidates: list[str] = [s]
    seen = {s}
    current = s
    for _ in range(5):
        # Strip trailing parenthetical/bracketed segment (e.g. "(Book 4)",
        # "[Tor 2024]", "(2019)").
        new = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", current).strip()
        if new and new != current:
            current = new
            if current not in seen:
                candidates.append(current)
                seen.add(current)
            continue
        # Strip trailing word-volume markers without a leading dash:
        # "Book 4", "Vol. 4", "Volume 4", "#4". (Dash-separated forms
        # like "- 4" are caught by the next branch.)
        new = re.sub(
            r"\s+(?:book|vol\.?|volume)\s+\d+(?:\.\d+)?\s*$|\s+#\d+\s*$",
            "",
            current,
            flags=re.I,
        ).strip()
        if new and new != current:
            current = new
            if current not in seen:
                candidates.append(current)
                seen.add(current)
            continue
        # Strip trailing " - <something>" segment. Matches " - ", " – ",
        # " — ". Use the LAST occurrence so we peel one tail-segment per
        # iteration; multi-suffix lines like
        # "Title - Narrated by X - 9h32m, MP3" peel cleanly.
        last = max(
            current.rfind(" - "),
            current.rfind(" – "),
            current.rfind(" — "),
        )
        if last > 0:
            new = current[:last].strip()
            if new and new != current and new not in seen:
                candidates.append(new)
                seen.add(new)
                current = new
                continue
        break
    return candidates


def _description_mentions_title_loose(
    description: Optional[str], title: str
) -> bool:
    """Loose substring match for single-torrent Cohort C rescue.

    Different from `_description_contains_title` (structured-line check
    used for bundle URL verification) — this accepts ANY mention with
    word-boundary anchoring. Used when text and cover both fail but
    the author matched and confidence is in the Possible band.

    Conservative pre-filter to reduce false-positive surface:
      - Strip subtitle (everything after first ":" or " - ")
      - Reject if cleaned title is < 2 tokens AND < 5 chars (short
        single words like "Raw" are too noisy; longer single words
        like "Incarceron" are distinctive enough)

    Word-boundary regex match (`\\b<title>\\b`) so "raw" doesn't match
    "raw emotion" inside another book's description that happens to
    appear in this torrent's body. The author-matched gate upstream
    is the primary false-positive defense; this match adds a second.
    """
    if not description or not title:
        return False
    short = title.split(":")[0].split(" - ")[0].strip()
    if not short:
        return False
    short_lower = re.sub(r"\s+", " ", short.lower())
    if len(short_lower.split()) < 2 and len(short_lower) < 5:
        return False
    desc_text = " ".join(_strip_to_lines(description)).lower()
    pattern = r"\b" + re.escape(short_lower) + r"\b"
    return bool(re.search(pattern, desc_text))


def _description_contains_title(
    description: Optional[str], *titles: str
) -> bool:
    """True if any of `titles` appears as a structured list entry in `description`.

    Used by the description-based fallback for bundle URL verification
    when filelist verification isn't available (mbsc not configured)
    or returned no match. Bundle uploaders typically list contents in
    the description with a structured pattern; this matches those
    patterns while rejecting prose mentions.

    Conservative on false positives — requires the line content (after
    stripping leading numbering and trailing volume/narrator markers)
    to EQUAL the title, not just contain it. This rejects:
      - prose mentions ("fans of Duel Nature will love this")
      - recommendation contexts ("if you enjoyed Duel Nature, ...")
      - negations ("does NOT include Duel Nature")
    Single-word titles are rejected (too noisy for standalone matching);
    requires ≥ 2 tokens, same threshold as `_filelist_contains_title`.
    """
    if not description:
        return False
    valid_titles: set[str] = set()
    for t in titles:
        if not t:
            continue
        normalized = re.sub(r"\s+", " ", t.strip().lower())
        if len(normalized.split()) < 2:
            continue
        valid_titles.add(normalized)
    if not valid_titles:
        return False
    for line in _strip_to_lines(description):
        for candidate in _line_match_candidates(line):
            if candidate in valid_titles:
                return True
    return False


async def _fetch_torrent_description(
    token: str, torrent_id: str
) -> Optional[str]:
    """Fetch a single torrent's description via the documented Search API.

    Uses /tor/js/loadSearchJSONbasic.php (on MAM's TOS 1.7 approved
    automation list) with `tor.id` to scope the query and the top-level
    `description` flag to include the description field in the response.
    Returns the raw description string (may include HTML and/or BBCode)
    or None on any failure — callers MUST treat None as "couldn't
    verify" not "verified absent".

    Used by the bundle URL verification path as a fallback when
    filelist verification isn't available (mbsc not configured) or
    returned no match. Description-mention is a weaker signal than
    filelist-mention (uploaders can mention books that aren't in the
    bundle in prose), so the matching helper applies a structured-line
    check rather than naive substring.
    """
    if not torrent_id:
        return None
    try:
        tid_int = int(torrent_id)
    except (TypeError, ValueError):
        return None
    payload = json.dumps(
        {
            "tor": {
                "id": tid_int,
                "searchType": "all",
                "searchIn": "torrents",
                "browseFlagsHideVsShow": "0",
                "startDate": "",
                "endDate": "",
                "hash": "",
                "sortType": "default",
                "startNumber": "0",
            },
            "description": "",
            "perpage": 5,
        }
    )
    try:
        resp = await _do_post(MAM_SEARCH_URL, token, payload)
        if resp.status_code != 200 or not resp.text:
            return None
        data = resp.json()
    except Exception as e:
        logger.debug(f"  Description fetch {torrent_id} failed: {e}")
        return None
    results = data.get("data") or []
    if not results:
        return None
    desc = results[0].get("description")
    if not isinstance(desc, str):
        return None
    return desc or None


# (filelist headers, login markers, fetch helpers were REMOVED in v2.4.0
# along with the whole mbsc browser-tier path — TOS-disallowed per MAM
# staff. See project_seshat_filelist_future_reenable.md for restoration
# design IF MAM exposes filelist via the documented API in the future.)


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
# Cover-image verification (Part C) — multi-candidate ranker
# ---------------------------------------------------------------------------


async def _annotate_candidate_covers(
    candidates: list[dict],
    seshat_cover_phash: str,
    token: str,
    cover_phash_cache: dict,
    *,
    topn: int = _COVER_TOPN_CANDIDATES,
) -> None:
    """Annotate top-N non-bundle candidates with cover_distance + cover_signal.

    Mutates `candidates` in place. Bundles get `cover_signal="skipped_bundle"`
    because MAM's bundle covers show the omnibus art, not the individual
    book — bundle URL verification (description-based) owns those.

    For non-bundles in the top-N pool: fetches each cover (cache flow:
    in-memory `cover_phash_cache` → persistent `mam_cover_hashes` table →
    HTTP), computes Hamming distance vs `seshat_cover_phash`, and assigns
    a signal:
      - "promote" — distance <= _COVER_PROMOTE_DIST_MAX (10)
      - "demote"  — distance >= _COVER_DEMOTE_DIST_MIN (22)
      - "neutral" — in the deadband
      - "no_data" — fetch/decode failed (degrade gracefully, no action)

    Candidates outside the top-N pool get `cover_signal="not_evaluated"` —
    we never fetched their cover, so callers must not infer anything from
    the absence of a promote/demote.
    """
    if not seshat_cover_phash or not candidates:
        return
    # Initialize all candidates with the appropriate skip signal so the
    # debug-match endpoint can clearly distinguish "we didn't try" from
    # "we tried and got nothing".
    for c in candidates:
        if c.get("is_bundle"):
            c["cover_signal"] = "skipped_bundle"
        else:
            c["cover_signal"] = "not_evaluated"
        c.setdefault("cover_distance", None)
        c.setdefault("mam_cover_phash", None)

    # Top-N non-bundle pool by text confidence (highest first).
    pool = sorted(
        [c for c in candidates if not c.get("is_bundle")],
        key=lambda c: c.get("confidence", 0.0),
        reverse=True,
    )[:topn]
    if not pool:
        return

    from app.mam.cover_hash import (
        fetch_and_hash_mam_cover,
        hamming_distance,
    )

    for cand in pool:
        tid = cand["torrent_id"]
        if tid in cover_phash_cache:
            mam_phash = cover_phash_cache[tid]
        else:
            mam_phash = await fetch_and_hash_mam_cover(tid, token)
            cover_phash_cache[tid] = mam_phash
        if mam_phash is None:
            cand["cover_signal"] = "no_data"
            cand["cover_distance"] = None
            continue
        dist = hamming_distance(seshat_cover_phash, mam_phash)
        cand["cover_distance"] = dist
        cand["mam_cover_phash"] = mam_phash
        if dist <= _COVER_PROMOTE_DIST_MAX:
            cand["cover_signal"] = "promote"
        elif dist >= _COVER_DEMOTE_DIST_MIN:
            cand["cover_signal"] = "demote"
        else:
            cand["cover_signal"] = "neutral"


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
    seshat_cover_phash: Optional[str] = None,
) -> dict:
    """
    Five-pass search cascade for a single book, with format preference scoring.

    `content_type` routes the whole cascade through the ebook or
    audiobook variants — search main_cat, format filtering, category
    rejection, default priority list. Callers that don't pass
    content_type get the ebook path (historical behavior).

    `seshat_cover_phash` (optional, hex string) enables Part C cover
    verification when also gated by `_COVER_VERIFICATION_ENABLED`. When
    set, cover-pHash distance against top-N MAM candidates can promote
    a non-text-winner to Found (and, when `_COVER_DEMOTION_ENABLED`,
    filter out cover-mismatched candidates). Callers that don't pass
    it (or pass None) get the legacy text-only cascade.

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

    # Per-book cache of description fetches keyed by torrent_id. Bundles
    # frequently appear as the best candidate in multiple cascade passes
    # for the same author (passes 1, 4, 5 returning the same bundle), so
    # cache to hit the description endpoint at most once per bundle per
    # evaluation. Cache is intentionally local to one check_book call —
    # no reason to keep cross-book state.
    description_cache: dict[str, Optional[str]] = {}
    # Per-evaluation cache for cover-pHash fetches. Same torrent surfacing
    # in multiple passes only fetches its cover once. The persistent
    # `mam_cover_hashes` table behind `cover_hash.fetch_and_hash_mam_cover`
    # carries hits across books; this in-memory cache covers the in-call
    # repeat candidates that don't need a SQLite round-trip.
    cover_phash_cache: dict[str, Optional[str]] = {}

    async def _try_evaluate(pass_num: int, resp: dict, search_title: str) -> bool:
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

        # ── Volume disambiguation (Cohort C rescue) ───────────────
        # Use the ORIGINAL `title` (closure variable, the calibre title)
        # for volume comparison, NOT the per-pass `search_title`. The
        # variant passes (B2) strip trailing numbers from search_title
        # to bridge MAM's tokenization mismatch — those stripped queries
        # MUST still volume-disambiguate against the user's original
        # intent. For Raw Bk1 (orig vol=None), penalize sibling-volume
        # candidates (Raw V, Raw VI, etc.); for Right of Retribution 2
        # (orig vol=2), drop candidates with mismatched volumes outright.
        # Imported lazily to avoid a hard import dep at module load.
        from app.metadata.scoring import _extract_volume as _vol_extract
        orig_vol = _vol_extract(title)
        for m in matches:
            cand_vol = _vol_extract(m["mam_title"])
            if cand_vol is not None and orig_vol is not None and cand_vol != orig_vol:
                # Definitive: same series, different book.
                m["confidence"] = 0.0
                m["volume_mismatch"] = True
            elif orig_vol is None and cand_vol is not None:
                # Soft penalty: user searched without a volume marker,
                # candidate has one — likely a sibling, prefer the
                # no-volume sibling if one exists. -0.20 is enough to
                # break ties at conf=0.40 without burying genuinely
                # high-confidence matches.
                m["confidence"] = max(0.0, m["confidence"] - 0.20)
                m["volume_penalty_applied"] = True

        # Re-filter against the min threshold after penalties — drops
        # candidates whose post-penalty confidence falls below MATCH_MIN_SCORE.
        matches = [m for m in matches if m["confidence"] >= MATCH_MIN_SCORE]
        if not matches:
            return False

        # Separate into author-confirmed and author-unconfirmed
        confirmed = [m for m in matches if m["author_matched"]]
        all_viable = confirmed if confirmed else matches

        # ── Cover-image verification (Part C) ──────────────────
        # Multi-candidate ranker: when the searched book has a cover
        # hash and the master gate is on, fetch covers for top-N
        # non-bundle candidates and let pHash distance promote (low) /
        # demote (high) regardless of text score. Bundles are excluded
        # — bundle URL verification owns those decisions.
        #
        # Promote behavior: any non-bundle candidate with cover_signal
        # "promote" REPLACES the text winner — even if text picked a
        # different candidate as best. This is the fix for cases like
        # Raw (Bk1 vs Bk6) and Right of Retribution 2 (D6) where a
        # lower-text-score candidate has the actually-matching cover.
        #
        # Demote behavior: candidates with cover_signal "demote" are
        # filtered OUT of the pool before _pick_best_result. Gated
        # SEPARATELY because demotion needs more production data to
        # validate — Cohort C (right book, different cover art) was
        # only sampled ~20/800 Possibles in Mark's library.
        #
        # All cover code is dead (no fetches, no annotations) when
        # _COVER_VERIFICATION_ENABLED is False, so the legacy text-only
        # cascade pays nothing.
        cover_promoter_winner = None
        if _COVER_VERIFICATION_ENABLED and seshat_cover_phash:
            await _annotate_candidate_covers(
                all_viable, seshat_cover_phash, token, cover_phash_cache,
            )
            promoters = [
                c for c in all_viable
                if c.get("cover_signal") == "promote"
            ]
            if promoters:
                # Format-priority pick within the promoter set —
                # multiple cover-promoted candidates (rare) tiebreak on
                # the same format/match_pct/seeders rules as elsewhere.
                cover_promoter_winner = _pick_best_result(
                    promoters, format_priority,
                )
                logger.debug(
                    f"  Pass {pass_num}: COVER-PROMOTE "
                    f"'{cover_promoter_winner['mam_title'][:50]}' — "
                    f"distance={cover_promoter_winner.get('cover_distance')} "
                    f"<= {_COVER_PROMOTE_DIST_MAX}; replaces text winner"
                )
                # PROMOTER-ANCHORED DEMOTION: now that cover-pHash has
                # positive evidence (a promoter), it's safe to filter
                # out competing candidates whose covers are clearly
                # different. This is the ONLY context where demotion
                # fires — without a promoter, demote signals are
                # ignored to protect Cohort C cases (right book whose
                # cover happens to differ from ours, e.g. publisher
                # rebrand). See project_seshat_mam_url_confidence.
                if _COVER_DEMOTION_ENABLED:
                    pre_demote_count = len(all_viable)
                    all_viable = [
                        c for c in all_viable
                        if c.get("cover_signal") != "demote"
                    ]
                    demoted_count = pre_demote_count - len(all_viable)
                    if demoted_count:
                        logger.debug(
                            f"  Pass {pass_num}: COVER-DEMOTE filtered "
                            f"{demoted_count} candidate(s) (anchored to "
                            f"the cover promoter)"
                        )

        # Check if multiple distinct uploads exist (different torrent IDs)
        unique_ids = set(m["torrent_id"] for m in all_viable)
        has_multiple = len(unique_ids) > 1

        # Pick best result by format preference. The cover-promoter
        # winner (if any) takes precedence over the text-priority pick.
        best = cover_promoter_winner or _pick_best_result(
            all_viable, format_priority,
        )
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

        # Bundle URL verification: when the best candidate is a
        # multi-book torrent and the user's calibre title doesn't strongly
        # match the bundle's own title (e.g. searching for "Duel Nature"
        # against "Demon Accords Series"), confidence alone can't tell us
        # whether the bundle URL actually contains the searched book or
        # is a coincidental author-only match.
        #
        # DESCRIPTION verification — fetch the bundle's torrent description
        # via the documented Search JSON API (TOS 1.7 approved automation
        # list) and check whether the title appears as a structured list
        # entry. Structured-line check rejects most prose-mention false
        # positives. The previous filelist-based signal was removed in
        # v2.4.0 per MAM staff confirmation that mbsc scraping isn't on
        # the approved automation surface; if the documented API ever
        # exposes filelist, see project_seshat_filelist_future_reenable.
        #
        # Gate: bundle + author overlap + title-similarity below the
        # bundle floor. The author check keeps us from spending fetches
        # on totally-unrelated bundles; the ts floor skips books whose
        # title already strongly matches the bundle name (intentional
        # bundle catalog entries) since those promote via the normal
        # path without needing verification.
        bundle_contents_verified = False
        needs_bundle_verification = (
            _BUNDLE_VERIFICATION_ENABLED
            and is_bundle
            and best.get("author_matched", False)
            and ts < _BUNDLE_PROMOTE_TS_FLOOR
        )
        if needs_bundle_verification:
            tid = best["torrent_id"]
            if tid not in description_cache:
                description_cache[tid] = await _fetch_torrent_description(token, tid)
            description = description_cache[tid]
            if description and _description_contains_title(
                description, title, search_title
            ):
                bundle_contents_verified = True
                logger.debug(
                    f"  Pass {pass_num}: BUNDLE-VERIFIED-DESCRIPTION "
                    f"'{best['mam_title'][:50]}' — search title found in "
                    f"bundle description; promoting to FOUND"
                )
            else:
                logger.debug(
                    f"  Pass {pass_num}: BUNDLE '{best['mam_title'][:50]}' "
                    f"— title not in description; held as possible"
                )

        # The cap on confidence-driven promotes for bundles still applies
        # when neither verification signal succeeded — a high-confidence
        # author-only match on a bundle whose contents don't include the
        # search title is exactly the false-Found we want to avoid.
        promote_blocked_by_bundle = (
            is_bundle
            and ts < _BUNDLE_PROMOTE_TS_FLOOR
            and not bundle_contents_verified
        )

        # Cover-pHash verification (Part C): when the cover-promoter
        # winner replaced the text winner above, treat that as decisive
        # evidence the URL is right and promote to Found regardless of
        # text confidence. Bundles can never reach this branch (excluded
        # from cover verification) so we don't need to interact with
        # promote_blocked_by_bundle.
        cover_verified = best.get("cover_signal") == "promote"

        # Cohort C rescue (B3a): single-torrent description verification.
        # When cover-pHash didn't promote AND the candidate sits in the
        # Possible band with author matched, fetch the description and
        # check if the searched title appears (loose substring match).
        # Cohort C cases (right book, visually-different cover) often
        # leave a strong title mention in the publisher's description,
        # which gives us a positive signal cover-pHash can't provide.
        # Gated narrowly: non-bundle, author matched, conf in Possible
        # band, no cover promote — keeps API fetches scoped to cases
        # that actually need rescue. TOS-allowed via documented Search
        # API (same as bundle description path Phase 4).
        single_torrent_description_verified = False
        needs_single_desc_check = (
            _BUNDLE_VERIFICATION_ENABLED
            and not cover_verified
            and not is_bundle
            and best.get("author_matched", False)
            and MATCH_MIN_SCORE <= conf < MATCH_PROMOTE_SCORE
        )
        if needs_single_desc_check:
            tid = best["torrent_id"]
            if tid not in description_cache:
                description_cache[tid] = await _fetch_torrent_description(token, tid)
            description = description_cache[tid]
            if description and _description_mentions_title_loose(description, title):
                single_torrent_description_verified = True
                logger.debug(
                    f"  Pass {pass_num}: COHORT-C-RESCUE-DESCRIPTION "
                    f"'{best['mam_title'][:50]}' — title found in description; "
                    f"promoting Possible-band candidate to FOUND"
                )

        # Promote to FOUND when:
        #  - cover-pHash verification matched (definitive — same image
        #    means same upload, even when text score disagrees), OR
        #  - bundle contents verification succeeded via description
        #    (definitive enough — promote even at low conf because we
        #    have evidence the URL points at the right book), OR
        #  - single-torrent description verification matched (Cohort C
        #    rescue — title mentioned in description), OR
        #  - confidence clears the regular threshold and the bundle cap
        #    isn't blocking it.
        should_promote = (
            cover_verified
            or bundle_contents_verified
            or single_torrent_description_verified
            or (conf >= MATCH_PROMOTE_SCORE and not promote_blocked_by_bundle)
        )
        if should_promote:
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
        if await _try_evaluate(1, r, title):
            return result

        # --- Pass 2: author + core title (volume prefix stripped) ---
        core = _extract_core_title(title)
        if core:
            r = await _mam_search(token, authors, core, lang_ids=lang_ids, content_type=content_type)
            await asyncio.sleep(delay)
            if await _try_evaluate(2, r, core):
                return result

        # --- Pass 3: author + subtitle right (part after colon) ---
        sub_right = _extract_subtitle_part(title)
        if sub_right and sub_right != core:
            r = await _mam_search(token, authors, sub_right, lang_ids=lang_ids, content_type=content_type)
            await asyncio.sleep(delay)
            if await _try_evaluate(3, r, sub_right):
                return result

        # --- Pass 4: author + short title (part before colon) ---
        short = _strip_subtitle(title)
        if short and short != title and short != core:
            r = await _mam_search(token, authors, short, lang_ids=lang_ids, content_type=content_type)
            await asyncio.sleep(delay)
            if await _try_evaluate(4, r, short):
                return result

        # --- Pass 5: title only (no author), loose cleaning ---
        title_only = core or sub_right or short or title
        r = await _mam_search(token, None, title_only, lang_ids=lang_ids, content_type=content_type)
        await asyncio.sleep(delay)
        if await _try_evaluate(5, r, title_only):
            return result

        # --- Passes 6+: alternate forms ---
        # MAM tokenizes more strictly than we send. Two known patterns
        # that drop the right candidate from earlier passes:
        #   1. Trailing zero-padded volumes ("Right of Retribution 02")
        #      vs source's bare "2" — search for nothing returns an
        #      empty result, search without the number returns both.
        #   2. Multi-initial authors ("JJ Cross") vs source's spaced
        #      ("J J Cross") — combined text-token search can't bridge.
        #
        # Generate variant title/author forms and run them as additional
        # passes. Dedupe against forms already tried in passes 1-5 so we
        # don't burn API quota on duplicates. Cascade still short-
        # circuits on a FOUND, so passes 6+ only run for Possible / NotFound
        # cases — adds zero cost when text already nailed it.
        tried_pairs: set[tuple] = {(authors, title)}
        if core:
            tried_pairs.add((authors, core))
        if sub_right and sub_right != core:
            tried_pairs.add((authors, sub_right))
        if short and short != title and short != core:
            tried_pairs.add((authors, short))
        tried_pairs.add((None, title_only))

        alt_titles = _alternate_title_forms(title)
        # Also try variants of the short / core forms in case the trailing
        # number is on a sub-form rather than the full title.
        for base in (short, core):
            if base:
                alt_titles.extend(
                    t for t in _alternate_title_forms(base)
                    if t not in alt_titles
                )
        alt_authors = _alternate_author_forms(authors)

        # Build the variant pass list. Order:
        #   - alt-title with original author (catches D6 / Warhawk
        #     trailing-number case)
        #   - alt-author with original title (catches Veil
        #     "JJ Cross" case)
        #   - alt-author × alt-title compounding (catches both at once)
        # Capped at 6 extras so we don't quintuple the API budget per
        # book in the worst case (large initial sets × many alt titles).
        variant_passes: list[tuple[Optional[str], str]] = []
        for alt_t in alt_titles:
            variant_passes.append((authors, alt_t))
        for alt_a in alt_authors:
            variant_passes.append((alt_a, title))
            for alt_t in alt_titles:
                variant_passes.append((alt_a, alt_t))
        # Dedupe in order
        seen_pairs: set[tuple] = set()
        deduped_variants: list[tuple[Optional[str], str]] = []
        for pair in variant_passes:
            if pair in seen_pairs or pair in tried_pairs:
                continue
            seen_pairs.add(pair)
            deduped_variants.append(pair)
        deduped_variants = deduped_variants[:6]

        for vidx, (v_author, v_title) in enumerate(deduped_variants):
            pass_num = 6 + vidx
            r = await _mam_search(
                token, v_author, v_title,
                lang_ids=lang_ids, content_type=content_type,
            )
            await asyncio.sleep(delay)
            if await _try_evaluate(pass_num, r, v_title):
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
    seshat_cover_phash: Optional[str] = None,
) -> dict:
    """Run the cascade for one book and return a full structured trace.

    `seshat_cover_phash` (optional, hex pHash) enables Part C cover
    verification surfacing. When provided, top-N non-bundle candidates
    per pass get fetched + compared, and each result's trace gains a
    `cover_check` field with `distance`, `signal`, and `mam_phash`.
    Unlike production scanning, debug-match runs cover annotation
    UNGATED (regardless of `_COVER_VERIFICATION_ENABLED`) — the whole
    point is to let Mark see the signal even when production won't act
    on it yet.

    Trace shape (cover_check + cover_input fields are new in step 5):
      {
        "input": {...},
        "cover_input": {                # NEW: cover-verification inputs
          "seshat_phash": str | None,    # the hash used for comparisons
          "thresholds": {                # surfaced for UI display
            "promote_max": int,
            "demote_min": int,
            "topn": int,
          },
        },
        "passes": [
          {
            ...,
            "results": [
              {
                ...,
                "cover_check": {              # NEW (only when phash given)
                  "distance": int | None,
                  "signal": "promote" | "demote" | "neutral" |
                            "no_data" | "skipped_bundle" | "not_evaluated",
                  "mam_phash": str | None,
                },
                "decision": ...,  # extended with would_promote_via_cover_verification
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
        # Part C cover-verification surface. `seshat_phash` is whatever
        # the caller provided (None when omitted → cover_check will be
        # absent on per-result entries). Production master gate
        # (`_COVER_VERIFICATION_ENABLED`) is INTENTIONALLY ignored here:
        # debug-match always shows the signal so Mark can validate the
        # design before flipping the production gate.
        "cover_input": {
            "seshat_phash": seshat_cover_phash,
            "thresholds": {
                "promote_max": _COVER_PROMOTE_DIST_MAX,
                "demote_min": _COVER_DEMOTE_DIST_MIN,
                "topn": _COVER_TOPN_CANDIDATES,
            },
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

    # Description cache mirrors production check_book — same torrent
    # surfacing in multiple debug passes only fetches once.
    debug_description_cache: dict[str, Optional[str]] = {}
    # Cover-pHash cache (in-memory only — persistent cache lives in the
    # global `mam_cover_hashes` table, hit transparently via
    # cover_hash.fetch_and_hash_mam_cover). Same torrent across multiple
    # debug passes only fetches once.
    debug_cover_phash_cache: dict[str, Optional[str]] = {}

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

            ts_max = max(
                score_full_breakdown["title_similarity"],
                score_search_breakdown["title_similarity"],
            )
            is_bundle = _is_bundle(item)
            author_matched = _author_match(authors, item)

            # Mirror the production bundle-verification gate: bundle +
            # author overlap + ts below the bundle floor → fetch
            # description and check if the search title appears as a
            # structured list entry. (Filelist signal removed in v2.4.0
            # per TOS — description is the sole bundle-content signal.)
            bundle_check: dict = {
                "is_bundle": is_bundle,
                "author_matched": author_matched,
                "verification_attempted": False,
                "description_fetched": False,
                "description_length": 0,
                "description_match": False,
                "description_first_500_chars": None,
            }
            if (
                is_bundle
                and author_matched
                and ts_max < _BUNDLE_PROMOTE_TS_FLOOR
                and confidence >= MATCH_MIN_SCORE
            ):
                bundle_check["verification_attempted"] = True
                tid = str(item.get("id", ""))
                if tid not in debug_description_cache:
                    debug_description_cache[tid] = (
                        await _fetch_torrent_description(token, tid)
                    )
                desc = debug_description_cache.get(tid)
                bundle_check["description_fetched"] = desc is not None
                if desc:
                    bundle_check["description_length"] = len(desc)
                    bundle_check["description_first_500_chars"] = desc[:500]
                    if _description_contains_title(desc, title, search_title):
                        bundle_check["description_match"] = True

            # Decision reflects what production would actually do.
            verified = bundle_check["description_match"]
            if confidence < MATCH_MIN_SCORE:
                decision = "skipped_below_min"
            elif verified:
                decision = "would_promote_via_description_verification"
            elif (
                is_bundle
                and ts_max < _BUNDLE_PROMOTE_TS_FLOOR
                and confidence >= MATCH_PROMOTE_SCORE
            ):
                # Conf would normally promote, but bundle cap blocks it
                # (and the description verification signal didn't fire).
                decision = "bundle_capped_kept_as_possible"
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
                "title_similarity_max": round(ts_max, 4),
                "bundle_check": bundle_check,
                "decision": decision,
            })

        # Part C cover-verification surfacing — only runs when caller
        # provided a seshat-side phash. Uses the same helper as
        # production (_annotate_candidate_covers), but UNGATED relative
        # to `_COVER_VERIFICATION_ENABLED` so debug-match shows the
        # signal even before the production gate flips.
        #
        # Updates each result's `cover_check` block + extends `decision`
        # to "would_promote_via_cover_verification" when the cover
        # signal would have driven the result's promotion (and the
        # candidate isn't already promoting via description).
        if seshat_cover_phash and pass_trace["results"]:
            cover_pool = [
                {
                    "torrent_id": r["torrent_id"],
                    "confidence": r["confidence_max"],
                    "is_bundle": r["bundle_check"]["is_bundle"],
                }
                for r in pass_trace["results"]
                if r["torrent_id"]
            ]
            await _annotate_candidate_covers(
                cover_pool, seshat_cover_phash, token,
                debug_cover_phash_cache,
            )
            by_tid = {c["torrent_id"]: c for c in cover_pool}
            for r in pass_trace["results"]:
                annotated = by_tid.get(r["torrent_id"], {})
                r["cover_check"] = {
                    "distance": annotated.get("cover_distance"),
                    "signal": annotated.get(
                        "cover_signal", "not_evaluated"
                    ),
                    "mam_phash": annotated.get("mam_cover_phash"),
                }
                # If cover signal would promote AND nothing else already
                # promoted this result, override the decision so the
                # trace clearly attributes the would-be promote to the
                # cover signal.
                if (
                    r["cover_check"]["signal"] == "promote"
                    and not r["decision"].startswith("would_promote_via")
                ):
                    r["decision"] = "would_promote_via_cover_verification"

        trace["passes"].append(pass_trace)

    return trace


# ---------------------------------------------------------------------------
# Batch scanning — processes books from the DB
# ---------------------------------------------------------------------------


# Max wall-clock time we'll voluntarily pause MAM for concurrent writers
# before resuming anyway. 20 minutes is a safety net for a stuck flag —
# under normal operation a Calibre sync of even Mark's 2,855-book library
# completes in ~80s, well below the cap.
_MAM_PAUSE_MAX_SECONDS = 1200.0

# Number of times we'll retry a per-book UPDATE that hits "database is
# locked" (after pausing for concurrent writers each time). 3 attempts
# spans the worst-case window where a sync starts AFTER our last pause
# check but BEFORE our UPDATE submission — pause, retry, and we should
# get through. If we hit attempt 3 we re-raise so the surrounding
# scan_books_batch can record the error and abort cleanly instead of
# silently dropping the book.
_MAM_LOCK_RETRY_ATTEMPTS = 3


def _concurrent_writers_active() -> bool:
    """True when a source scan or library sync is holding the writer lock.

    Used by both the per-book pause helper and the retry-on-locked path
    so the two paths share one definition of "another writer is live".
    """
    return state._source_scan_refs > 0 or state._library_sync_in_progress


async def _pause_for_concurrent_writers(db, i: int, total: int) -> None:
    """Commit MAM's pending write + sleep-poll until concurrent writers clear.

    Called from per-book hot paths in `scan_books_batch` to yield the
    SQLite writer lock to:
      - source scans (state._source_scan_refs > 0) — incremented by
        lookup_author and decremented in its finally
      - library syncs (state._library_sync_in_progress) — set by both
        the scheduled sync_all_libraries and the manual /sync endpoint

    SQLite's single-writer lock means a long MAM batch (tens of UPDATE
    books per minute) can starve other writers past the 30s busy_timeout.
    Calibre sync of Mark's full library takes ~80s — longer than busy_
    timeout will absorb — so without this yield, the next MAM UPDATE
    crashes the whole scan task with `database is locked`.

    Two call sites per book (top of iteration AND right before UPDATE)
    cover the multi-second window during check_book's network calls —
    sync can start at any point in that window and we want to catch it
    before MAM's UPDATE blocks on the held writer lock.

    CRITICAL: commit before sleeping. Any uncommitted MAM write keeps
    the writer lock held for the duration of the sleep, defeating the
    yield. v1.1.9-dev3 testing confirmed this when Goodreads spent 30s
    blocked on UPDATE authors while MAM sat paused with its last
    UPDATE uncommitted.

    20-minute cap is a safety net for a stuck flag (refcount stuck or
    sync flag never cleared). Logs at INFO so the unified scan widget
    user can see the yield happening.
    """
    if not _concurrent_writers_active():
        return
    await db.commit()
    reason_parts: list[str] = []
    if state._source_scan_refs > 0:
        reason_parts.append(f"{state._source_scan_refs} source scan(s)")
    if state._library_sync_in_progress:
        reason_parts.append("library sync")
    logger.info(
        f"MAM [{i+1}/{total}] paused — {' + '.join(reason_parts)} in progress"
    )
    paused_at = asyncio.get_event_loop().time()
    while _concurrent_writers_active():
        if asyncio.get_event_loop().time() - paused_at > _MAM_PAUSE_MAX_SECONDS:
            logger.warning(
                f"MAM [{i+1}/{total}] paused {_MAM_PAUSE_MAX_SECONDS / 60:.0f}min — "
                f"resuming anyway (refcount={state._source_scan_refs}, "
                f"library_sync={state._library_sync_in_progress})"
            )
            break
        await asyncio.sleep(1.0)
    else:
        logger.info(
            f"MAM [{i+1}/{total}] resumed — concurrent writers finished"
        )


async def _execute_with_lock_retry(db, sql: str, params: tuple, i: int, total: int) -> None:
    """Run a per-book write that may race with a concurrent writer.

    The two pause checks in scan_books_batch (top-of-iteration + pre-
    UPDATE) eliminate the BIG window where a long check_book network
    call lets a sync start unnoticed. They can NOT eliminate the
    sub-second residual race where a sync starts AFTER our last flag
    check but BEFORE the UPDATE submission reaches SQLite — the flag
    check is in Python while the UPDATE awaits at the C layer, and
    asyncio can yield between them.

    Catch sqlite3.OperationalError("database is locked"), pause for
    whichever writer is holding the lock, then retry. The pause loop
    waits up to 20 minutes per attempt; 3 attempts is generous enough
    that we'd only hit the re-raise path in a genuine deadlock the
    user should know about (which we do by surfacing the error).
    """
    for attempt in range(_MAM_LOCK_RETRY_ATTEMPTS):
        try:
            await db.execute(sql, params)
            return
        except sqlite3.OperationalError as e:
            if "database is locked" not in str(e).lower():
                raise
            if attempt == _MAM_LOCK_RETRY_ATTEMPTS - 1:
                logger.error(
                    f"MAM [{i+1}/{total}] UPDATE locked after "
                    f"{_MAM_LOCK_RETRY_ATTEMPTS} attempts; aborting batch"
                )
                raise
            logger.warning(
                f"MAM [{i+1}/{total}] UPDATE locked on attempt {attempt + 1}; "
                f"pausing for concurrent writer + retrying"
            )
            await _pause_for_concurrent_writers(db, i, total)


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
        # Snapshot mode (book_ids provided) doesn't apply the recently-
        # scanned skip — the caller already curated the ID set, our job
        # is to scan exactly those books. Snapshot was built upstream
        # (e.g. start_full_scan) where the skip filter does apply.
        rows = await db.execute_fetchall(f"""
            SELECT b.id, b.title, a.name as author_name, b.owned, b.is_unreleased,
                   s.name as series_name
            FROM books b
            JOIN authors a ON b.author_id = a.id
            LEFT JOIN series s ON b.series_id = s.id
            WHERE b.id IN ({placeholders})
            ORDER BY {_recent_scan_order_clause('b.')}
        """, tuple(book_ids))
    else:
        # Live-eligibility mode: apply the skip clause + oldest-first
        # ordering so the queue rotates through the full library.
        cutoff = _recent_scan_cutoff_seconds()
        skip_clause = _recent_scan_skip_clause(cutoff, prefix="b.")
        rows = await db.execute_fetchall(f"""
            SELECT b.id, b.title, a.name as author_name, b.owned, b.is_unreleased,
                   s.name as series_name
            FROM books b
            JOIN authors a ON b.author_id = a.id
            LEFT JOIN series s ON b.series_id = s.id
            WHERE {_NEEDS_SCAN_BASIC_ALIASED}{skip_clause}
            ORDER BY {_recent_scan_order_clause('b.')}
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
        # Library syncs (Calibre / ABS) get the same per-book deference
        # via `state._library_sync_in_progress`. The `/api/mam/scan`
        # router has a between-batches `_wait_for_other_writers` check,
        # but that's too coarse — a sync that fires mid-batch (a 150-
        # book batch can take ~15 minutes) holds the writer lock long
        # enough for MAM's next UPDATE to time out and crash the whole
        # scan task with `database is locked` (Mark's UAT 2026-05-09).
        # Per-book pause prevents that by committing + waiting at sub-
        # second granularity instead of letting MAM bash against a
        # held lock until busy_timeout expires.
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
        await _pause_for_concurrent_writers(db, i, len(rows))

        logger.debug(f"MAM [{i+1}/{len(rows)}] {book_title[:65]} — {author_name[:35]}")

        # Surface the title BEFORE the network call so the progress widget
        # shows what we're waiting on, not what we just finished. MAM shows
        # every attempt — no filter-noise to hide here.
        stats["current_book"] = book_title
        if on_progress:
            on_progress(dict(stats))

        # Resolve seshat-side cover hash (lazy-compute + persist if NULL).
        # Skipped silently on any failure — cover verification gracefully
        # degrades to text-only behavior.
        from app.discovery.cover_phash import ensure_cover_phash
        seshat_phash = await ensure_cover_phash(db, book_id, token=session_id)

        check = await check_book(session_id, book_title, author_name, format_priority, delay, lang_ids=lang_ids, series_name=book_series or "", content_type=content_type, seshat_cover_phash=seshat_phash)
        stats["scanned"] += 1

        # Second pause check, RIGHT before the UPDATE. check_book's
        # network calls take 5-10s per book — easily enough time for
        # a sync to start mid-iteration. Without this re-check, MAM
        # would proceed to the UPDATE while the sync holds the writer
        # lock, then crash on busy_timeout (Mark's UAT 2026-05-09).
        await _pause_for_concurrent_writers(db, i, len(rows))

        # Write result to DB. Stamp mam_last_scanned_at on
        # successful scans (any status that actually represents a
        # round-trip with MAM) but NOT on auth_error — otherwise a
        # bad cookie would mark every book as recently scanned and
        # starve the queue when auth recovers. The CASE keeps the
        # existing timestamp untouched on auth_error.
        #
        # `_execute_with_lock_retry` wraps the actual UPDATE so the
        # tiny residual race window (sync starts AFTER our pause
        # check but BEFORE the UPDATE submission lands at SQLite)
        # gets retried-with-pause instead of crashing the batch.
        await _execute_with_lock_retry(
            db,
            """
                UPDATE books SET mam_url=?, mam_status=?, mam_formats=?,
                       mam_torrent_id=?, mam_category=?, mam_has_multiple=?,
                       mam_my_snatched=?, mam_is_bundle=?,
                       mam_last_scanned_at=CASE
                           WHEN ? = 'auth_error' THEN mam_last_scanned_at
                           ELSE ?
                       END
                WHERE id=?
            """,
            (
                check["mam_url"],
                check["status"],
                check["mam_formats"],
                check["mam_torrent_id"],
                check.get("mam_category", "") or "",
                1 if check["mam_has_multiple"] else 0,
                1 if check.get("mam_my_snatched") else 0,
                1 if check.get("mam_is_bundle") else 0,
                check["status"],
                time.time(),
                book_id,
            ),
            i,
            len(rows),
        )

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

    cutoff = _recent_scan_cutoff_seconds()
    skip_clause = _recent_scan_skip_clause(cutoff)
    id_rows = await db.execute_fetchall(f"""
        SELECT id FROM books
        WHERE {_NEEDS_SCAN_STRICT_BARE}{skip_clause}
        ORDER BY {_recent_scan_order_clause()}
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
            ORDER BY {_recent_scan_order_clause('b.')}
        """, tuple(batch_ids))
    else:
        # Legacy no-snapshot fallback. Apply skip + oldest-first the
        # same way the snapshot path's source query (start_full_scan)
        # does so the two paths produce comparable queues.
        cutoff = _recent_scan_cutoff_seconds()
        skip_clause = _recent_scan_skip_clause(cutoff, prefix="b.")
        book_rows = await db.execute_fetchall(f"""
            SELECT b.id, b.title, a.name as author_name
            FROM books b
            JOIN authors a ON b.author_id = a.id
            WHERE {_NEEDS_SCAN_STRICT_ALIASED}{skip_clause}
            ORDER BY {_recent_scan_order_clause('b.')}
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

        from app.discovery.cover_phash import ensure_cover_phash
        seshat_phash = await ensure_cover_phash(db, book_id, token=session_id)

        check = await check_book(session_id, book_title, author_name, format_priority, delay, lang_ids=lang_ids, content_type=content_type, seshat_cover_phash=seshat_phash)
        scanned += 1

        # Same auth-error-aware timestamp guard as scan_books_batch —
        # see that function's UPDATE for full rationale.
        await db.execute("""
            UPDATE books SET mam_url=?, mam_status=?, mam_formats=?,
                   mam_torrent_id=?, mam_has_multiple=?, mam_my_snatched=?,
                   mam_is_bundle=?,
                   mam_last_scanned_at=CASE
                       WHEN ? = 'auth_error' THEN mam_last_scanned_at
                       ELSE ?
                   END
            WHERE id=?
        """, (
            check["mam_url"], check["status"], check["mam_formats"],
            check["mam_torrent_id"], 1 if check["mam_has_multiple"] else 0,
            1 if check.get("mam_my_snatched") else 0,
            1 if check.get("mam_is_bundle") else 0,
            check["status"],
            time.time(),
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
