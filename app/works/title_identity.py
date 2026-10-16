"""
One volume-aware title identity: are two titles the same book?

ADR-0028. Shared by every place Seshat asks "is this the book we
already have?" about two titles: scan merges (`lookup._merge_result`),
the Goodreads worker's known-title check, Calibre-sync absorb,
claim-for-owned and format dedup's owned check. Pure: strings in,
bool out, no I/O. Kept dependency-free (like `works/normalize.py`) so
`discovery/` and `orchestrator/` can both import it.

The rules, strongest first:

  1. Format. A manga / graphic novel / comic is never the prose book,
     and a dramatised / full-cast audio edition is never the plain one.
     "(Light Novel)" words are ignored.
  2. Numbers written in a title are compared strictly, whatever series
     they name. A title carries volume evidence of three kinds:
       core  - a number in the title proper: "Monster's Mercy 3",
               "Splinter Angel: Book 3", "Catgirl!: Volume Four",
               "13 ARC", Hardcover's "Book 06.0 - " prefix, "(Book 2)"
               with no series name;
       tag   - a number in a series parenthetical or in a subtitle that
               names a series: "(The Shadow Saga Book 3)",
               "Book Two of the Legends of Dune";
       index - `series_index`, with its series name (not written in the
               title).
     Both titles carry written numbers and share none -> different. A
     written number other than 0 or 1 on one side only must appear on
     the other side (as any kind), unless the other side's number is an
     index in a differently named series (Hardcover's reading-order
     meta-series), or the other side names the same series with no
     index at all; then the titles decide.
  3. Tag and index numbers are compared only within one normalised
     series name: "Faith is Earned (Book 1)" is not the "Faith is
     Earned" row filed as #3 of the same series, but "Shadow Puppets
     (The Shadow Saga Book 3)" is "Shadow Puppets" filed as #7 of
     "Enderverse: Publication Order".
  4. Title keys: the whole title without parentheticals, generic
     subtitles (": A … Novel / Adventure / LitRPG …"), volume words,
     articles and acronym dots; "Series: Title" -> Title and
     "Title: Series N" -> Title when the other part is a series name.
     A title part one author shares across 3+ different titles ("Star
     Wars Legends") is a label (`author_labels`), never an identity.
     Two "Head: Tail" titles that share only one half are the same only
     when the other halves agree ("Star Trek Deep Space Nine - The
     Siege" is not "… - Wrath of the Prophets").
  5. Series position alone decides only when one side has no real title
     ("Wild Wastes 3" = "Southern Storm" [Wild Wastes #3]).

When unsure the answer is "different": on the merge side a wrong
"same" silently loses a missing book and a wrong "different" makes a
visible duplicate; on the grab side a wrong "same" loses a wanted
volume. The rules were measured on two replays (Amazon's cache through
`_try_source`, and 1,946 snatched auto-grabs; scripts in
`.scratch/discovery-audit-2026-10/scripts/`). Don't loosen a number
rule to remove a duplicate without re-running them; a new failure
becomes a case in `tests/works/test_title_identity.py` first.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Iterable, Optional


@dataclass(frozen=True)
class TitleRef:
    """One side of a comparison: a title and its series placement.

    `series` / `index` are the stored placement (`series.name`,
    `books.series_index`), not anything parsed from the title.
    """
    title: str
    series: Optional[str] = None
    index: Optional[float] = None


@dataclass(frozen=True)
class ParsedTitle:
    """What `parse_title` reads out of one title.

    `proper` is the title without parentheticals, brackets or a
    Hardcover "Book N - " prefix. `tags` are the (series name, number)
    pairs written in series parentheticals and subtitles; a name of
    None means the number belongs to the title itself ("(Book 2)").
    `core` is the title's own volume number, or None.
    """
    proper: str
    tags: tuple[tuple[Optional[str], float], ...]
    core: Optional[float]
    prefix_volume: Optional[float]


# ─── Numbers ──────────────────────────────────────────────────

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}
_ROMAN_NUMBERS = {
    "i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7,
    "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12, "xiii": 13,
    "xiv": 14, "xv": 15,
}


def _number(token: str) -> Optional[float]:
    """"3" / "06.0" / "Four" / "IV" -> a float; anything else -> None."""
    tok = token.lower().strip(".")
    if re.fullmatch(r"\d+(?:\.\d+)?", tok):
        return float(tok)
    if tok in _WORD_NUMBERS:
        return float(_WORD_NUMBERS[tok])
    if tok in _ROMAN_NUMBERS:
        return float(_ROMAN_NUMBERS[tok])
    return None


# A number token after a volume word ("Book Two", "Vol. 3", "Part IV").
_NUMBER_TOKEN = (
    r"(\d{1,3}(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|i{1,3}|iv|v|vi{0,3}|ix|x)"
)
_RX_VOLUME = re.compile(
    r"(?i)\b(?:book|volume|vol\.?|part|no\.?|episode|tome|season)\s*#?\s*"
    + _NUMBER_TOKEN + r"\b|#\s*(\d{1,3}(?:\.\d+)?)"
)
# A trailing bare number: "(Black Fleet Saga 8)".
_RX_TRAILING_NUMBER = re.compile(r"(?i)(?:^|\s)" + _NUMBER_TOKEN + r"\s*$")
# A subtitle that is nothing but a volume: ": Book 2", " - Volume Four".
_RX_BARE_VOLUME = re.compile(
    r"(?i)^\s*(?:book|volume|vol\.?|part|episode|tome)\s*#?\s*"
    + _NUMBER_TOKEN + r"\s*$"
)
# Hardcover's "Book 06.0 - Blood Rites".
_RX_BOOK_PREFIX = re.compile(r"(?i)^\s*book\s+(\d+(?:\.\d+)?)\s*[-:]\s*")
# "He Who Fights with Monsters 13 ARC": an edition word after the number.
_RX_EDITION_NUMBER = re.compile(
    r"(?i)\s+(\d{1,3}(?:\.\d)?)\s+"
    r"(?:arc|advance reader copy|special edition|limited edition)\s*$"
)
# The title's head ends in a number: "Monster's Mercy 3", "Rocky IV".
_RX_HEAD_NUMBER = re.compile(r"(?:^|\s)(\d{1,3}(?:\.\d)?|[IVX]{1,4})\s*$")

# Every volume word + number, stripped from title keys.
_NUMBER_WORDS = (
    r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|first|second|third|fourth|fifth|sixth|"
    r"seventh|eighth|ninth|tenth|i{1,3}|iv|vi{0,3}|ix|xi{0,3}|xiv|xv|v|x"
)
_RX_ANY_VOLUME = re.compile(
    r"(?i)\b(?:book|volume|vol\.?|part|no\.|number|episode|tome|issue|season)"
    r"\s*[#:]?\s*(\d+(?:\.\d+)?|(?:" + _NUMBER_WORDS + r"))\b"
    r"|#\s*(\d+(?:\.\d+)?)"
)

_RX_SEGMENTS = re.compile(r"\s*:\s*|\s+-\s+")
_RX_PAREN = re.compile(r"\(([^)]*)\)")


# ─── Format ───────────────────────────────────────────────────

_RX_FORMAT = re.compile(r"(?i)\b(manga|graphic\s+novel|comic)s?\b")
# A dramatised / full-cast audio edition is its own product (Mark,
# 2026-10-16: "Threshing Day [Dramatized Adaptation]" beside the owned
# plain audiobook was wanted).
_RX_DRAMATIZED = re.compile(
    r"(?i)\b(?:dramati[sz](?:ed|ation)|full[\s-]cast|graphic\s*audio|"
    r"radio\s+(?:drama|play))\b"
)
_RX_LIGHT_NOVEL = re.compile(r"(?i)\(?\s*light\s+novel\s*\)?,?")


def _format(title: str) -> tuple[Optional[str], bool]:
    """('manga' / 'graphic' / 'comic' or None, dramatised edition?)."""
    m = _RX_FORMAT.search(title or "")
    visual = m.group(1).lower().split()[0] if m else None
    return visual, bool(_RX_DRAMATIZED.search(title or ""))


# ─── Title text ───────────────────────────────────────────────

# The generic words a descriptive subtitle ends in.
_GENERIC_WORDS = (
    r"novel|novella|novelette|story|stories|adventure|saga|thriller|romance|"
    r"series|litrpg|gamelit|fantasy|epic|mystery|tale|chronicle|anthology|"
    r"collection|edition|harem|progression|apocalypse|opera|odyssey|"
    r"cultivation|isekai|sci-fi|scifi|space\s+opera"
)
# ": A Witchlands Novel", " - A LitRPG Adventure" (to the end). Only after
# a real separator: the hyphen in "Sci-Fi" or "Spider-Man" is not one.
_RX_GENERIC_SUBTITLE = re.compile(
    r"(?i)(?:\s*:\s*|\s+-\s+)(?:(?:a|an|the)\s+)?(?:[\w'&+-]+\s+){0,6}"
    r"(?:" + _GENERIC_WORDS + r")s?\b.*$"
)
# A whole segment that is only such a description: "A Witchlands Novel",
# "A Magical Papillon Mystery" (two books sharing it are not one book).
_RX_GENERIC_ALONE = re.compile(
    r"(?i)^(?:(?:a|an|the)\s+)?(?:[\w'&+-]+\s+){0,4}"
    r"(?:" + _GENERIC_WORDS + r"|trilogy)s?$"
)
# ", Book 3 of the Expanse" / ": Volume Two" at the end.
_RX_BOOK_OF = re.compile(
    r"(?i)\s*[:\-,]?\s*(?:book|volume|vol\.?|part|episode)\s+(?:\d+|[a-z]+)"
    r"(?:\s+of\s+(?:the\s+)?[\w' -]+)?\s*$"
)
_PLACEHOLDER_TITLES = frozenset({
    "untitled", "tba", "tbd", "untitled novel", "title tbd", "book",
})


def _ascii_fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _clean(title: str) -> str:
    t = (title or "").replace("’", "'").replace("‘", "'")
    t = t.replace("—", "-").replace("–", "-")
    t = re.sub(r"(?<=\S)--(?=\S)", "-", t)
    t = re.sub(r"(?<=\w)-\s+", " - ", t)            # "Prism Academy- Prophecy"
    t = re.sub(r"\([^)]*$", "", t)                    # unclosed trailing "(..."
    t = re.sub(r"(?i),\s*(the|a|an)\s*$", "", t)     # "Demon King, The"
    t = re.sub(                                        # "T.A.P." -> "TAP"
        r"\b((?:[A-Za-z]\.){2,})", lambda m: m.group(1).replace(".", ""), t,
    )
    return re.sub(r"\s+", " ", _ascii_fold(t)).strip()


def _key_text(t: str) -> str:
    """Lowercased title text -> a comparison key: volume words, numbers,
    punctuation and a leading article removed."""
    t = _RX_ANY_VOLUME.sub(" ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"^\s*(?:the|a|an)\s+", "", t)
    t = re.sub(r"\b\d{1,3}\b", " ", t)
    t = re.sub(r"\s+(?:i|ii|iii|iv|v|vi|vii|viii|ix|x)\s*$", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _without_description(t: str) -> str:
    return _RX_BOOK_OF.sub("", _RX_GENERIC_SUBTITLE.sub("", t))


# ─── Series names ─────────────────────────────────────────────

# The same normalisation as `lookup._norm_consensus_series`, so a series
# name compares the way series consensus groups it.
_RX_SERIES_PARENS = re.compile(
    r"\s*\(\s*(?:light\s+novel|ln|web\s+novel|wn|manga|comic|graphic\s+novel|"
    r"audio|audiobook|omnibus|hardcover|paperback|ebook|kindle)\s*\)",
    re.IGNORECASE,
)
_RX_SERIES_LEAD = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
_RX_SERIES_TAIL = re.compile(
    r"\s+(saga|series|trilogy|cycle|chronicles|novels|books)\s*$",
    re.IGNORECASE,
)


@lru_cache(maxsize=8192)
def series_key(name: Optional[str]) -> str:
    """A series name normalised for comparison ("" for none)."""
    if not name:
        return ""
    n = _RX_SERIES_PARENS.sub("", name.strip())
    n = _RX_SERIES_LEAD.sub("", n)
    for _ in range(3):  # "Mistborn Saga Series"
        stripped = _RX_SERIES_TAIL.sub("", n)
        if stripped == n:
            break
        n = stripped
    n = re.sub(r"[^\w\s]", " ", n).lower()
    return re.sub(r"\s+", " ", n).strip()


# ─── Parsing ──────────────────────────────────────────────────


def _match_number(m: re.Match) -> Optional[float]:
    return _number(next(g for g in m.groups() if g))


def _parenthetical_tags(text: str) -> list[tuple[Optional[str], float]]:
    """(series name or None, number) from every "(…)" in the title."""
    tags: list[tuple[Optional[str], float]] = []
    for inner in _RX_PAREN.findall(text):
        for part in re.split(r"\)\s*\(|;", inner):
            part = part.strip()
            m = _RX_VOLUME.search(part) or _RX_TRAILING_NUMBER.search(part)
            if not m:
                continue
            v = _match_number(m)
            if v is not None:
                tags.append((part[:m.start()].strip(" ,:-") or None, v))
    return tags


def _subtitle_tags(proper: str) -> list[tuple[str, float]]:
    """(series name, number) written in a subtitle: "Book Two of the
    Legends of Dune" -> ("Legends of Dune", 2)."""
    tags: list[tuple[str, float]] = []
    for sub in _RX_SEGMENTS.split(proper)[1:]:
        if _RX_BARE_VOLUME.match(sub):
            continue
        m = _RX_VOLUME.search(sub) or re.search(r"(?:^|\s)(\d{1,3}(?:\.\d)?)\s*$", sub)
        if not m:
            continue
        v = _match_number(m)
        after = re.sub(r"(?i)^\s*(?:of|in)\s+(?:the\s+)?", "", sub[m.end():])
        name = after.strip(" ,") or sub[:m.start()].strip(" ,")
        if v is not None and name:
            tags.append((name, v))
    return tags


