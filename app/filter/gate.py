"""
The filter gate.

`evaluate_announce(announce, config)` is the single decision function
that everything else in Seshat consults to decide whether to grab a
book. It's a pure function over plain dataclasses — no I/O, no
side effects — so it's trivial to unit-test against fixture data and
trivial to call from any pipeline (the IRC listener, the manual-grab
endpoint, the dry-run replay, the test harness).

Decision matrix (faithful port of `previous-stuff/ebook_gate.sh`,
extended with format, language, and category-exclude gates):

    1. Format gate (prefix before " - " in category):
       a. If allowed_formats non-empty and format not in it → SKIP, reason=format_not_allowed
       b. If format in excluded_formats                     → SKIP, reason=format_excluded
    2. If allowed_languages non-empty and language not in it → SKIP, reason=language_not_allowed
    3. If allowed_categories non-empty and cat not in it     → SKIP, reason=category_not_allowed
    4. If category in excluded_categories                    → SKIP, reason=category_excluded
    5. If no author can be detected                          → SKIP, reason=author_not_detected
    6. Walk every parsed author:
       a. If ANY author is on the allow list  → ALLOW, reason=allowed_author
          (allow wins over ignore — if a co-author is allowed, the
          whole release is allowed)
       b. Else if author is on the ignore list → mark as ignored, keep walking
       c. Else                                  → mark as unknown, add to
                                                  weekly-skip set, keep walking
    7. After the walk:
       a. If any author was unknown            → SKIP, reason=author_not_allowlisted
       b. Else if any author was ignored       → SKIP, reason=ignored_author
       c. Else (shouldn't happen — defensive)  → SKIP, reason=author_not_allowlisted_fallback

The original shell script also writes to a CSV skip log and a debug log
file. Those side effects are deliberately NOT in this module — the
caller (the IRC listener / batch processor / test harness) is
responsible for persisting decisions to the `announces` table and
updating the `authors_weekly_skip` table.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.filter.normalize import extract_format, normalize_author, normalize_category

# ─── Data classes ────────────────────────────────────────────


@dataclass(frozen=True)
class Announce:
    """A normalized representation of one MAM announce.

    Built either from a real IRC announce parsed by `mam.announce`, from
    a manual-grab API call, or from a unit-test fixture. The filter
    doesn't care which.

    `author_blob` is the cleanest source of author info when available
    — the MAM IRC announce regex captures the author field directly.
    `title` and `description` are fallbacks for cases where the author
    blob isn't already populated, kept around so the historical
    title-suffix extraction trick (matching `... by Author` patterns)
    can still rescue the rare announce that comes through with a bare
    title and no author field. Manually-injected grabs typically only
    set torrent_id + torrent_name + category and let the post-download
    embedded-metadata layer fill in author later.
    """

    torrent_id: str
    torrent_name: str
    category: str
    author_blob: str = ""
    title: str = ""
    description: str = ""
    # Phase 5 (download-folder template tokens). Distinct from the
    # `title` fallback above: those are filter-internal scraped from
    # the announce text. These are clean book metadata supplied by
    # callers that have it (Discovery's send-to-pipeline). Empty for
    # raw IRC announces — template segments referencing them will be
    # dropped by the renderer rather than producing junk paths.
    series_name: str = ""
    book_title: str = ""
    info_url: str = ""
    size: str = ""
    filetype: str = ""
    language: str = ""
    vip: bool = False


@dataclass(frozen=True)
class FilterConfig:
    """The user-configured rules the filter consults.

    All fields are pre-normalized — the caller is responsible for
    running each entry through `normalize_author` / `normalize_category`
    before constructing this. Frozenset gives us O(1) membership checks
    and stops anyone from mutating the rules mid-evaluation.
    """

    allowed_categories: frozenset[str]
    excluded_categories: frozenset[str] = frozenset()
    allowed_formats: frozenset[str] = frozenset()
    excluded_formats: frozenset[str] = frozenset()
    allowed_languages: frozenset[str] = frozenset()
    allowed_authors: frozenset[str] = frozenset()
    ignored_authors: frozenset[str] = frozenset()


Action = Literal["allow", "skip"]


@dataclass(frozen=True)
class Decision:
    """The output of `evaluate_announce`.

    Designed to give the caller everything they need to:
      - act on the decision (allow → fetch grab; skip → log only)
      - record an audit row in the `announces` table
      - update `authors_weekly_skip` for any unknown authors
      - show a useful explanation in the UI

    `unknown_authors` is the subset of `all_authors` that were not on
    either list — these are the ones the caller adds to the weekly-skip
    table. `primary_log_author` is a stable choice for the single
    "main author" field on the audit row, picking the first unknown if
    any, otherwise the first ignored, otherwise empty.
    """

    action: Action
    reason: str
    matched_author: str = ""
    all_authors: tuple[str, ...] = ()
    unknown_authors: tuple[str, ...] = ()
    primary_log_author: str = ""


# ─── Author extraction (fallback path) ───────────────────────
# These regex helpers are for the case where `announce.author_blob` is
# empty and we need to scrape an author out of free text. The MAM IRC
# regex normally fills `author_blob` directly, so this code path is
# mostly exercised by manually-injected announces and old-style
# Autobrr-style title-only data — kept around as a safety net.

# "New Torrent: Foo By: Author Names Category: ( ... )"
_RX_MAM_ANNOUNCE_BY = re.compile(r"[Bb][Yy]:\s*(.*?)\s+[Cc]ategory:\s*\(", re.DOTALL)

# "Book Title by Author1, Author2 [English / epub]"
# "Book Title by Author1, Author2"
# Also matches "by Author" at the very start of the input — the
# preceding-whitespace requirement from the original shell version
# was incidental, not intentional, and caused legitimate inputs to
# slip through.
_RX_TITLE_BY = re.compile(r"(?:^|\s)[Bb][Yy]\s+(.*)$")
# Trailing format/lang brackets to strip after title-by extraction.
_RX_TRAILING_BRACKETS = re.compile(r"\s*\[[^\]]*\]\s*$")

# "By: Author Names" with no category section
_RX_BARE_BY = re.compile(r"[Bb][Yy]:\s*([^|\[]+)")


def extract_author_blob_from_text(*texts: str) -> str:
    """Try to pull an author blob out of any of the given text fields.

    Mirrors the `extract_author_blob()` function from
    `previous-stuff/ebook_gate.sh`. Walks the inputs in order, trying
    three regex strategies on each, returning the first non-empty match.
    Returns "" if nothing matched.
    """
    for text in texts:
        if not text:
            continue

        m = _RX_MAM_ANNOUNCE_BY.search(text)
        if m:
            blob = m.group(1).strip()
            if blob:
                return blob

        m = _RX_TITLE_BY.search(text)
        if m:
            blob = m.group(1).strip()
            blob = _RX_TRAILING_BRACKETS.sub("", blob).strip()
            if blob:
                return blob

        m = _RX_BARE_BY.search(text)
        if m:
            blob = m.group(1).strip()
            if blob:
                return blob

    return ""


# ─── Author splitting ────────────────────────────────────────
# Faithful port of the shell script's split logic: the blob may
# contain multiple authors joined by " and ", " & ", " / ", "; ",
# or ", ". Comma-separated names ARE intentionally split apart —
# matches existing production behavior for entries like
# "J N Chaney, Jason Anspach".

_SEP_RX = re.compile(
    r"(?:\s+and\s+|\s+&\s+|\s*/\s*|\s*;\s*|\s*,\s*)",
    re.IGNORECASE,
)


def split_authors(blob: str) -> list[str]:
    """Split a multi-author blob into individual author names.

    Returns the raw, un-normalized author names in their original case
    so the caller can preserve display strings. Apply `normalize_author`
    on top when comparing against allow/ignore lists.
    """
    if not blob:
        return []
    parts = _SEP_RX.split(blob)
    return [p.strip() for p in parts if p and p.strip()]


# ─── The gate ────────────────────────────────────────────────


def evaluate_announce(announce: Announce, config: FilterConfig) -> Decision:
    """Decide whether to grab, skip, or queue this announce.

    Pure function. The caller persists the result.
    """
    # Step 1: format gate. The format is the prefix before " - " in the
    # raw MAM category (e.g. "Ebooks" from "Ebooks - Fantasy"). Checked
    # before category so a blanket format exclusion like "comics/graphic
    # novels" doesn't require listing every subcategory individually.
    fmt = extract_format(announce.category)
    if config.allowed_formats and fmt not in config.allowed_formats:
        return Decision(
            action="skip",
            reason="format_not_allowed",
        )
    if fmt in config.excluded_formats:
        return Decision(
            action="skip",
            reason="format_excluded",
        )

    # Step 2: language gate. MAM announces include a Language field
    # (English, Spanish, etc.). Empty allowed_languages = accept all.
    if config.allowed_languages:
        lang_norm = announce.language.strip().lower()
        if lang_norm not in config.allowed_languages:
            return Decision(
                action="skip",
                reason="language_not_allowed",
            )

    # Step 3: category gate (inclusion).
    cat_norm = normalize_category(announce.category)
    if config.allowed_categories and cat_norm not in config.allowed_categories:
        return Decision(
            action="skip",
            reason="category_not_allowed",
        )

    # Step 4: category gate (exclusion). Lets the user include a whole
    # format but carve out specific subcategories they don't want.
    if cat_norm in config.excluded_categories:
        return Decision(
            action="skip",
            reason="category_excluded",
        )

    # Step 5: author detection. Prefer the explicit author_blob field
    # (MAM IRC regex sets this), fall back to scraping torrent_name /
    # title / description for the rare manually-injected case.
    # (Step 6 is the author walk loop below.)
    blob = announce.author_blob or extract_author_blob_from_text(
        announce.torrent_name,
        announce.title,
        announce.description,
    )
    if not blob:
        return Decision(
            action="skip",
            reason="author_not_detected",
        )

    raw_authors = split_authors(blob)
    if not raw_authors:
        return Decision(
            action="skip",
            reason="author_not_detected",
        )

    # Step 6: walk authors. Track the first allow hit (which short-circuits)
    # plus the unknown / ignored buckets so we can pick the right skip
    # reason after the walk.
    matched_allowed: str = ""
    unknowns: list[str] = []
    first_ignored: str = ""

    for raw in raw_authors:
        norm = normalize_author(raw)
        if not norm:
            continue

        if norm in config.allowed_authors:
            matched_allowed = raw
            break

        if norm in config.ignored_authors:
            if not first_ignored:
                first_ignored = raw
            continue

        unknowns.append(raw)

    all_tuple = tuple(raw_authors)

    if matched_allowed:
        return Decision(
            action="allow",
            reason="allowed_author",
            matched_author=matched_allowed,
            all_authors=all_tuple,
        )

    if unknowns:
        return Decision(
            action="skip",
            reason="author_not_allowlisted",
            all_authors=all_tuple,
            unknown_authors=tuple(unknowns),
            primary_log_author=unknowns[0],
        )

    if first_ignored:
        return Decision(
            action="skip",
            reason="ignored_author",
            all_authors=all_tuple,
            primary_log_author=first_ignored,
        )

    # Defensive fallback — shouldn't happen because every author either
    # matched, was unknown, or was ignored. Mirrors the shell script's
    # author_not_allowlisted_fallback branch.
    return Decision(
        action="skip",
        reason="author_not_allowlisted_fallback",
        all_authors=all_tuple,
        primary_log_author=blob,
    )
