"""
Author-name normalization, matching, and query-variant generation.

Shared by any code that needs to compare author names across
representations — Goodreads returning `A.K. DuBoff` for a Calibre row
stored as `A. K. Duboff`, two Calibre authors recorded inconsistently
as `A K Duboff` and `A.K. Duboff`, diacritic variations like
`Noël Carré` vs `Noel Carre`, etc.

Three public helpers:

  * `normalize_author_name(s)` — canonical lowercase form. Strips
    diacritics, periods, extra whitespace, and merges adjacent
    single-letter tokens into one initial group (`"a k duboff"` →
    `"ak duboff"`). All four variants of `A K Duboff` collapse to
    `"ak duboff"`.

  * `authors_match(a, b)` — True iff the normalized forms are equal
    or close under `SequenceMatcher` (default ≥ 0.92 ratio). The
    fuzzy threshold catches a missing letter / transposition without
    false-matching unrelated names.

  * `author_name_variants(name)` — expand an author name into up to
    four query variants covering the common punctuation shapes
    search engines tokenize differently. For `"A K Duboff"` returns
    `["A K Duboff", "A. K. Duboff", "A.K. Duboff", "AK Duboff"]`.
    Names without initial patterns return just `[name]`.

The variants helper exists because some source search engines (e.g.,
Goodreads circa 2026) treat `"A K Duboff"` as semantically different
from `"A.K. Duboff"` at the search-ranker level. The stored name
might happen to be the "wrong" variant for a given source, in which
case the first query returns a wrong-author result; retrying with
alternate punctuation shapes is a cheap fix that only costs extra
HTTP requests when the first query already failed to match anyway.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher


_FUZZY_THRESHOLD = 0.92

# A bare single letter, e.g. "A" in "A K Duboff".
_SINGLE_LETTER_RE = re.compile(r"^[A-Za-z]$")

# A periodized initial group, e.g. "A.", "A.K.", "J.R.R.", "A.K" (no
# trailing period). Requires at least one letter-period pair so plain
# words like "Duboff" don't get classified as initials.
_PERIODIZED_INITIALS_RE = re.compile(r"^([A-Za-z]\.)+[A-Za-z]?$")


def normalize_author_name(s: str) -> str:
    """Return a canonical lowercase form of `s` for matching.

    Pipeline:
      1. Unicode NFKD decompose + strip combining marks (diacritics).
      2. Lowercase.
      3. Strip periods.
      4. Collapse internal whitespace.
      5. Merge adjacent single-letter tokens into one run.

    Empty/None input returns ``""``.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = s.replace(".", "")
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return ""
    tokens = s.split(" ")
    merged: list[str] = []
    i = 0
    while i < len(tokens):
        if len(tokens[i]) == 1 and tokens[i].isalpha():
            run = [tokens[i]]
            j = i + 1
            while j < len(tokens) and len(tokens[j]) == 1 and tokens[j].isalpha():
                run.append(tokens[j])
                j += 1
            merged.append("".join(run))
            i = j
        else:
            merged.append(tokens[i])
            i += 1
    return " ".join(merged)


def authors_match(a: str, b: str) -> bool:
    """Return True if `a` and `b` plausibly refer to the same author.

    Strong signal is normalized-equal. Falls back to a close
    `SequenceMatcher` ratio to catch transpositions or a dropped
    letter. Threshold 0.92 picked empirically — low enough to accept
    `"a k dubof"` vs `"ak duboff"`, high enough to reject
    `"amy duboff"` vs `"ak duboff"` (ratio ~0.5).
    """
    na = normalize_author_name(a)
    nb = normalize_author_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= _FUZZY_THRESHOLD


def _parse_name_tokens(name: str) -> list[tuple[str, bool]]:
    """Split `name` into `(content, is_initial_group)` pairs.

    A single letter (`"A"`) or a periodized initials token (`"A.K."`,
    `"J.R.R."`, `"A.K"`) becomes ``(letters, True)`` with periods
    stripped. Everything else is `(token, False)` untouched.
    """
    parsed: list[tuple[str, bool]] = []
    for tok in name.split():
        if _SINGLE_LETTER_RE.match(tok):
            parsed.append((tok, True))
        elif _PERIODIZED_INITIALS_RE.match(tok) and "." in tok:
            parsed.append((tok.replace(".", ""), True))
        else:
            parsed.append((tok, False))
    return parsed


def pick_canonical_display_name(existing: str, incoming: str) -> str:
    """Decide which punctuation variant to keep as the stored display name.

    Invariant: both inputs are expected to normalize to the same form.
    Used by `calibre_sync` when two Calibre author rows map to the
    same Seshat author (same normalized name but different
    punctuation), and by the one-time dedup pass when merging pre-
    existing duplicates.

    Heuristic (option 4a — most punctuation wins): the name with more
    periods wins. Matches external-source conventions (Goodreads
    renders `"A. K. DuBoff"`, not `"A K DuBoff"`) and is what a
    reader would consider "more formal". Ties keep the existing
    stored name so subsequent syncs don't thrash the row on every
    call.
    """
    if not existing:
        return incoming
    if not incoming:
        return existing
    incoming_periods = incoming.count(".")
    existing_periods = existing.count(".")
    if incoming_periods > existing_periods:
        return incoming
    return existing


def author_name_variants(name: str) -> list[str]:
    """Expand `name` into ordered punctuation variants for search retries.

    When the name contains initials (single-letter tokens, possibly
    punctuated), returns up to four variants covering:

      - the original (first, so it's tried as-is)
      - periodized with spaces: `"A. K. Duboff"`
      - periodized compact:     `"A.K. Duboff"`
      - spaces, no periods:     `"A K Duboff"`
      - compact, no periods:    `"AK Duboff"`

    Duplicates dropped in order (so the original is always first,
    and equal subsequent shapes don't recur). Names without any
    initials return `[name]` — no extra work for plain authors.
    """
    if not name:
        return []
    original = name.strip()
    if not original:
        return []
    parsed = _parse_name_tokens(original)

    # Collapse consecutive initial tokens into a single letter-run.
    # `[("A", True), ("K", True), ("Duboff", False)]` becomes
    # `[("initials", "AK"), ("word", "Duboff")]`.
    runs: list[tuple[str, str]] = []
    i = 0
    while i < len(parsed):
        if parsed[i][1]:
            letters = parsed[i][0]
            j = i + 1
            while j < len(parsed) and parsed[j][1]:
                letters += parsed[j][0]
                j += 1
            runs.append(("initials", letters))
            i = j
        else:
            runs.append(("word", parsed[i][0]))
            i += 1

    if not any(kind == "initials" for kind, _ in runs):
        return [original]

    def _render(shape: str) -> str:
        parts: list[str] = []
        for kind, content in runs:
            if kind == "initials":
                letters = list(content)
                if shape == "periodized_spaces":
                    parts.append(" ".join(l + "." for l in letters))
                elif shape == "periodized_compact":
                    parts.append("".join(l + "." for l in letters))
                elif shape == "spaces_no_period":
                    parts.append(" ".join(letters))
                elif shape == "compact_no_period":
                    parts.append("".join(letters))
            else:
                parts.append(content)
        return " ".join(parts)

    variants: list[str] = [original]
    for shape in (
        "periodized_spaces",
        "periodized_compact",
        "spaces_no_period",
        "compact_no_period",
    ):
        v = _render(shape)
        if v and v not in variants:
            variants.append(v)
    return variants[:4]