def _core_volume(proper: str, tags, prefix: Optional[float]) -> Optional[float]:
    """The title's own volume number, or None."""
    if prefix is not None:
        return prefix
    for name, v in tags:
        if name is None:  # "(Book 2)" with no series name belongs to the title
            return v
    segments = _RX_SEGMENTS.split(proper)
    head = segments[0]
    m = _RX_VOLUME.search(head)
    if m:
        v = _match_number(m)
        if v is not None:
            return v
    m = _RX_HEAD_NUMBER.search(head)
    if m and not re.fullmatch(r"\d{4}", m.group(1)):
        v = _number(m.group(1))
        if v is not None:
            return v
    for sub in segments[1:]:
        m = _RX_BARE_VOLUME.match(sub)
        if m:
            return _match_number(m)
    m = _RX_EDITION_NUMBER.search(head)
    return float(m.group(1)) if m else None


@lru_cache(maxsize=16384)
def parse_title(title: str) -> ParsedTitle:
    """Read the volume evidence out of one title (see the module doc)."""
    t = _clean(title)
    prefix = None
    m = _RX_BOOK_PREFIX.match(t)
    if m:
        prefix = float(m.group(1))
        t = t[m.end():]
    paren_tags = _parenthetical_tags(t)
    proper = _RX_PAREN.sub(" ", t)
    proper = re.sub(r"\[[^\]]*\]", " ", proper)
    proper = re.sub(r"\s+", " ", proper).strip()
    core = _core_volume(proper, paren_tags, prefix)
    tags = tuple(paren_tags) + tuple(_subtitle_tags(proper))
    return ParsedTitle(proper=proper, tags=tags, core=core, prefix_volume=prefix)


