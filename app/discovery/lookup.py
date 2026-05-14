"""
Source-lookup engine.

Iterates the user's authors against external book catalogs (Goodreads,
Hardcover, Kobo, in that priority order) and merges what each source
returns into the local DB. The merge layer is the heart of this module:
it has to reconcile conflicting metadata between sources without
clobbering the user's Calibre-sourced library, and it has to do that
hundreds of times per scan without producing false-positive book matches
or duplicate series rows.

Three things to know before reading:

  1. Title matching is *relaxed* by design — sources spell, capitalize,
     and subtitle-decorate the same book in incompatible ways. The
     `_fuzzy_match` function in this file is the single source of truth
     for "are these the same book?" and is heavily commented because
     small changes silently mis-link books across the entire library.

  2. The merge layer protects Calibre-sourced rows with field-level
     rules instead of a blanket lock — see `_update_existing`. The user
     curates Calibre, so source scans are allowed to enrich it (fill
     missing fields, correct edition-specific dates) but never to
     overwrite curated content.

  3. Series rows are upserted lazily: if a per-source series ends up
     with no linked books in this author after filtering, no series row
     is created. This prevents orphaned series rows from leaking into
     the DB during library-only scans.
"""
import asyncio, time, re, logging, json
from dataclasses import dataclass
from typing import Any
from difflib import SequenceMatcher
import aiosqlite
from app.config import load_settings
from app.discovery.database import get_db
from app.discovery.sources.hardcover import HardcoverSource
from app.discovery.sources.openlibrary import OpenLibrarySource
from app.discovery.sources.goodreads import GoodreadsSource
from app.discovery.sources.kobo import KoboSource
from app.discovery.sources.amazon import AmazonSource
from app.discovery.sources.ibdb import IbdbSource
from app.discovery.sources.google_books import GoogleBooksSource
from app.discovery.sources.audible import AudibleDiscoverySource
from app.discovery.sources.base import AuthorResult
from app import state

logger = logging.getLogger("seshat.discovery.lookup")


# ─── Pre-compiled regex patterns ─────────────────────────────
# Hoisted to module scope so `_normalize`, `_normalize_light`,
# `_looks_foreign`, and `_is_series_ref_title` don't re-lookup (and in
# the worst case recompile) the same patterns thousands of times per
# scan. Python's built-in regex LRU is only 512 entries and can thrash
# with inline literal patterns in hot loops — explicit compilation is
# faster AND makes the patterns visible at the top of the file.
_RX_LEADING_ARTICLE = re.compile(r'^(the|a|an)\s+')
_RX_PARENS = re.compile(r'\s*\([^)]*\)\s*')
_RX_SUBTITLE = re.compile(r'\s*:.*$')
# Inverse of _RX_SUBTITLE: strips up to and including the first colon.
# Used for the "SeriesPrefix: BookTitle" case that Hardcover often uses
# (e.g., "Mistborn: The Final Empire" while Calibre has just "The Final
# Empire"). _normalize strips the suffix after `:` which is the wrong
# half for this layout — _normalize_strip_prefix handles the other.
_RX_SERIES_PREFIX = re.compile(r'^[^:]+:\s*')

# "Generic-subtitle" matcher: detects suffixes that are obviously
# marketing/edition taglines rather than real book titles. Used by
# _normalize to decide whether stripping the suffix-after-colon is safe.
# Without this, "Mistborn: The Final Empire" reduced to "mistborn" and
# any two books in the same series collapsed to identical normalized
# forms — fine when only Hardcover used "Series:" prefix format because
# Calibre never did, but fragile if a future scan ever produced two
# such titles. Patterns covered:
#   - "A Novel" / "A Memoir" / "A Tale" / etc.
#   - "The Definitive/Complete/Illustrated/… Edition"
#   - "Book/Volume/Vol/Part/Chapter/Tome <number>" or "<word number>"
#   - "<n>th Anniversary Edition"
_RX_GENERIC_SUBTITLE = re.compile(
    r'\s*:\s*'
    r'(?:'
    r'an?\s+(?:novel|novella|memoir|story|tale|history|biography|autobiography'
    r'|guide|companion|handbook|introduction|adventure|fable|romance|mystery'
    r'|thriller|epic|chronicle|trilogy)s?'
    r'|the\s+(?:definitive|complete|illustrated|annotated|original|expanded'
    r'|revised|special|limited|deluxe|collector\'?s|anniversary)\s+'
    r'(?:edition|version|collection)'
    r'|\d+(?:st|nd|rd|th)\s+anniversary\s+edition'
    r'|(?:book|volume|vol\.?|part|chapter|tome)\s+\d+'
    r'|(?:book|volume|vol\.?|part|chapter|tome)\s+'
    r'(?:one|two|three|four|five|six|seven|eight|nine|ten)'
    r')\s*$',
    re.IGNORECASE,
)
_RX_NONWORD = re.compile(r'[^\w\s]')
_RX_SPACES = re.compile(r'\s+')
# Word-joining punctuation that needs to become a SPACE before _RX_NONWORD
# strips it. Without this, "The Dragon's Path/Leviathan Wakes" normalized
# to "dragons pathleviathan wakes" (no space between "path" and "leviathan"
# because the slash got eaten as a non-word char), which then matched
# "leviathan wakes" via substring containment — a false positive that
# linked "Leviathan Wakes" to a Daniel Abraham anthology containing an
# ARC of it. Slashes, ampersands, and explicit ' and ' are the typical
# joiners between bound-edition titles.
_RX_TITLE_JOINERS = re.compile(r'\s*[/&+]\s*')
_RX_FOREIGN_ACCENTS = re.compile(
    r'[àáâãäåæçèéêëìíîïðñòóôõöùúûüýþÿąćęłńóśźżšžřůďťňĺľŕäöüß]',
    re.I,
)
_RX_FOREIGN_UNICODE = re.compile(
    r'[\u0400-\u04ff\u3000-\u9fff\u0600-\u06ff\uac00-\ud7af]'
)
_RX_SERIES_REF_TITLE = re.compile(r'^.+\s+#\d+\s*$')

# Omnibus / compilation detection. Titles matching these patterns get
# the is_omnibus flag and their series_index is cleared so they don't
# push other books out of position.
_RX_OMNIBUS = re.compile(
    r'(?i)(?:'
    r'\bomnibus\b'
    r'|\bcompilation\b|\banthology\b'
    r'|\bbox\s*set\b|\bboxed\s+set\b'
    r'|\bfull\s+series\b'
    r'|\bvolume\s+set\b'
    r'|\bbooks?\s+\d+\s*[-–&]\s*\d+'  # "Books 1-3", "Book 1 & 2"
    r'|\b\d+[-\s]book\s+(?:collection|set|bundle|omnibus)\b'  # "6-Book Collection", "3-Book Boxed Set"
    r'|\bcomplete\s+(?:series|collection|trilogy|saga|anthology|stories|novellas|novels|chronicles|short\s+fiction|graphic\s+novel\s+series)\b'
    r'|\b(?:the\s+)?complete(?:\s+\w+){1,5}\s+(?:series|trilogy|saga|collection|anthology|edition|stories|chronicles|tales|novels|novellas|set|bundle)\b'
    r'|:\s*the\s+complete\b'  # subtitle "The Complete ..." — accepts the rare single-book FP
    r'|\(\s*complete\s*\)'  # bare "(complete)" parenthetical anywhere — series-status annotation pulled into title
    r')'
)

# Audiobook / non-ebook format detection. Matches titles and metadata
# that indicate audiobook-only editions, narrator credits, or non-ebook
# formats. Two-part detection:
#   1. Format markers in/around the title: "[Audible Audio]", "Audio CD",
#      "MP3 CD", "Audiobook" as standalone words or bracketed tags.
#   2. Contributor-role markers: "(Narrator)", "(Read by)", "(Foreword)",
#      "(Illustrator)" — these appear in author strings that some sources
#      pack into the title field.
_RX_AUDIOBOOK_FORMAT = re.compile(
    r'(?i)\b(audible\s*audio|audio\s*cd|mp3\s*cd|audiobook)\b'
    r'|\[audible\b'
)
_RX_CONTRIBUTOR_ROLE = re.compile(
    r'\(\s*(?:narrator|read\s+by|foreword|illustrator|introduction)\s*\)',
    re.IGNORECASE,
)


def _merge_source_urls(existing_json: str, source_name: str, new_url: str) -> str:
    """Merge a new source URL into the JSON dict stored in source_url column."""
    if not new_url:
        return existing_json or "{}"
    try:
        urls = json.loads(existing_json) if existing_json else {}
    except (json.JSONDecodeError, TypeError):
        urls = {}
    if not isinstance(urls, dict):
        # Migrate from old plain-string format
        urls = {}
    urls[source_name] = new_url
    return json.dumps(urls)

hardcover = HardcoverSource()
goodreads = GoodreadsSource()
kobo = KoboSource()
amazon = AmazonSource()
ibdb = IbdbSource()
google_books = GoogleBooksSource()
openlibrary = OpenLibrarySource()
audible = AudibleDiscoverySource()


def reload_sources():
    """Rebuild the module-level source singletons from current settings.

    Rate limits come from `metadata_sources[<name>].rate_limit` via
    `get_source_rate_limit` — the unified Phase-7 shape. Legacy
    `rate_*` keys were retired once this file was the last reader;
    the helper falls back to each source's default_rate when the
    setting is missing so upgrade paths keep working before the
    panel is touched.
    """
    global hardcover, goodreads, kobo, amazon, ibdb, google_books, openlibrary, audible
    from app.metadata.source_config import get_source_rate_limit
    s = load_settings()
    hardcover = HardcoverSource(api_key=s.get("hardcover_api_key", ""))
    goodreads = GoodreadsSource(rate_limit=get_source_rate_limit(s, "goodreads"))
    # v2.11.1 N5: kobo.concurrency exposed via metadata_sources panel.
    # Read with default=4 so pre-v2.11.1 settings.json files (no
    # `concurrency` key) keep the v2.11.0 ship-default behaviour.
    _kobo_entry = (s.get("metadata_sources") or {}).get("kobo") or {}
    kobo = KoboSource(
        rate_limit=get_source_rate_limit(s, "kobo"),
        concurrency=int(_kobo_entry.get("concurrency", 4) or 4),
    )
    # Stage 5++: format + language drive Amazon's server-side
    # authorFilters API on /juvec. Defaults applied if missing.
    _amazon_entry = (s.get("metadata_sources") or {}).get("amazon") or {}
    amazon = AmazonSource(
        rate_limit=get_source_rate_limit(s, "amazon"),
        format_filter=_amazon_entry.get("format", "kindle"),
        language=_amazon_entry.get("language", "English"),
    )
    ibdb = IbdbSource(rate_limit=get_source_rate_limit(s, "ibdb"))
    google_books = GoogleBooksSource(
        rate_limit=get_source_rate_limit(s, "google_books"),
        api_key=s.get("google_books_api_key", ""),
    )
    openlibrary = OpenLibrarySource(rate_limit=get_source_rate_limit(s, "openlibrary"))
    audible = AudibleDiscoverySource(
        region=s.get("audible_region", "us"),
        rate_limit=get_source_rate_limit(s, "audible"),
    )


# ─── Source orchestration registry ──────────────────────────
# Centralizes per-source metadata so `lookup_author` walks a single
# typed list instead of a hand-rolled if-chain. Each spec carries:
#   - role: "primary" | "secondary" | "supplementary"
#       Informational; not yet used to short-circuit. The pipeline's
#       per-book enricher short-circuits at score >= 0.8, but discovery
#       legitimately *wants* every source's per-book signals (Amazon
#       for series confirmation, IBDB for ISBN, GB for descriptions) so
#       cross-source short-circuit would lose backfills. Kept here for
#       future use (e.g., owned-only mode could skip supplementary).
#   - timeout_sec: hard wall-clock cap on a single-author scan of
#       this source. Generous enough that a normal scan completes,
#       tight enough that a stuck source can't hang the whole pipeline.
#       A timeout returns the partial work the source has already
#       merged (writes happen incrementally during the scan), then
#       the loop moves to the next source.
#   - getter: returns the live module-level instance so reload_sources()
#       takes effect without re-registering the table.
#
# A global per-author wall-clock budget on top of these caps the
# total time even if every source individually stays under its
# timeout — see PER_AUTHOR_BUDGET_SEC.
@dataclass
class SourceSpec:
    name: str
    role: str
    timeout_sec: float
    getter: Any  # callable returning the live source instance
    default_enabled: bool = True


def _src_goodreads():    return goodreads
def _src_hardcover():    return hardcover
def _src_kobo():         return kobo
def _src_amazon():       return amazon
def _src_ibdb():         return ibdb
def _src_google_books(): return google_books
def _src_openlibrary():  return openlibrary
def _src_audible():      return audible


# Ebook-library source list. Walked for every library whose
# `content_type == "ebook"`. Goodreads primary, Hardcover primary, rest
# filling supplementary roles.
SOURCES: list[SourceSpec] = [
    SourceSpec("goodreads",    "primary",       300.0, _src_goodreads,    True),
    SourceSpec("hardcover",    "primary",       180.0, _src_hardcover,    True),
    # Kobo: 180s (was 300s through v2.10.10). Per-book detail fetches
    # now run with bounded concurrency (default 4 via the v2.11.0
    # asyncio.Semaphore refactor in `app/discovery/sources/kobo.py`),
    # so the ~125s sequential floor that justified the 300s cap is
    # gone. Sanderson's 80-book bibliography drops from ~220s to ~60s
    # at the default concurrency/rate-limit ratio. 180s leaves ~3×
    # headroom over the new typical, matching Hardcover's primary-tier
    # timeout shape.
    SourceSpec("kobo",         "secondary",     180.0, _src_kobo,         True),
    # Amazon: 600s timeout (was 180s through v2.11.0 stage 5+). The
    # curl_cffi Chrome 120 TLS impersonation defeats Akamai's bot
    # gate, but the 30s rate-limit (required to stay under Akamai's
    # density scoring) makes a per-author scan walk slowly: search +
    # 2-3 page fetches + up to _MAX_DETAIL_FETCHES detail pages at
    # ~35s each. Realistic worst-case at the v2.11.0 limits is
    # 10 details × 35s + overhead = ~6.5 minutes. 600s leaves ~30%
    # headroom. Paired with _MAX_DETAIL_FETCHES=10 in amazon.py so
    # the budget caps work, not just elapsed time.
    SourceSpec("amazon",       "secondary",     600.0, _src_amazon,       False),
    SourceSpec("ibdb",         "supplementary",  90.0, _src_ibdb,         False),
    SourceSpec("google_books", "supplementary",  60.0, _src_google_books, False),
    SourceSpec("openlibrary",  "supplementary", 120.0, _src_openlibrary,  False),
]


# Audiobook-library source list. Used for libraries whose
# `content_type == "audiobook"` (e.g. an Audiobookshelf-backed
# library). Audible runs as primary since its catalog + Audnexus
# hydration covers narrator/duration/series cleanly. Hardcover
# stays secondary because it does track audiobook editions when
# given an API key. Goodreads/Kobo/etc. are omitted — they don't
# surface audiobook-specific metadata and the catalog coverage
# heavily overlaps Audible.
AUDIOBOOK_SOURCES: list[SourceSpec] = [
    SourceSpec("audible",     "primary",       300.0, _src_audible,     True),
    SourceSpec("hardcover",   "secondary",     180.0, _src_hardcover,   True),
    SourceSpec("openlibrary", "supplementary", 120.0, _src_openlibrary, False),
]


def _sources_for_content_type(content_type: str) -> list[SourceSpec]:
    """Pick the right source-registry list for a library.

    Routes by the library's `content_type` (from the registered
    `LibraryApp`). Defaults to `SOURCES` for anything other than
    "audiobook" so unknown/future content types fall back to the
    ebook scan — safer than silently skipping the scan entirely.
    """
    if content_type == "audiobook":
        return AUDIOBOOK_SOURCES
    return SOURCES

# Total wall-clock budget across all sources for a single author.
# At 25 minutes (v2.3.1, up from 15), even prolific authors with
# slow Goodreads days have headroom. Eric Vall's 359-book scan at
# ~3.5s/book on a slow Goodreads day = ~21min just for Goodreads;
# under the old 15min cap the multi-retry loop ran out of budget
# before completing. If the budget is exceeded, remaining sources
# are skipped and a warning is logged — the partial result is still
# committed because each source's writes are independent.
PER_AUTHOR_BUDGET_SEC = 25 * 60


def _smart_strip_subtitle(t: str) -> str:
    """Strip the part after `:` only when it looks like a real subtitle.

    Two acceptance rules — strip if EITHER fires:
      1. The PREFIX (before colon) has 3+ words. Real titles tend to be
         multi-word ("Project Hail Mary", "The Catcher in the Rye");
         series names tend to be 1-2 words ("Mistborn", "Star Wars",
         "Doctor Who"). 3-word prefixes are unambiguous.
      2. The SUFFIX (after colon) matches a generic-subtitle pattern
         like "A Novel", "Part 1", "The Definitive Edition" — those
         are never real book titles, so stripping is always safe even
         when the prefix is just one word ("Dune: A Novel").

    Otherwise, leave the colon and everything after it intact. The
    `_normalize_strip_prefix` path in _fuzzy_match handles the inverse
    case ("Series: BookTitle" vs bare "BookTitle"), so dropping the
    blanket strip doesn't lose match coverage — it just stops the
    spurious cross-book collision where "Mistborn: The Final Empire"
    and "Mistborn: The Hero of Ages" both reduced to just "mistborn".
    """
    if ':' not in t:
        return t
    prefix = t.split(':', 1)[0]
    if len(prefix.split()) >= 3:
        return _RX_SUBTITLE.sub('', t)
    if _RX_GENERIC_SUBTITLE.search(t):
        return _RX_SUBTITLE.sub('', t)
    return t


def _normalize(t: str) -> str:
    t = t.lower().strip()
    t = _RX_LEADING_ARTICLE.sub('', t)
    t = _RX_PARENS.sub(' ', t)  # Remove parenthetical
    t = _smart_strip_subtitle(t)  # Strip "X: Subtitle" only when safe
    t = _RX_TITLE_JOINERS.sub(' ', t)  # "/" "&" "+" between titles → space
    t = _RX_NONWORD.sub('', t)
    t = _RX_SPACES.sub(' ', t)
    return t.strip()

