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
    r'(?i)\b('
    r'omnibus|complete\s+(?:series|collection|trilogy|saga)'
    r'|books?\s+\d+\s*[-–&]\s*\d+'  # "Books 1-3", "Book 1 & 2"
    r'|(?:the\s+)?complete\s+\w+\s+(?:series|trilogy|saga)'
    r'|compilation|anthology|box\s*set'
    r')\b'
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
    global hardcover, goodreads, kobo, amazon, ibdb, google_books, audible
    from app.metadata.source_config import get_source_rate_limit
    s = load_settings()
    hardcover = HardcoverSource(api_key=s.get("hardcover_api_key", ""))
    goodreads = GoodreadsSource(rate_limit=get_source_rate_limit(s, "goodreads"))
    kobo = KoboSource(rate_limit=get_source_rate_limit(s, "kobo"))
    amazon = AmazonSource(rate_limit=get_source_rate_limit(s, "amazon"))
    ibdb = IbdbSource(rate_limit=get_source_rate_limit(s, "ibdb"))
    google_books = GoogleBooksSource(rate_limit=get_source_rate_limit(s, "google_books"))
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
def _src_audible():      return audible


# Ebook-library source list. Walked for every library whose
# `content_type == "ebook"`. Goodreads primary, Hardcover primary, rest
# filling supplementary roles.
SOURCES: list[SourceSpec] = [
    SourceSpec("goodreads",    "primary",       300.0, _src_goodreads,    True),
    SourceSpec("hardcover",    "primary",       180.0, _src_hardcover,    True),
    SourceSpec("kobo",         "secondary",     120.0, _src_kobo,         True),
    SourceSpec("amazon",       "secondary",     180.0, _src_amazon,       False),
    SourceSpec("ibdb",         "supplementary",  90.0, _src_ibdb,         False),
    SourceSpec("google_books", "supplementary",  60.0, _src_google_books, False),
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
    SourceSpec("audible",   "primary",   300.0, _src_audible,   True),
    SourceSpec("hardcover", "secondary", 180.0, _src_hardcover, True),
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
# At 15 minutes, even worst-case (Goodreads timing out at 300s + a
# couple of slow secondaries) leaves room. If hit, remaining sources
# are skipped and a warning is logged — the partial result is still
# committed because each source's writes are independent.
PER_AUTHOR_BUDGET_SEC = 15 * 60


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


def _lang_ok(book_lang: str, allowed: list[str]) -> bool:
    """Check if a book's language is in the allowed list."""
    if not allowed: return True
    if not book_lang: return True  # Unknown language, assume ok
    bl = book_lang.lower().strip()
    return any(al.lower().strip() in bl or bl in al.lower().strip() for al in allowed)


def _looks_foreign(title: str) -> bool:
    """Detect titles that are likely non-English."""
    if _RX_FOREIGN_ACCENTS.search(title):
        return True
    if _RX_FOREIGN_UNICODE.search(title):
        return True
    # Common foreign words in translated titles
    tl = title.lower()
    foreign_kw = ['hamvai', 'kapuja', 'háborúja', 'bosszúja', 'przebudzenie',
                  'ekspansja', 'lewiatana', 'babilon', 'пробуждение', 'врата']
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


async def _merge_result(author_id: int, result: AuthorResult, source_name: str, languages: list[str], full_scan: bool = False, owned_only: bool = False, series_collector: dict | None = None, on_new_book=None, exclude_audiobooks: bool = True, linked_author_ids: list[int] = None):
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
            f"pub_date, expected_date, description, isbn, author_id "
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

        # Source priority: Goodreads can overwrite series from any other source
        SOURCE_PRIORITY = {"mam": 1, "goodreads": 2, "amazon": 3, "hardcover": 4, "kobo": 5, "ibdb": 6, "google_books": 6, "manual": 7, "import": 7, "calibre": 0}
        
        def _update_existing(matched_row, bk, series_id=None):
            """Build UPDATE for an existing book — URL merge always, series with priority, metadata in full_scan.

            Calibre source-of-truth protection: owned-Calibre books treat
            each metadata field with a tailored rule rather than a blanket
            lock. The user curates Calibre, so we want sources to fill
            gaps and correct edition-specific dates without ever
            clobbering curated content. Current rules:

              cover_url        : LOCKED (Calibre cover_path is authoritative)
              title            : LOCKED (structural — never updated by sources)
              author_id        : LOCKED (structural — never updated by sources)
              description      : SMART (see below)
              pub_date         : OLDEST WINS (see below)
              expected_date    : COALESCE-fill (only if Calibre left it null)
              page_count       : COALESCE-fill (only if Calibre left it null)
              isbn             : COALESCE-fill (only if Calibre left it null)
              is_unreleased    : LOCKED (almost always False for owned books)
              series_id/index  : priority-gated (calibre=0 always wins)
              source_url       : merged into JSON dict (additive, never destructive)
              {source}_id      : COALESCE-fill

            DESCRIPTION (smart stub-detection):
              Calibre imports of older books often have "stub" descriptions —
              one-sentence blurbs from a metadata source that didn't have a
              good summary at the time. The rule is:
                - If existing is null/empty → fill from source.
                - If existing is < 10 words AND new is at least 3x longer
                  in word count → overwrite (the source has a real summary).
                - Otherwise → leave existing alone.
              Threshold is conservative on purpose: a 9-word Calibre stub
              upgrading to a 27-word source description is a clear win;
              we don't want to thrash a 9-word user-curated description
              into a 12-word source description because that's not a real
              improvement.

            PUB_DATE (oldest wins):
              Calibre often has edition-specific dates (a 2015 paperback
              reprint of a 1965 novel). Source scans should be allowed to
              correct that DOWNWARD to the original publication date, but
              never UPWARD (a more-recent edition shouldn't displace the
              original). Rule: if the source's pub_date is strictly older
              (lexicographic compare on ISO YYYY-MM-DD strings, which works
              correctly for dates) than the existing pub_date, overwrite.
              Iterates correctly across multiple sources because each one
              compares against the current value.

            For unowned/missing/discovered books (`source != 'calibre'`),
            none of this applies — the source IS the authority for those
            rows, so the existing full-overwrite behavior continues.
            """
            nonlocal updated_books
            sets = []; vals = []

            try: existing_source = matched_row["source"]
            except (IndexError, KeyError): existing_source = ""
            is_owned_calibre = (existing_source == "calibre")

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
            if full_scan:
                fields_updated = []
                if is_owned_calibre:
                    # ── Owned-Calibre book: per-field rules ──

                    # Description: smart stub-detection
                    if bk.description:
                        existing_desc = (matched_row["description"] or "").strip() if "description" in matched_row.keys() else ""
                        existing_words = len(existing_desc.split()) if existing_desc else 0
                        new_words = len(bk.description.split())
                        # Fill if Calibre is empty, OR if Calibre is a stub
                        # (<10 words) AND new is at least 3x longer.
                        if existing_words == 0:
                            sets.append("description=?"); vals.append(bk.description); fields_updated.append("description(filled)")
                        elif existing_words < 10 and new_words >= existing_words * 3:
                            sets.append("description=?"); vals.append(bk.description); fields_updated.append(f"description(stub→{new_words}w)")

                    # pub_date: oldest wins (lexicographic compare on ISO dates)
                    if bk.pub_date:
                        existing_pub = matched_row["pub_date"] if "pub_date" in matched_row.keys() else None
                        if not existing_pub:
                            sets.append("pub_date=?"); vals.append(bk.pub_date); fields_updated.append("pub_date(filled)")
                        elif bk.pub_date < existing_pub:
                            sets.append("pub_date=?"); vals.append(bk.pub_date); fields_updated.append(f"pub_date({existing_pub}→{bk.pub_date})")

                    # expected_date: COALESCE-fill only
                    if bk.expected_date:
                        existing_exp = matched_row["expected_date"] if "expected_date" in matched_row.keys() else None
                        if not existing_exp:
                            sets.append("expected_date=?"); vals.append(bk.expected_date); fields_updated.append("expected_date(filled)")

                    # page_count + isbn: COALESCE-fill only.
                    if bk.page_count: sets.append("page_count=COALESCE(page_count,?)"); vals.append(bk.page_count); fields_updated.append("page_count")
                    if bk.isbn: sets.append("isbn=COALESCE(isbn,?)"); vals.append(bk.isbn); fields_updated.append("isbn")

                    if fields_updated:
                        updated_books += 1
                        logger.debug(f"    MERGE UPDATE (owned): '{bk.title}' (id={matched_row['id']}) fields=[{','.join(fields_updated)}]")
                    else:
                        logger.debug(f"    MERGE NOOP (owned, all rules satisfied): '{bk.title}' (id={matched_row['id']})")
                else:
                    # Unowned / missing / discovered book: full overwrite
                    # behavior. No user data to protect — the source IS the
                    # authority for these rows.
                    if bk.description: sets.append("description=?"); vals.append(bk.description); fields_updated.append("description")
                    if bk.pub_date: sets.append("pub_date=?"); vals.append(bk.pub_date); fields_updated.append("pub_date")
                    if bk.expected_date: sets.append("expected_date=?"); vals.append(bk.expected_date); fields_updated.append("expected_date")
                    if bk.cover_url: sets.append("cover_url=COALESCE(cover_url,?)"); vals.append(bk.cover_url); fields_updated.append("cover_url")
                    if bk.page_count: sets.append("page_count=COALESCE(page_count,?)"); vals.append(bk.page_count); fields_updated.append("page_count")
                    if bk.isbn: sets.append("isbn=COALESCE(isbn,?)"); vals.append(bk.isbn); fields_updated.append("isbn")
                    if bk.is_unreleased is not None: sets.append("is_unreleased=?"); vals.append(1 if bk.is_unreleased else 0)
                    updated_books += 1
                    logger.debug(f"    MERGE UPDATE: '{bk.title}' (id={matched_row['id']}) fields=[{','.join(fields_updated)}]")
            else:
                logger.debug(f"    MERGE URL: '{bk.title}' (id={matched_row['id']}) ← {source_name}")
            vals.append(matched_row["id"])
            return f"UPDATE books SET {', '.join(sets)} WHERE id=?", vals

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

                Lookup order on the SELECT side matters:
                  1. Exact LOWER(name) match — fast path for the common
                     case where the source's name already matches what's
                     stored.
                  2. Normalized-name match against this author's existing
                     series. This collapses canonical-form variants like
                     "Mistborn" vs "The Mistborn Saga" into a single row
                     instead of accumulating duplicates over time.

                First-source-wins on the stored name: if a later scan
                brings a more canonical name, it still links to the
                existing row but does NOT rename it. The user can
                rename via the Series page if they want a different
                canonical form.
                """
                nonlocal sid
                if sid is not None:
                    return sid
                row = await (await db.execute(
                    "SELECT id FROM series WHERE LOWER(name) = LOWER(?)",
                    (_sr.name,),
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
                            "SELECT id, name FROM series WHERE author_id = ?",
                            (author_id,),
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
                            # Series-index conflict guard: `_fuzzy_match`
                            # accepts "Incubus Inc." against "Incubus
                            # Inc. 3" via substring-containment, but
                            # those are different books (#1 vs #3). If
                            # both sides carry a series_index and they
                            # disagree, skip this candidate and keep
                            # looking — a real match with agreeing
                            # indices might still be in `rows`.
                            if _series_index_conflicts(
                                bk.series_index, r["series_index"],
                            ):
                                logger.debug(
                                    f"    FUZZY MATCH REJECTED: '{bk.title}' "
                                    f"(#{bk.series_index}) vs '{r['title']}' "
                                    f"(#{r['series_index']}) — series indices differ"
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
                    # ── Pen-name dedup: matched a linked author's book ──
                    # Don't UPDATE the linked author's row (that's their
                    # book), just suppress the INSERT so we don't create
                    # a duplicate under this author.
                    if matched_row["author_id"] != author_id:
                        logger.debug(
                            f"    PEN-NAME DEDUP: '{bk.title}' matches "
                            f"'{matched_row['title']}' under linked author "
                            f"(id={matched_row['author_id']}) — skipping"
                        )
                        continue
                    # Lazy series upsert: only create the series row
                    # now that we know a real book is going to link to it.
                    sid_use = await _ensure_series()
                    sql, vals = _update_existing(matched_row, bk, series_id=sid_use)
                    await db.execute(sql, vals)
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
                await db.execute(f"INSERT OR IGNORE INTO books (title,author_id,series_id,series_index,isbn,cover_url,pub_date,expected_date,is_unreleased,description,page_count,source,source_url,owned,is_new,is_omnibus,{source_name}_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,1,?,?)",
                    (bk.title, author_id, sid_use, s_idx, bk.isbn, bk.cover_url, bk.pub_date, bk.expected_date, 1 if bk.is_unreleased else 0, bk.description, bk.page_count, source_name, initial_urls, 1 if omnibus else 0, bk.external_id))
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
                        # Same series-index guard as the series-books
                        # path above. Catches the case where an ibdb/
                        # Hardcover result reports a book as standalone
                        # but with a series_index hint — avoids it
                        # colliding onto a different-numbered existing
                        # book whose title fuzzy-prefixes.
                        if _series_index_conflicts(
                            bk.series_index, r["series_index"],
                        ):
                            logger.debug(
                                f"    FUZZY MATCH REJECTED: '{bk.title}' "
                                f"(#{bk.series_index}) vs '{r['title']}' "
                                f"(#{r['series_index']}) — series indices differ"
                            )
                            continue
                        matched_row = r
                        break
            if matched_row:
                # Pen-name dedup for standalone books
                if matched_row["author_id"] != author_id:
                    logger.debug(
                        f"    PEN-NAME DEDUP: '{bk.title}' matches "
                        f"'{matched_row['title']}' under linked author "
                        f"(id={matched_row['author_id']}) — skipping"
                    )
                    continue
                sql, vals = _update_existing(matched_row, bk)
                await db.execute(sql, vals)
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
            initial_urls = json.dumps({source_name: bk.source_url}) if bk.source_url else "{}"
            await db.execute(f"INSERT OR IGNORE INTO books (title,author_id,isbn,cover_url,pub_date,expected_date,is_unreleased,description,page_count,source,source_url,owned,is_new,{source_name}_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,1,?)",
                (bk.title, author_id, bk.isbn, bk.cover_url, bk.pub_date, bk.expected_date, 1 if bk.is_unreleased else 0, bk.description, bk.page_count, source_name, initial_urls, bk.external_id))
            existing.add(norm); new_books += 1
            if on_new_book:
                on_new_book()
            logger.debug(f"    NEW: '{bk.title}' → standalone from {source_name}")

        # ── Orphan series safety net ─────────────────────────────────
        # Defense in depth: even with the lazy upsert above, some other
        # code path could in theory leave a series row pointing at no
        # books for this author. Scope the cleanup to THIS author's
        # series only (not a global sweep) so we don't accidentally
        # touch concurrently-running scans for other authors. Idempotent
        # — running it twice deletes nothing the second time.
        cleanup_cur = await db.execute(
            "DELETE FROM series WHERE author_id = ? AND id NOT IN "
            "(SELECT DISTINCT series_id FROM books "
            " WHERE series_id IS NOT NULL AND author_id = ?)",
            (author_id, author_id),
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


async def _title_to_series_pass(author_id: int):
    """Post-scan pass: link standalone books to series by title substring.

    For each book that has no series association, check if its title
    contains the name of an existing series by the same author. If so,
    extract the series index from the remaining title text and link it.

    Example: "Super Sales on Super Heroes 4 (Super Sales on Super Heroes #4)"
    → match series "Super Sales on Super Heroes", extract index 4.
    """
    db = await get_db()
    try:
        # Get all series for this author
        series_rows = await (await db.execute(
            "SELECT id, name FROM series WHERE author_id = ?",
            (author_id,),
        )).fetchall()
        if not series_rows:
            return 0

        # Get all standalone books (no series) for this author
        standalone = await (await db.execute(
            "SELECT id, title FROM books WHERE author_id = ? AND series_id IS NULL",
            (author_id,),
        )).fetchall()
        if not standalone:
            return 0

        # Sort series by name length descending — try longer names first
        # to avoid "The Fold" matching before "The Fold Series"
        series_list = sorted(series_rows, key=lambda r: len(r["name"]), reverse=True)

        linked = 0
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

        if linked:
            await db.commit()
            logger.info(
                f"  Title→series pass: linked {linked} standalone "
                f"book(s) to existing series for author_id={author_id}"
            )
        return linked
    finally:
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


async def _try_source(source, author_name, author_id, our_titles, languages, source_name, existing_titles=None, full_scan=False, owned_only=False, series_collector=None, on_new_book=None, exclude_audiobooks=True, linked_author_ids=None, start_at=0):
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
            # In full_scan mode, pass empty existing_titles to force page visits
            scan_existing = set() if full_scan else (existing_titles or set())
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

        n, u = await _merge_result(author_id, full, source_name, languages, full_scan=full_scan, owned_only=owned_only, series_collector=series_collector, on_new_book=on_new_book, exclude_audiobooks=exclude_audiobooks, linked_author_ids=linked_author_ids)
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
    exclude_audiobooks = bool(settings.get("exclude_audiobooks", True))
    if owned_only:
        logger.info(f"  Library-only mode: only enriching owned books for '{author_name}', no new discoveries")

    db = await get_db()
    try:
        # ── Pen-name expansion: include linked authors' books for dedup ──
        # If this author has pen-name links, owned titles and existing
        # titles from the linked author(s) are included so dedup works
        # across identities (e.g., William D. Arand ↔ Randi Darren).
        linked_ids = [author_id]
        pen_rows = await (await db.execute(
            "SELECT canonical_author_id, alias_author_id FROM pen_name_links "
            "WHERE canonical_author_id = ? OR alias_author_id = ?",
            (author_id, author_id),
        )).fetchall()
        for pr in pen_rows:
            for col in ("canonical_author_id", "alias_author_id"):
                if pr[col] != author_id and pr[col] not in linked_ids:
                    linked_ids.append(pr[col])
        if len(linked_ids) > 1:
            logger.info(f"  Pen-name expansion: {author_name} linked to {len(linked_ids)-1} other author(s)")
        # IDs of linked authors (excluding self) — passed to _merge_result
        pen_linked = [i for i in linked_ids if i != author_id]

        id_placeholders = ",".join("?" * len(linked_ids))
        rows = await (await db.execute(
            f"SELECT title FROM books WHERE author_id IN ({id_placeholders}) AND owned = 1",
            linked_ids,
        )).fetchall()
        our_titles = [r["title"] for r in rows]
        all_rows = await (await db.execute(
            f"SELECT title FROM books WHERE author_id IN ({id_placeholders})",
            linked_ids,
        )).fetchall()
        existing_titles = set()
        for r in all_rows:
            t = re.sub(r'[^\w\s]', '', r["title"].lower()).strip()
            t = re.sub(r'\s+', ' ', t)
            existing_titles.add(t)
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
    # Read Hardcover API key once; the source needs it injected before its
    # _try_source runs. Same encrypted-store-then-legacy fallback as before.
    try:
        from app.secrets import get_secret as _get_secret
        _hc_key = await _get_secret("hardcover_api_key") or settings.get("hardcover_api_key")
    except Exception:
        _hc_key = settings.get("hardcover_api_key")

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
        # Per-source pre-flight (Hardcover is the only one that needs it).
        if spec.name == "hardcover":
            source.update_api_key(_hc_key)
            source._owned_titles = our_titles
            source._owned_series_names = our_series_names

        # Cap the source at its per-source timeout *or* the remaining
        # budget, whichever is smaller. Prevents a slow source near the
        # end of the scan from individually blowing the global budget.
        timeout = min(spec.timeout_sec, max(1.0, PER_AUTHOR_BUDGET_SEC - elapsed))

        try:
            n = await asyncio.wait_for(
                _try_source(
                    source, author_name, author_id, our_titles, languages, spec.name,
                    existing_titles=existing_titles, full_scan=full_scan,
                    owned_only=owned_only, series_collector=series_collector,
                    exclude_audiobooks=exclude_audiobooks, linked_author_ids=pen_linked,
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
    # v1.2: currently only Goodreads participates — it's the slowest
    # primary source and the one where a prolific author's book list
    # can blow the 300s cap. The retry gets whatever remains of the
    # per-author budget (capped at the source's own timeout) and picks
    # up from where the first call left off. On a second timeout we
    # log the "likely missed" count and move on; the Phase 1 timeout
    # collector already surfaced the author to the Dashboard so this
    # just adds detail for the log reader.
    for spec in per_author_timed_out:
        source = spec.getter()
        partial = getattr(source, '_partial_state', None)
        if not partial:
            continue  # source doesn't support resume — nothing to retry
        elapsed = time.monotonic() - scan_started_at
        remaining = PER_AUTHOR_BUDGET_SEC - elapsed
        if remaining < 30:
            logger.info(
                f"  [{spec.name}] retry skipped for '{author_name}' — "
                f"only {remaining:.0f}s left in per-author budget"
            )
            break  # remaining sources would hit the same budget wall
        retry_timeout = min(spec.timeout_sec, remaining)
        start_at = partial["index"]
        total_books = partial.get("total", 0)
        logger.info(
            f"  [{spec.name}] retry for '{author_name}' — resuming from "
            f"book {start_at}/{total_books} with {retry_timeout:.0f}s budget"
        )
        try:
            n = await asyncio.wait_for(
                _try_source(
                    source, author_name, author_id, our_titles, languages, spec.name,
                    existing_titles=existing_titles, full_scan=full_scan,
                    owned_only=owned_only, series_collector=series_collector,
                    exclude_audiobooks=exclude_audiobooks, linked_author_ids=pen_linked,
                    start_at=start_at,
                ),
                timeout=retry_timeout,
            )
            total += n
            visible[0] = total
            if on_progress:
                on_progress(total)
        except asyncio.TimeoutError:
            # Second timeout: surface how far we got so the log reader
            # has a concrete "N of M books processed" number instead of
            # just "Goodreads timed out twice". The partial state has
            # been updated in place during the retry, so it reflects
            # the furthest point reached across both calls.
            retry_partial = getattr(source, '_partial_state', None) or partial
            reached = retry_partial.get("index", start_at)
            missed = max(0, retry_partial.get("total", 0) - reached)
            logger.warning(
                f"  [{spec.name}] retry ALSO timed out for '{author_name}' — "
                f"processed {reached}/{retry_partial.get('total', '?')} "
                f"books total; ~{missed} likely unscanned"
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                f"  [{spec.name}] retry raised for '{author_name}': {e}",
                exc_info=True,
            )

    # Clear current_book so the next author's scan widget doesn't show
    # the last book of THIS author until its first DETAIL fetch lands.
    state._lookup_progress["current_book"] = ""

    # Post-scan pass: check standalone books whose titles contain a
    # known series name and link them. Runs after all sources so
    # series created by any source are available for matching.
    await _title_to_series_pass(author_id)

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