@lru_cache(maxsize=16384)
def _parsed(title: str) -> tuple[tuple[Optional[str], bool], ParsedTitle]:
    """(format, parse) of a raw title; the parse ignores light-novel words."""
    return _format(title), parse_title(_RX_LIGHT_NOVEL.sub(" ", title or ""))


# ─── Keys ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Keys:
    """How one title may match another.

    `whole`: the whole title without a generic subtitle, and the title
    with colons as spaces. `head` / `tail`: for a "Head: Tail" title, the
    two halves on their own when they stand as a title (2+ words, the
    other half is a series name, or the volumes already agree; never a
    series name, never an author's label; a tail never a generic
    description), else "". `head_text` / `tail_text`: the halves as
    written, for checking that the half not matched agrees.
    """
    whole: frozenset
    head: str
    tail: str
    head_text: str
    tail_text: str


@lru_cache(maxsize=65536)
def _title_keys(
    proper: str,
    series: Optional[str],
    same_core: bool,
    labels: frozenset,
    position_agrees: bool,
    other_series: Optional[str],
) -> _Keys:
    p = proper.lower()
    # Any length: "It" is a title. The 3-character floor is for halves.
    whole = frozenset(
        k for k in (_key_text(_without_description(p)), _key_text(p.replace(":", " ")))
        if k
    )
    segments = [s for s in _RX_SEGMENTS.split(p) if s.strip()]
    if len(segments) < 2:
        return _Keys(whole, "", "", "", "")
    names = {series_key(s) for s in (series, other_series) if s} - {""}
    head_text = _key_text(segments[0])
    rest = [s for s in segments[1:] if not _RX_BARE_VOLUME.match(s)]
    tail_text = _key_text(_without_description(rest[0])) if rest else ""
    tail_is_series = bool(tail_text and series_key(tail_text) in names)
    head_is_series = bool(head_text and series_key(head_text) in names)

    def usable(k: str) -> bool:
        return (
            len(k) >= 3 and k not in labels
            and (same_core or series_key(k) not in names)
        )

    tail = tail_text if tail_text and not _RX_GENERIC_ALONE.match(tail_text) and (
        len(tail_text.split()) >= 2 or head_is_series or position_agrees
    ) and usable(tail_text) else ""
    head = head_text if head_text and (
        len(head_text.split()) >= 2 or tail_is_series or same_core or position_agrees
    ) and usable(head_text) else ""
    return _Keys(whole, head, tail, head_text, tail_text)