def _normalize_light(t: str) -> str:
    """Light normalization — keeps subtitles, just cleans punctuation."""
    t = t.lower().strip()
    t = _RX_PARENS.sub(' ', t)
    t = _RX_NONWORD.sub(' ', t)
    t = _RX_SPACES.sub(' ', t)
    return t.strip()


def _normalize_strip_prefix(t: str) -> str:
    """Same as _normalize but strips the part BEFORE the first colon
    instead of after. Lets `_fuzzy_match` link Hardcover-style
    "Mistborn: The Final Empire" against Calibre's "The Final Empire"
    by reducing the former to "final empire" instead of "mistborn".
    Returns "" for inputs with no colon (caller can skip).
    """
    if ':' not in t:
        return ''
    t = t.lower().strip()
    t = _RX_PARENS.sub(' ', t)
    t = _RX_SERIES_PREFIX.sub('', t, count=1)
    t = _RX_LEADING_ARTICLE.sub('', t)  # post-strip "the" survivor
    t = _RX_TITLE_JOINERS.sub(' ', t)
    t = _RX_NONWORD.sub('', t)
    t = _RX_SPACES.sub(' ', t)
    return t.strip()


def _fuzzy_match(a: str, b: str) -> bool:
    """Relaxed title matching using normalization + sequence matching.

    Substring-containment branches require a length ratio of at least
    0.75 — the shorter title must be ≥75% of the longer. Without this
    guard, three documented false positives slipped through:

      - "Leviathan Wakes" (15) vs "Dragons Path Leviathan Wakes" (28),
        ratio 0.54 — a Daniel Abraham anthology containing an ARC of
        the Corey book. Wrongly linked the user's Corey book to the
        Abraham anthology page on Goodreads.
      - "Pride and Prejudice" (19) vs "Pride and Prejudice and Zombies"
        (31), ratio 0.61 — a parody is a different book.
      - "Foundation" (10) vs "Foundation Trilogy" (18), ratio 0.55 —
        an omnibus is a different cataloged work.

    Legitimate matches almost always go through the exact-normalized
    path (the prefilter dict in _merge_result), so the substring branch
    is a tiebreaker for cases like "Title: Subtitle" where the user's
    Calibre has the bare title. 0.75 is the same threshold the
    SequenceMatcher branch already uses, which keeps the two paths in
    sync. The trade-off: very-short-title vs very-long-extended-subtitle
    cases ("Foo" vs "Foo, A Tale of Bar and Baz") won't auto-link via
    substring; they'd need either exact normalization (which usually
    works thanks to subtitle stripping) or manual linking.
    """
    def _len_ratio_ok(short_len: int, long_len: int) -> bool:
        if long_len == 0:
            return False
        return (short_len / long_len) >= 0.75

    na, nb = _normalize(a), _normalize(b)
    if na == nb: return True
    if na in nb and _len_ratio_ok(len(na), len(nb)): return True
    if nb in na and _len_ratio_ok(len(nb), len(na)): return True
    # Also check with light normalization (keeps subtitles)
    la, lb = _normalize_light(a), _normalize_light(b)
    if la == lb: return True
    if la in lb and _len_ratio_ok(len(la), len(lb)): return True
    if lb in la and _len_ratio_ok(len(lb), len(la)): return True
    # SequenceMatcher fallback for close-but-not-exact matches. The 0.85
    # threshold is deliberate: 0.75 once admitted "Pride and Prejudice"
    # ↔ "Pride and Prejudice and Zombies" (ratio 0.76 — a parody is not
    # the same book). 0.85 still catches spelling variants ("Colour" vs
    # "Color", ratio 0.91), light typos, and minor punctuation drift.
    # Anything below 0.85 should either go through the exact-normalized
    # path or be treated as a different book.
    if len(na) > 3 and len(nb) > 3:
        if SequenceMatcher(None, na, nb).ratio() > 0.85: return True
    if len(la) > 3 and len(lb) > 3:
        if SequenceMatcher(None, la, lb).ratio() > 0.85: return True

    # Strip-prefix path: handles "SeriesPrefix: BookTitle" against
    # bare-title Calibre rows. The default `_normalize` strips the
    # suffix after `:`, which is the wrong half for this layout —
    # Hardcover returns "Mistborn: The Final Empire" while Calibre has
    # "The Final Empire", and strip-suffix reduces both sides to
    # mismatched halves. `_normalize_strip_prefix` does the inverse.
    #
    # Two-word minimum: a 1-word strip-prefix result is too generic to
    # auto-link safely. "Stephen King: A Biography" → "biography" would
    # match any Calibre row called "Biography", and "Star Wars:
    # Aftermath" → "aftermath" would collide across multiple authors'
    # unrelated books. The 2-word floor still catches the cases that
    # actually show up in scans ("final empire", "new hope", "way of
    # kings").
    def _strip_prefix_ok(p: str) -> bool:
        return bool(p) and len(p.split()) >= 2

    pa = _normalize_strip_prefix(a)
    pb = _normalize_strip_prefix(b)
    if _strip_prefix_ok(pa):
        if pa == nb: return True
        if pa in nb and _len_ratio_ok(len(pa), len(nb)): return True
        if nb in pa and _len_ratio_ok(len(nb), len(pa)): return True
    if _strip_prefix_ok(pb):
        if pb == na: return True
        if pb in na and _len_ratio_ok(len(pb), len(na)): return True
        if na in pb and _len_ratio_ok(len(na), len(pb)): return True
    if _strip_prefix_ok(pa) and _strip_prefix_ok(pb) and pa == pb: return True

    # Strip-suffix path: handles "BookTitle: SeriesName" against bare
    # "BookTitle". The strip-prefix path above handles the inverse
    # layout; this catches the case where the book title IS the prefix
    # and the subtitle is a series name or edition tag. Example:
    # "Otherlife Dreams: The Selfless Hero Trilogy" → prefix "otherlife
    # dreams" matches bare "Otherlife Dreams".
    #
    # Two-word minimum on the prefix to prevent "A: Novel" style
    # single-word matches from false-linking.
    def _colon_prefix(t: str) -> str:
        if ':' not in t:
            return ''
        p = t.split(':', 1)[0]
        return _normalize(p)

    ca = _colon_prefix(a)
    cb = _colon_prefix(b)
    if ca and len(ca.split()) >= 2:
        if ca == nb: return True
    if cb and len(cb.split()) >= 2:
        if cb == na: return True

    return False


def _series_index_conflicts(
    incoming_index: float | int | None,
    existing_index: float | int | None,
) -> bool:
    """True iff both sides assert a series_index and the two disagree.

    Used as a post-hoc guard on `_fuzzy_match` results: the fuzzy
    title matcher accepts `"Incubus Inc."` against `"Incubus Inc. 3"`
    via its substring-containment path, but those are clearly
    different books (series #1 vs #3). If both sides have an index
    and the numbers don't match, they can't be the same book —
    reject the fuzzy match. When one side is missing its index, we
    can't prove conflict and defer to the fuzzy match (a source
    might legitimately report a series book without its position,
    and we'd want to still merge it).

    Cast through `float` so `1 == 1.0` returns True regardless of
    which side happened to store an int vs a float.
    """
    if incoming_index is None or existing_index is None:
        return False
    try:
        return float(incoming_index) != float(existing_index)
    except (TypeError, ValueError):
        # Non-numeric index (shouldn't happen per the dataclass typing,
        # but be defensive) — can't prove a conflict, don't reject.
        return False


def _title_extracted_index(title: str | None) -> float | None:
    """Pull a series-position number out of a title via regex, if one
    can be inferred. `_RX_TITLE_SERIES_IDX` is the same regex
    `_title_to_series_pass` uses to repair untagged sources.

    Catches the bare-trailing-number case ("Super Sales on Super
    Heroes 4"), the explicit "#N" case, and the "Book N" case. Used
    by `_fuzzy_match_blocked` as a second-line conflict signal when
    the source returns the book as a standalone (no `series_index`
    on the BookResult) but the title encodes a position.
    """
    if not title:
        return None
    m = _RX_TITLE_SERIES_IDX.search(title)
    if not m:
        return None
    for grp in m.groups():
        if grp is not None:
            try:
                return float(grp)
            except (TypeError, ValueError):
                return None
    return None


def _fuzzy_match_blocked(bk, row) -> str | None:
    """Return a short reason string if a fuzzy title match between
    `bk` (BookResult) and `row` (DB row) should be REJECTED, or
    None if the match is acceptable.

    Two reject signals on top of the `_fuzzy_match` substring/
    SequenceMatcher acceptance:

    1. **Omnibus / non-omnibus mismatch**: if one side's title
       triggers `_is_omnibus` (matches "compilation", "omnibus",
       "complete trilogy", etc.) and the other doesn't, they're
       different books regardless of how high the title overlap
       runs. Catches Hardcover's "Right of Retribution: Compilation:
       The Starting Point" fuzzy-matching onto the user's owned
       book #1 "Right of Retribution" via prefix containment.

    2. **Series-position conflict (extended)**: the original
       `_series_index_conflicts` check only fires when BOTH sides
       carry an explicit `series_index`. ibdb commonly returns
       series books as standalones (no series_index tag) with the
       position encoded in the title — "Super Sales on Super
       Heroes 4". Without this extension the fuzzy matcher accepts
       #4 onto an existing #2 row and `_update_existing` overwrites
       the wrong row's metadata. Fall back to title-extracted
       indices on either side when the explicit index is missing.

    Returns `None` when the match is acceptable, or a short reason
    code suitable for log lines (`omnibus_mismatch` /
    `position_conflict`).
    """
    bk_title = bk.title
    row_title = row["title"]
    if _is_omnibus(bk_title) != _is_omnibus(row_title):
        return "omnibus_mismatch"
    bk_idx = bk.series_index
    if bk_idx is None:
        bk_idx = _title_extracted_index(bk_title)
    row_idx = row["series_index"] if "series_index" in row.keys() else None
    if row_idx is None:
        row_idx = _title_extracted_index(row_title)
    if bk_idx is not None and row_idx is not None:
        try:
            if float(bk_idx) != float(row_idx):
                return "position_conflict"
        except (TypeError, ValueError):
            pass
    return None


def _lang_ok(book_lang: str, allowed: list[str]) -> bool:
    """Check if a book's language is in the allowed list."""
    if not allowed: return True
    if not book_lang: return True  # Unknown language, assume ok
    bl = book_lang.lower().strip()
    return any(al.lower().strip() in bl or bl in al.lower().strip() for al in allowed)


def _looks_foreign(title: str) -> bool:
    """Detect titles that are likely non-English.

    Three signals, OR'd:
      1. Diacritics (covers French/Spanish/German/Polish/etc. accented forms)
      2. Non-Latin script (Cyrillic, CJK, Arabic, Hangul — already-block)
      3. Edition/volume keywords that are unambiguously non-English

    The keyword list intentionally avoids ambiguous English-overlapping
    words ("tome" exists in English; "edition" can appear in English
    titles). Only words that are essentially proof of foreign translation
    go in: "coffret" (French boxed-set marker), "ausgabe" (German
    edition), "edizione" (Italian edition), "wydanie" (Polish edition).
    """
    if _RX_FOREIGN_ACCENTS.search(title):
        return True
    if _RX_FOREIGN_UNICODE.search(title):
        return True
    # Common foreign words in translated titles. Hungarian + Polish kept
    # from original list; v2.11.0 added French/German/Italian/Polish
    # edition markers surfaced by the Hasekura Hardcover/OL UAT.
    tl = title.lower()
    foreign_kw = [
        # Original (Hungarian, Polish, Russian)
        'hamvai', 'kapuja', 'háborúja', 'bosszúja', 'przebudzenie',
        'ekspansja', 'lewiatana', 'babilon', 'пробуждение', 'врата',
        # v2.11.0 additions — unambiguously non-English edition markers
        'coffret',   # French boxed-set
        'ausgabe',   # German edition
        'edizione',  # Italian edition
        'wydanie',   # Polish edition
    ]
    if any(fw in tl for fw in foreign_kw):
        return True
    return False


def _is_series_ref_title(title: str) -> bool:
    """Detect titles like 'The Expanse #3' or 'New Novella #2' — series position refs, not real titles."""
    return bool(_RX_SERIES_REF_TITLE.match(title.strip()))


# Patterns that indicate a book set/collection
_SET_PATTERNS = re.compile(
    r'(?i)\b(box\s*set|boxset|books?\s+#?\d+\s*[-–]\s*#?\d+|'
    r'series\s+#?\d+\s*[-–]\s*#?\d+|series\s+\d+\s+books?\b|'
    r'collection\s+#?\d+\s*[-–]\s*#?\d+|collection\s+set|'
    r'\d+\s*books?\s+in\s+\d|complete\s+series|book\s+set|'
    r'series\s+set|hardcover\s+set|paperback\s+set|'
    r'volumes?\s+\d+\s*[-–]\s*\d+|'
    r'\d+\s+books?\s+collection|roleplaying\s+game|'
    r'\d+\s+set)\b'  # "(4 Set)", "(6 Set)" etc.
)
# Bound-edition / anthology detector. A title like "The Dragon's Path /
# Leviathan Wakes" is two distinct books pressed into one publishing
# event (often an Advance Reading Copy giveaway or a publisher promo
# pack). Source scanners return these as single books, and the fuzzy
# matcher used to false-positive them onto the user's owned book of
# the same name.
#
# We ONLY match `/` as the joiner — not `&`, `+`, or ` and `. Those
# all appear in legitimate single-book titles ("Pride and Prejudice",
# "War and Peace", "Beauty & the Beast", "Foundation and Empire"),
# and rejecting them would skip thousands of real books. Forward-slash
# is the only marker that is essentially never used in a real title.
# It must have non-space chars on BOTH sides so we don't trip on
# stray punctuation, and we tolerate optional whitespace around it.
_RX_ANTHOLOGY = re.compile(r'\S\s*/\s*\S')


def _is_book_set(title: str) -> bool:
    """Check if a title looks like a book set/collection rather than an individual book."""
    if _SET_PATTERNS.search(title):
        return True
    if _RX_ANTHOLOGY.search(title):
        return True
    # Semicolon-joined titles: "Book A; Book B; Book C" — almost always
    # a multi-book bundle. Require 2+ semicolons to avoid false positives
    # on titles that use a single semicolon as punctuation.
    if title.count(';') >= 2:
        return True
    return False


def _is_omnibus(title: str) -> bool:
    """Detect omnibus editions and compilations.

    Catches titles like:
      - "The Selfless Hero Trilogy: Omnibus"
      - "Mistborn: The Complete Trilogy"
      - "Super Sales on Super Heroes Books 1-3"
      - "The Expanse Box Set"
    """
    return bool(_RX_OMNIBUS.search(title))


def _is_audiobook(title: str) -> bool:
    """Detect audiobook-only editions and non-ebook formats.

    Catches titles like:
      - "Some Book [Audible Audio]"
      - "Some Book (Audio CD)"
      - "Some Book (Ray Porter (Narrator))"
      - "Some Book: The Audiobook"
    """
    if _RX_AUDIOBOOK_FORMAT.search(title):
        return True
    if _RX_CONTRIBUTOR_ROLE.search(title):
        return True
    return False


async def _validate_author(author_name: str, our_titles: list[str], result: AuthorResult) -> bool:
    """Validate found author by checking if ANY of our books fuzzy-match their catalog."""
    if not our_titles: return True
    src_titles = [b.title for b in result.books]
    for sr in result.series:
        src_titles.extend([b.title for b in sr.books])
    if not src_titles: return False
    for ours in our_titles:
        for theirs in src_titles:
            if _fuzzy_match(ours, theirs):
                return True
    logger.info(f"  Validation FAILED for '{author_name}': 0/{len(our_titles)} matched in {len(src_titles)} source books")
    return False


