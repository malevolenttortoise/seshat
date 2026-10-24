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
     and a dramatised / full-cast audio edition is never the plain one
     (by its title's words, or `TitleRef.dramatised` from MAM's category).
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
     index at all; then the titles decide. They also decide when the
     other side gives no index and every number is a series position:
     "(Series Book N)", or a subtitle beside a real title, "Title: Book
     Two of Series" / "Title: Series, Book 2" (F29, `_numbers_are_positions`).
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
     Also dropped: a description after a comma (", an Urban Fantasy
     Adventure"), a " - She's …" tagline, articles inside a title; and,
     when both are stored at one place in one series, that series' name as
     a prefix ("Geostorm The Shift" = "The Shift", Geostorm #1).
     Two "Head: Tail" titles that share only one half are the same only
     when the other halves agree ("Star Trek Deep Space Nine - The
     Siege" is not "… - Wrath of the Prophets").
  5. Series position alone decides only when one side has no real title
     ("Wild Wastes 3" = "Southern Storm" [Wild Wastes #3]).

Wave D (2026-10-23) added: marketing tails as descriptions (G225),
spelled-out numbers in a subtitle (G226; titles also compare as
written), spaces when both books can't be in different series (G227),
"Series, Book N: Title" / "Title: Series: Book N" as positions (G228),
and a series named in either title as a series name (G229).

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
    `dramatised`: the edition is dramatised / full cast by a signal
    outside the title (F28: MAM's "Dramatization/Full Cast" category).
    """
    title: str
    series: Optional[str] = None
    index: Optional[float] = None
    dramatised: bool = False


@dataclass(frozen=True)
class ParsedTitle:
    """What `parse_title` reads out of one title.

    `proper` is the title without parentheticals, brackets or a
    Hardcover "Book N - " prefix. `tags` are the (series name, number)
    pairs written in series parentheticals and subtitles; a name of
    None means the number belongs to the title itself ("(Book 2)").
    `core` is the title's own volume number, or None. `position_only`:
    every number the title carries sits in a "(Series Book N)"
    parenthetical, so it says where the book sits, not which book it is.
    `subtitle_shapes`: for each subtitle tag, (series name, shape, the
    title parts before it); see `_subtitle_tags`. `written`: `proper` with
    its spelled-out numbers as written ("Sermon Three", G226), for
    comparing titles as spelled.
    """
    proper: str
    tags: tuple[tuple[Optional[str], float], ...]
    core: Optional[float]
    prefix_volume: Optional[float]
    position_only: bool = False
    subtitle_shapes: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    written: str = ""


# ─── Numbers ──────────────────────────────────────────────────

_WORD_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
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
    r"(\d{1,3}(?:\.\d+)?|zero|one|two|three|four|five|six|seven|eight|nine|ten|"
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
    r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
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
    return visual, is_dramatised(title)


def is_dramatised(text: Optional[str]) -> bool:
    """A dramatised / full-cast edition named in `text`: a title's words,
    or a MAM category ("Audiobooks - Dramatization/Full Cast")."""
    return bool(_RX_DRAMATIZED.search(text or ""))


# ─── Title text ───────────────────────────────────────────────

# The generic words a descriptive subtitle ends in.
_GENERIC_WORDS = (
    r"novel|novella|novelette|story|stories|adventure|saga|thriller|romance|"
    r"series|litrpg|gamelit|fantasy|epic|mystery|tale|chronicle|anthology|"
    r"collection|edition|harem|progression|apocalypse|opera|odyssey|"
    r"cultivation|isekai|sci-fi|scifi|space\s+opera"
)
# ": A Witchlands Novel", " - A LitRPG Adventure": a description that is
# the title's last part. Only after a real separator (the hyphen in
# "Sci-Fi" or "Spider-Man" is not one), and never across another one:
# "Star Wars: Tales of the Jedi - Redemption" keeps its title.
# A word of a description: hyphenated is fine ("Sci-Fi"), a lone "-" is a
# separator, never a word ("Starfleet Academy - Worf's First Adventure" is a
# title part, then a description).
_DESC_WORD = r"[\w'&+]+(?:-[\w'&+]+)*"
# The generic word ends the part, or what follows it is more of a
# description ("… Romance with Forced Proximity", "… thriller about", "…
# Saga, Book 2", "… Novella A Penguin eSpecial", "… Fantasy Novel THE …",
# "… epic Agent Cormac science fiction series"), not a title going on
# ("Long Story Short").
_DESC_FOLLOW = (
    r"(?:\s*$|(?=\s*[,;(\[!./·|])"
    r"|\s+(?:with|for|about|in|of|set|from|featuring|where|that|and|to|on|by|like|a|an|the|inspired|based|box|boxed)\b"
    r"|\s+(?:" + _GENERIC_WORDS + r")s?\b"
    r"|(?:\s+" + _DESC_WORD + r")*?\s+(?:" + _GENERIC_WORDS + r")s?\s*$)"
)
_RX_GENERIC_SUBTITLE = re.compile(
    # ", an Urban Fantasy Men's Adventure" too: a comma, then "a" / "an".
    r"(?i)(?:\s*:\s*|\s+-\s+|,\s+(?=(?:a|an)\s))(?:"
    r"(?:a|an|the)\s+(?:" + _DESC_WORD + r"\s+){0,6}(?:" + _GENERIC_WORDS + r")s?\b" + _DESC_FOLLOW
    + r"|(?:" + _DESC_WORD + r"\s+){1,6}(?:" + _GENERIC_WORDS + r")s?\b" + _DESC_FOLLOW
    # A part that starts with its generic word is a title going on ("Tales
    # From the Cobra Wars", "Stories of the Raksura"), unless it ends there
    # or says what it's based on ("Stories Inspired by the Music of Rush").
    + r"|(?:" + _GENERIC_WORDS + r")s?\b(?:\s*$|(?=\s*[,;(\[!./·|])|\s+(?:inspired|based|featuring)\b)"
    r")(?:(?!\s*:\s|\s+-\s+).)*$"
)
# ": A Matt Kearns Novel 3", ": The Valkeryn Chronicles 1": an article-led
# description that numbers the book (": Collection 2" is a title).
_RX_NUMBERED_DESCRIPTION = re.compile(
    r"(?i)(?:\s*:\s*|\s+-\s+)(?:a|an|the)\s+(?:[\w'&+-]+\s+){0,6}"
    r"(?:" + _GENERIC_WORDS + r")s?\s+#?\d{1,3}(?:\.\d+)?\s*$"
)
# " - She's at the end of her rope": a tagline, not a title part. Only
# after a dash and starting with a pronoun ("… - The Golden Age of the
# Sith" is a title).
_RX_TAGLINE = re.compile(
    r"(?i)\s+-\s+(?:she|he|they|we|you|it|i)(?:'s|'re|'ll|'d|'ve|'m)?\s+(?:\S+\s+){2,}\S+\s*$"
)
# A whole segment that is only such a description: "A Witchlands Novel",
# "A Magical Papillon Mystery" (two books sharing it are not one book),
# or a publisher's "A Tor Original" / "An Audible Original".
_RX_GENERIC_ALONE = re.compile(
    r"(?i)^(?:(?:(?:a|an|the)\s+)?(?:[\w'&+-]+\s+){0,4}"
    r"(?:" + _GENERIC_WORDS + r")s?|(?:a|an)\s+(?:[\w'&+-]+\s+){0,3}original)$"
)
_RX_PUBLISHER_ORIGINAL = re.compile(
    r"(?i)(?:\s*:\s*|\s+-\s+)(?:a|an)\s+(?:[\w'&+-]+\s+){0,3}original\s*$"
)
# ", Book 3 of the Expanse" / ": Volume Two" at the end.
_RX_BOOK_OF = re.compile(
    r"(?i)\s*[:\-,]?\s*(?:book|volume|vol\.?|part|episode)\s+(?:\d+|[a-z]+)"
    r"(?:\s+of\s+(?:the\s+)?[\w' -]+)?\s*$"
)
# A last part that is a series and its volume, after a title and a
# description: "…: A LitRPG Adventure: He Who Fights with Monsters, Book
# 13". Only as a third part or later: in "Dune: House Corrino, Vol. 2"
# the last part is the title.
_RX_SERIES_VOLUME_PART = re.compile(
    r"(?i)(?:\s*:\s*|\s+-\s+)(?:(?!\s*:\s|\s+-\s+).)*?(?:,\s*|\s+)"
    r"(?:book|volume|vol\.?|part|episode)\s+(?:\d+(?:\.\d+)?|[a-z]+)\s*$"
)
_PLACEHOLDER_TITLES = frozenset({
    "untitled", "tba", "tbd", "untitled novel", "title tbd", "book",
})
# A bare word that is never a title part on its own (owned "Series:
# Operator Chronicles, Book 1" isn't "Series: The Children of Earth, Book 1").
_GENERIC_PARTS = frozenset({
    "series", "book", "books", "volume", "novel", "novels", "saga", "collection",
})


def _ascii_fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def _clean(title: str) -> str:
    t = (title or "").replace("’", "'").replace("‘", "'")
    t = t.replace("—", "-").replace("–", "-")
    t = t.replace("·", ",")                           # "LitRPG · Time Travel · …"
    t = re.sub(r"(?<=\S)--(?=\S)", "-", t)
    t = re.sub(r"(?<=\w)-\s+", " - ", t)            # "Prism Academy- Prophecy"
    for _ in range(2):                                 # "(Supergirl (1996-2003))"
        t = re.sub(r"\(([^()]*)\(([^()]*)\)([^()]*)\)", r"(\1 \2 \3)", t)
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
    """Drop trailing descriptions, one part at a time ("Apocalypse
    Cultivator: The Complete Series: A Cultivation Novel")."""
    t = _RX_TAGLINE.sub("", t)
    for _ in range(4):
        stripped = _RX_NUMBERED_DESCRIPTION.sub("", t)
        stripped = _RX_GENERIC_SUBTITLE.sub("", _RX_PUBLISHER_ORIGINAL.sub("", stripped))
        if len(_RX_SEGMENTS.findall(stripped)) >= 2:
            stripped = _RX_SERIES_VOLUME_PART.sub("", stripped)
        stripped = _RX_BOOK_OF.sub("", stripped)
        if stripped == t:
            break
        t = stripped
    return t


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
            name = part[:m.start()].strip(" ,:-") or None
            if v is not None and (name or "").lower() != "of":   # "#2 (of 5)" counts issues
                tags.append((name, v))
    return tags


def _subtitle_tags(proper: str) -> list[tuple[str, float, str, tuple[str, ...]]]:
    """(series name, number, shape, the parts before it) written in a
    subtitle: "Book Two of the Legends of Dune" -> ("Legends of Dune",
    2, "of", …).

    The shape says whether the number may be only a series position
    (F29): "of" for "Book N of [the] Series", "named" for "Series Book
    N" / "Series, Book N", "" for anything else ("Vol. 3", "#5", a bare
    trailing number), which always stays a volume.
    """
    tags: list[tuple[str, float, str, tuple[str, ...]]] = []
    segments = _RX_SEGMENTS.split(proper)
    for i, sub in enumerate(segments[1:], 1):
        if _RX_BARE_VOLUME.match(sub):
            continue
        m = _RX_VOLUME.search(sub) or re.search(
            r"(?:^|\s)(\d{1,3}(?:\.\d)?|II|III|IV|V|VI|VII|VIII|IX|X|XI|XII|XIII|XIV|XV)\s*$", sub,
        )                                  # "Dark Nest III" (upper case: "I" is a word)
        if not m:
            continue
        v = _match_number(m)
        rest = sub[m.end():]
        after = re.sub(r"(?i)^\s*(?:of|in)\s+(?:the\s+)?", "", rest)
        before = sub[:m.start()].strip(" ,")
        name = after.strip(" ,") or before
        if v is None or not name:
            continue
        shape = ""
        if m.group(0).lower().startswith("book"):
            if after.strip(" ,") and after != rest:
                shape = "of"
            elif not rest.strip(" ,") and before:
                shape = "named"
        tags.append((name, v, shape, tuple(segments[:i])))
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


# G225: a last part that sells the book is a description, its numbers
# included ("…: Now a Netflix Originals Movie", "…: The Humorous Science
# Fiction Thriller From the Author of Mickey 7", "… #1 Bestseller").
_RX_MARKETING_PART = re.compile(
    r"(?i)(?:\s*:\s*|\s+-\s+)(?:(?!\s*:\s|\s+-\s+).)*?\b(?:"
    r"now\s+an?\s+(?:[\w'-]+\s+){0,4}(?:movie|film|motion\s+picture|series|show)"
    r"|soon\s+to\s+be\s+an?\s"
    r"|(?:from|by)\s+the\s+(?:[\w'-]+\s+){0,3}(?:author|creator|writer)s?\s+of\b"
    r"|(?:new\s+york\s+times|usa\s+today|international|#1|number\s+one)\s+best\s*-?sell"
    r")(?:(?!\s*:\s|\s+-\s+).)*$"
)
# G226: a spelled-out number ending a subtitle part is that number
# ("Sermon Three" = "Sermon 3"). Not in the head: "Ready Player One".
_SUBTITLE_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15,
}
_RX_SUBTITLE_NUMBER_WORD = re.compile(
    r"(?i)(?<=[A-Za-z]\s)(" + "|".join(_SUBTITLE_NUMBER_WORDS) + r")(?=\s*$)"
)
_RX_PART_SEPARATOR = re.compile(r"(\s*:\s*|\s+-\s+)")


def _is_description_part(piece: str) -> bool:
    return bool(
        _RX_GENERIC_SUBTITLE.fullmatch(": " + piece)
        or _RX_NUMBERED_DESCRIPTION.fullmatch(": " + piece)
        or _RX_GENERIC_ALONE.match(_key_text(piece.lower()))
    )


def _subtitle_numbers_as_digits(t: str) -> str:
    """Only in the last part that isn't a description ("Sermon One: A
    financial thriller about …"): an earlier part is a name ("The Star
    Trek: Deep Space Nine: The Siege"), and a description's number isn't
    this book's ("… Novel THE MALEVOLENT SEVEN")."""
    pieces = _RX_PART_SEPARATOR.split(t)
    last = max(
        (i for i in range(0, len(pieces), 2) if not _is_description_part(pieces[i])),
        default=0,
    )
    if last == 0:
        return t
    pieces[last] = _RX_SUBTITLE_NUMBER_WORD.sub(
        lambda m: str(_SUBTITLE_NUMBER_WORDS[m.group(1).lower()]), pieces[last],
    )
    return "".join(pieces)


@lru_cache(maxsize=16384)
def parse_title(title: str) -> ParsedTitle:
    """Read the volume evidence out of one title (see the module doc)."""
    written = _RX_MARKETING_PART.sub("", _clean(title))
    t = _subtitle_numbers_as_digits(written)
    prefix = None
    m = _RX_BOOK_PREFIX.match(t)
    if m:
        prefix = float(m.group(1))
        t = t[m.end():]
        written = written[_RX_BOOK_PREFIX.match(written).end():]
    paren_tags = _parenthetical_tags(t)
    proper = _proper_text(t)
    core = _core_volume(proper, paren_tags, prefix)
    sub_tags = _subtitle_tags(proper)
    return ParsedTitle(
        proper=proper,
        tags=tuple(paren_tags) + tuple((name, v) for name, v, _, _ in sub_tags),
        core=core,
        prefix_volume=prefix,
        position_only=(
            core is None and not sub_tags and bool(paren_tags)
            and all(name for name, _ in paren_tags)
        ),
        subtitle_shapes=tuple((name, shape, before) for name, _, shape, before in sub_tags),
        written=_proper_text(written),
    )


def _proper_text(t: str) -> str:
    proper = _RX_PAREN.sub(" ", t)
    proper = re.sub(r"\[[^\]]*\]", " ", proper)
    return re.sub(r"\s+", " ", proper).strip()


@lru_cache(maxsize=16384)
def _parsed(title: str) -> tuple[tuple[Optional[str], bool], ParsedTitle]:
    """(format, parse) of a raw title; the parse ignores light-novel words."""
    return _format(title), parse_title(_RX_LIGHT_NOVEL.sub(" ", title or ""))


# ─── Keys ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Keys:
    """How one title may match another.

    `whole`: the whole title without a generic subtitle, and the title
    with colons as spaces. For a title in parts ("Head: Middle: Tail",
    generic descriptions and bare "Book 3" parts dropped) also `head`
    (the first part) and `tail` (the last: in "Star Trek: New Frontier:
    Blind Man's Bluff" the title is the last part) on their own, when
    they stand as a title (2+ words, the rest is a series name, or the
    volumes already agree; never a series name or an author's label),
    else "". `head_rest` / `tail_rest`: the other parts, for checking
    that what isn't matched agrees.
    """
    whole: frozenset
    head: str
    tail: str
    head_rest: tuple
    tail_rest: tuple


@lru_cache(maxsize=65536)
def _title_keys(
    proper: str,
    series: Optional[str],
    same_core: bool,
    labels: frozenset,
    position_agrees: bool,
    other_series: Optional[str],
    shared_place: str = "",
    named_series: frozenset = frozenset(),
) -> _Keys:
    p = proper.lower()
    # Any length: "It" is a title. The 3-character floor is for parts.
    whole_keys = [
        k for k in (_key_text(_without_description(p)), _key_text(p.replace(":", " ")))
        if k
    ]
    # "Geostorm The Shift: A Post-Apocalyptic …" filed as Geostorm #1 is
    # "The Shift", Geostorm #1: the series name as a prefix with no colon,
    # when both are stored at that place in that series (`shared_place`;
    # a number read from a title isn't enough: "Hulk: Visionaries - Peter
    # David Vol. 4" isn't "X-Factor Visionaries: Peter David, Vol. 4").
    if shared_place:
        for k in list(whole_keys):
            if k.startswith(shared_place + " "):
                rest = re.sub(r"^(?:the|a|an)\s+", "", k[len(shared_place) + 1:]).strip()
                if len(rest) >= 3:
                    whole_keys.append(rest)
            # G229: or as a suffix ("Turning of the Tides Daughters of the
            # High Seas (Book 2)" = "Turning of the Tides", both at #2).
            if k.endswith(" " + shared_place):
                rest = k[:-len(shared_place) - 1].strip()
                if len(rest) >= 3:
                    whole_keys.append(rest)
    whole = frozenset(whole_keys)
    parts = title_parts(proper)
    if len(parts) < 2:
        return _Keys(whole, "", "", (), ())
    # G229: a series either title names in its parentheses counts too
    # ("Invasion (The Autumn War #1)" against "The Autumn War: Volume 1:
    # Invasion" filed as Pinwheel #25).
    names = ({series_key(s) for s in (series, other_series) if s} | named_series) - {""}
    head_text, tail_text = parts[0], parts[-1]
    head_rest, tail_rest = tuple(parts[1:]), tuple(parts[:-1])
    rest_is_series = series_key(" ".join(head_rest)) in names
    front_is_series = series_key(" ".join(tail_rest)) in names

    def usable(k: str) -> bool:
        return (
            len(k) >= 3 and k not in labels and k not in _GENERIC_PARTS
            and (same_core or series_key(k) not in names)
        )

    tail = tail_text if (
        len(tail_text.split()) >= 2 or front_is_series or position_agrees
    ) and usable(tail_text) else ""
    head = head_text if (
        len(head_text.split()) >= 2 or rest_is_series or same_core or position_agrees
    ) and usable(head_text) else ""
    return _Keys(whole, head, tail, head_rest, tail_rest)


@lru_cache(maxsize=65536)
def title_parts(proper: str) -> list[str]:
    """A title proper's parts as keys ("Head: Middle: Tail"), bare "Book 3"
    parts and generic descriptions left out."""
    parts: list[str] = []
    # Trailing descriptions go first, as for the whole title (": LitRPG ,
    # Time Travel , Dystopia" is no title part; split alone it would be).
    for i, segment in enumerate(s for s in _RX_SEGMENTS.split(_without_description(proper.lower())) if s.strip()):
        if i and _RX_BARE_VOLUME.match(segment):
            continue
        text = _key_text(_without_description(segment) if i else segment)
        if text and not (i and _RX_GENERIC_ALONE.match(text)):
            parts.append(text)
    return parts


def _close(x: str, y: str) -> bool:
    """Equal, or a near-identical spelling ("unauthorized" / "unauthorised",
    a truncated title)."""
    if x == y:
        return True
    # "The Voyage of Sable Keech" / "… of the Sable Keech". (Not spaces:
    # "Endgame" and "End Game" are two different books of one author.)
    if min(len(x), len(y)) >= 6 and _without_articles(x) == _without_articles(y):
        return True
    if min(len(x), len(y)) < 8:
        return False
    m = SequenceMatcher(None, x, y)
    if not (m.real_quick_ratio() >= 0.92 and m.quick_ratio() >= 0.92 and m.ratio() >= 0.92):
        return False
    # Word by word when the words line up: one different word is another
    # title ("… South Island" / "… North Island", "Clockwork Prince" /
    # "… Princess"), a misspelt one isn't ("Countrey", "Jorneyman").
    xs, ys = x.split(), y.split()
    if len(xs) == len(ys):
        return all(a == b or SequenceMatcher(None, a, b).ratio() >= 0.88 for a, b in zip(xs, ys))
    # Different word counts: only a title cut short ("… of the Sable Ke");
    # extra words in front are another title ("I, Q: Star Trek The Next
    # Generation" isn't "Star Trek The Next Generation").
    if x.replace(" ", "") == y.replace(" ", ""):
        return True                        # "Counterstrike" / "Counter Strike" (8+ letters)
    short, long_ = (xs, ys) if len(xs) < len(ys) else (ys, xs)
    return long_[:len(short) - 1] == short[:-1] and long_[len(short) - 1].startswith(short[-1])


def _without_articles(k: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\b(?:the|a|an)\b", " ", k)).strip()


def _rests_agree(x: tuple, y: tuple) -> bool:
    """The parts not matched: missing on a side, the same, or — one part
    each — one inside the other ("Dean Koontz's Frankenstein" /
    "Frankenstein"). "Star Trek DS9 - The Siege" and "… - Wrath of the
    Prophets" share a head but these disagree; so do "Star Trek: New
    Frontier" and "Star Trek: New Frontier: Blind Man's Bluff"."""
    if not x or not y:
        return True
    if len(x) == 1 and len(y) == 1:
        a, b = x[0], y[0]
        return _close(a, b) or f" {a} " in f" {b} " or f" {b} " in f" {a} "
    return len(x) == len(y) and all(_close(a, b) for a, b in zip(x, y))


_SMALL_WORDS = frozenset({"the", "a", "an", "of", "and", "&"})


def _same_words(x: str, y: str) -> bool:
    """The same words, small ones aside ("tales from mos eisley cantina" =
    "tales from the mos eisley cantina"; "lost dragon" isn't "lost
    dragons")."""
    return x == y or [w for w in x.split() if w not in _SMALL_WORDS] == [w for w in y.split() if w not in _SMALL_WORDS]


def _titles_match(ka: _Keys, kb: _Keys, spelling: bool = True) -> bool:
    """`spelling`: a near-miss spelling counts ("Alvin Jorneyman"); off
    across two stored series, where only small words may differ ("The Lost
    Dragon" in Cycle of Dragons is not "Lost Dragons" in The Shadow's
    Dragon, wave D)."""
    near = _close if spelling else _same_words
    if any(near(x, y) for x in ka.whole for y in kb.whole):
        return True
    # One side's head or tail is the other side's whole title:
    # "Dragon Bound" = "Dragon Bound: A Legacy Building Fantasy".
    for ends, whole in (((ka.head, ka.tail), kb.whole), ((kb.head, kb.tail), ka.whole)):
        if any(near(e, w) for e in ends if e for w in whole):
            return True
    # Part to part, only when the other parts agree.
    for x, x_rest in ((ka.head, ka.head_rest), (ka.tail, ka.tail_rest)):
        if not x:
            continue
        for y, y_rest in ((kb.head, kb.head_rest), (kb.tail, kb.tail_rest)):
            if y and near(x, y) and _rests_agree(x_rest, y_rest):
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


def _contains_words(inner: str, outer: str) -> bool:
    return bool(inner and outer) and f" {inner} " in f" {outer} "


_RX_HEAD_BOOK = re.compile(r"(?i)^(.*?),?\s+book\s+#?\s*" + _NUMBER_TOKEN + r"\s*$")


def _core_is_position(parsed: ParsedTitle, my_series: Optional[str]) -> bool:
    """G228, F29's mirror: the title's own number says where the book sits
    in the series it's filed in, beside a real title: "Rise of the
    Infernal Paladin, Book Three: Infernal Liberation" and "Zero Point:
    The Owner: Book Two" (filed in "Owner Trilogy"). Only "Book"; "86—
    EIGHTY-SIX, Vol. 7: Mist" stays a volume."""
    if parsed.core is None or parsed.prefix_volume is not None:
        return False
    if any(name is None for name, _ in parsed.tags):
        return False                       # "(Book 2)" belongs to the title
    stored = series_key(my_series)
    if not stored:
        return False
    segments = [s for s in _RX_SEGMENTS.split(parsed.proper) if s.strip()]
    if len(segments) < 2:
        return False

    def names_stored(text: str) -> bool:
        k = series_key(text)
        return bool(k) and (k == stored or _contains_words(k, stored))

    def real(text: str) -> bool:
        k = _key_text(_without_description(text.lower()))
        return len(k) >= 3 and not _RX_GENERIC_ALONE.match(k) and not _contains_words(k, stored)

    m = _RX_HEAD_BOOK.match(segments[0])
    if m and names_stored(m.group(1)) and any(real(s) for s in segments[1:]):
        return True                        # "Series, Book N: Title"
    return (                               # "Title: Series: Book N"
        len(segments) >= 3 and bool(_RX_BARE_VOLUME.match(segments[-1]))
        and segments[-1].strip().lower().startswith("book")
        and names_stored(segments[-2]) and real(segments[0])
    )


def _numbers_are_positions(parsed: ParsedTitle, my_series: Optional[str]) -> bool:
    """Every number this title writes says where the book sits, not which
    book it is: "(Series Book N)" parentheticals, or (F29) a subtitle
    naming a series beside a real title. "Book N of [the] Series" always
    qualifies ("The Well of Ascension: Book Two of Mistborn"); "Series
    Book N" / "Series, Book N" only when the book is stored in that
    series ("Sua Sponte: Forgotten Ruin, Book 9" in Forgotten Ruin; in
    "Apocalypse: Regression, Book 8", stored in "Apocalypse: Regression",
    it is book 8). The real title is a part before the subtitle that isn't
    inside the stored series (a title that is only the named series is
    `_no_real_title`'s case, checked first).
    """
    if parsed.position_only:
        return True
    if parsed.core is not None and _core_is_position(parsed, my_series):
        return all(shape for _, shape, _ in parsed.subtitle_shapes)
    if parsed.core is not None or not parsed.subtitle_shapes:
        return False
    stored = series_key(my_series)
    for name, shape, before in parsed.subtitle_shapes:
        named = series_key(name)
        if not shape or not named or (shape == "named" and named != stored):
            return False
        if not any(
            len(k) >= 3 and not _RX_GENERIC_ALONE.match(k)
            and not _contains_words(k, stored)
            for k in (_key_text(p.lower()) for p in before)
        ):
            return False
    return True


def _lone_number_differs(
    mine: set[float], parsed: ParsedTitle, my_series: Optional[str],
    other_series: Optional[str], other_index: Optional[float],
) -> bool:
    """A written number on this side only: does it make them different?

    It must appear as the other side's index, unless that index is in a
    differently named series. When the other side gives no index at all,
    the titles decide if this side's numbers are only series positions
    (`_numbers_are_positions`), or the other side names one of this
    side's tagged series — as long as this side's title isn't just a
    series name.
    """
    big = mine - _UNNUMBERED
    if not big:
        return False
    tag_names = [s for s, _ in parsed.tags if s]
    my_names = {series_key(s) for s in tag_names}
    if my_series:
        my_names.add(series_key(my_series))
    if other_index is None:
        if _no_real_title(parsed.proper, None) or any(
            _no_real_title(parsed.proper, s) for s in [*tag_names, other_series] if s
        ):
            return True
        tagged = {series_key(s) for s in tag_names}
        return not (
            _numbers_are_positions(parsed, my_series)
            or (other_series and series_key(other_series) in tagged)
        )
    if other_index in big:
        return False
    if other_series and series_key(other_series) not in my_names:
        return False   # a reading-order meta-series index: the titles decide
    return True


# Any number or volume word in a title proper (outside its parentheticals):
# such a title may name its volume in a way the parse doesn't read as one
# ("Incredible Hulk Visionaries - Peter David, Vol. 3", "JSA: Strange
# Adventures #4", "Dune: House Harkonnen, Volume Three"), so G213 leaves it
# to the number rules.
_RX_TITLE_NUMBER = re.compile(
    r"\d|\b(?:book|books|volume|volumes|vol|part|parts|issue|episode|chapter|arc|tome|season|no)\b",
    re.IGNORECASE,
)


# `author_labels` marks a title the author writes with several numbers.
_NUMBERED = "#numbered:"


def _plain_key(proper: str) -> str:
    """The title proper without descriptions and punctuation, every word and
    number kept ("Halo: Evolutions, Volume II" isn't "… Volume I")."""
    t = re.sub(r"[^\w\s]", " ", _without_description(proper.lower()))
    t = re.sub(r"^\s*(?:the|a|an)\s+", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _one_title(
    pa: ParsedTitle, pb: ParsedTitle, series_a: Optional[str], series_b: Optional[str],
    labels: frozenset,
) -> bool:
    """G213: both titles' own text is one real title, so where a source
    puts the book in its series doesn't make it another book. Amazon and
    Calibre number Halo, Pinwheel or "Mistborn: Secret History" (#8 vs
    #3.5) differently, and the old merge left rows one place off ("Sword of
    the Legion" stored at Galaxy's Edge #6). Only position numbers are
    overridden: a number of the title's own ("Splinter Angel: Book 3", a
    Hardcover "Book 06.0 -") still decides, and a placeholder, a bare series
    name or one of the author's labels is no title."""
    if pa.prefix_volume is not None or pb.prefix_volume is not None:
        return False
    # A number of the title's own, written in its parentheses: "Roommate
    # Control (Part Four)" isn't "(Part Three)".
    if pa.core is not None or pb.core is not None:
        return False
    wa, wb = pa.written or pa.proper, pb.written or pb.proper   # as spelled (G226)
    if _RX_TITLE_NUMBER.search(wa) or _RX_TITLE_NUMBER.search(wb):
        return False
    ka, kb = _plain_key(wa), _plain_key(wb)
    if not ka or ka != kb or not _key_text(ka) or _key_text(ka) in labels or _NUMBERED + ka in labels:
        return False
    return not (_no_real_title(wa, series_a) or _no_real_title(wb, series_b)
                or _no_real_title(wa, series_b) or _no_real_title(wb, series_a))


def same_book(
    a: TitleRef, b: TitleRef, labels: frozenset = frozenset(), *, positions_decide: bool = False,
) -> bool:
    """True when the two titles name the same book (ADR-0028).

    `labels` is the author's label set (`author_labels`): title parts
    that never identify a book on their own. `positions_decide`: series
    positions separate even one title (no G213), for the grab side, where
    a wrong "same" skips a wanted volume as owned (C35).
    """
    fmt_a, pa = _parsed(a.title or "")
    fmt_b, pb = _parsed(b.title or "")
    if a.dramatised:
        fmt_a = (fmt_a[0], True)
    if b.dramatised:
        fmt_b = (fmt_b[0], True)
    if fmt_a != fmt_b:
        return False
    if not positions_decide and _one_title(pa, pb, a.series, b.series, labels):
        return True
    if _cut_off_volume(pa, pb, a, b):
        return False
    if _numbered_apart(a, b) and not _same_written(pa, pb):
        return False
    position_agrees = _volumes_agree(a, b, pa, pb)
    if position_agrees is None:
        return False

    # Titles.
    same_core = pa.core is not None and pa.core == pb.core
    ia, ib = _as_index(a.index), _as_index(b.index)
    shared_place = (
        series_key(a.series)
        if a.series and ia is not None and ia == ib and series_key(a.series) == series_key(b.series)
        else ""
    )
    named = frozenset(series_key(n) for n, _ in (*pa.tags, *pb.tags) if n)
    ka = _title_keys(pa.proper, a.series, same_core, labels, position_agrees, b.series, shared_place, named)
    kb = _title_keys(pb.proper, b.series, same_core, labels, position_agrees, a.series, shared_place, named)
    spelling = _placements_agree(a, b)
    if _titles_match(ka, kb, spelling):
        return True
    # G226: also as written ("Round One" = "Mixed Martial Cultivator: Round
    # One", whose "One" was read as 1); the numbers were compared above.
    wa, wb = pa.written or pa.proper, pb.written or pb.proper
    if (wa, wb) != (pa.proper, pb.proper) and _titles_match(
        _title_keys(wa, a.series, same_core, labels, position_agrees, b.series, shared_place, named),
        _title_keys(wb, b.series, same_core, labels, position_agrees, a.series, shared_place, named),
        spelling,
    ):
        return True
    # G227: spaces don't count when the placements can't disagree ("The War
    # Ship", no series, = "The Warship"; "Endgame" and "End Game" in two
    # series stay two books).
    if _placements_agree(a, b):
        joined_a = {k.replace(" ", "") for k in ka.whole}
        if any(len(k) >= 7 for k in joined_a & {k.replace(" ", "") for k in kb.whole}):
            return True
    return position_agrees and (
        _no_real_title(pa.proper, a.series) or _no_real_title(pb.proper, b.series)
    )


# "Secrets of the Silent Witch, Vol. ": a volume whose number the source
# lost.
_RX_CUT_OFF_VOLUME = re.compile(r"(?i)(?:[,:]|\s[-–])\s*vol(?:ume)?\.?\s*$")


def _cut_off_volume(pa: ParsedTitle, pb: ParsedTitle, a: TitleRef, b: TitleRef) -> bool:
    """One title ends in a volume word with no number and neither writes
    one: some volume of the series, not the plain-titled book (wave D, the
    light novel's "Vol. " filed at 4.5 isn't "Secrets of the Silent
    Witch"), unless both are filed at one place in one series."""
    if pa.core is not None or pb.core is not None:
        return False
    if bool(_RX_CUT_OFF_VOLUME.search(pa.proper)) == bool(_RX_CUT_OFF_VOLUME.search(pb.proper)):
        return False
    ia, ib = _as_index(a.index), _as_index(b.index)
    return not (a.series and ia is not None and ia == ib and series_key(a.series) == series_key(b.series))


def _numbered_apart(a: TitleRef, b: TitleRef) -> bool:
    """Both filed at a number, in two unrelated series, at two numbers
    ("Final Days: Colony", Final Days #2, and "The Colony", The Survivors
    #17). Not one series written two ways ("Towerbound (LITRPG + REGRESSOR +
    OPMC)" and "Towerbound"; "Star Force" and "Star Force Universe")."""
    ia, ib = _as_index(a.index), _as_index(b.index)
    if not (a.series and b.series and ia is not None and ib is not None and ia != ib):
        return False
    sa, sb = (series_key(re.sub(r"\([^)]*\)", " ", n)) for n in (a.series, b.series))
    return not (sa == sb or sa.startswith(sb + " ") or sb.startswith(sa + " "))


def _same_written(pa: ParsedTitle, pb: ParsedTitle) -> bool:
    """The two titles as written are one, descriptions and parentheticals
    aside ("Star Force: Termination" = "Star Force: Termination (SF12)")."""
    def key(p: ParsedTitle) -> str:
        return _key_text(_without_description((p.written or p.proper).lower()))
    return bool(key(pa)) and key(pa) == key(pb)


def _placements_agree(a: TitleRef, b: TitleRef) -> bool:
    """One side has no series, or both are in one series (two places in it
    were already told apart by the numbers)."""
    return not (a.series and b.series) or series_key(a.series) == series_key(b.series)


def position_only_match(a: TitleRef, b: TitleRef) -> bool:
    """One title is only its series name (or a placeholder) and the two
    titles differ: `same_book` could only have joined them by series
    position (rule 5). The owned "Dune" filed as Dune #1 and "Dune: A
    Whisper of Caladan Seas" a source filed there are two books; an owned
    book takes in such a row only when the titles themselves match."""
    pa, pb = parse_title(a.title or ""), parse_title(b.title or "")
    # Neither title writes a number: only stored series numbers joined them
    # ("He Who Fights With Monsters 4" is "…, Book 4: Home", and the bare
    # "He Who Fights with Monsters" is "…, Book 1: Outworlder").
    if not (_no_real_title(pa.proper, a.series) or _no_real_title(pb.proper, b.series)):
        return False
    if any(p.core is not None or p.tags for p in (pa, pb)):
        return False
    ka = _key_text(_without_description(pa.proper.lower()))
    kb = _key_text(_without_description(pb.proper.lower()))
    return ka != kb


def different_edition_or_volume(a: TitleRef, b: TitleRef) -> bool:
    """`same_book`'s format and number rules alone (rules 1–3): another
    format (comic, dramatised) or another volume, whatever the titles.

    Enrichment's veto beside its fuzzy title score (G189): the score gave
    Hardcover's "My Dungeon Life: Volume 1" 1.00 for "My Dungeon Life Vol
    2". The title rules stay out of it — the score judges titles, and the
    identity's keys miss a description with no separator ("Aurora Rises A
    LitRPG Adventure").
    """
    fmt_a, pa = _parsed(a.title or "")
    fmt_b, pb = _parsed(b.title or "")
    if (fmt_a[0], fmt_a[1] or a.dramatised) != (fmt_b[0], fmt_b[1] or b.dramatised):
        return True
    return _volumes_agree(a, b, pa, pb) is None


def _volumes_agree(
    a: TitleRef, b: TitleRef, pa: ParsedTitle, pb: ParsedTitle,
) -> Optional[bool]:
    """None when the numbers written in the titles, or their indexes,
    make them different volumes (rules 2 and 3); else whether a series
    position is shared."""
    a_index, b_index = _as_index(a.index), _as_index(b.index)
    na = {v for _, v in pa.tags} | ({pa.core} if pa.core is not None else set())
    nb = {v for _, v in pb.tags} | ({pb.core} if pb.core is not None else set())

    # Numbers written in either title, whatever series they name.
    if na and nb and not (na & nb):
        return None
    if na and not nb and _lone_number_differs(na, pa, a.series, b.series, b_index):
        return None
    if nb and not na and _lone_number_differs(nb, pb, b.series, a.series, a_index):
        return None

    # Volumes within one series name.
    sa = _series_numbers(pa.tags, a.series, a_index, pa.core)
    sb = _series_numbers(pb.tags, b.series, b_index, pb.core)
    if pa.core is not None and pb.core is not None and pa.core != pb.core:
        return None
    if (pa.core is None) != (pb.core is None):
        v = pa.core if pa.core is not None else pb.core
        other = sb if pa.core is not None else sa
        other_written = nb if pa.core is not None else na
        anywhere = set().union(*other.values()) if other else set()
        # G228: a position core ("Series, Book 3: Title") against a copy
        # with no number and no index: the titles decide.
        position = (
            _core_is_position(pa, a.series) if pa.core is not None else _core_is_position(pb, b.series)
        ) and not other_written and (b_index if pa.core is not None else a_index) is None
        if v != 1.0 and v not in anywhere and v not in other_written and not position:
            return None
    shared = [k for k in sa if k and k in sb]
    if any(not (sa[k] & sb[k]) for k in shared):
        return None
    return any(sa[k] & sb[k] for k in shared)


def author_labels(titles: Iterable[str]) -> frozenset:
    """Title parts one author uses beside 3+ different other parts.

    "Star Wars Legends" in "Crucible: Star Wars Legends", "Apocalypse:
    Star Wars Legends", …: a franchise label, not a title. An author's
    own duplicate rows of one book ("Dune: House Harkonnen" ×3) don't
    make "Dune" a label; three different books do.
    """
    partners: dict[str, set[str]] = defaultdict(set)
    for t in set(titles):
        parsed = parse_title(t or "")
        parts = [
            _key_text(x)
            for x in _RX_SEGMENTS.split((parsed.written or parsed.proper).lower())
        ]
        if len(parts) < 2:
            continue
        for i in (0, len(parts) - 1):
            if parts[i]:
                partners[parts[i]].add(
                    " ".join(p for j, p in enumerate(parts) if j != i)
                )
    labels = {k for k, rest in partners.items() if len(rest) >= 3}
    # G213's guard: a title this author writes with 2+ different numbers
    # ("Blade Angels", "Blade Angels (Blade Academy Book 2)", "… Book 3":
    # one title for every book of the series) is told apart by its numbers.
    numbers: dict[str, set[float]] = defaultdict(set)
    for t in set(titles):
        p = parse_title(t or "")
        k = _plain_key(p.written or p.proper)
        if k:
            numbers[k] |= {v for _, v in p.tags} | ({p.core} if p.core is not None else set())
    labels |= {_NUMBERED + k for k, vals in numbers.items() if len(vals) >= 2}
    return frozenset(labels)


# ─── Series written in a title (G171) and Hardcover's prefix ──

_RX_RAW_BOOK_PREFIX = re.compile(r"(?i)^\s*book\s+(\d+(?:\.\d+)?)\s*[-\u2013\u2014:]\s*")


def book_prefix(title: str) -> tuple[str, Optional[float]]:
    """Hardcover's "Book 06.0 - Blood Rites" -> ("Blood Rites", 6.0);
    any other title -> (title, None)."""
    m = _RX_RAW_BOOK_PREFIX.match(title or "")
    if not m or not title[m.end():].strip():
        return title, None
    return title[m.end():].strip(), float(m.group(1))


def series_in_title(title: str, known: Iterable[str] = ()) -> Optional[tuple[str, float]]:
    """The series and number a title names when its source gave none
    (F14 / G171): "Title (Series Book N)", "(Series #N)", "(Series, Book
    N)" (the last such parenthetical), or "Series: Book N". A number is
    required; "(Book 2)" alone names no series.

    `known`: the author's series. A "Head: Book N" head that ends with one
    of them (2+ words) names that series (G232): "Treachery at Martinique
    Isle Daughters of the High Seas: Book 1" is Daughters of the High Seas
    #1, not a series named after the whole head."""
    t = _clean(title)
    named = [(name, v) for name, v in _parenthetical_tags(t) if name]
    if named:
        return named[-1]
    proper = re.sub(r"\s+", " ", _RX_PAREN.sub(" ", t)).strip()
    segments = _RX_SEGMENTS.split(proper)
    if len(segments) == 2 and segments[0].strip():
        m = _RX_BARE_VOLUME.match(segments[1])
        if m:
            v = _match_number(m)
            if v is not None:
                return _known_series_ending(segments[0].strip(), known), v
    return None


def _known_series_ending(head: str, known: Iterable[str]) -> str:
    key = series_key(head)
    ends = [
        k for k in known
        if k and len(series_key(k).split()) >= 2
        and series_key(k) != key and key.endswith(" " + series_key(k))
    ]
    if not ends or any(series_key(k) == key for k in known):
        return head
    return max(ends, key=lambda k: len(series_key(k)))


# ─── An index of one author's titles ──────────────────────────

_INDEX_STOPWORDS = frozenset({
    "the", "a", "an", "of", "and", "in", "to", "on", "for", "with", "at",
    "by", "from", "or", "is",
})


class TitleIndex:
    """Titles to look a new title up against, `same_book` deciding.

    `same_book` runs only on candidates: titles sharing a title word (or
    the first 4 letters of one, for spelling variants) or a series
    position with the title looked up. Every title `same_book` can call
    the same shares one of those, so this finds what checking every
    title would, at a fraction of the cost on a big catalogue.
    """

    def __init__(self, labels: frozenset = frozenset()):
        self.labels = labels
        self._entries: list[tuple[TitleRef, object]] = []
        self._by_token: dict[str, list[int]] = defaultdict(list)

    @staticmethod
    def _tokens(ref: TitleRef) -> set[str]:
        _, parsed = _parsed(ref.title or "")
        tokens: set[str] = set()
        words = _key_text(parsed.proper.lower()).split()
        words += [w for w in _key_text((parsed.written or parsed.proper).lower()).split() if w not in words]
        for word in words:
            if word in _INDEX_STOPWORDS:
                continue
            tokens.add(word)
            if len(word) >= 5:
                tokens.add("~" + word[:4])
        # G227: "The War Ship" finds "The Warship" by the joined words.
        for left, right in zip(words, words[1:]):
            if len(left + right) >= 5:
                tokens.add("~" + (left + right)[:4])
        numbers = _series_numbers(
            parsed.tags, ref.series, _as_index(ref.index), parsed.core,
        )
        for name, values in numbers.items():
            for v in values:
                tokens.add(f"#{name}#{v}")
        return tokens

    def add(self, ref: TitleRef, item: object = None) -> None:
        position = len(self._entries)
        self._entries.append((ref, item))
        for token in self._tokens(ref):
            self._by_token[token].append(position)

    def find_all(self, ref: TitleRef) -> list[object]:
        """Items whose titles are the same book as `ref`, in the order
        they were added."""
        seen: set[int] = set()
        for token in self._tokens(ref):
            seen.update(self._by_token.get(token, ()))
        return [
            self._entries[i][1] for i in sorted(seen)
            if same_book(ref, self._entries[i][0], self.labels)
        ]

    def __len__(self) -> int:
        return len(self._entries)


__all__ = [
    "ParsedTitle",
    "TitleIndex",
    "TitleRef",
    "author_labels",
    "book_prefix",
    "parse_title",
    "position_only_match",
    "same_book",
    "series_in_title",
    "series_key",
    "title_parts",
]
