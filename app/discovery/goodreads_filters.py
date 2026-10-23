"""
What keeps a Goodreads book out of discovery (2026-10 audit wave 4b, S4).

Pure functions, used by the candidate worker (`goodreads_candidates`) at
each step and by a live scan's book-page loop (G103):

- **title** — before any request: a title in a non-Latin script, a
  language edition or import marker ("(German Edition)", "[Japanese
  Import]", "ISBN:"), and, while non-fiction isn't allowed (G96 / G101), a
  title naming a non-book product (crosswords, a journal, a planner).
- **autocomplete hit** (G95) — the hit's title by the same rules, a page
  count of 0, and a description snippet in another language.
- **book page** (G95 / G96) — a language other than English, a translation,
  a box set, an audiobook edition, and, while non-fiction isn't allowed, a
  non-fiction genre.

A5b (2026-10-09, 40 real candidates) is what these were set against: hit
filters took 6 of 13 non-books out of 31 confirmed hits; genres catch
well-shelved non-fiction only (a book with no ratings has no genres).
"""
from __future__ import annotations

import re
from typing import Optional

# Any character outside the Latin ranges (and general punctuation).
_NON_LATIN_RX = re.compile(r"[^\x00-ɏ -⁯\s]")

_EDITION_LANGUAGES = (
    "german|french|spanish|italian|portuguese|dutch|polish|russian|japanese|"
    "chinese|korean|turkish|swedish|norwegian|danish|finnish|czech|hungarian|"
    "romanian|greek|hebrew|arabic|hindi|vietnamese|thai|indonesian|catalan|"
    "ukrainian|brazilian"
)
_EDITION_RX = re.compile(
    rf"\b(?:{_EDITION_LANGUAGES})\s+edition\b|\bjapanese\s+import\b|\bisbn\s*:"
    r"|édition|edizione|ausgabe|edición",
    re.IGNORECASE,
)

# G101: titles naming a non-book product, rejected while non-fiction isn't
# allowed. Not "guide" / "handbook": real fiction uses those words.
_NON_BOOK_RX = re.compile(
    r"\b(?:crosswords?|puzzles?|colou?ring|journal|notebook|planner|workbook"
    r"|log\s?book)\b",
    re.IGNORECASE,
)

# G96: genres that mark a book as non-fiction (while it isn't allowed).
# "Nonfiction" counts unless "Fiction" is also listed; the rest only as the
# book's top genre ("Sports" is common on sports romances further down).
_NONFICTION_TOP_GENRES = frozenset({
    "self help", "sports", "puzzles", "cookbooks", "reference",
    "picture books",
})


def title_skip_reason(title: str, *, include_nonfiction: bool) -> Optional[str]:
    """Why a list or hit title is skipped without a request, or None."""
    t = title or ""
    if _NON_LATIN_RX.search(t):
        return "non_latin"
    if _EDITION_RX.search(t):
        return "edition_marker"
    if not include_nonfiction and _NON_BOOK_RX.search(t):
        return "non_book_title"
    return None


# ─── Snippet language ────────────────────────────────────────

_WORD_RX = re.compile(r"[^\W\d_]+")
_ENGLISH = frozenset(
    "the and of to in is that his her he she it with for was on as but they "
    "their be you are this from at by an not have has who what when will one "
    "all can into more than him them there been would which out up if about "
    "no so or my we your just".split()
)
_OTHER = {
    "de": frozenset("der die das und ist nicht ein eine mit sich auf für dem "
                    "den zu von im sie er wird auch noch".split()),
    "es": frozenset("el los las y que del en un una por con para su es se lo "
                    "como más pero sus al".split()),
    "fr": frozenset("le les et est une des du qui dans pour pas sur avec il "
                    "elle au ses son leur mais".split()),
    "it": frozenset("il gli della che non una per con sono nel alla del suo "
                    "sua anche".split()),
    "pt": frozenset("os um uma não com para que do da dos das em seu sua mas "
                    "pelo".split()),
    "pl": frozenset("i w nie się na z że do jest jak ale po od za już jego "
                    "jej co tym".split()),
    "nl": frozenset("het een en van niet dat zijn op voor met ook maar".split()),
}


def snippet_language(text: str) -> Optional[str]:
    """'en', another language code, or None (too short to tell): which
    language's common words the snippet holds most of."""
    words = _WORD_RX.findall((text or "").lower())
    if len(words) < 8:
        return None
    en = sum(1 for w in words if w in _ENGLISH)
    best, best_n = None, 0
    for code, vocab in _OTHER.items():
        n = sum(1 for w in words if w in vocab and w not in _ENGLISH)
        if n > best_n:
            best, best_n = code, n
    if best and best_n >= 3 and best_n > en:
        return best
    return "en" if en else None


def hit_reject_reason(
    hit_title: str, num_pages: Optional[int], snippet: str,
    *, include_nonfiction: bool,
) -> Optional[str]:
    """Why an autocomplete hit is rejected before its page, or None."""
    reason = title_skip_reason(hit_title, include_nonfiction=include_nonfiction)
    if reason:
        return reason
    if num_pages == 0:
        return "zero_pages"
    lang = snippet_language(snippet)
    if lang and lang != "en":
        return f"snippet_language:{lang}"
    return None


def nonfiction_genre(genres: list[str]) -> Optional[str]:
    """The genre that marks the book as non-fiction, or None."""
    lowered = [g.lower() for g in genres or []]
    if "nonfiction" in lowered and "fiction" not in lowered:
        return "Nonfiction"
    if lowered and lowered[0] in _NONFICTION_TOP_GENRES:
        return genres[0]
    return None


def page_reject_reason(details: dict, *, include_nonfiction: bool) -> Optional[str]:
    """Why a book is rejected from its loaded page, or None."""
    from app.discovery.language import is_foreign
    lang = details.get("language")
    if lang and is_foreign(str(lang)):
        return f"language:{str(lang).lower()}"
    if details.get("is_translation") and lang and str(lang).lower() not in ("english", "en", "eng"):
        return "translation"
    if details.get("is_set"):
        return "set"
    if details.get("is_audiobook"):
        return "audiobook"
    if not include_nonfiction:
        g = nonfiction_genre(details.get("genres") or [])
        if g:
            return f"nonfiction_genre:{g}"
    return None