async def _merge_result(author_id: int, result: AuthorResult, source_name: str, languages: list[str], full_scan: bool = False, owned_only: bool = False, series_collector: dict | None = None, on_new_book=None, exclude_audiobooks: bool = True, linked_author_ids: list[int] = None, link_type_by_id: dict[int, str] | None = None):
    """Merge an AuthorResult, filtering by language. In full_scan mode, updates metadata on existing books.

    When owned_only=True (the "Library-only source scan" setting), the
    function still UPDATEs existing books with new URLs, series links, and
    (in full_scan mode) refreshed metadata, but it skips the INSERT branches
    entirely. The result: source scans become a metadata-enrichment pass
    over the user's owned library without ever discovering new missing or
    upcoming books. Useful for getting an existing library polished before
    turning the discovery firehose on.

    When `series_collector` is a dict, this function also records each
    matched `(book_id → source_name → (series_name, series_index))`
    tuple into it. The collector is owned by `lookup_author` and
    threaded through every source's merge call so that after all
    sources run, `_compute_series_suggestions` can tally per-source
    agreement and write suggestion rows wherever 2+ sources agree on a
    series that differs from what's stored. Recording is purely
    observational — the merge layer's write behavior (priority-gated
    for series, Calibre-locked for owned books) is unchanged.
    """
    db = await get_db()
    try:
        new_books = 0; updated_books = 0
        # Update author metadata
        up = []; pr = []
        if result.image_url: up.append("image_url = COALESCE(image_url, ?)"); pr.append(result.image_url)
        if result.bio: up.append("bio = COALESCE(bio, ?)"); pr.append(result.bio)
        if result.external_id: up.append(f"{source_name}_id = ?"); pr.append(result.external_id)
        up.append("last_lookup_at = ?"); pr.append(time.time()); pr.append(author_id)
        if up: await db.execute(f"UPDATE authors SET {', '.join(up)} WHERE id = ?", pr)

        # SELECT includes pub_date, description, expected_date so the
        # owned-book metadata logic in _update_existing can compare against
        # what's currently stored without a second round-trip per book.
        # The smart-description and oldest-pub_date rules need to read the
        # current value to decide whether to overwrite.
        # Load books for this author + any pen-name-linked authors.
        # Linked author rows are included so fuzzy match can find them
        # for dedup, but only the current author's rows get UPDATEd.
        # Linked matches suppress the INSERT (the book already exists
        # under the pen name).
        all_author_ids = [author_id] + (linked_author_ids or [])
        id_ph = ",".join("?" * len(all_author_ids))
        rows = await (await db.execute(
            f"SELECT id, title, source_url, series_id, series_index, source, "
            f"pub_date, expected_date, description, isbn, author_id, is_omnibus, hidden "
            f"FROM books WHERE author_id IN ({id_ph})",
            all_author_ids,
        )).fetchall()
        existing = {_normalize(r["title"]) for r in rows}
        # Build an O(1) prefilter: normalized-title → row. The book-merge
        # loops below used to linearly scan all `rows` for each incoming
        # source book, which was O(n*m) — 200 owned × 200 source = 40k
        # fuzzy-match calls per author. Most matches hit on exact
        # normalized equality (the first check inside _fuzzy_match), so
        # checking the dict first short-circuits the common case. The
        # linear loop stays as the fallback for substring and sequence-
        # matching cases the dict can't catch.
        rows_by_norm = {_normalize(r["title"]): r for r in rows}
        # ISBN prefilter: ISBN → row for O(1) merge by ISBN. Strongest
        # dedup signal — if a source's book has the same ISBN as an
        # existing row, it's definitely the same book regardless of title.
        rows_by_isbn = {}
        for r in rows:
            isbn = (r["isbn"] or "").strip().replace("-", "")
            if isbn and len(isbn) >= 10:
                rows_by_isbn[isbn] = r

        # ── Cross-author owned-ISBN map ─────────────────────────────
        # Defense against the "Halo: Evolutions" failure mode: a source
        # returns a book that the user already OWNS under a different
        # author (typically "Various authors" anthologies, or a
        # co-author the user hasn't manually linked yet) and the dedup
        # window above can't see it because it's outside the linked
        # author set. Look up every owned book's ISBN in the DB and
        # block INSERT for any incoming ISBN that already lives there
        # under a different author. Owned-only is the conservative
        # boundary: discovered cross-author duplicates are still
        # allowed (they may be legitimate co-authored entries the
        # consensus pass will reconcile), and curated co-author rows
        # where the user owns one entry per author also stay safe
        # (those rows would already be in the same-author set above).
        cross_isbn_owners: dict[str, int] = {}
        for r in await (await db.execute(
            "SELECT author_id, isbn FROM books "
            "WHERE owned = 1 AND isbn IS NOT NULL AND isbn != ''"
        )).fetchall():
            isbn = (r["isbn"] or "").strip().replace("-", "")
            if not isbn or len(isbn) < 10:
                continue
            # Skip ISBNs already owned by this author or its linked
            # authors — those are handled by the same-author dedup
            # path. We only want to flag truly external owners.
            if r["author_id"] in all_author_ids:
                continue
            cross_isbn_owners.setdefault(isbn, r["author_id"])

        # Same-series-position prefilter: `(series_id, series_index)` →
        # row. Used to dedup incoming books against existing ones that
        # share a slot in the same series but carry a different title
        # convention ("Remnant II" existing, "Remnant Book 2" incoming).
        # Without this the fuzzy title match fails on the low-similarity
        # pair and the new row inserts as a duplicate.
        rows_by_series_pos: dict[tuple[int, float], dict] = {}
        for r in rows:
            if r["series_id"] is not None and r["series_index"] is not None:
                rows_by_series_pos[(r["series_id"], float(r["series_index"]))] = r

        # Incoming series-name → existing series_id lookup. Populated
        # from the series table for this author + linked authors so
        # the same-series-position dedup above can resolve `sr.name`
        # to a numeric id without calling `_ensure_series` (which
        # would eagerly upsert the series row — wrong in `owned_only`
        # mode, where we intentionally defer series creation).
        author_series_id_by_name: dict[str, int] = {}
        for sr_row in await (await db.execute(
            f"SELECT id, name FROM series WHERE author_id IN ({id_ph})",
            all_author_ids,
        )).fetchall():
            s_name = sr_row["name"]
            if not s_name:
                continue
            author_series_id_by_name[s_name.lower()] = sr_row["id"]
            norm = _norm_consensus_series(s_name)
            if norm:
                author_series_id_by_name.setdefault(norm, sr_row["id"])

        # Series names for this author — used by the omnibus guard to
        # distinguish "BookTitle: SeriesName" (omnibus) from
        # "BookTitle: Subtitle" (dedup candidate).
        author_series_rows = await (await db.execute(
            "SELECT name FROM series WHERE author_id = ?", (author_id,)
        )).fetchall()
        author_series_norms = {
            _normalize(r["name"]): r["name"] for r in author_series_rows if r["name"]
        }

        def _extract_series_position(title: str) -> tuple[int, float] | None:
            """Parse "<series> N: <title>" or "<title> (<series> #N)" forms
            and resolve the prefix/parenthetical against this author's
            known series. Returns (series_id, series_index) on success
            or None if nothing parseable resolves to a known series.

            Backstops the cross-format duplicate failure: Goodreads's
            parenthetical form and Hardcover/Kobo's prefix form
            normalize to disjoint tokens, but both encode the same
            series-position pair which we use as the dedup key.
            """
            candidates: list[tuple[str, float]] = []
            mp = _RX_SERIES_PREFIX_TITLE.match(title)
            if mp:
                candidates.append((mp.group(1).strip(), float(mp.group(2))))
            mq = _RX_SERIES_PAREN_TITLE.search(title)
            if mq:
                candidates.append((mq.group(1).strip(), float(mq.group(2))))
            for s_name, s_idx in candidates:
                sid = (
                    author_series_id_by_name.get(s_name.lower())
                    or author_series_id_by_name.get(_norm_consensus_series(s_name))
                )
                if sid:
                    return (sid, s_idx)
            return None

        # Second pass: index existing rows by title-extracted position
        # too, so Goodreads-inserted standalone rows like "The Expanse
        # (Paths of Akashic #5)" — which carry NULL in series_id /
        # series_index because Goodreads emits them as standalone but
        # encode the position in the title — can still be matched as
        # the canonical row when Hardcover/Kobo arrives later in the
        # same scan with "Paths of Akashic 5: The Expanse".
        # `setdefault` keeps the explicitly-stored entry winning if
        # both forms exist for the same row.
        for r in rows:
            extracted = _extract_series_position(r["title"] or "")
            if extracted is not None:
                rows_by_series_pos.setdefault(extracted, r)

        # Source priority: Goodreads can overwrite series from any other source
        SOURCE_PRIORITY = {"mam": 1, "goodreads": 2, "amazon": 3, "hardcover": 4, "kobo": 5, "ibdb": 6, "google_books": 6, "manual": 7, "import": 7, "calibre": 0}
        
        def _is_hidden(matched_row) -> bool:
            """Hidden = "ignore" — see `_update_existing_url_only` for what
            still gets written (source_url merge + {source}_id COALESCE-
            fill, both additive). Metadata writes (title/author/series/
            description/dates/etc.) and series-collector contributions
            stay suppressed so a hidden row never participates in
            metadata enrichment or consensus suggestions.

            The motivation (v2.3.4): for prolific authors like John
            Walker (1,069 books on Goodreads) where individual scans
            don't match every book on the first pass, populating
            per-source URLs on hidden rows lets future scans fast-path
            past them via URL match instead of paying DETAIL on every
            unmatched title. v2.2.3's "true garbage bin" stance — no
            writes whatsoever — bounded the worst case but threw away
            useful URL discoveries."""
            try:
                return ("hidden" in matched_row.keys()) and (matched_row["hidden"] == 1)
            except (IndexError, KeyError):
                return False

        def _update_existing_url_only(matched_row, bk):
            """Build a URL-only UPDATE for hidden books — only `source_url`
            (merged additively) and `{source}_id` (COALESCE-fill) get
            written. Returns (sql, vals) or (None, None) when there's
            nothing to write (no URL and no external_id).

            This is the v2.3.4 hidden-book write rule. Mirrors the
            URL-write portion of `_update_existing` exactly so future
            edits to that block stay in sync — see lines around 932-935
            in `_update_existing`."""
            sets: list[str] = []
            vals: list = []
            if bk.source_url:
                merged = _merge_source_urls(
                    matched_row["source_url"], source_name, bk.source_url,
                )
                sets.append("source_url=?")
                vals.append(merged)
            if bk.external_id is not None:
                sets.append(f"{source_name}_id=COALESCE({source_name}_id,?)")
                vals.append(bk.external_id)
            if not sets:
                return None, None
            vals.append(matched_row["id"])
            sql = f"UPDATE books SET {', '.join(sets)} WHERE id=?"
            return sql, vals

        async def _flush_queue_rows(queue_rows):
            """UPSERT review-queue rows produced by `_update_existing`.

            One row per (book, field, source). The schema's
            UNIQUE(book_id, field, source) means re-running the same
            source against the same book replaces prior proposals
            rather than piling them up — keeps the Metadata Manager
            UI from drowning in stale alternatives.

            old_value/new_value are stored as TEXT — caller passes
            whatever type the column holds; we coerce here."""
            if not queue_rows:
                return
            for book_id, field, old_val, new_val, src in queue_rows:
                await db.execute(
                    "INSERT OR REPLACE INTO metadata_review_queue "
                    "(book_id, field, old_value, new_value, source) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        book_id,
                        field,
                        None if old_val is None else str(old_val),
                        None if new_val is None else str(new_val),
                        src,
                    ),
                )

        def _update_existing(matched_row, bk, series_id=None):
            """Build UPDATE for an existing book.

            Always writes (additive):
              source_url       : merged into JSON dict
              {source}_id      : COALESCE-fill
              series_id/index  : priority-gated (calibre=0 always wins)
              is_omnibus       : promote-additive (0→1 only)

            Locked (sources never touch):
              title, author_id

            v2.3.4 metadata rule (full_scan only):
              For each of {description, pub_date, expected_date,
              cover_url, page_count, isbn} the source returned a
              value for:
                - existing column NULL/empty → write through.
                - existing has a value AND incoming differs →
                  enqueue `metadata_review_queue` row (keyed on
                  (book_id, field, source) so re-runs replace).
                - matches → no-op.

              is_unreleased stays outside the rule: binary flag,
              not a reviewable diff. Always-overwrite-if-not-None.

            Owned-Calibre data is protected by `books_calibre_snapshot`
            — `_merge_result` writes to `books` (Seshat-live) only,
            so populated→queue keeps the user's curated values
            untouched until they accept in the Metadata Manager UI.

            Returns (sql, vals, queue_rows). Caller executes the
            UPDATE and passes queue_rows to `_flush_queue_rows`.
            """
            nonlocal updated_books
            sets = []; vals = []

            try: existing_source = matched_row["source"]
            except (IndexError, KeyError): existing_source = ""
            # `existing_source` is used by the series-priority gate
            # below. Pre-v2.3.4 we also branched on `is_owned_calibre`
            # for the per-field metadata rules — that branch is gone
            # now (uniform write-through-on-empty + queue-on-populated;
            # see the v2.3.4 block further down).

            if bk.source_url:
                merged = _merge_source_urls(matched_row["source_url"], source_name, bk.source_url)
                sets.append("source_url=?"); vals.append(merged)
            sets.append(f"{source_name}_id=COALESCE({source_name}_id,?)"); vals.append(bk.external_id)
            # Series update: fill if empty, or overwrite if current source has higher priority
            if series_id:
                existing_series = matched_row["series_id"]
                cur_priority = SOURCE_PRIORITY.get(source_name, 5)
                existing_priority = SOURCE_PRIORITY.get(existing_source or "", 5)
                if not existing_series or (cur_priority < existing_priority and existing_series != series_id):
                    sets.append("series_id=?"); vals.append(series_id)
                    if bk.series_index: sets.append("series_index=?"); vals.append(bk.series_index)
                    logger.debug(f"    MERGE SERIES: '{bk.title}' (id={matched_row['id']}) → series_id={series_id} #{bk.series_index} (source={source_name}, was={existing_source})")
            # Omnibus flag promotion (additive only): existing rows that
            # were inserted before _RX_OMNIBUS matched their title — or
            # imported from Calibre, which never sets the flag — get
            # caught here on the next merge. Only check the EXISTING
            # title, never the incoming. The upstream
            # `_fuzzy_match_blocked` guard rejects omnibus/non-omnibus
            # mismatches before they reach this code, but be defensive
            # anyway: if a fuzzy match somehow accepted an omnibus
            # incoming onto a non-omnibus existing row (or vice versa),
            # we should NOT then stamp the existing row with the
            # incoming's classification — that flips the wrong row's
            # flag (Mark's "Right of Retribution" → "Right of
            # Retribution: Compilation: The Starting Point" case from
            # the v2.2.0 UAT). Never clears the flag (existing 1 stays
            # 1). Placed after the series block so its
            # series_index=NULL overrides whatever series_index the
            # series block may have set — omnibus entries shouldn't
            # push other books out of position.
            existing_omni = matched_row["is_omnibus"] if "is_omnibus" in matched_row.keys() else 0
            if not existing_omni and _is_omnibus(matched_row["title"]):
                sets.append("is_omnibus=?"); vals.append(1)
                sets.append("series_index=?"); vals.append(None)
                logger.info(
                    f"    OMNIBUS PROMOTE: '{matched_row['title']}' "
                    f"(id={matched_row['id']}) → is_omnibus=1"
                )
            # v2.3.4 source-scan write rule (replaces the prior owned-
            # Calibre per-field rules AND the unowned full-overwrite):
            #
            #   For each metadata field the source returned a value for:
            #     - existing column is NULL/empty → write through.
            #     - existing column has a value AND incoming differs →
            #       enqueue `metadata_review_queue` row for the user
            #       to accept/reject in the Metadata Manager UI.
            #     - existing matches incoming → no-op.
            #
            # Uniform across owned-Calibre + unowned. Curated Calibre
            # metadata is now protected by the dual-storage snapshot
            # table (`books_calibre_snapshot`) — `_merge_result` writes
            # to `books` (Seshat-live) only, so source-scan writes can't
            # stomp Calibre data even on the populated→queue path.
            #
            # is_unreleased stays outside the rule: binary flag, not a
            # reviewable diff. Existing always-overwrite-if-not-None
            # behavior preserved. Same for cover_url-as-COALESCE — we
            # apply the new rule to it too since users may swap covers
            # between sources.
            queue_rows: list[tuple] = []
            if full_scan:
                REVIEW_FIELDS = (
                    ("description", bk.description),
                    ("pub_date", bk.pub_date),
                    ("expected_date", bk.expected_date),
                    ("cover_url", bk.cover_url),
                    ("page_count", bk.page_count),
                    ("isbn", bk.isbn),
                )
                fields_updated: list[str] = []
                for field, new_val in REVIEW_FIELDS:
                    if new_val is None:
                        continue
                    try:
                        existing = matched_row[field]
                    except (IndexError, KeyError):
                        existing = None
                    is_empty = existing is None or (
                        isinstance(existing, str) and not existing.strip()
                    )
                    if is_empty:
                        sets.append(f"{field}=?")
                        vals.append(new_val)
                        fields_updated.append(f"{field}(filled)")
                    elif existing != new_val:
                        queue_rows.append((
                            matched_row["id"], field, existing, new_val, source_name,
                        ))
                        fields_updated.append(f"{field}(queued)")
                    # else: existing matches incoming — no-op
                # is_unreleased: binary flag, not reviewable. Keep
                # existing always-overwrite-if-not-None behavior.
                if bk.is_unreleased is not None:
                    sets.append("is_unreleased=?")
                    vals.append(1 if bk.is_unreleased else 0)
                # v2.11.1: amazon_format_asins JSON cross-reference.
                # Source-private cache, not user-editable, not
                # reviewable. Always overwrite when the source emits
                # it — newer mediaMatrix data wins. Only Amazon sets
                # this today; merge no-ops for every other source.
                if bk.amazon_format_asins is not None:
                    sets.append("amazon_format_asins=?")
                    vals.append(bk.amazon_format_asins)
                if fields_updated or queue_rows:
                    updated_books += 1
                    logger.debug(
                        f"    MERGE UPDATE: '{bk.title}' "
                        f"(id={matched_row['id']}) "
                        f"fields=[{','.join(fields_updated)}]"
                    )
            else:
                logger.debug(f"    MERGE URL: '{bk.title}' (id={matched_row['id']}) ← {source_name}")
            vals.append(matched_row["id"])
            return f"UPDATE books SET {', '.join(sets)} WHERE id=?", vals, queue_rows

        for sr in result.series:
            # ── Lazy series upsert (orphan-row prevention) ───────────
            # The series row is created on first need rather than up
            # front. In library-only mode the inner book loop skips
            # every non-owned book, so eagerly creating the series row
            # would leave it pointing at nothing — exactly how 649
            # orphan rows accumulated on a real library before this
            # was fixed. With lazy creation, if no book in this series
            # actually needs the row, none is written.
            sid = None  # populated lazily by _ensure_series()

            async def _ensure_series(_sr=sr):
                """Upsert the series row on first call; return cached id thereafter.

                Lookup order:
                  1. Author-scoped (current author + pen-name partners)
                     exact LOWER(name) match. Replaces the prior global
                     lookup that caused cross-author collisions
                     (Cressman/Savarovsky "The Last Paladin").
                  2. Normalized-name match against this author's existing
                     series. Collapses canonical-form variants like
                     "Mistborn" vs "The Mistborn Saga" into a single row
                     instead of accumulating duplicates over time.

                Pen-name partners are picked up via `pen_name_links` so
                Darren and Arand still share the "Incubus Inc." row.
                Unrelated authors with the same series name now get
                their own per-author rows.

                First-source-wins on the stored name: if a later scan
                brings a more canonical name, it still links to the
                existing row but does NOT rename it.
                """
                nonlocal sid
                if sid is not None:
                    return sid
                # Author-scoped lookup (current author + pen-name partners)
                related = {author_id}
                pn_rows = await (await db.execute(
                    "SELECT canonical_author_id, alias_author_id "
                    "FROM pen_name_links "
                    "WHERE canonical_author_id = ? OR alias_author_id = ?",
                    (author_id, author_id),
                )).fetchall()
                for r in pn_rows:
                    related.add(r["canonical_author_id"])
                    related.add(r["alias_author_id"])
                placeholders = ",".join("?" * len(related))
                row = await (await db.execute(
                    f"SELECT id FROM series WHERE LOWER(name) = LOWER(?) "
                    f"AND author_id IN ({placeholders})",
                    (_sr.name, *related),
                )).fetchone()
                if row:
                    sid = row["id"]
                else:
                    # Normalized fallback: scan this author's existing
                    # series for a name that normalizes to the same
                    # form. `_norm_consensus_series` strips leading
                    # articles ("The"), trailing tail words ("Saga",
                    # "Series", "Trilogy"…), and punctuation.
                    target_norm = _norm_consensus_series(_sr.name)
                    if target_norm:
                        author_series = await (await db.execute(
                            f"SELECT id, name FROM series WHERE author_id IN ({placeholders})",
                            tuple(related),
                        )).fetchall()
                        for ar in author_series:
                            if _norm_consensus_series(ar["name"]) == target_norm:
                                sid = ar["id"]
                                break
                if sid is not None:
                    await db.execute(
                        "UPDATE series SET last_lookup_at = ? WHERE id = ?",
                        (time.time(), sid),
                    )
                else:
                    cur = await db.execute(
                        "INSERT INTO series (name, author_id, total_books, last_lookup_at) VALUES (?,?,?,?)",
                        (_sr.name, author_id, _sr.total_books, time.time()),
                    )
                    sid = cur.lastrowid
                return sid

            for bk in sr.books:
                if not _lang_ok(bk.language, languages): continue
                if _is_book_set(bk.title): continue
                if _is_series_ref_title(bk.title): continue
                if "English" in languages and _looks_foreign(bk.title): continue
                if exclude_audiobooks and _is_audiobook(bk.title): continue
                norm = _normalize(bk.title)
                matched_row = rows_by_norm.get(norm)
                # ISBN merge: strongest dedup signal — same ISBN = same book
                if matched_row is None and bk.isbn:
                    clean_isbn = bk.isbn.strip().replace("-", "")
                    if clean_isbn in rows_by_isbn:
                        matched_row = rows_by_isbn[clean_isbn]
                        logger.debug(f"    ISBN MERGE: '{bk.title}' → '{matched_row['title']}' (isbn={clean_isbn})")
                # Same-series-position merge — strong signal that's
                # independent of title. Two books sharing `(series_id,
                # series_index)` are the same book; catches
                # "Remnant II" vs "Remnant Book 2" where fuzzy title
                # match fails because the conventions differ too much.
                # Runs BEFORE the fuzzy fallback so the stronger signal
                # wins before we consult title similarity at all.
                if matched_row is None and bk.series_index is not None:
                    existing_sid = (
                        author_series_id_by_name.get(sr.name.lower())
                        or author_series_id_by_name.get(
                            _norm_consensus_series(sr.name)
                        )
                    )
                    if existing_sid is not None:
                        pos_key = (existing_sid, float(bk.series_index))
                        candidate = rows_by_series_pos.get(pos_key)
                        if candidate is not None:
                            matched_row = candidate
                            logger.debug(
                                f"    SAME-SERIES-POSITION MERGE: "
                                f"'{bk.title}' → '{matched_row['title']}' "
                                f"(series_id={existing_sid}, "
                                f"index={bk.series_index})"
                            )
                if matched_row is None:
                    for r in rows:
                        if _fuzzy_match(bk.title, r["title"]):
                            # Multi-signal reject. Subsumes the old
                            # series-index-only check and adds (a)
                            # omnibus/standalone mismatch and (b)
                            # title-extracted index when a side lacks
                            # an explicit series_index. See
                            # `_fuzzy_match_blocked` for the rationale.
                            reason = _fuzzy_match_blocked(bk, r)
                            if reason:
                                logger.debug(
                                    f"    FUZZY MATCH REJECTED ({reason}): "
                                    f"'{bk.title}' vs '{r['title']}'"
                                )
                                continue
                            matched_row = r
                            break
                # ── Omnibus guard: "BookTitle: SeriesName" detection ──
                # If the fuzzy match found a candidate via colon-prefix
                # (the title before ":" matches an existing book) AND the
                # suffix after ":" matches a known series name for this
                # author, this is an omnibus — not a dedup merge. Reject
                # the match so it falls through to the INSERT path with
                # is_omnibus=1. Example: "Otherlife Dreams: The Selfless
                # Hero Trilogy" → prefix "Otherlife Dreams" matches owned
                # book, suffix "The Selfless Hero Trilogy" matches series
                # → omnibus, don't merge into the owned book.
                if matched_row and ':' in bk.title:
                    prefix_norm = _normalize(bk.title.split(':', 1)[0])
                    suffix_norm = _normalize(bk.title.split(':', 1)[1])
                    matched_norm = _normalize(matched_row["title"])
                    if (prefix_norm == matched_norm and
                            suffix_norm in author_series_norms):
                        logger.debug(
                            f"    OMNIBUS GUARD: '{bk.title}' — prefix matches "
                            f"'{matched_row['title']}' but suffix matches series "
                            f"'{author_series_norms[suffix_norm]}' → treating as "
                            f"omnibus, not merge"
                        )
                        matched_row = None  # reject the merge
                if matched_row:
                    # ── Linked-author dedup: matched a linked author's book ──
                    # `linked_author_ids` covers both pen names and
                    # co-authors (the link_type only labels the UI).
                    # Don't UPDATE the linked author's row (that's their
                    # book), just suppress the INSERT so we don't create
                    # a duplicate under this author.
                    if matched_row["author_id"] != author_id:
                        lt = (link_type_by_id or {}).get(matched_row["author_id"], "linked")
                        logger.debug(
                            f"    LINKED-AUTHOR DEDUP ({lt}): '{bk.title}' "
                            f"matches '{matched_row['title']}' under linked "
                            f"author (id={matched_row['author_id']}) — skipping"
                        )
                        continue
                    if _is_hidden(matched_row):
                        # v2.3.4: URL-only write for hidden rows so
                        # future scans can fast-path via URL match. No
                        # series claim, no metadata write — preserves
                        # the v2.2.3 "hidden = ignore for enrichment"
                        # intent while still capturing per-source URL
                        # ownership.
                        sql_u, vals_u = _update_existing_url_only(matched_row, bk)
                        if sql_u:
                            await db.execute(sql_u, vals_u)
                            logger.debug(
                                f"    HIDDEN URL-FILL: '{bk.title}' "
                                f"(id={matched_row['id']}) — {source_name} URL/id only"
                            )
                        else:
                            logger.debug(
                                f"    SKIP HIDDEN: '{bk.title}' "
                                f"(id={matched_row['id']}) — no URL/id to fill"
                            )
                        continue
                    # Lazy series upsert: only create the series row
                    # now that we know a real book is going to link to it.
                    sid_use = await _ensure_series()
                    sql, vals, queue_rows = _update_existing(matched_row, bk, series_id=sid_use)
                    await db.execute(sql, vals)
                    await _flush_queue_rows(queue_rows)
                    # Record this source's series claim for the matched
                    # book so consensus can be computed at the end of
                    # lookup_author. We store the SOURCE's reported
                    # series name (`sr.name`), not the resolved
                    # series_id — different sources use slightly
                    # different canonical names ("The Mistborn Saga"
                    # vs "Mistborn Saga") and the consensus pass
                    # normalizes them when grouping votes.
                    if series_collector is not None:
                        series_collector.setdefault(matched_row["id"], {})[source_name] = (sr.name, bk.series_index)
                    continue
                if owned_only:
                    # Library-only scan: don't add discovered series books
                    # that we don't already own. _ensure_series() is NOT
                    # called on this path — if NO matched_row in this
                    # series fired _ensure_series above, no series row
                    # gets created at all (the orphan-row prevention).
                    continue
                if norm in existing:
                    logger.debug(f"    SKIP (norm dup): '{bk.title}'")
                    continue
                # Cross-author owned-ISBN safety net: if this ISBN is
                # already owned under a different (un-linked) author,
                # don't create a duplicate. Catches the "Halo: Evolutions"
                # case where a source attributes an anthology entry to a
                # contributor while the user owns the canonical row under
                # "Various authors". Linked-author owners are excluded
                # from `cross_isbn_owners` upstream.
                if bk.isbn:
                    clean_isbn = bk.isbn.strip().replace("-", "")
                    if clean_isbn in cross_isbn_owners:
                        logger.info(
                            f"    CROSS-AUTHOR DEDUP: '{bk.title}' "
                            f"(isbn={clean_isbn}) already owned under "
                            f"author_id={cross_isbn_owners[clean_isbn]} — "
                            f"skipping insert"
                        )
                        continue
                # Insert path: also needs the series row to exist.
                sid_use = await _ensure_series()
                initial_urls = json.dumps({source_name: bk.source_url}) if bk.source_url else "{}"
                # Omnibus detection: regex patterns OR context-aware
                # "BookTitle: SeriesName" pattern (the omnibus guard above
                # rejected the merge for this reason).
                omnibus = _is_omnibus(bk.title)
                if not omnibus and ':' in bk.title:
                    suffix_norm = _normalize(bk.title.split(':', 1)[1])
                    prefix_norm = _normalize(bk.title.split(':', 1)[0])
                    if suffix_norm in author_series_norms and prefix_norm in rows_by_norm:
                        omnibus = True
                s_idx = None if omnibus else bk.series_index
                await db.execute(f"INSERT OR IGNORE INTO books (title,author_id,series_id,series_index,isbn,cover_url,pub_date,expected_date,is_unreleased,description,page_count,source,source_url,owned,is_new,is_omnibus,{source_name}_id,amazon_format_asins) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,1,?,?,?)",
                    (bk.title, author_id, sid_use, s_idx, bk.isbn, bk.cover_url, bk.pub_date, bk.expected_date, 1 if bk.is_unreleased else 0, bk.description, bk.page_count, source_name, initial_urls, 1 if omnibus else 0, bk.external_id, bk.amazon_format_asins))
                existing.add(norm); new_books += 1
                if on_new_book:
                    on_new_book()
                logger.debug(f"    NEW: '{bk.title}' → series '{sr.name}'{' [OMNIBUS]' if omnibus else ''} from {source_name}")

        for bk in result.books:
            if not _lang_ok(bk.language, languages): continue
            if _is_book_set(bk.title): continue
            if _is_series_ref_title(bk.title): continue
            if "English" in languages and _looks_foreign(bk.title): continue
            if exclude_audiobooks and _is_audiobook(bk.title): continue
            norm = _normalize(bk.title)
            matched_row = rows_by_norm.get(norm)
            if matched_row is None and bk.isbn:
                clean_isbn = bk.isbn.strip().replace("-", "")
                if clean_isbn in rows_by_isbn:
                    matched_row = rows_by_isbn[clean_isbn]
                    logger.debug(f"    ISBN MERGE: '{bk.title}' → '{matched_row['title']}' (isbn={clean_isbn})")
            if matched_row is None:
                for r in rows:
                    if _fuzzy_match(bk.title, r["title"]):
                        # Same multi-signal reject as the series-
                        # books path above. Critical for the
                        # standalone path because sources like ibdb
                        # routinely return series books as
                        # standalones (no series_index tag) with
                        # the position encoded in the title — the
                        # title-extracted-index arm of the check
                        # is what stops "Super Sales on Super
                        # Heroes 4" from overwriting an existing
                        # "Super Sales on Super Heroes 2" row.
                        reason = _fuzzy_match_blocked(bk, r)
                        if reason:
                            logger.debug(
                                f"    FUZZY MATCH REJECTED ({reason}): "
                                f"'{bk.title}' vs '{r['title']}'"
                            )
                            continue
                        matched_row = r
                        break
            # Series-position fallback for cross-format duplicates.
            # When a source returns a series book in standalone form
            # (no series tagging) but encodes the position in the title
            # — "Paths of Akashic 5: The Expanse" or "The Expanse
            # (Paths of Akashic #5)" — we extract (series_id,
            # series_index) from the title and look it up against
            # existing rows. Mirrors the same-series-position guard
            # the series path uses; this is the standalone-side
            # equivalent.
            if matched_row is None:
                pos = _extract_series_position(bk.title)
                if pos is not None and pos in rows_by_series_pos:
                    matched_row = rows_by_series_pos[pos]
                    logger.info(
                        f"    SERIES-POSITION MATCH: '{bk.title}' → "
                        f"'{matched_row['title']}' via "
                        f"series_id={pos[0]} #{pos[1]}"
                    )
            if matched_row:
                # Linked-author dedup (pen names + co-authors).
                if matched_row["author_id"] != author_id:
                    lt = (link_type_by_id or {}).get(matched_row["author_id"], "linked")
                    logger.debug(
                        f"    LINKED-AUTHOR DEDUP ({lt}): '{bk.title}' "
                        f"matches '{matched_row['title']}' under linked "
                        f"author (id={matched_row['author_id']}) — skipping"
                    )
                    continue
                if _is_hidden(matched_row):
                    # v2.3.4: URL-only write for hidden rows (standalone
                    # path). Same rationale as the series-books branch
                    # above — capture per-source URL ownership without
                    # touching metadata or series claims.
                    sql_u, vals_u = _update_existing_url_only(matched_row, bk)
                    if sql_u:
                        await db.execute(sql_u, vals_u)
                        logger.debug(
                            f"    HIDDEN URL-FILL: '{bk.title}' "
                            f"(id={matched_row['id']}) — {source_name} URL/id only"
                        )
                    else:
                        logger.debug(
                            f"    SKIP HIDDEN: '{bk.title}' "
                            f"(id={matched_row['id']}) — no URL/id to fill"
                        )
                    continue
                sql, vals, queue_rows = _update_existing(matched_row, bk)
                await db.execute(sql, vals)
                await _flush_queue_rows(queue_rows)
                # Record `(None, None)` — this source thinks the book
                # is a standalone. Surfacing "Source A says series X,
                # Source B says standalone" disagreements is just as
                # important as resolving conflicting series names.
                if series_collector is not None:
                    series_collector.setdefault(matched_row["id"], {})[source_name] = (None, None)
                continue
            if owned_only:
                # Library-only scan: skip discovered standalone books we don't own.
                continue
            if norm in existing:
                logger.debug(f"    SKIP (norm dup): '{bk.title}'")
                continue
            # Cross-author owned-ISBN safety net (see series-books path
            # comment above for the full rationale).
            if bk.isbn:
                clean_isbn = bk.isbn.strip().replace("-", "")
                if clean_isbn in cross_isbn_owners:
                    logger.info(
                        f"    CROSS-AUTHOR DEDUP: '{bk.title}' "
                        f"(isbn={clean_isbn}) already owned under "
                        f"author_id={cross_isbn_owners[clean_isbn]} — "
                        f"skipping insert"
                    )
                    continue
            initial_urls = json.dumps({source_name: bk.source_url}) if bk.source_url else "{}"
            omnibus = _is_omnibus(bk.title)
            await db.execute(f"INSERT OR IGNORE INTO books (title,author_id,isbn,cover_url,pub_date,expected_date,is_unreleased,description,page_count,source,source_url,owned,is_new,is_omnibus,{source_name}_id,amazon_format_asins) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,1,?,?,?)",
                (bk.title, author_id, bk.isbn, bk.cover_url, bk.pub_date, bk.expected_date, 1 if bk.is_unreleased else 0, bk.description, bk.page_count, source_name, initial_urls, 1 if omnibus else 0, bk.external_id, bk.amazon_format_asins))
            existing.add(norm); new_books += 1
            if on_new_book:
                on_new_book()
            logger.debug(f"    NEW: '{bk.title}' → standalone{' [OMNIBUS]' if omnibus else ''} from {source_name}")

        # ── Orphan series safety net ─────────────────────────────────
        # Defense in depth: even with the lazy upsert above, some other
        # code path could in theory leave a series row pointing at no
        # books at all. We only consider series rows owned by THIS
        # author for deletion (so we don't touch concurrently-running
        # scans for other authors), but the "is anyone referencing it"
        # check has to look at books from EVERY author — pen-name and
        # co-author links park books from one author against another
        # author's series row, and scoping the subquery to author_id
        # would pretend those references don't exist and DELETE the
        # series, then trip `books.series_id REFERENCES series(id)`
        # and roll back every merge in this scan's transaction.
        # Idempotent — running it twice deletes nothing the second time.
        cleanup_cur = await db.execute(
            "DELETE FROM series WHERE author_id = ? AND id NOT IN "
            "(SELECT DISTINCT series_id FROM books "
            " WHERE series_id IS NOT NULL)",
            (author_id,),
        )
        if cleanup_cur.rowcount > 0:
            logger.debug(
                f"  Orphan series cleanup: dropped {cleanup_cur.rowcount} "
                f"unlinked series rows for author_id={author_id}"
            )
        await db.commit()
        return new_books, updated_books
    finally:
        await db.close()


