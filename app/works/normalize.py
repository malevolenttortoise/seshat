"""
Normalization helpers for cross-library work matching.

Matching works across libraries requires canonicalizing names so small
cosmetic differences (Unicode apostrophes, leading articles, trailing
series markers) don't split a single work into two candidate matches.

We keep this module tiny and dependency-free so the matcher can import
it without dragging in the full discovery stack.

Rules — kept deliberately conservative (false positives hurt more than
false negatives; unlinked books can be linked manually by the user):

  `normalize_author`
    - lowercase, strip
    - collapse whitespace
    - strip suffixes like "Jr.", "Sr.", "PhD" (audio rippers sometimes
      add these inconsistently)

  `normalize_title`
    - lowercase, strip
    - drop leading articles "the/a/an"
    - replace unicode apostrophes with ascii
    - strip ONLY format-hint parenthetical/bracketed sections
      ("(Unabridged)", "[Audiobook]") — volume numbers in parens
      like "(Book 3)" are preserved so distinct volumes don't merge
    - strip punctuation + collapse whitespace
    - do NOT strip trailing volume markers ("#7", ", Book 1") — those
      are load-bearing for volume distinction

The pair `(normalized_author, normalized_title)` is the matcher's
equality key. It is NOT stored in the DB — we compute it on each
scan so a normalization tweak (or a Unicode library upgrade) takes
effect without a backfill migration.
"""
from __future__ import annotations

import re
import unicodedata

_ARTICLE_RX = re.compile(r"^\s*(the|a|an)\s+", re.IGNORECASE)
# Format-hint parentheticals only — do NOT strip arbitrary paren content.
# Blanket paren stripping collapsed "Ghost in the System (Book 5)" with
# (Book 2)/(Book 3)/(Book 4)/(Book 6) into one bucket; the same bug hit
# "Aquilon: The Water Mage (Book N)". Allowlist known format/edition
# tokens instead — any paren group whose contents aren't in the list is
# left alone so volume numbers can't disappear.
_FORMAT_PAREN_RX = re.compile(
    r"[\(\[\{]\s*"
    r"(unabridged|abridged|audio[\s-]?book|audiobook|audible|"
    r"e[\s-]?book|ebook|m4b|mp3|paperback|hardcover|"
    r"kindle(?:\s+edition)?|special\s+edition)"
    r"\s*[\)\]\}]",
    re.IGNORECASE,
)
# Trailing volume-marker stripping (`,?\s*#\d+`, dash/colon/semi variants,
# etc.) was attempted repeatedly and always merged distinct volumes of the
# same series ("Spice & Wolf, Vol. 21" through Vol. 1 → "spice wolf";
# "The Last Paladin #1" through #7 → "last paladin"). There's no way to
# tell decoration from content without external metadata, so we don't
# strip at all. Rely on:
#   1. Exact strict match (primary path)
#   2. " - Subtitle" loose variant in match_keys (covers the
#      Halo: Evolutions publisher-subtitle drift case) — skipped when
#      the suffix is itself a volume marker like "Book 3"
#   3. Manual linking via the Works UI for anything else
# Apostrophes get dropped (not replaced with space) so "Don't" and
# "Dont" normalize identically. Other punctuation becomes a space so
# "Title:Subtitle" stays splittable.
_APOSTROPHE_RX = re.compile(r"['\u2019\u2018]")
_PUNCT_RX = re.compile(r"[^\w\s]+")
_SPACE_RX = re.compile(r"\s+")
_AUTHOR_SUFFIX_RX = re.compile(
    r"[,\s]+(jr|sr|ii|iii|iv|phd|md|esq)\.?\s*$", re.IGNORECASE,
)
# "Book 3", "Vol. 1", "Volume 21", "Part 2", "#7" at the start of a
# suffix — marks a volume indicator, not a publisher subtitle. Used by
# match_keys to suppress the loose variant in these cases.
_VOLUME_MARKER_RX = re.compile(
    r"^(book|vol\.?|volume|part|chapter|#)\s*\d", re.IGNORECASE,
)


def _replace_unicode(text: str) -> str:
    """Replace fancy quotes/dashes with ASCII equivalents."""
    return (
        text.replace("\u2018", "'").replace("\u2019", "'")
            .replace("\u201c", '"').replace("\u201d", '"')
            .replace("\u2013", "-").replace("\u2014", "-")
    )


