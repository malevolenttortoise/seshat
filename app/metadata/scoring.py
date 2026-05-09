"""
Similarity scoring for metadata matches.

When a source returns a book, we need to decide: is THIS what we were
searching for, or a different book that happened to share some words?
The enricher calls `score_match()` with the search criteria and the
returned record; the score is used to accept, reject, or fall through
to the next source.

Three title-matching signals combined:

  - **Jaccard overlap** — token intersection / union over normalized
    title words. Good at catching same-words-different-order.
  - **Containment** — fraction of search tokens found in the result.
    Scores high when the result is a superset of the search (e.g.
    "The Triangulum Fold" inside "The Triangulum Fold: The Fold
    Series Book 8"). Pure Jaccard punishes this case; containment
    rewards it.
  - **Substring bonus** — if the search title appears verbatim in the
    result (case-insensitive), apply a floor of 0.85. This catches
    cases like exact title + series decoration.

Author overlap: normalize both sides, check set intersection.

Final confidence is a weighted blend of the best title signal (max of
Jaccard and containment) with author overlap.
"""
from __future__ import annotations

import re

from app.filter.gate import split_authors
from app.filter.normalize import normalize_author

_WORD_RX = re.compile(r"[a-z0-9']+")

# Noise patterns stripped before scoring — series decorations, edition
# info, and "Book N" suffixes that Amazon/Kobo append to titles.
_NOISE_PATTERNS = [
    re.compile(r"\b(?:book|volume|vol|tome|part|episode)\s*#?\d+(?:\.\d+)?\b", re.I),
    re.compile(r"\b(?:kindle|paperback|hardcover|large print|unabridged)\s*edition\b", re.I),
    re.compile(r"\b(?:a novel|a memoir|a thriller)\b", re.I),
]

_STOPWORDS = frozenset({"the", "a", "an", "of", "and", "in", "on", "for", "to", "by"})

# Pulls a volume index out of titles like "Foo: Book 5" / "Foo Vol. 3" /
# "Foo, Volume 12". Used by the empty-residue path in score_match: when
# the series-strip leaves nothing meaningful, we fall back to comparing
# volumes directly so we don't promote Book 2 as a match for Book 5.
_VOLUME_RX = re.compile(
    r"\b(?:book|volume|vol|tome|part|episode)\b\.?\s*#?(\d+)(?:\.\d+)?\b",
    re.I,
)

# Volume RANGES like "Books 1-4", "Vol. 1-12", "Volumes 3-7". Bundle
# torrents commonly use these patterns. Used by the volume-range
# mismatch short-circuit to definitively reject candidates whose range
# excludes the searched book's volume.
_VOLUME_RANGE_KEYWORDED_RX = re.compile(
    r"\b(?:books?|volumes?|vols?|tomes?|parts?|episodes?)\b\.?\s*"
    r"#?(\d+)(?:\.\d+)?\s*[-–—]\s*#?(\d+)(?:\.\d+)?\b",
    re.I,
)
# Bare trailing range like "Demon Accords 1-4" / "Foo (1-3)" — only
# matches at title end to avoid false positives on dates / hyphenated
# words mid-title.
_VOLUME_RANGE_TRAILING_RX = re.compile(
    r"(?:\s|\()(\d+)\s*[-–—]\s*(\d+)\s*\)?\s*$",
)


def _extract_volume(title: str) -> int | None:
    """Return the integer volume index from a title, or None if absent.

    Examples: "Foo: Book 5" → 5, "Foo, Vol. 12" → 12, "Foo" → None.
    Title-range markers like "1-4" deliberately don't match (no keyword
    prefix) — those signal bundles, handled by `_extract_volume_range`.
    """
    if not title:
        return None
    m = _VOLUME_RX.search(title)
    return int(m.group(1)) if m else None


def _extract_volume_range(title: str) -> tuple[int, int] | None:
    """Return (start, end) volume range from a bundle title, or None.

    Catches range markers that signal multi-book torrents:
      - "Books 1-4" / "Vol. 1-12" / "Volumes 3-7" — keyworded
      - "Demon Accords 1-4" / "Foo (1-3)" — bare trailing range

    Used by the volume-range-mismatch short-circuit in
    `score_match_with_breakdown`: when a candidate has a range AND the
    search has a single-volume marker OUTSIDE that range, the result
    is definitively wrong (no need for filelist/description verification).

    Bounds reject false positives:
      - keyworded form: end <= 999, span <= 99 (lenient — keyword is
        strong evidence)
      - bare trailing form: end <= 50, span <= 30 (conservative — no
        keyword to anchor; rejects year ranges like "1990-2000")
    """
    if not title:
        return None
    m = _VOLUME_RANGE_KEYWORDED_RX.search(title)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        if 0 < start < end <= 999 and end - start <= 99:
            return (start, end)
    m = _VOLUME_RANGE_TRAILING_RX.search(title)
    if m:
        start, end = int(m.group(1)), int(m.group(2))
        if 0 < start < end <= 50 and end - start <= 30:
            return (start, end)
    return None