# ─── Source-consensus series suggestions ─────────────────────────────
# When two or more sources independently agree on a series name (or on
# a series index, or on "standalone") that disagrees with what the user
# has stored, we write a suggestion row instead of overwriting silently.
# The user reviews suggestions in the Series page and can Apply, Ignore,
# or do nothing.
#
# Normalization is what makes the vote work — sources spell series
# names inconsistently ("The Mistborn Saga" vs "Mistborn Saga" vs
# "Mistborn"), and a strict string compare would split every vote into
# singletons. The `_norm_consensus_*` helpers below collapse those
# variants into a single bucket. Standalone (None) is its own bucket
# and never normalizes into a name group.
_RX_CONSENSUS_LEAD = re.compile(r'^(the|a|an)\s+', re.IGNORECASE)
_RX_CONSENSUS_TAIL = re.compile(
    r'\s+(saga|series|trilogy|cycle|chronicles|novels|books)\s*$',
    re.IGNORECASE,
)
_RX_CONSENSUS_PUNCT = re.compile(r'[^\w\s]')
# Parenthetical format/edition tags that sources append to series names
# but don't affect identity. Example: "86--EIGHTY-SIX (Light Novel)"
# and "86--EIGHTY-SIX" are the same series. Stripping these before
# comparison prevents trivial rename suggestions.
_RX_CONSENSUS_PARENS = re.compile(
    r'\s*\(\s*(?:light\s+novel|ln|web\s+novel|wn|manga|comic|graphic\s+novel|'
    r'audio|audiobook|omnibus|hardcover|paperback|ebook|kindle)\s*\)',
    re.IGNORECASE,
)


# Regex for extracting series index from book titles:
# "Super Sales on Super Heroes 4", "#4", "Book 4", trailing "(#4)" etc.
_RX_TITLE_SERIES_IDX = re.compile(
    r'(?:'
    r'#(\d+(?:\.\d+)?)'        # #4, #3.5
    r'|Book\s+(\d+(?:\.\d+)?)' # Book 4
    r'|\b(\d+(?:\.\d+)?)\s*$'  # trailing number
    r')',
    re.IGNORECASE,
)


_RX_BOOK_N_SUFFIX = re.compile(r"\s+(book|bk)\s+\d+(\.\d+)?\s*$", re.IGNORECASE)