def normalize_author(name: str) -> str:
    """Normalize an author name for cross-library matching.

    Returns "" for empty input so callers can gate on a falsy check
    without raising on None. Folds diacritics via NFKD decomposition
    (François → Francois) to paper over inconsistent encoding between
    Calibre (utf-8) and Audnexus (also utf-8 but post-processed).
    """
    if not name:
        return ""
    s = _replace_unicode(name).strip()
    # NFKD decomposition + ascii filter drops accented chars to their
    # base letter. Audible / Audnexus data isn't always diacritic-clean
    # even when Calibre's is.
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = _AUTHOR_SUFFIX_RX.sub("", s)
    s = _APOSTROPHE_RX.sub("", s)
    s = _PUNCT_RX.sub(" ", s.lower())
    s = _SPACE_RX.sub(" ", s).strip()
    return s


def normalize_title(title: str) -> str:
    """Normalize a book title for cross-library matching.

    Matching is deliberately strict after normalization — we want a
    clean (normalized_author, normalized_title) pair to be a hard
    equality check, not a fuzzy score. The discovery-side
    `lookup._fuzzy_match` already has a separate fuzzy pass for
    same-library dedupe; cross-library linking is a separate concern.
    """
    if not title:
        return ""
    s = _replace_unicode(title).strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    # Drop format-hint parentheticals only ("(Unabridged)",
    # "[Audiobook]"). Do NOT blanket-strip paren content — "(Book 3)"
    # and "(Vol. 21)" must survive so distinct volumes don't collapse.
    s = _FORMAT_PAREN_RX.sub(" ", s)
    # Drop leading article (The/A/An) so "The Way of Kings" matches
    # "Way of Kings".
    s = _ARTICLE_RX.sub("", s)
    s = _APOSTROPHE_RX.sub("", s)
    s = _PUNCT_RX.sub(" ", s.lower())
    s = _SPACE_RX.sub(" ", s).strip()
    return s


def match_key(author: str, title: str) -> str:
    """Return the (normalized) matcher key, or "" if either half is empty.

    Legacy single-key API. Callers that want full multi-variant matching
    (handles the "Calibre has subtitle / Audible doesn't" case) should
    use `match_keys()` instead — that returns every variant a book
    should be indexed under.
    """
    na = normalize_author(author)
    nt = normalize_title(title)
    if not na or not nt:
        return ""
    return f"{na}||{nt}"


def match_keys(author: str, title: str) -> list[str]:
    """Return every match key a book should be indexed under.

    Strict key (full title) comes first. Additional loose variants are
    appended when the title carries a separable publisher subtitle.
    Callers bucket books into connected components via any shared key —
    two books share a bucket if ANY of their keys coincide.

    Why multi-key instead of fuzzy scoring?
      Strict keys stay exact-equality for fast bucketing, and each
      loose variant is explicit (deterministic, auditable) so we can
      trust the matcher's output without a human score review loop.
      Two false-positive vectors we deliberately avoid:

        * Stripping after `:` would collide "Mistborn: The Final Empire"
          with "Mistborn: The Hero of Ages" — both lose their
          distinguishing body. Not done.
        * Stripping after any ` - ` would collide Doctor Who tie-ins
          ("Doctor Who - The Day of the Doctor" ≡ "Doctor Who - The
          Time of the Doctor"). Not done.

      The one variant we DO generate is "strip trailing ` - Subtitle`
      IF the base title has 2+ content words AND the suffix isn't a
      volume marker" — covers the "Halo: Evolutions - Essential Tales
      of the Halo Universe" vs "Halo: Evolutions" case, while the
      volume-marker check keeps "Isekai Herald - Book 1: ..." through
      "Book 3: ..." from collapsing onto a bare "Isekai Herald" key.
    """
    na = normalize_author(author)
    if not na:
        return []
    keys: list[str] = []
    seen: set[str] = set()

    strict = normalize_title(title)
    if strict:
        key = f"{na}||{strict}"
        keys.append(key)
        seen.add(key)

    # Loose variant: strip " - Subtitle" tail when prefix has 2+ words
    # AND the stripped tail isn't a volume marker ("Book 3", "Vol. 1",
    # "#7"). Volume-marker tails would collapse distinct volumes onto
    # the prefix key, which is the Isekai-Herald false-merge class.
    if title and " - " in title:
        prefix, suffix = title.rsplit(" - ", 1)
        if not _VOLUME_MARKER_RX.match(suffix.strip()):
            loose = normalize_title(prefix)
            if loose and len(loose.split()) >= 2:
                key = f"{na}||{loose}"
                if key not in seen:
                    keys.append(key)
                    seen.add(key)

    return keys