def _title_tokens(title: str) -> set[str]:
    """Normalize a title into a set of comparison tokens."""
    lowered = title.lower() if title else ""
    tokens = set(_WORD_RX.findall(lowered))
    tokens -= _STOPWORDS
    return tokens


def _clean_title(title: str) -> str:
    """Strip series/edition noise from a title before scoring."""
    cleaned = title
    for pat in _NOISE_PATTERNS:
        cleaned = pat.sub("", cleaned)
    # Strip trailing colons, dashes, and whitespace left by removal.
    cleaned = re.sub(r"[\s:—–-]+$", "", cleaned).strip()
    return cleaned


def title_similarity(a: str, b: str) -> float:
    """Combined title similarity using Jaccard + containment + substring.

    Returns the best score from multiple signals:
      - Jaccard: good for same-words-different-order
      - Containment: good when result is superset of search
      - Substring: catches verbatim title inside longer strings

    Both titles are cleaned (series/edition noise stripped) before
    tokenizing so "The Triangulum Fold" vs "The Triangulum Fold:
    The Fold Series Book 8" compares as cleanly as possible.
    """
    if not a or not b:
        return 0.0

    # First try with cleaned titles (noise stripped).
    ca = _clean_title(a)
    cb = _clean_title(b)
    ta = _title_tokens(ca)
    tb = _title_tokens(cb)

    if not ta or not tb:
        return 0.0

    inter = len(ta & tb)
    union = len(ta | tb)

    jaccard = inter / union if union else 0.0

    # Containment: what fraction of the SEARCH tokens are in the result?
    # This rewards cases where the search is fully contained in the result.
    # Only use containment when the search has 2+ meaningful tokens —
    # single-word titles trivially get 100% containment against anything.
    containment = 0.0
    if len(ta) >= 2 and len(tb) >= 2:
        containment_a = inter / len(ta) if ta else 0.0  # how much of A is in B
        containment_b = inter / len(tb) if tb else 0.0  # how much of B is in A
        containment = max(containment_a, containment_b)

    # Substring bonus: if one title appears verbatim in the other
    # (case-insensitive), boost the score — but scale by how much of
    # the longer string the shorter one covers. "Foundation" inside
    # "Foundation and Empire" covers ~45% → lower bonus. "The
    # Triangulum Fold" inside "The Triangulum Fold: Book 8" covers
    # ~60% → higher bonus.
    a_low = a.lower().strip()
    b_low = b.lower().strip()
    substring = 0.0
    if a_low and b_low:
        shorter, longer = (a_low, b_low) if len(a_low) <= len(b_low) else (b_low, a_low)
        if shorter in longer:
            coverage = len(shorter) / len(longer) if longer else 0
            # Scale: 50% coverage → 0.70, 75% → 0.85, 100% → 0.95
            substring = 0.50 + coverage * 0.45

    # Take the best signal. Weight containment slightly below substring
    # since full containment with noise is still very strong.
    best = max(jaccard, containment * 0.95, substring)
    return min(best, 1.0)


def author_overlap(
    candidates: list[str] | str, targets: list[str] | str
) -> float:
    """Fraction of target authors matched by candidates.

    Both sides accept either a pre-split list or a raw blob string.
    Normalization goes through `normalize_author` so typographic
    variants and "Lastname, Firstname" forms compare equal.

    Returns the fraction of TARGET authors that appear in the
    candidate set — 1.0 when every target matches, 0.5 when half
    match, 0.0 when none do. Anchoring on the target side means
    an announce with one author still scores high even if the
    scraped record lists three.
    """
    cand = _normalize_set(candidates)
    tgt = _normalize_set(targets)
    if not tgt:
        return 0.0
    hits = sum(1 for t in tgt if t in cand)
    return hits / len(tgt)


def _normalize_set(value: list[str] | str) -> set[str]:
    if isinstance(value, str):
        raw = split_authors(value)
    else:
        raw = value
    out: set[str] = set()
    for entry in raw:
        norm = normalize_author(entry)
        if norm:
            out.add(norm)
    return out