# ─── Cross-format series-position dedup ─────────────────────────────
# Goodreads emits "Title (Series #N)", Hardcover/Kobo emit
# "Series N: Title" or "Series Book N: Title". `_normalize` strips
# parens (the Goodreads form) and the "Subtitle" portion after a
# colon (the Hardcover form), giving wildly different tokens for the
# same book — `_normalize("The Expanse (Paths of Akashic #5)") =
# "expanse"` vs `_normalize("Paths of Akashic 5: The Expanse") =
# "paths of akashic 5"`. Even SequenceMatcher gives 0.24 ratio.
#
# These regexes pull the implicit `(series_name, series_index)` tuple
# out of either layout so we can look up the existing book via
# `rows_by_series_pos` instead of relying on title fuzziness.
_RX_SERIES_PREFIX_TITLE = re.compile(
    r"^(.+?)\s+(?:Book\s+|Vol(?:ume)?\.?\s+)?(\d+(?:\.\d+)?)\s*[:\-]\s+(.+)$",
    re.IGNORECASE,
)
_RX_SERIES_PAREN_TITLE = re.compile(
    r"\(([^()]+?)[,\s]+(?:Book\s+|Vol(?:ume)?\.?\s+|#)(\d+(?:\.\d+)?)\)",
    re.IGNORECASE,
)


async def _title_to_series_pass(author_id: int):
    """Post-scan pass: link standalone books to series by title substring.

    For each book that has no series association, check if its title
    contains the name of an existing series by the same author. If so,
    extract the series index from the remaining title text and link it.

    Example: "Super Sales on Super Heroes 4 (Super Sales on Super Heroes #4)"
    → match series "Super Sales on Super Heroes", extract index 4.

    When the extracted index collides with an existing book at the same
    `(series_id, series_index)` slot (the "Remnant Book 2" vs
    "Remnant II" case), the pass dedups in place instead of creating
    a duplicate row:

      - If the existing row is OWNED (from Calibre), DELETE the
        incoming standalone — user's curated row wins.
      - If neither is owned, prefer the title WITHOUT a "Book N"
        suffix — matches canonical convention ("Remnant II" beats
        "Remnant Book 2").
      - Otherwise, keep the existing row (lowest id wins on ties).

    book_series_suggestions.book_id has ON DELETE CASCADE so dropped
    loser rows auto-clean their suggestions.
    """
    db = await get_db()
    # v2.10.2 — collision dedup folds through `merge_books`, which
    # writes to the global pipeline DB (book_grab_links redirect).
    from app.database import get_db as _get_pipeline_db
    from app.discovery.database import get_active_library as _get_slug
    pipeline_db = await _get_pipeline_db()
    library_slug = _get_slug() or ""
    try:
        # Get series the author's books are linked to.
        #
        # Under pen-name linking, `series.author_id` can be a DIFFERENT
        # author (e.g. Darren's "Incubus Inc." books all reference
        # Arand's series row id=715 because `_ensure_series` resolves
        # by name globally, not per-author). Filtering by
        # `series.author_id = author_id` would miss those cross-linked
        # series and skip the standalone → series link + dedup entirely.
        #
        # Query by "series my books reference" instead so every series
        # this author's library actually uses is in scope, regardless
        # of which author's id sits on the series row.
        series_rows = await (await db.execute(
            "SELECT id, name FROM series WHERE id IN ("
            "  SELECT DISTINCT series_id FROM books "
            "  WHERE author_id = ? AND series_id IS NOT NULL"
            ")",
            (author_id,),
        )).fetchall()
        if not series_rows:
            return 0

        # Get all standalone books (no series) for this author. Hidden
        # books are excluded — once a row is hidden it's a garbage-bin
        # entry and shouldn't be re-linked into a series by a later
        # source-driven pass.
        standalone = await (await db.execute(
            "SELECT id, title, owned FROM books WHERE author_id = ? AND series_id IS NULL AND hidden = 0",
            (author_id,),
        )).fetchall()
        if not standalone:
            return 0

        # Sort series by name length descending — try longer names first
        # to avoid "The Fold" matching before "The Fold Series"
        series_list = sorted(series_rows, key=lambda r: len(r["name"]), reverse=True)

        linked = 0
        deduped = 0
        for book in standalone:
            title = book["title"]
            title_lower = title.lower()

            for series in series_list:
                sname = series["name"]
                sname_lower = sname.lower()

                if sname_lower not in title_lower:
                    continue

                # Series name found in title — extract index from remainder
                # Remove the series name portion to get potential index text
                remainder = title_lower.replace(sname_lower, "").strip()
                # Also check the original title for parenthetical patterns
                idx = None
                m = _RX_TITLE_SERIES_IDX.search(remainder) or _RX_TITLE_SERIES_IDX.search(title)
                if m:
                    num_str = m.group(1) or m.group(2) or m.group(3)
                    if num_str:
                        try:
                            idx = float(num_str)
                        except ValueError:
                            pass

                # If we extracted an index, check whether another book
                # already occupies that (series_id, series_index) slot.
                # Ibdb + Hardcover regularly return "Remnant Book 2" as
                # a standalone which we'd link to series "Remnant" at
                # index 2 — but "Remnant II" (OWNED from Calibre) is
                # already at index 2. Without this dedup the UI shows
                # two books for the same series position.
                #
                # v2.10.2: route through `_resolve_position_collision`
                # which folds via `merge_books` so loser identity
                # fields (mam_torrent_id, goodreads_id, isbn, etc.)
                # coalesce into the winner and an audit row is
                # written. Behavior is identical from the loop's POV
                # — same winner-policy, same flow afterwards.
                if idx is not None:
                    incoming_wins, dedup_n = await _resolve_position_collision(
                        db, pipeline_db, library_slug,
                        author_id, series["id"], idx,
                        book["id"], title,
                        incoming_owned=int(book["owned"] or 0),
                    )
                    if dedup_n:
                        deduped += dedup_n
                        if incoming_wins:
                            # The merge already carried over the
                            # loser's series_id+series_index to
                            # the winner (the incoming row), but
                            # be explicit so the standalone is
                            # firmly anchored to the target series
                            # slot regardless of what the loser had.
                            await db.execute(
                                "UPDATE books SET series_id = ?, "
                                "series_index = ? WHERE id = ?",
                                (series["id"], idx, book["id"]),
                            )
                            linked += 1
                        break  # matched — done with this book

                # Link the book to the series
                if idx is not None:
                    await db.execute(
                        "UPDATE books SET series_id = ?, series_index = ? WHERE id = ?",
                        (series["id"], idx, book["id"]),
                    )
                else:
                    await db.execute(
                        "UPDATE books SET series_id = ? WHERE id = ?",
                        (series["id"], book["id"]),
                    )
                linked += 1
                logger.debug(
                    f"    TITLE→SERIES: '{title}' → '{sname}'"
                    f"{f' #{idx}' if idx else ''}"
                )
                break  # matched — don't try other series

        if linked or deduped:
            await db.commit()
            logger.info(
                f"  Title→series pass: linked {linked} standalone "
                f"book(s) to existing series (and dedup'd {deduped} "
                f"slot collision(s)) for author_id={author_id}"
            )
        return linked
    finally:
        await pipeline_db.close()
        await db.close()


# ─── Orphan series promotion ──────────────────────────────────
# Sources sometimes return entire series as a string of standalones
# (no series_index tag). The Warden Locke canary: 9 books titled
# "Player Slayer: Spicy Gamelit Fantasy Episode N" and 4 books with
# the literal "(Manassassin #N)" parenthetical, all cataloged as
# standalones because no source asserted a series. `_title_to_series_pass`
# can't help — it only links to series that already exist.
#
# This pass detects clusters of standalones with shared prefixes plus
# per-book volume markers and bootstraps new series from them. Two
# signal arms:
#
#   1. **Parenthetical** — title contains literal "(<SeriesName> #<N>)".
#      Strongest signal; the source already named the series, the
#      parser just dropped it. Zero FP risk: someone wrote that
#      annotation deliberately.
#
#   2. **Prefix + volume marker** — title is "<Prefix>" or
#      "<Prefix> N" or "<Prefix>: ... Episode N" / "Book N" /
#      "Volume N" / "Vol N" / "Part N" / "Chapter N". Cluster by
#      prefix; promote when ≥ 2 cluster members carry an explicit
#      number. The numeric-marker requirement keeps coincidental
#      prefix matches ("The Final Empire" vs "The Final Hour") out.
#
# Members that fall into both arms are credited to the parenthetical
# arm (stronger signal). Members of a valid cluster that lack an
# explicit number default to index 1 ("Dungeon Depot" alongside
# "Dungeon Depot 2/3").

_RX_PAREN_SERIES_REF = re.compile(
    r'\(([^()#]+?)\s*#(\d+(?:\.\d+)?)\)',
)
_RX_TRAILING_PAREN = re.compile(r'\s*\([^()]*\)\s*')
_RX_VOLUME_MARKER_SUBTITLE = re.compile(
    r'\b(?:episode|book|bk|vol|vol\.|volume|part|chapter|ep|tome|installment)'
    r'\s+#?(\d+(?:\.\d+)?)\b',
    re.IGNORECASE,
)
_RX_PREFIX_TRAILING_NUM = re.compile(
    r'^(.+?)\s+#?(\d+(?:\.\d+)?)\s*$',
)
# Same as above but strips a volume-marker word ("Book", "Vol", "Volume",
# "Part", "Episode", "Chapter") when it's attached to the prefix rather
# than the subtitle. Without this, "The Last Legend Reborn Book 2: ..."
# extracts "The Last Legend Reborn Book" as the series name (Borgy60
# canary). Tried before the bare-trailing-number form.
_RX_PREFIX_VOLUME_MARKER = re.compile(
    r'^(.+?)\s+(?:book|bk|vol\.?|volume|part|episode|ep|chapter|tome|installment)'
    r'\s+#?(\d+(?:\.\d+)?)\s*$',
    re.IGNORECASE,
)

# A bare volume-marker word inside a parenthetical (e.g. "(Book #3)") is a
# positional hint, not a series name. Without this guard, Savarovsky's
# "Guardian's Journey (Book #1)" / "The Last Paladin (Book #4)" all
# clustered into a single fictitious "Book" series.
_VOLUME_MARKER_WORDS = frozenset({
    "book", "bk", "vol", "vol.", "volume", "part",
    "episode", "ep", "chapter", "tome", "installment",
})

_MIN_PREFIX_LEN = 4  # short prefixes ("X 1" / "II 2") are too generic


def _extract_series_signal(title: str) -> tuple[str, float | None] | None:
    """Extract (series_name, index) from a standalone title.

    Returns None when no signal is found. Index may be None when only
    a prefix is present (no explicit volume number) — caller decides
    whether to default to 1 based on whether the cluster has other
    explicit-index members.

    Order matters:
      1. Parenthetical "(SeriesName #N)" → use directly. A bare
         volume-marker name like "(Book #3)" is treated as a
         positional hint instead and threaded into arm 2's bare-prefix
         return so "Guardian's Journey (Book #3)" still yields
         ("Guardian's Journey", 3.0).
      2. Strip parentheticals, split on first colon. Subtitle search
         for volume markers ("Episode 4", "Book 2", "Volume III"…).
      3. Subtitle had no marker → try trailing number on the prefix
         itself ("Manassassin 2" → prefix "Manassassin", idx 2).
      4. No colon → trailing number on the whole cleaned title.
      5. Otherwise → prefix-only signal with idx=None (singleton
         clusters won't promote; multi-member clusters default to 1).
    """
    if not title:
        return None

    # Arm 1: parenthetical
    volume_hint: float | None = None
    m = _RX_PAREN_SERIES_REF.search(title)
    if m:
        sname = m.group(1).strip()
        if sname.lower() in _VOLUME_MARKER_WORDS:
            volume_hint = float(m.group(2))
        elif len(sname) >= _MIN_PREFIX_LEN:
            return (sname, float(m.group(2)))

    # Arm 2: prefix + volume marker. Strip ALL parentheticals first
    # so they don't pollute prefix parsing for borderline cases.
    cleaned = _RX_TRAILING_PAREN.sub(' ', title).strip()
    cleaned = re.sub(r'\s+', ' ', cleaned)

    def _strip_prefix_marker(p: str) -> tuple[str, float | None]:
        """Strip a trailing volume-marker word + number from a prefix.

        Tries volume-marker form first ('… Book 2', '… Vol 3') so we
        keep the marker word out of the series name, then falls back
        to bare trailing number ('Manassassin 2'). Returns
        (base, idx_or_None).
        """
        pm = _RX_PREFIX_VOLUME_MARKER.match(p)
        if pm:
            return (pm.group(1).strip(), float(pm.group(2)))
        pm = _RX_PREFIX_TRAILING_NUM.match(p)
        if pm:
            base = pm.group(1).strip()
            if base.lower() not in _VOLUME_MARKER_WORDS:
                return (base, float(pm.group(2)))
        return (p, None)

    if ':' in cleaned:
        prefix, subtitle = cleaned.split(':', 1)
        prefix = prefix.strip()
        subtitle = subtitle.strip()

        # Subtitle volume marker first — strongest signal. Strip any
        # trailing volume marker / number off the prefix too so a
        # source emitting "Player Slayer 4: ... Episode 4" still
        # extracts "Player Slayer" as the base.
        sm = _RX_VOLUME_MARKER_SUBTITLE.search(subtitle)
        if sm:
            base, _ = _strip_prefix_marker(prefix)
            if len(base) >= _MIN_PREFIX_LEN:
                return (base, float(sm.group(1)))

        # No subtitle marker → try volume-marker / trailing number on
        # the prefix itself. "The Last Legend Reborn Book 2: subtitle"
        # → ("The Last Legend Reborn", 2.0). "Manassassin 2: subtitle"
        # → ("Manassassin", 2.0).
        base, idx = _strip_prefix_marker(prefix)
        if idx is not None and len(base) >= _MIN_PREFIX_LEN:
            return (base, idx)

        # Bare prefix ("Dungeon Depot: Slice of Life ..."). If arm 1
        # found a "(Book #N)" volume hint earlier, use its index.
        if len(prefix) >= _MIN_PREFIX_LEN:
            return (prefix, volume_hint)
        return None

    # No colon — try volume-marker / trailing number on full title
    base, idx = _strip_prefix_marker(cleaned)
    if idx is not None and len(base) >= _MIN_PREFIX_LEN:
        return (base, idx)

    # No structural marker but arm 1 had a volume hint and the cleaned
    # title is long enough to be a series name on its own ("Guardian's
    # Journey (Book #1)" → cleaned "Guardian's Journey" → idx 1).
    if volume_hint is not None and len(cleaned) >= _MIN_PREFIX_LEN:
        return (cleaned, volume_hint)

    return None


async def _resolve_position_collision(
    db, pipeline_db, library_slug,
    author_id: int, sid: int, idx: float, incoming_id: int, incoming_title: str,
    *, incoming_owned: int | None = None,
) -> tuple[bool, int]:
    """Shared dedup helper for series-position collisions.

    Used by both `_title_to_series_pass` and
    `_orphan_series_promotion_pass`. Compares the incoming book
    against any existing book at (sid, idx) and decides a winner
    using (owned, non-Book-N title, lowest id), then routes the
    fold through `merge_books` so identity fields coalesce and an
    audit row is written.

    Prior to v2.10.2 this helper hard-DELETEd the loser. That
    silently discarded identity fields the loser carried
    (mam_torrent_id, goodreads_id, isbn, etc.) when the winner
    didn't already have them — the unowned discovery row in a
    typical pair held the only Goodreads ID for that work. The
    v2.10.0 `merge_books` engine coalesces those fields, so this
    helper now reuses it; behavior is identical from the caller's
    POV, only the loser's data is preserved.

    `incoming_owned` is the owned bit of the incoming row when the
    caller already has it (in `_orphan_series_promotion_pass` the
    caller fetched it from the same row). If omitted, the helper
    treats the incoming as unowned (matches v2.10.1- behavior in
    the orphan-promotion caller).

    Returns (incoming_wins, deduped_count).
      - incoming_wins=True  → caller should link incoming at idx
        (the existing row was folded INTO the incoming).
      - incoming_wins=False → caller should drop further work on
        incoming (the incoming was folded INTO the existing).
      - deduped_count is 1 when a merge happened, else 0.
    """
    from app.discovery.book_merge import MergeError, merge_books

    existing = await (await db.execute(
        "SELECT id, title, owned FROM books "
        "WHERE author_id = ? AND series_id = ? "
        "AND series_index = ? AND id != ?",
        (author_id, sid, idx, incoming_id),
    )).fetchone()
    if existing is None:
        return (True, 0)

    ex_owned = int(existing["owned"] or 0)
    in_owned = int(incoming_owned or 0)
    ex_has_book_n = bool(_RX_BOOK_N_SUFFIX.search(existing["title"] or ""))
    in_has_book_n = bool(_RX_BOOK_N_SUFFIX.search(incoming_title or ""))
    ex_score = (ex_owned, 0 if ex_has_book_n else 1, -existing["id"])
    in_score = (in_owned, 0 if in_has_book_n else 1, -incoming_id)

    if ex_score >= in_score:
        winner_id, loser_id = existing["id"], incoming_id
        log_verb = "dropped"
        result = (False, 1)
    else:
        winner_id, loser_id = incoming_id, existing["id"]
        log_verb = "replaces"
        result = (True, 1)

    try:
        await merge_books(
            db, pipeline_db,
            library_slug=library_slug,
            winner_id=winner_id,
            loser_id=loser_id,
            reason="series_position_collision",
        )
    except MergeError as exc:
        logger.warning(
            "ORPHAN→SERIES DEDUP skipped (id=%d vs id=%d, series_id=%d "
            "#%s): %s",
            incoming_id, existing["id"], sid, idx, exc,
        )
        return (False, 0)
    logger.info(
        f"    ORPHAN→SERIES DEDUP: '{incoming_title}' (id={incoming_id}) "
        f"{log_verb} '{existing['title']}' (id={existing['id']}, "
        f"owned={ex_owned}) at series_id={sid} #{idx}"
    )
    return result