def _close(x: str, y: str) -> bool:
    """Equal, or a near-identical spelling ("unauthorized" / "unauthorised",
    a truncated title)."""
    return x == y or (
        min(len(x), len(y)) >= 8 and SequenceMatcher(None, x, y).ratio() >= 0.92
    )


def _halves_agree(x: str, y: str) -> bool:
    """The halves not matched: either missing, one inside the other, or
    close. "Star Trek DS9 - The Siege" vs "… - Wrath of the Prophets"
    share a head but these disagree."""
    if not x or not y or _close(x, y):
        return True
    return f" {x} " in f" {y} " or f" {y} " in f" {x} "


def _titles_match(ka: _Keys, kb: _Keys) -> bool:
    if any(_close(x, y) for x in ka.whole for y in kb.whole):
        return True
    # One side's head or tail is the other side's whole title:
    # "Dragon Bound" = "Dragon Bound: A Legacy Building Fantasy".
    for halves, whole in (((ka.head, ka.tail), kb.whole), ((kb.head, kb.tail), ka.whole)):
        if any(_close(h, w) for h in halves if h for w in whole):
            return True
    # Half to half, only when the other halves agree.
    for x, x_other in ((ka.head, ka.tail_text), (ka.tail, ka.head_text)):
        if not x:
            continue
        for y, y_other in ((kb.head, kb.tail_text), (kb.tail, kb.head_text)):
            if y and _close(x, y) and _halves_agree(x_other, y_other):
                return True
    return False