def score_match_with_breakdown(
    *,
    record_title: str,
    record_authors: list[str],
    search_title: str,
    search_authors: list[str] | str,
    known_series: str = "",
) -> dict:
    """Compute confidence + return all the components that fed into it.

    Same logic as `score_match` but returns a dict with every signal
    so the MAM debug-match endpoint can show *why* a result scored the
    way it did. Keep `score_match` as a thin wrapper around this so
    there's a single source of truth.
    """
    # Volume-range mismatch short-circuit: when the candidate is a
    # multi-volume bundle (range in title) AND the searched book has
    # an explicit single-volume marker OUTSIDE that range, this is
    # definitively the wrong torrent regardless of every other signal.
    # Fires before the series-strip path because range mismatch is
    # decisive even when series matches — "Demon Accords: Book 7"
    # against "Demon Accords 1-4" can't be right no matter how much
    # the series name overlaps. Saves bundle-verification API fetches
    # on candidates we can already reject from the title alone.
    record_range = _extract_volume_range(record_title)
    search_vol = _extract_volume(search_title)
    if record_range and search_vol is not None:
        rstart, rend = record_range
        if not (rstart <= search_vol <= rend):
            return {
                "record_title": record_title,
                "effective_record_title": record_title,
                "search_title": search_title,
                "series_stripped": False,
                "title_similarity": 0.0,
                "author_overlap": round(
                    author_overlap(record_authors, search_authors), 4
                ),
                "series_boost": 0.0,
                "raw_score": 0.0,
                "confidence": 0.0,
                "volume_range_mismatch": True,
                "candidate_range": [rstart, rend],
                "search_volume": search_vol,
            }

    effective_record = record_title
    series_stripped = False
    series_boost = 0.0
    # When the series-strip + clean leaves no meaningful tokens we fall
    # back to scoring the original record_title (the book IS just the
    # series name, possibly with a volume marker). The fallback path
    # cross-checks volumes to avoid promoting Book 2 as a match for
    # Book 5 in a self-titled series.
    fallback_to_full_title = False

    if known_series and record_title:
        series_lower = known_series.lower().strip()
        record_lower = record_title.lower()
        if series_lower in record_lower:
            series_boost = 0.10
            series_stripped = True
            stripped = re.sub(
                re.escape(known_series), "", record_title, flags=re.IGNORECASE
            ).strip()
            stripped = re.sub(r"[\s:—–-]+$", "", stripped).strip()
            stripped = re.sub(r"^[\s:—–-]+", "", stripped).strip()

            # Probe what's left after the noise pass that title_similarity
            # would apply: if all remaining tokens are numeric (or there
            # are none), the strip removed everything that distinguishes
            # this record from a sibling volume in the same series. In
            # that case scoring "Book 5" vs "Bikini Days: Unconventional
            # Romance" would give 0.0 ts and the result lands at 0.40 —
            # the bug Mark hit. Fall back to comparing the original full
            # titles so the strong series-name match registers.
            residue_tokens = _title_tokens(_clean_title(stripped))
            non_numeric = {t for t in residue_tokens if not t.isdigit()}
            if non_numeric:
                effective_record = stripped
            else:
                # Volume-mismatch guard: if both titles specify a volume
                # AND those volumes differ, this is a different book in
                # the same series — definitively wrong, return zero.
                rec_vol = _extract_volume(record_title)
                srch_vol = _extract_volume(search_title)
                if rec_vol is not None and srch_vol is not None and rec_vol != srch_vol:
                    return {
                        "record_title": record_title,
                        "effective_record_title": stripped,
                        "search_title": search_title,
                        "series_stripped": True,
                        "title_similarity": 0.0,
                        "author_overlap": round(
                            author_overlap(record_authors, search_authors), 4
                        ),
                        "series_boost": 0.0,
                        "raw_score": 0.0,
                        "confidence": 0.0,
                        "volume_mismatch": True,
                    }
                effective_record = record_title
                fallback_to_full_title = True

    ts = title_similarity(effective_record, search_title)
    au = author_overlap(record_authors, search_authors)
    raw = 0.7 * ts + 0.3 * au + series_boost
    final = min(raw, 1.0)

    return {
        "record_title": record_title,
        "effective_record_title": effective_record,
        "search_title": search_title,
        "series_stripped": series_stripped,
        "fallback_to_full_title": fallback_to_full_title,
        "title_similarity": round(ts, 4),
        "author_overlap": round(au, 4),
        "series_boost": round(series_boost, 4),
        "raw_score": round(raw, 4),
        "confidence": round(final, 4),
    }


def score_match(
    *,
    record_title: str,
    record_authors: list[str],
    search_title: str,
    search_authors: list[str] | str,
    known_series: str = "",
) -> float:
    """Weighted confidence in [0, 1].

    Title similarity (best of Jaccard/containment/substring) gets 70%
    weight; author overlap gets 30%.

    If `known_series` is provided (e.g. from MAM), the series name is
    stripped from the record title before scoring AND a boost is applied
    if the series name was found (proves the result is about the same
    series, not a coincidental title match).
    """
    return score_match_with_breakdown(
        record_title=record_title,
        record_authors=record_authors,
        search_title=search_title,
        search_authors=search_authors,
        known_series=known_series,
    )["confidence"]