async def _ensure_series_for_author(
    db, name: str, author_id: int, total_books: int | None = None
) -> int:
    """Author-scoped series upsert with pen-name fallback.

    Replaces the global `LOWER(name) = LOWER(?)` lookup that caused
    cross-author collisions (the John Cressman vs Roman Savarovsky
    "The Last Paladin" case). Lookup order:

      1. This author's own series with a matching name.
      2. Pen-name partner's series (canonical ↔ alias via
         `pen_name_links`) — preserves the Darren/Arand "Incubus Inc."
         sharing.
      3. Otherwise, INSERT a new author-scoped row even if an
         unrelated author owns the same name globally.
    """
    related = {author_id}
    rows = await (await db.execute(
        "SELECT canonical_author_id, alias_author_id FROM pen_name_links "
        "WHERE canonical_author_id = ? OR alias_author_id = ?",
        (author_id, author_id),
    )).fetchall()
    for r in rows:
        related.add(r["canonical_author_id"])
        related.add(r["alias_author_id"])

    placeholders = ",".join("?" * len(related))
    row = await (await db.execute(
        f"SELECT id FROM series WHERE LOWER(name) = LOWER(?) "
        f"AND author_id IN ({placeholders})",
        (name, *related),
    )).fetchone()
    if row:
        await db.execute(
            "UPDATE series SET last_lookup_at = ? WHERE id = ?",
            (time.time(), row["id"]),
        )
        return row["id"]

    cur = await db.execute(
        "INSERT INTO series (name, author_id, total_books, last_lookup_at) "
        "VALUES (?, ?, ?, ?)",
        (name, author_id, total_books, time.time()),
    )
    return cur.lastrowid


async def _orphan_series_promotion_pass(author_id: int) -> int:
    """Bootstrap series from clusters of standalones with shared prefixes.

    Runs after `_title_to_series_pass`. Detects two signal types
    (parenthetical "(SeriesName #N)" and prefix+volume-marker), groups
    by canonical series name, and promotes clusters of ≥ 2 members
    where ≥ 2 carry an explicit numeric index.

    Skips:
      - Owned books (user's curated metadata wins).
      - Hidden books (garbage bin — once hidden, stays hidden).
      - Books already flagged as omnibus (those route to the omnibus
        sub-row, not a numbered position).

    The collision-aware `_ensure_series_for_author` keeps two unrelated
    authors who use the same series name from collapsing into one row
    (Cressman/Savarovsky "The Last Paladin" case).
    """
    db = await get_db()
    # v2.10.2 — `_resolve_position_collision` now folds through
    # `merge_books`, which needs the global pipeline DB to redirect
    # `book_grab_links` rows. Open it once per pass; close in finally.
    from app.database import get_db as _get_pipeline_db
    from app.discovery.database import get_active_library as _get_slug
    pipeline_db = await _get_pipeline_db()
    library_slug = _get_slug() or ""
    try:
        rows = await (await db.execute(
            "SELECT id, title FROM books "
            "WHERE author_id = ? AND series_id IS NULL "
            "AND hidden = 0 AND owned = 0 "
            "AND COALESCE(is_omnibus, 0) = 0",
            (author_id,),
        )).fetchall()
        if len(rows) < 2:
            return 0

        # Build clusters keyed by lowercased series-name signal.
        # Also skip rows whose titles match `_is_omnibus` — handles the
        # transient state right after a regex update where the column
        # is still 0 but the title is omnibus-shaped (the startup
        # backfill flips them on next restart, but a scan can run
        # before that).
        clusters: dict[str, list[tuple[int, str, str, float | None]]] = {}
        for r in rows:
            if _is_omnibus(r["title"]):
                continue
            sig = _extract_series_signal(r["title"])
            if sig is None:
                continue
            sname, idx = sig
            clusters.setdefault(sname.lower(), []).append(
                (r["id"], r["title"], sname, idx)
            )

        promoted = 0
        deduped = 0
        for key, members in clusters.items():
            if len(members) < 2:
                continue
            with_idx = [m for m in members if m[3] is not None]
            if len(with_idx) < 2:
                continue

            # Pick the most-frequently-used cased name as canonical.
            from collections import Counter
            canonical = Counter(m[2] for m in members).most_common(1)[0][0]
            sid = await _ensure_series_for_author(db, canonical, author_id)

            for book_id, title, _, idx in members:
                final_idx = idx if idx is not None else 1.0
                incoming_wins, dedup_n = await _resolve_position_collision(
                    db, pipeline_db, library_slug,
                    author_id, sid, final_idx, book_id, title,
                )
                deduped += dedup_n
                if incoming_wins:
                    await db.execute(
                        "UPDATE books SET series_id = ?, series_index = ? "
                        "WHERE id = ?",
                        (sid, final_idx, book_id),
                    )
                    promoted += 1
                    logger.info(
                        f"    ORPHAN→SERIES PROMOTE: '{title}' "
                        f"(id={book_id}) → '{canonical}' #{final_idx}"
                    )

        if promoted or deduped:
            await db.commit()
            logger.info(
                f"  Orphan-series promotion: promoted {promoted} "
                f"standalone(s) into new series (and dedup'd "
                f"{deduped} collision(s)) for author_id={author_id}"
            )
        return promoted
    finally:
        await pipeline_db.close()
        await db.close()


def _norm_consensus_series(name):
    """Normalize a series name for consensus grouping. Returns "" for None.

    Strips leading articles, trailing tail words (saga/series/trilogy…),
    and common parenthetical format tags like "(Light Novel)", "(LN)",
    "(Manga)" — these appear on some sources but not others and don't
    affect series identity.
    """
    if not name:
        return ""
    n = name.strip()
    n = _RX_CONSENSUS_PARENS.sub('', n)
    n = _RX_CONSENSUS_LEAD.sub('', n)
    for _ in range(3):  # iterative tail strip handles "Mistborn Saga Series"
        new_n = _RX_CONSENSUS_TAIL.sub('', n)
        if new_n == n:
            break
        n = new_n
    n = _RX_CONSENSUS_PUNCT.sub(' ', n).lower()
    return re.sub(r'\s+', ' ', n).strip()


def _norm_consensus_index(idx):
    """Normalize a series index for grouping. 3 == 3.0 == "3"; None stays None.

    Sources can return ints, floats, or strings. We coerce to float and
    round to 1 decimal so 3 / 3.0 / "3" group together but 3.0 and 3.5
    stay distinct (sub-numbered novellas like Edgedancer #2.5).
    """
    if idx is None:
        return None
    try:
        return round(float(idx), 1)
    except (ValueError, TypeError):
        return None


async def _compute_series_suggestions(author_id, series_collector):
    """Tally per-source series claims and write pending suggestions.

    Called by lookup_author() after all sources have run. For each
    book in series_collector, groups the per-source (name, index)
    tuples by their normalized form. If the largest group has >=2
    sources AND that group's value differs from the book's CURRENT
    series state in the DB, upsert a row in book_series_suggestions.

    Suggestion lifecycle (see schema doc in database.py):
      - No existing row + new disagreement → INSERT pending
      - status='applied' → leave alone (user already accepted)
      - status='ignored' + same (name, idx) → leave alone (respect ignore)
      - status='ignored' + different (name, idx) → UPDATE to pending
        (a NEW disagreement, the old ignore doesn't apply)
      - status='pending' → UPDATE to latest values + bump updated_at
      - Consensus no longer disagrees → DELETE row entirely (resolved)

    Computes the consensus by majority vote of normalized tuples. Ties
    are broken by preferring source priority (Goodreads > Hardcover >
    Kobo) — i.e., a 1-1 tie between Goodreads and Hardcover picks the
    Goodreads value, since Goodreads is the most-trusted source. (Tie
    handling rarely fires in practice — most disagreements are 1-1-1
    or 2-1, which is unambiguous.)
    """
    if not series_collector:
        return

    db = await get_db()
    try:
        # Pull current DB state for all books in the collector in one
        # query. Joins to series so we get the canonical name string.
        book_ids = list(series_collector.keys())
        placeholders = ",".join("?" * len(book_ids))
        rows = await (await db.execute(
            f"SELECT b.id, b.title, b.series_index AS cur_idx, "
            f"s.name AS cur_series_name "
            f"FROM books b LEFT JOIN series s ON b.series_id = s.id "
            f"WHERE b.id IN ({placeholders})",
            book_ids,
        )).fetchall()
        current_state = {
            r["id"]: (r["cur_series_name"], r["cur_idx"], r["title"])
            for r in rows
        }

        # ── Calibre series trust: count books per series for this author ──
        # If a Calibre series has 3+ books, we trust Calibre over any
        # source claiming those books are standalone. This prevents
        # sources that don't track series well from eroding the user's
        # curated series structure.
        series_counts = {}  # series_name → book count
        series_confirmed = set()  # series names with source confirmation
        count_rows = await (await db.execute(
            "SELECT s.name, COUNT(b.id) as cnt FROM series s "
            "JOIN books b ON b.series_id = s.id "
            "WHERE s.author_id = ? GROUP BY s.id",
            (author_id,),
        )).fetchall()
        for cr in count_rows:
            series_counts[cr["name"]] = cr["cnt"]

        # Also check which series have source-confirmed members: if ANY
        # book in a series has a source agreeing on that series name,
        # no other book in the same series should get a standalone suggestion.
        for book_id, per_source in series_collector.items():
            if book_id not in current_state:
                continue
            cur_name = current_state[book_id][0]
            if not cur_name:
                continue
            for src_name, (raw_name, raw_idx) in per_source.items():
                if raw_name and _norm_consensus_series(raw_name) == _norm_consensus_series(cur_name):
                    series_confirmed.add(cur_name)
                    break

        # Pull existing suggestion rows for these books in one query
        # so we don't issue N round-trips.
        existing_rows = await (await db.execute(
            f"SELECT id, book_id, suggested_series_name, suggested_series_index, "
            f"status FROM book_series_suggestions WHERE book_id IN ({placeholders})",
            book_ids,
        )).fetchall()
        existing_by_book = {r["book_id"]: dict(r) for r in existing_rows}

        # Source priority for tiebreaking the consensus vote. Mirrors
        # SOURCE_PRIORITY in _merge_result; lower number = higher trust.
        SOURCE_RANK = {"mam": 1, "goodreads": 2, "amazon": 3, "hardcover": 4, "kobo": 5, "ibdb": 6, "google_books": 6}

        suggestions_created = 0
        suggestions_updated = 0
        suggestions_resolved = 0

        for book_id, per_source in series_collector.items():
            if book_id not in current_state:
                continue  # book may have been deleted between scan and now
            cur_name, cur_idx, book_title = current_state[book_id]
            cur_norm_name = _norm_consensus_series(cur_name)
            cur_norm_idx = _norm_consensus_index(cur_idx)
            # A series_index without a series_id is meaningless — coerce
            # it to None so a standalone book doesn't spuriously diverge
            # from a "standalone" consensus just because the index column
            # carries a stale numeric value (Calibre and old imports
            # sometimes leave series_index=1.0 on books whose series
            # association has been removed).
            if not cur_norm_name:
                cur_norm_idx = None

            # Group sources by their normalized claim. Key is the
            # normalized tuple; value is a list of (source_name,
            # raw_name, raw_idx) so we can pick a canonical display
            # value when the group wins.
            groups = {}
            for src_name, (raw_name, raw_idx) in per_source.items():
                key = (_norm_consensus_series(raw_name), _norm_consensus_index(raw_idx))
                groups.setdefault(key, []).append((src_name, raw_name, raw_idx))

            if not groups:
                continue

            # None-index tolerance: when a source reports a series name
            # but no index (e.g. Kobo's detail page often omits the
            # number even when the book IS in a series), and exactly
            # ONE other group has the same name with a concrete index,
            # fold the None-index group into that one. The None vote
            # is better read as "I confirm the name, I just don't know
            # the number" than as "I claim no index". Two distinct
            # concrete indices for the same name are NOT collapsed —
            # those represent genuine disagreement about WHICH book in
            # the series this is.
            #
            # Concrete case that motivated this rule: Sanderson's
            # "Tress of the Emerald Sea" — Goodreads said "Hoid's
            # Travails #1", Kobo said "Hoid's Travails" (no index),
            # Hardcover said "Secret Projects". Without folding, every
            # group has 1 source and the 2+ threshold isn't met. With
            # folding, Goodreads + Kobo become a 2-source consensus on
            # "Hoid's Travails #1", which legitimately disagrees with
            # the user's Calibre value of "Secret Projects" → suggestion.
            none_keys = [k for k in groups if k[1] is None and k[0]]
            for nk in none_keys:
                name = nk[0]
                concrete = [k for k in groups if k != nk and k[0] == name and k[1] is not None]
                if len(concrete) == 1:
                    target = concrete[0]
                    groups[target].extend(groups[nk])
                    del groups[nk]
                # else: 0 concrete (just this None group, leave alone)
                # or 2+ concrete (ambiguous which to fold into, leave alone)

            # Pick the largest group, with source-priority tiebreak
            # (a group containing Goodreads beats a same-size group
            # without it). Equal-size groups containing the SAME
            # priority source — extremely rare — fall back to dict
            # iteration order, which is stable in Python 3.7+.
            def _group_score(item):
                key, members = item
                size = len(members)
                # Best (lowest) source rank in this group; missing
                # sources score 99 so they lose tiebreaks naturally.
                best_rank = min(SOURCE_RANK.get(m[0], 99) for m in members)
                # Score: size dominates (×100), tiebreak by inverted rank
                return size * 100 + (10 - best_rank)

            largest_key, largest_members = max(groups.items(), key=_group_score)

            # Per-book consensus diagnostic. Useful when investigating
            # why a specific book did or didn't produce a suggestion.
            # Logs the post-fold groups, the winner, and the current
            # stored value side by side. DEBUG level so it's only on
            # during verbose scans.
            logger.debug(
                f"    CONSENSUS '{book_title}' (book_id={book_id}): "
                f"current=({cur_name!r}, {cur_idx}) "
                f"groups={ {k: [m[0] for m in v] for k, v in groups.items()} } "
                f"winner={largest_key} ({len(largest_members)} src)"
            )

            # Threshold: need at least 2 sources to call it a consensus.
            if len(largest_members) < 2:
                # Check if there's a stale suggestion to clean up:
                # the consensus collapsed (used to have 2+, now only 1).
                if book_id in existing_by_book:
                    ex = existing_by_book[book_id]
                    if ex["status"] == "pending":
                        await db.execute(
                            "DELETE FROM book_series_suggestions WHERE id = ?",
                            (ex["id"],),
                        )
                        suggestions_resolved += 1
                        logger.debug(
                            f"    SUGGESTION RESOLVED (consensus collapsed): "
                            f"'{book_title}' (book_id={book_id})"
                        )
                continue

            consensus_norm_name, consensus_norm_idx = largest_key

            # ── Calibre series trust override ──
            # If the consensus says "standalone" (empty series name) but
            # the book is currently in a Calibre series, check whether
            # we should trust Calibre over the sources:
            #   - 3+ books in the series → always trust Calibre
            #   - Any source confirmed the series on a sibling book → trust Calibre
            # This prevents sources that don't track series well from
            # generating noise suggestions like "86--EIGHTY-SIX #7 → standalone".
            if not consensus_norm_name and cur_name:
                count = series_counts.get(cur_name, 0)
                if count >= 3 or cur_name in series_confirmed:
                    logger.debug(
                        f"    SERIES TRUST OVERRIDE: '{book_title}' "
                        f"(book_id={book_id}) — consensus says standalone "
                        f"but Calibre series '{cur_name}' has {count} books"
                        f"{', source-confirmed' if cur_name in series_confirmed else ''}"
                        f" — suppressing"
                    )
                    continue

            # Does the consensus actually differ from the current DB value?
            if (consensus_norm_name == cur_norm_name and
                    consensus_norm_idx == cur_norm_idx):
                # Consensus matches what's already in the DB — no
                # suggestion needed. Clean up any stale pending row
                # (the disagreement was resolved by a previous Apply
                # or by the user manually editing).
                if book_id in existing_by_book:
                    ex = existing_by_book[book_id]
                    if ex["status"] == "pending":
                        await db.execute(
                            "DELETE FROM book_series_suggestions WHERE id = ?",
                            (ex["id"],),
                        )
                        suggestions_resolved += 1
                        logger.debug(
                            f"    SUGGESTION RESOLVED (matches current): "
                            f"'{book_title}' (book_id={book_id})"
                        )
                continue

            # Pick the canonical display name from the group. Prefer the
            # highest-priority source's raw name string (so we display
            # "The Mistborn Saga" instead of "Mistborn Saga" if Goodreads
            # is in the winning group).
            canonical_member = min(
                largest_members,
                key=lambda m: SOURCE_RANK.get(m[0], 99),
            )
            suggested_name = canonical_member[1]
            suggested_idx = canonical_member[2]
            sources_agreeing = sorted(
                m[0] for m in largest_members
            )
            sources_json = json.dumps(sources_agreeing)
            now = time.time()

            ex = existing_by_book.get(book_id)
            if ex is None:
                # No existing suggestion — INSERT a new pending row
                await db.execute(
                    "INSERT INTO book_series_suggestions "
                    "(book_id, suggested_series_name, suggested_series_index, "
                    "sources_agreeing, current_series_name, current_series_index, "
                    "status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                    (book_id, suggested_name, suggested_idx, sources_json,
                     cur_name, cur_idx, now, now),
                )
                suggestions_created += 1
                logger.info(
                    f"    SUGGESTION NEW: '{book_title}' (book_id={book_id}) "
                    f"current=({cur_name!r}, {cur_idx}) → "
                    f"suggested=({suggested_name!r}, {suggested_idx}) "
                    f"by {sources_agreeing}"
                )
                continue

            # Existing row — branch on status
            if ex["status"] == "applied":
                logger.debug(
                    f"    SUGGESTION SKIP (status=applied): '{book_title}' "
                    f"(book_id={book_id})"
                )
                continue  # user already accepted; never re-suggest

            ex_norm_name = _norm_consensus_series(ex["suggested_series_name"])
            ex_norm_idx = _norm_consensus_index(ex["suggested_series_index"])
            same_as_existing = (
                ex_norm_name == consensus_norm_name and
                ex_norm_idx == consensus_norm_idx
            )

            if ex["status"] == "ignored":
                if same_as_existing:
                    logger.debug(
                        f"    SUGGESTION SKIP (status=ignored, same): "
                        f"'{book_title}' (book_id={book_id})"
                    )
                    continue  # respect the ignore
                # Different consensus → reset to pending. The user's
                # ignore was for the OLD values, not these.
                await db.execute(
                    "UPDATE book_series_suggestions SET "
                    "suggested_series_name=?, suggested_series_index=?, "
                    "sources_agreeing=?, current_series_name=?, "
                    "current_series_index=?, status='pending', updated_at=? "
                    "WHERE id=?",
                    (suggested_name, suggested_idx, sources_json,
                     cur_name, cur_idx, now, ex["id"]),
                )
                suggestions_updated += 1
                logger.info(
                    f"    SUGGESTION REOPENED (was ignored, new consensus): "
                    f"'{book_title}' → ({suggested_name!r}, {suggested_idx})"
                )
                continue

            # status == 'pending' — refresh values + updated_at
            if same_as_existing:
                # Same consensus, just touch updated_at to keep it fresh
                await db.execute(
                    "UPDATE book_series_suggestions SET "
                    "sources_agreeing=?, current_series_name=?, "
                    "current_series_index=?, updated_at=? WHERE id=?",
                    (sources_json, cur_name, cur_idx, now, ex["id"]),
                )
            else:
                # Pending row's consensus shifted to a different value
                await db.execute(
                    "UPDATE book_series_suggestions SET "
                    "suggested_series_name=?, suggested_series_index=?, "
                    "sources_agreeing=?, current_series_name=?, "
                    "current_series_index=?, updated_at=? WHERE id=?",
                    (suggested_name, suggested_idx, sources_json,
                     cur_name, cur_idx, now, ex["id"]),
                )
                suggestions_updated += 1
                logger.info(
                    f"    SUGGESTION UPDATED: '{book_title}' "
                    f"→ ({suggested_name!r}, {suggested_idx})"
                )

        await db.commit()
        # Unconditional summary log: a silent "all zeros" outcome is
        # important to surface, otherwise it's impossible to tell
        # "function ran and found nothing" from "function never ran"
        # without adding instrumentation.
        logger.info(
            f"  Series consensus pass for author_id={author_id}: "
            f"considered {len(series_collector)} books, "
            f"{suggestions_created} new, {suggestions_updated} updated, "
            f"{suggestions_resolved} resolved"
        )
    finally:
        await db.close()