def _no_real_title(proper: str, series: Optional[str]) -> bool:
    """A placeholder, nothing, or only the series name ("Wild Wastes 3")."""
    k = _key_text(_without_description(proper.lower()))
    return (
        not k or k in _PLACEHOLDER_TITLES
        or bool(series and series_key(k) == series_key(series))
    )


# ─── The identity ─────────────────────────────────────────────

# Written numbers that say nothing on their own: book 1 is usually the
# bare title, and "Book 0" is a prequel that sits beside its series.
_UNNUMBERED = frozenset({0.0, 1.0})


def _as_index(value) -> Optional[float]:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _series_numbers(
    tags, series: Optional[str], index: Optional[float], core: Optional[float],
) -> dict[str, set[float]]:
    """Normalised series name -> the numbers this side gives it."""
    out: dict[str, set[float]] = defaultdict(set)
    if series and core is not None:
        out[series_key(series)].add(core)
    for name, v in tags:
        if name:
            out[series_key(name)].add(v)
    if series and index is not None:
        out[series_key(series)].add(index)
    return out


def _lone_number_differs(
    mine: set[float], mine_tags, my_series: Optional[str], my_proper: str,
    other_series: Optional[str], other_index: Optional[float],
) -> bool:
    """A written number on this side only: does it make them different?

    It must appear as the other side's index, unless that index is in a
    differently named series, or the other side names one of this
    side's tagged series with no index (the titles decide then).
    """
    big = mine - _UNNUMBERED
    if not big:
        return False
    my_names = {series_key(s) for s, _ in mine_tags if s}
    if my_series:
        my_names.add(series_key(my_series))
    if other_index is None:
        tagged = {series_key(s) for s, _ in mine_tags if s}
        return not (
            other_series and series_key(other_series) in tagged
            and not _no_real_title(my_proper, other_series)
        )
    if other_index in big:
        return False
    if other_series and series_key(other_series) not in my_names:
        return False   # a reading-order meta-series index: the titles decide
    return True