async def _try_source(source, author_name, author_id, our_titles, languages, source_name, existing_titles=None, hidden_titles=None, full_scan=False, owned_only=False, series_collector=None, on_new_book=None, exclude_audiobooks=True, linked_author_ids=None, link_type_by_id=None, start_at=0):
    """Try a single source with validation and detailed logging.

    `start_at` is forwarded to `source.get_author_books()` for the
    Goodreads resume-from-position feature (v1.2). Sources that don't
    accept the kwarg hit the TypeError fallback path below and run
    normally — `start_at` is a Goodreads-only opt-in, not a required
    part of the base interface.
    """
    try:
        if start_at > 0:
            logger.info(f"  [{source_name}] {'Full scan' if full_scan else 'Searching'} for '{author_name}' (resuming from book {start_at})...")
        else:
            logger.info(f"  [{source_name}] {'Full scan' if full_scan else 'Searching'} for '{author_name}'...")
        # Hardcover needs `owned_titles` to search by book title, and
        # `owned_series_names` so its per-book series picker can prefer
        # candidates that match what Calibre already has (this is what
        # stops "Mistborn Saga: Original Trilogy" from beating "The
        # Mistborn Saga" as the picked series). Both attributes are
        # stashed on the source instance by lookup_author before this
        # runs.
        if hasattr(source, '_owned_titles'):
            found = await source.search_author(
                author_name,
                owned_titles=source._owned_titles,
                owned_series_names=getattr(source, '_owned_series_names', None),
            )
        else:
            found = await source.search_author(author_name)
        if not found:
            logger.info(f"  [{source_name}] No author match found")
            return 0
        if not found.external_id:
            logger.info(f"  [{source_name}] Found author but no external ID")
            return 0
        logger.info(f"  [{source_name}] Found: '{found.name}' (id={found.external_id})")

        # Some sources (like Hardcover) return full results from search_author
        has_data = len(found.books) > 0 or len(found.series) > 0
        if has_data:
            full = found
        else:
            # In full_scan mode, hidden books still ride the URL-backfill
            # path so we don't waste a detail-page fetch on them — the
            # merge layer's `_is_hidden` guard drops the resulting UPDATE.
            # Non-hidden books fall through to the slow DETAIL fetch (the
            # whole point of full_scan), since they aren't in the set.
            scan_existing = (hidden_titles or set()) if full_scan else (existing_titles or set())
            # Signature fallback ladder: newest kwargs first (start_at for
            # Goodreads resume), then owned_only, then older shapes. The
            # TypeError catches are for sources that haven't adopted the
            # newer kwargs — they run with whatever subset they accept.
            try:
                full = await source.get_author_books(
                    found.external_id,
                    existing_titles=scan_existing,
                    owned_titles=our_titles or [],
                    owned_only=owned_only,
                    start_at=start_at,
                )
            except TypeError:
                try:
                    full = await source.get_author_books(
                        found.external_id,
                        existing_titles=scan_existing,
                        owned_titles=our_titles or [],
                        owned_only=owned_only,
                    )
                except TypeError:
                    try:
                        full = await source.get_author_books(
                            found.external_id,
                            existing_titles=scan_existing,
                            owned_titles=our_titles or [],
                        )
                    except TypeError:
                        full = await source.get_author_books(found.external_id)
        
        if not full:
            logger.info(f"  [{source_name}] No books returned")
            return 0

        total_src = len(full.books) + sum(len(s.books) for s in full.series)
        if total_src == 0:
            logger.info(f"  [{source_name}] No books found in catalog")
            return 0
        logger.info(f"  [{source_name}] Retrieved {total_src} books ({len(full.series)} series, {len(full.books)} standalone)")

        # Validate: skip if author already confirmed from previous scans
        if existing_titles and len(existing_titles) > 0:
            logger.debug(f"  [{source_name}] Author already confirmed ({len(existing_titles)} known books)")
        elif not await _validate_author(author_name, our_titles, full):
            logger.info(f"  [{source_name}] Author validation failed — skipping (likely wrong author)")
            return 0

        n, u = await _merge_result(author_id, full, source_name, languages, full_scan=full_scan, owned_only=owned_only, series_collector=series_collector, on_new_book=on_new_book, exclude_audiobooks=exclude_audiobooks, linked_author_ids=linked_author_ids, link_type_by_id=link_type_by_id)
        parts = []
        if n > 0: parts.append(f"{n} new")
        if u > 0: parts.append(f"{u} updated")
        if parts:
            logger.info(f"  [{source_name}] ✓ Merged {', '.join(parts)} books for {author_name}")
        else:
            logger.info(f"  [{source_name}] ✓ No changes (all {total_src} already known)")
        return n
    except Exception as e:
        logger.error(f"  [{source_name}] Error for {author_name}: {e}")
        return 0


def _log_source_timeout_summary(timeouts: dict[str, list[str]]) -> None:
    """Emit a single warning per source that hit its wall-clock cap during
    a bulk scan. Each line names the source, count, and author list so the
    user can see at a glance whether a primary source (Goodreads) under-
    scanned a meaningful chunk of the library. No-op when nothing timed
    out. Called at the end of `run_full_lookup` / `run_full_rescan`."""
    if not timeouts:
        return
    for source_name, author_names in timeouts.items():
        if not author_names:
            continue
        preview = ", ".join(author_names[:8])
        more = f" (+{len(author_names) - 8} more)" if len(author_names) > 8 else ""
        logger.warning(
            f"Source '{source_name}' timed out for {len(author_names)} "
            f"author(s) — those authors may be under-scanned: {preview}{more}"
        )


async def lookup_author(author_id: int, author_name: str, full_scan: bool = False, on_progress=None, timeout_collector: dict | None = None):
    """Scan all enabled sources for one author and merge results.

    `on_progress`, if supplied, is called after each source completes
    with the running per-author `new_books` total. Callers wire this
    to update `state._lookup_progress["new_books"]` so the unified
    Dashboard scan widget shows the count climbing in real time
    instead of jumping from 0 to the final value at the end. Up to
    three callbacks fire per author scan (one per enabled source).

    `timeout_collector`, if supplied, accumulates per-source timeout
    occurrences across a bulk scan. On timeout for source X, the
    collector dict gets `X` → list of author names appended. Bulk
    callers (`run_full_lookup`, `run_full_rescan`) surface the
    aggregate in the final summary so the user can see which
    authors may be under-scanned when a primary source (Goodreads)
    blows its 300s cap repeatedly. None (the default) is a no-op
    for single-author scans, where the per-source warning log line
    is enough on its own.
    """
    logger.info(f"{'Full re-scan' if full_scan else 'Looking up'} author: {author_name}")
    # Signal MAM to pause its batch loop while we hold the writer
    # lock for merges. Counter (not boolean) so overlapping scans
    # don't let MAM sneak writes through between them. Wrapped in
    # try/finally so exceptions, cancellation, or early returns
    # never leave MAM stranded.
    state._source_scan_refs += 1
    try:
        return await _lookup_author_inner(
            author_id, author_name, full_scan=full_scan,
            on_progress=on_progress, timeout_collector=timeout_collector,
        )
    finally:
        state._source_scan_refs = max(0, state._source_scan_refs - 1)


async def _lookup_author_inner(author_id: int, author_name: str, full_scan: bool = False, on_progress=None, timeout_collector: dict | None = None):
    total = 0
    settings = load_settings()
    languages = settings.get("languages", ["English"])
    owned_only = bool(settings.get("author_scan_owned_only", False))
    # `exclude_audiobooks` drops title-marked audiobook editions at
    # merge time ("[Audible Audio]", "(Narrator)", etc.). Only apply
    # to ebook scans — on audiobook libraries we want those results.
    exclude_audiobooks = bool(settings.get("exclude_audiobooks", True))
    if state.get_active_library_content_type() == "audiobook":
        exclude_audiobooks = False
    if owned_only:
        logger.info(f"  Library-only mode: only enriching owned books for '{author_name}', no new discoveries")

    db = await get_db()
    try:
        # ── Linked-author expansion (pen names + co-authors) ──────────
        # `pen_name_links` carries both link types (`pen_name` and
        # `co_author`); we load BOTH for dedup so Buckell's scan sees
        # books that exist under his linked co-author Karen Traviss
        # exactly the same way it sees books under his pen names. The
        # dedup pre-filters and merge candidate-set treat all linked
        # IDs identically — only the log labels distinguish the two.
        linked_ids = [author_id]
        link_type_by_id: dict[int, str] = {}
        pen_rows = await (await db.execute(
            "SELECT canonical_author_id, alias_author_id, link_type "
            "FROM pen_name_links "
            "WHERE canonical_author_id = ? OR alias_author_id = ?",
            (author_id, author_id),
        )).fetchall()
        for pr in pen_rows:
            for col in ("canonical_author_id", "alias_author_id"):
                if pr[col] != author_id and pr[col] not in linked_ids:
                    linked_ids.append(pr[col])
                    link_type_by_id[pr[col]] = pr["link_type"]
        if len(linked_ids) > 1:
            n_pen = sum(1 for v in link_type_by_id.values() if v == "pen_name")
            n_co = sum(1 for v in link_type_by_id.values() if v == "co_author")
            logger.info(
                f"  Linked-author expansion: {author_name} linked to "
                f"{n_pen} pen name(s) + {n_co} co-author(s)"
            )
        # IDs of linked authors (excluding self) — passed to _merge_result.
        # Variable name kept as `pen_linked` for legacy compatibility with
        # `_merge_result`'s `linked_author_ids=` param; semantics now
        # cover both pen names and co-authors.
        pen_linked = [i for i in linked_ids if i != author_id]
        # Resolve linked author IDs → display names so sources can
        # accept books bylined under pen-name aliases. Amazon and ibdb
        # set these on the source via `_linked_author_names` before
        # calling get_author_books; a scan of "Randi Darren" then
        # accepts results attributed to "William D. Arand" (the real
        # author) and vice versa.
        pen_name_rows = []
        if pen_linked:
            ph = ",".join("?" * len(pen_linked))
            pen_name_rows = await (await db.execute(
                f"SELECT name FROM authors WHERE id IN ({ph})",
                pen_linked,
            )).fetchall()
        linked_author_names = [
            r["name"] for r in pen_name_rows if r["name"]
        ]

        id_placeholders = ",".join("?" * len(linked_ids))
        rows = await (await db.execute(
            f"SELECT title FROM books WHERE author_id IN ({id_placeholders}) AND owned = 1",
            linked_ids,
        )).fetchall()
        our_titles = [r["title"] for r in rows]
        all_rows = await (await db.execute(
            f"SELECT title, hidden, source_url FROM books WHERE author_id IN ({id_placeholders})",
            linked_ids,
        )).fetchall()
        # `existing_titles` holds EVERY known book (hidden included) so a
        # source returning a known title still hits the URL-backfill path
        # and never inserts a fresh unhidden duplicate. `hidden_titles` is
        # the subset that's been hidden — used only in full_scan mode to
        # keep hidden rows on the URL-backfill path (no detail fetch)
        # instead of forcing a refresh visit.
        #
        # v2.3.4: When the merge layer hits a hidden row, it now does a
        # URL-only UPDATE (source_url merge + {source}_id COALESCE-fill)
        # instead of dropping the write entirely. Metadata, series
        # claims, and series-collector contributions stay suppressed.
        # Net effect: hidden books are still ignored for metadata
        # enrichment and dedup, but their per-source URLs accumulate
        # across scans — letting future scans of huge-catalog authors
        # (John Walker — 1,069 books) fast-path past hidden titles via
        # URL match instead of paying DETAIL on every unmatched one.
        # See `_update_existing_url_only` for the write-rule details.
        existing_titles = set()
        hidden_titles = set()
        # v2.3.2 mandatory-source gating: track per-source which titles
        # have already been URL'd, plus the union ("any source has it").
        # The source loop below picks `existing_titles` per source based
        # on the source's `mandatory` flag — mandatory sources fast-path
        # only on titles THIS source has URL'd, so "Quarks and Qi"
        # missing a Goodreads URL still triggers a Goodreads DETAIL
        # fetch on every scan instead of being silently skipped.
        per_source_titles_with_url: dict[str, set[str]] = {}
        titles_with_any_url: set[str] = set()
        for r in all_rows:
            t = re.sub(r'[^\w\s]', '', r["title"].lower()).strip()
            t = re.sub(r'\s+', ' ', t)
            existing_titles.add(t)
            if r["hidden"] == 1:
                hidden_titles.add(t)
            # Parse source_url JSON to extract per-source URL ownership.
            raw_urls = r["source_url"]
            if raw_urls:
                try:
                    urls = json.loads(raw_urls)
                except (json.JSONDecodeError, TypeError):
                    urls = {}
                if isinstance(urls, dict) and urls:
                    titles_with_any_url.add(t)
                    for src_name in urls:
                        per_source_titles_with_url.setdefault(
                            src_name, set()
                        ).add(t)
        # Distinct series names the user already has tagged for this
        # author. Hardcover (and any future source with the same hook)
        # uses this to prefer matching series candidates over deeper
        # sub-series in its own taxonomy.
        series_rows = await (await db.execute(
            f"SELECT DISTINCT s.name FROM series s "
            f"JOIN books b ON b.series_id = s.id "
            f"WHERE b.author_id IN ({id_placeholders}) AND b.owned = 1 AND s.name IS NOT NULL",
            linked_ids,
        )).fetchall()
        our_series_names = [r["name"] for r in series_rows]
    finally:
        await db.close()

    # Per-source series collector. Threaded through every `_try_source`
    # call so each source's matched-book series claims are recorded.
    # After all sources have run, `_compute_series_suggestions` tallies
    # the per-source agreement and writes pending suggestions for any
    # book where 2+ sources agree on a series different from the
    # current value. The dict is local to this scan and discarded after
    # the consensus pass — we don't need to persist per-source raw data,
    # only the final consensus diffs.
    series_collector: dict[int, dict[str, tuple]] = {}

    # Per-book progress hook for the unified scan widget. Stashed on
    # each source instance before scanning so the source can write the
    # title of the book it's currently fetching into
    # `_lookup_progress["current_book"]`. Sources only call this for
    # work that actually does something (DETAIL fetches + URL-backfill
    # matches), so the user-visible feed never flickers through filter
    # noise like foreign-language / box-set / contributor-only skips.
    def _on_book(title: str) -> None:
        state._lookup_progress["current_book"] = title or ""
    goodreads._on_book = _on_book
    hardcover._on_book = _on_book
    kobo._on_book = _on_book
    amazon._on_book = _on_book
    ibdb._on_book = _on_book
    google_books._on_book = _on_book
    audible._on_book = _on_book

    # Per-book new-candidate counter. Fired by each source from inside
    # its slow DETAIL-fetch loop — same call sites as `_on_book` but
    # only on the paths that produce a *new* candidate (the URL-
    # backfill paths for already-known books deliberately don't fire
    # this).
    #
    # This is what makes the new_books count climb in real time during
    # the rate-limited fetch phase instead of bursting at the source-
    # completion boundary. The count is an estimate during the scan —
    # some candidates get filtered/deduped at merge time — and gets
    # synced to the accurate post-merge total via the on_progress(total)
    # call after each source finishes. That sync may visibly correct
    # the count slightly downward if any candidates got filtered, but
    # the per-second tick in the widget feels right and the final
    # number always lands at the accurate value.
    visible = [0]
    def _on_new_candidate():
        visible[0] += 1
        if on_progress:
            on_progress(visible[0])
    goodreads._on_new_candidate = _on_new_candidate
    hardcover._on_new_candidate = _on_new_candidate
    kobo._on_new_candidate = _on_new_candidate
    amazon._on_new_candidate = _on_new_candidate
    ibdb._on_new_candidate = _on_new_candidate
    google_books._on_new_candidate = _on_new_candidate
    audible._on_new_candidate = _on_new_candidate

    # ── Walk the source registry ──────────────────────────────
    # Iterates SOURCES in declared priority order. Each source is
    # gated behind asyncio.wait_for() so a single hung HTTP call
    # can't stall the whole scan. A timeout is logged but otherwise
    # treated like a successful zero-result scan — partial writes
    # made before the timeout are kept.
    #
    # A global per-author wall-clock budget (PER_AUTHOR_BUDGET_SEC)
    # guards against the worst case where every source bumps up
    # against its own cap and the scan still takes too long. Once
    # the budget is exceeded, remaining sources are skipped.
    scan_started_at = time.monotonic()
    # Specs that timed out in the main source loop below. Fed into the
    # retry pass after the loop for sources that expose `_partial_state`
    # (Goodreads today) so a slow primary source gets a second shot
    # with whatever scan budget is left over.
    per_author_timed_out: list = []
    # Read Hardcover + Google Books API keys once; both sources get
    # them injected via the per-source pre-flight below. Encrypted
    # store first, then fall back to settings (for env-var
    # populated installs and pre-secrets-store deployments).
    try:
        from app.secrets import get_secret as _get_secret
        _hc_key = await _get_secret("hardcover_api_key") or settings.get("hardcover_api_key")
        _gb_key = await _get_secret("google_books_api_key") or settings.get("google_books_api_key")
    except Exception:
        _hc_key = settings.get("hardcover_api_key")
        _gb_key = settings.get("google_books_api_key")

    # Pick ebook vs audiobook source list based on the active library.
    # Content type is stamped on each discovered library at startup.
    active_sources = _sources_for_content_type(
        state.get_active_library_content_type()
    )
    # Enabled-check reads from the Phase-7 unified `metadata_sources`
    # dict. The scan surface (ebook_scan / audiobook_scan) matches the
    # active library's content_type — ebook libraries read
    # `metadata_sources[name].ebook_scan`, audiobook libraries read
    # `metadata_sources[name].audiobook_scan`. `default_enabled` on
    # the SourceSpec is the fallback for fresh installs whose
    # metadata_sources dict hasn't been populated yet.
    ct = state.get_active_library_content_type()
    scan_surface = "audiobook_scan" if ct == "audiobook" else "ebook_scan"
    meta_sources = settings.get("metadata_sources") or {}
    for spec in active_sources:
        entry = meta_sources.get(spec.name)
        if entry is not None:
            if not entry.get(scan_surface, False):
                continue
        elif not spec.default_enabled:
            # No panel entry AND default is off — stay off.
            continue
        # Hardcover requires a configured API key — silently skip otherwise
        # so the user doesn't see a failed scan they never asked for.
        if spec.name == "hardcover" and not _hc_key:
            continue
        # Global budget gate — if we've already burned our wall-clock
        # budget, abandon the rest of the sources for this author.
        elapsed = time.monotonic() - scan_started_at
        if elapsed >= PER_AUTHOR_BUDGET_SEC:
            logger.warning(
                f"  Per-author scan budget ({PER_AUTHOR_BUDGET_SEC}s) exceeded for "
                f"'{author_name}' — skipping remaining sources: "
                f"{[s.name for s in active_sources[active_sources.index(spec):]]}"
            )
            break

        source = spec.getter()
        # Inject pen-name aliases on every source — sources that care
        # (amazon, ibdb) read this attribute off the instance to widen
        # their author-byline gate. Harmless on sources that don't.
        source._linked_author_names = linked_author_names
        # Active library's content_type — Hardcover reads this to pick
        # the right `reading_format_id` filter (audiobook libraries need
        # audiobook editions, not print/ebook). Harmless on sources that
        # don't check it.
        source._content_type = ct
        # Per-source pre-flight injections.
        if spec.name == "hardcover":
            source.update_api_key(_hc_key)
            source._owned_titles = our_titles
            source._owned_series_names = our_series_names
        elif spec.name == "google_books":
            source.update_api_key(_gb_key)

        # Cap the source at its per-source timeout *or* the remaining
        # budget, whichever is smaller. Prevents a slow source near the
        # end of the scan from individually blowing the global budget.
        timeout = min(spec.timeout_sec, max(1.0, PER_AUTHOR_BUDGET_SEC - elapsed))

        # v2.3.2 per-source existing_titles gating. In full_scan mode
        # the existing logic still applies (hidden_titles only — see
        # _try_source); in incremental mode we narrow the fast-path
        # set per source based on its `mandatory` flag.
        # - Mandatory source: fast-path only on books THIS source has
        #   already URL'd. Books missing this source's URL still go
        #   through DETAIL fetch every scan until matched.
        # - Non-mandatory source: fast-path on any book with at least
        #   one URL from any enabled source. Preserves pre-v2.3.2
        #   behavior so supplementary sources (Google Books, IBDB,
        #   Amazon) don't waste DETAIL fetches on books they'll
        #   probably never match.
        from app.metadata.source_config import is_source_mandatory
        if full_scan:
            existing_for_source = existing_titles
        elif is_source_mandatory(settings, spec.name):
            existing_for_source = per_source_titles_with_url.get(
                spec.name, set(),
            )
        else:
            existing_for_source = titles_with_any_url

        try:
            n = await asyncio.wait_for(
                _try_source(
                    source, author_name, author_id, our_titles, languages, spec.name,
                    existing_titles=existing_for_source,
                    hidden_titles=hidden_titles,
                    full_scan=full_scan,
                    owned_only=owned_only, series_collector=series_collector,
                    exclude_audiobooks=exclude_audiobooks, linked_author_ids=pen_linked,
                    link_type_by_id=link_type_by_id,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # The source's HTTP requests get cancelled by wait_for.
            # Anything it merged before the timeout is durable —
            # _try_source commits incrementally. We just don't know
            # the exact `n` for this source; conservatively count 0
            # and let the visible counter re-sync from `total` below.
            logger.warning(
                f"  Source '{spec.name}' timed out after {timeout:.0f}s "
                f"for '{author_name}' — keeping any partial writes, moving on"
            )
            if timeout_collector is not None:
                timeout_collector.setdefault(spec.name, []).append(author_name)
            # Tag this source for the retry pass below. Only sources that
            # expose a `_partial_state` attribute actually benefit from
            # the retry (Goodreads is the only one today), but listing
            # every timed-out spec here keeps the retry logic uniform —
            # specs without partial state are filtered out later.
            per_author_timed_out.append(spec)
            n = 0
        except asyncio.CancelledError:
            # Cancellation propagates — the user clicked Stop.
            raise
        except Exception as e:
            # An unhandled source exception shouldn't kill the whole
            # author scan; log + continue with the next source so other
            # signals still land.
            logger.error(
                f"  Source '{spec.name}' raised for '{author_name}': {e}",
                exc_info=True,
            )
            n = 0

        total += n
        # Sync the visible count to the accurate post-merge total.
        # If candidates over-counted (filters/dedupe at merge time
        # discarded some), this corrects downward. If under-counted
        # (a source path fires no candidates), it catches up.
        visible[0] = total
        if on_progress:
            on_progress(total)

    # ── Retry pass for sources that timed out and preserved state ──
    # v1.2 introduced a single retry; v2.3.1 loops it. Currently only
    # Goodreads participates — it's the slowest primary source and the
    # one where a prolific author's book list can blow the 300s cap.
    # Each retry resumes from where the prior call left off via the
    # source's `_partial_state`. We keep going while:
    #   - the per-author budget has at least 30s left, AND
    #   - the retry made forward progress (index advanced).
    # If two consecutive retries make zero progress, we treat that as
    # a soft outage rather than a slowdown and bail rather than burning
    # the rest of the budget on guaranteed timeouts. Eric Vall's 359-
    # book scan was the canary: pre-v2.3.1 a single retry only got us
    # to 174/359; the loop pushes through to ~340+ when Goodreads is
    # merely slow rather than down.
    MAX_RETRIES = 8  # safety ceiling; loop normally exits via budget
    for spec in per_author_timed_out:
        source = spec.getter()
        partial = getattr(source, '_partial_state', None)
        if not partial:
            continue  # source doesn't support resume — nothing to retry

        retry_num = 0
        last_index = -1
        while True:
            partial = getattr(source, '_partial_state', None)
            if not partial:
                break  # source completed cleanly on the prior retry
            elapsed = time.monotonic() - scan_started_at
            remaining = PER_AUTHOR_BUDGET_SEC - elapsed
            if remaining < 30:
                reached = partial.get("index", 0)
                missed = max(0, partial.get("total", 0) - reached)
                logger.warning(
                    f"  [{spec.name}] giving up on '{author_name}' — "
                    f"processed {reached}/{partial.get('total', '?')} books "
                    f"after {retry_num} retries; ~{missed} likely unscanned "
                    f"(out of per-author budget)"
                )
                break

            retry_num += 1
            if retry_num > MAX_RETRIES:
                logger.warning(
                    f"  [{spec.name}] giving up on '{author_name}' after "
                    f"{MAX_RETRIES} retries"
                )
                break

            retry_timeout = min(spec.timeout_sec, remaining)
            start_at = partial["index"]
            total_books = partial.get("total", 0)
            logger.info(
                f"  [{spec.name}] retry {retry_num} for '{author_name}' — "
                f"resuming from book {start_at}/{total_books} with "
                f"{retry_timeout:.0f}s budget"
            )
            # Mirror the v2.3.2 per-source existing_titles selection
            # used by the main loop so a mandatory source's retry
            # also keeps DETAIL-fetching books missing this source's
            # URL rather than fast-pathing them.
            if full_scan:
                retry_existing = existing_titles
            elif is_source_mandatory(settings, spec.name):
                retry_existing = per_source_titles_with_url.get(
                    spec.name, set(),
                )
            else:
                retry_existing = titles_with_any_url
            try:
                n = await asyncio.wait_for(
                    _try_source(
                        source, author_name, author_id, our_titles, languages, spec.name,
                        existing_titles=retry_existing,
                        hidden_titles=hidden_titles,
                        full_scan=full_scan,
                        owned_only=owned_only, series_collector=series_collector,
                        exclude_audiobooks=exclude_audiobooks, linked_author_ids=pen_linked,
                        link_type_by_id=link_type_by_id,
                        start_at=start_at,
                    ),
                    timeout=retry_timeout,
                )
                total += n
                visible[0] = total
                if on_progress:
                    on_progress(total)
                # Clean completion — _partial_state was cleared by the
                # source. Loop exits on the next iteration's check.
            except asyncio.TimeoutError:
                # Stall detection: if the index didn't advance from the
                # last retry, this is a soft outage, not a slowdown.
                # Keep going only when we're making real progress.
                new_partial = getattr(source, '_partial_state', None) or partial
                new_index = new_partial.get("index", start_at)
                if new_index <= last_index:
                    reached = new_index
                    missed = max(0, new_partial.get("total", 0) - reached)
                    logger.warning(
                        f"  [{spec.name}] no progress on retry {retry_num} "
                        f"for '{author_name}' (index stuck at {reached}/"
                        f"{new_partial.get('total', '?')}); ~{missed} likely "
                        f"unscanned (Goodreads appears unreachable)"
                    )
                    break
                last_index = new_index
                # Loop continues to next retry attempt.
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"  [{spec.name}] retry {retry_num} raised for "
                    f"'{author_name}': {e}",
                    exc_info=True,
                )
                break

    # Clear current_book so the next author's scan widget doesn't show
    # the last book of THIS author until its first DETAIL fetch lands.
    state._lookup_progress["current_book"] = ""

    # Post-scan pass: check standalone books whose titles contain a
    # known series name and link them. Runs after all sources so
    # series created by any source are available for matching.
    await _title_to_series_pass(author_id)

    # Bootstrap series from clusters of standalones that share a
    # prefix and per-book numeric markers. Catches the Warden Locke
    # case where every source returned "Player Slayer: ... Episode N"
    # books as standalones with no series tag — the title→series pass
    # can't help because the series doesn't exist yet.
    await _orphan_series_promotion_pass(author_id)

    # Compute consensus and write pending suggestions for any per-book
    # disagreement that meets the 2+ sources threshold. Runs after all
    # sources so it sees the full per-book picture.
    if series_collector:
        await _compute_series_suggestions(author_id, series_collector)

    # Final author marker write. Retry on `database is locked` because a
    # concurrent MAM scan can hold a writer lock for longer than the
    # default 30s busy_timeout — observed in v1.1.9 testing where a full
    # MAM scan was running while the user kicked off a single-author
    # re-scan. Failing this UPDATE used to bubble all the way out and
    # kill the entire author-scan task; the source-scan results were
    # already committed, so losing the verified=1/last_lookup_at stamp
    # was the only visible damage. We try a few times with backoff and
    # then log + continue (the next scheduled lookup will re-attempt
    # the stamp because last_lookup_at stayed at its prior value).
    for attempt in range(5):
        db2 = await get_db()
        try:
            await db2.execute("UPDATE authors SET verified=1, last_lookup_at=? WHERE id=?", (time.time(), author_id))
            await db2.commit()
            break
        except aiosqlite.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == 4:
                logger.warning(
                    f"Could not stamp last_lookup_at for '{author_name}' "
                    f"(attempt {attempt + 1}/5): {e}. Source-scan writes "
                    f"already committed; next scheduled lookup will retry."
                )
                break
            await asyncio.sleep(2 ** attempt)
        finally:
            await db2.close()

    logger.info(f"{'Full re-scan' if full_scan else 'Lookup'} complete for '{author_name}': {total} new books found across all sources")
    return total


async def run_full_lookup(on_progress=None):
    """Scan every author whose last lookup is older than the cache window.

    Iterates `lookup_author` over the due-author list one at a time so
    DB writes interleave naturally with reads from the UI. Books-only
    authors (rows in `authors` with no entries in `books` — typically
    secondary co-authors of multi-author Calibre rows) are excluded
    here so the scan budget isn't spent on lookups that have nothing
    to merge into.

    `on_progress` receives `{checked, total, current_author, new_books}`
    after each author so the unified scan widget can render progress.
    """
    logger.info("Starting scheduled lookup...")
    reload_sources()
    start = time.time()
    settings = load_settings()
    cache_sec = settings.get("lookup_interval_days", 3) * 86400
    sid = None
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO sync_log (sync_type, started_at) VALUES (?, ?)", ("lookup", start))
        sid = cur.lastrowid; await db.commit()
        # Skip orphan authors (no linked books). These are typically
        # secondary co-authors of multi-author Calibre entries — see
        # routers/authors.py:get_authors() docstring. Scanning them
        # wastes time on a lookup that has no books to merge into.
        rows = await (await db.execute("SELECT id, name FROM authors WHERE COALESCE(last_lookup_at,0) < ? AND id IN (SELECT DISTINCT author_id FROM books) ORDER BY COALESCE(last_lookup_at,0) ASC", (time.time() - cache_sec,))).fetchall()
        authors = list(rows)
        total = 0; checked = 0
        timeouts: dict[str, list[str]] = {}
        for a in authors:
            if on_progress:
                on_progress({"checked": checked, "total": len(authors), "current_author": a["name"], "new_books": total})
            # Per-source closure: forward the running per-author total
            # added to the cumulative-so-far baseline so the widget
            # climbs in real time within each author's scan, not just
            # at author boundaries.
            def _bump(running, _baseline=total):
                if on_progress:
                    on_progress({"checked": checked, "total": len(authors), "current_author": a["name"], "new_books": _baseline + int(running)})
            try: total += await lookup_author(a["id"], a["name"], on_progress=_bump, timeout_collector=timeouts); checked += 1
            except Exception as e: logger.error(f"Error for {a['name']}: {e}")
        if on_progress:
            on_progress({"checked": checked, "total": len(authors), "current_author": "", "new_books": total})
        await db.execute("UPDATE sync_log SET finished_at=?,status='complete',books_found=?,books_new=? WHERE id=?", (time.time(), checked, total, sid))
        await db.commit()
        logger.info(f"Lookup done: {checked} authors, {total} new books")
        _log_source_timeout_summary(timeouts)
        return {"authors_checked": checked, "new_books": total, "source_timeouts": {k: len(v) for k, v in timeouts.items()}}
    except Exception as e:
        if sid:
            try:
                await db.execute("UPDATE sync_log SET finished_at=?,status='error',error=? WHERE id=?", (time.time(), str(e), sid))
                await db.commit()
            except Exception as cleanup_err:
                # Don't mask the original error, but don't lose the cleanup
                # failure either — log it so debugging is possible.
                logger.warning(f"Failed to mark sync_log {sid} as errored: {cleanup_err}")
        raise
    finally:
        await db.close()


async def run_full_rescan(on_progress=None):
    """Full re-scan: visit every book page to refresh metadata.

    Differs from `run_full_lookup` in two ways:
      - Ignores the cache window — every author is rescanned regardless
        of when they were last looked up.
      - Passes `full_scan=True` down to `lookup_author`, which forces
        the per-source DETAIL fetches to actually re-visit pages
        instead of skipping known-already-cached books. This is the
        only way to refresh stale metadata (descriptions, page counts,
        edition dates) from the source side.

    Slow and expensive. Surfaced via the Dashboard "Full Re-Scan"
    button so it's always an explicit user choice, never automatic.
    """
    logger.info("Starting FULL RE-SCAN of all authors...")
    reload_sources()
    start = time.time()
    sid = None
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO sync_log (sync_type, started_at) VALUES (?, ?)", ("full_rescan", start))
        sid = cur.lastrowid; await db.commit()
        # Skip orphan authors — same reasoning as run_full_lookup above.
        rows = await (await db.execute("SELECT id, name FROM authors WHERE id IN (SELECT DISTINCT author_id FROM books) ORDER BY sort_name ASC")).fetchall()
        authors = list(rows)
        total = 0; checked = 0
        timeouts: dict[str, list[str]] = {}
        for a in authors:
            if on_progress:
                on_progress({"checked": checked, "total": len(authors), "current_author": a["name"], "new_books": total})
            # Per-source closure: see run_full_lookup above for the
            # rationale (real-time widget climb within each author).
            def _bump(running, _baseline=total):
                if on_progress:
                    on_progress({"checked": checked, "total": len(authors), "current_author": a["name"], "new_books": _baseline + int(running)})
            try: total += await lookup_author(a["id"], a["name"], full_scan=True, on_progress=_bump, timeout_collector=timeouts); checked += 1
            except Exception as e: logger.error(f"Full re-scan error for {a['name']}: {e}")
        if on_progress:
            on_progress({"checked": checked, "total": len(authors), "current_author": "", "new_books": total})
        await db.execute("UPDATE sync_log SET finished_at=?,status='complete',books_found=?,books_new=? WHERE id=?", (time.time(), checked, total, sid))
        await db.commit()
        logger.info(f"Full re-scan done: {checked} authors, {total} new books")
        _log_source_timeout_summary(timeouts)
        return {"authors_checked": checked, "new_books": total, "source_timeouts": {k: len(v) for k, v in timeouts.items()}}
    except Exception as e:
        if sid:
            try:
                await db.execute("UPDATE sync_log SET finished_at=?,status='error',error=? WHERE id=?", (time.time(), str(e), sid))
                await db.commit()
            except Exception as cleanup_err:
                # Don't mask the original error, but don't lose the cleanup
                # failure either — log it so debugging is possible.
                logger.warning(f"Failed to mark sync_log {sid} as errored: {cleanup_err}")
        raise
    finally:
        await db.close()