def same_book(a: TitleRef, b: TitleRef, labels: frozenset = frozenset()) -> bool:
    """True when the two titles name the same book (ADR-0028).

    `labels` is the author's label set (`author_labels`): title parts
    that never identify a book on their own.
    """
    fmt_a, pa = _parsed(a.title or "")
    fmt_b, pb = _parsed(b.title or "")
    if fmt_a != fmt_b:
        return False
    a_index, b_index = _as_index(a.index), _as_index(b.index)
    na = {v for _, v in pa.tags} | ({pa.core} if pa.core is not None else set())
    nb = {v for _, v in pb.tags} | ({pb.core} if pb.core is not None else set())

    # Numbers written in either title, whatever series they name.
    if na and nb and not (na & nb):
        return False
    if na and not nb and _lone_number_differs(
        na, pa.tags, a.series, pa.proper, b.series, b_index,
    ):
        return False
    if nb and not na and _lone_number_differs(
        nb, pb.tags, b.series, pb.proper, a.series, a_index,
    ):
        return False

    # Volumes within one series name.
    sa = _series_numbers(pa.tags, a.series, a_index, pa.core)
    sb = _series_numbers(pb.tags, b.series, b_index, pb.core)
    if pa.core is not None and pb.core is not None and pa.core != pb.core:
        return False
    if (pa.core is None) != (pb.core is None):
        v = pa.core if pa.core is not None else pb.core
        other = sb if pa.core is not None else sa
        other_written = nb if pa.core is not None else na
        anywhere = set().union(*other.values()) if other else set()
        if v != 1.0 and v not in anywhere and v not in other_written:
            return False
    shared = [k for k in sa if k and k in sb]
    if any(not (sa[k] & sb[k]) for k in shared):
        return False
    position_agrees = any(sa[k] & sb[k] for k in shared)

    # Titles.
    same_core = pa.core is not None and pa.core == pb.core
    ka = _title_keys(pa.proper, a.series, same_core, labels, position_agrees, b.series)
    kb = _title_keys(pb.proper, b.series, same_core, labels, position_agrees, a.series)
    if _titles_match(ka, kb):
        return True
    return position_agrees and (
        _no_real_title(pa.proper, a.series) or _no_real_title(pb.proper, b.series)
    )


def author_labels(titles: Iterable[str]) -> frozenset:
    """Title parts one author uses beside 3+ different other parts.

    "Star Wars Legends" in "Crucible: Star Wars Legends", "Apocalypse:
    Star Wars Legends", …: a franchise label, not a title. An author's
    own duplicate rows of one book ("Dune: House Harkonnen" ×3) don't
    make "Dune" a label; three different books do.
    """
    partners: dict[str, set[str]] = defaultdict(set)
    for t in set(titles):
        parts = [
            _key_text(x)
            for x in _RX_SEGMENTS.split(parse_title(t or "").proper.lower())
        ]
        if len(parts) < 2:
            continue
        for i in (0, len(parts) - 1):
            if parts[i]:
                partners[parts[i]].add(
                    " ".join(p for j, p in enumerate(parts) if j != i)
                )
    return frozenset(k for k, rest in partners.items() if len(rest) >= 3)


__all__ = [
    "ParsedTitle",
    "TitleRef",
    "author_labels",
    "parse_title",
    "same_book",
    "series_key",
]
