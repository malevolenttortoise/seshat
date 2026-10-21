"""Discovery filters: what a scan finds that isn't a book to list.

A book a source returns can be a comic issue, a box set, an anthology, a
foreign edition, a part of a book, a sampler, a seller's listing, a study
guide, something that isn't a book at all, or a title that isn't a title.
Until the 2026-10 discovery audit the merge skipped a few of these (book
sets, "Series #3" titles, foreign-looking titles) and listed the rest as
missing books: ~2,100 of 9,272 missing rows were one of these families.

Since the fix pass (ADR-0029) the merge creates them **hidden, with the
family as `books.hidden_reason`**, instead of skipping them: later scans and
the Goodreads worker recognise them, the Hidden page shows them apart from
the books you hid, "Show" on one sticks (reason `shown`), and the comic and
box-set families follow their settings (D2 / D10: on by default; turned off,
their rows show at once, no re-scan).

`filter_reason()` is the one verdict: the first family that matches, in
`FAMILIES` order. Every rule here is held to (almost) no match among the
reference install's 3,775 owned titles; the families where you do own real
members (box sets, anthologies, manga) are the exceptions, and they are
right there too.

Books also match only within their family (G197): a hidden comic row is
never "the same book" as an incoming prose title, so it can't absorb the
real book on a later scan, and an incoming filtered book never merges into
a book you'd list. `book_class()` gives a stored row's family for that check.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

# ─── Reasons (stored in books.hidden_reason) ─────────────────

COMIC = "comic"
BOX_SET = "box_set"
ANTHOLOGY = "anthology"
FOREIGN = "not_english"
SPLIT = "split"
SAMPLER = "sampler"
LISTING = "listing"
STUDY_GUIDE = "study_guide"
NOT_A_BOOK = "not_a_book"
JUNK = "junk"
# Not families: Mark's "Show" on a filtered row, and S5's "list missing
# books" switch turned off for every contributor.
SHOWN = "shown"
AUTHOR_OFF = "author_off"

# F11: a book under this many pages is badged "short" (computed on read,
# C8; the frontend's `SHORT_UNDER_PAGES` is the same number).
SHORT_UNDER_PAGES = 100

FAMILIES: tuple[str, ...] = (
    FOREIGN, JUNK, NOT_A_BOOK, STUDY_GUIDE, COMIC, BOX_SET, SPLIT, SAMPLER,
    ANTHOLOGY, LISTING,
)
FILTER_REASONS = frozenset(FAMILIES)

# Families with a setting that lets them through (F17): shown when it's off.
SETTING_FOR = {COMIC: "hide_comics", BOX_SET: "hide_box_sets"}


def hides(reason: Optional[str], settings: dict) -> bool:
    """A row filtered for `reason` is hidden under these settings."""
    if reason not in FILTER_REASONS:
        return False
    key = SETTING_FOR.get(reason)
    return True if key is None else bool(settings.get(key, True))


# ─── Family rules (titles) ───────────────────────────────────

# Light novels are prose (F12): never a comic, whatever else the title says.
_RX_LIGHT_NOVEL = re.compile(r"(?i)\blight\s+novel\b")

_RX_COMIC = re.compile(
    # "comic" alone is often an adjective ("a darkly comic fantasy").
    r"(?i)\b(?:manga|graphic\s+novels?|comics|comic\s+(?:books?|series|strips?|collection|issues?))\b|\(\s*comic\s*\)"
    r"|#\d+\s*\(of\s*\d+\)"                       # "#2 (of 5)": an issue of a run
    r"|\(\d{4}\s*[-–]\s*(?:\d{4}|present)?\s*\)"  # "Lucifer (2015-2017) #1": a run's years
    r"|\(\d{4}\)\s*#\d+"                          # "Spider-Gwen (2015) #3"
    r"|\bTPB\b|\bOGN\b|\bomnibus\s+HC\b"
    r"|\bannual\s+#?\d"
    r"|\bissues?\s+#\d"
    r"|\b(?:marvel|dc\s+comics|idw|dark\s+horse|image\s+comics|boom!?\s+studios|dynamite)\b.*#\d"
)
# A superhero's name: almost always a comic. A prose tie-in that says so
# ("Spider-Man 3: A Novelization") is let through; one that doesn't (Jim
# Butcher's "Spider-Man: The Darkest Hours") is hidden as a comic and shows
# under Hidden → Filtered (C23).
_RX_HERO = re.compile(
    r"(?i)\b(?:spider-man|x-men|x-factor|she-hulk|hulk|batman|aquaman|supergirl|superman|avengers|wolverine|deadpool)\b"
)
_RX_PROSE_TIE_IN = re.compile(r"(?i)(?<!graphic\s)\bnovel(?:i[sz]ation)?s?\b")

_RX_BOX_SET = re.compile(
    r"(?i)\bbox(?:ed)?\s*set\b|\bboxset\b|\bomnibus\b|\bcompilation\b"
    r"|\bbooks?\s+#?\d+\s*(?:[-–&+]|to|and|through)\s*#?\d+"   # "Books 1-3", "Book 1 & 2"
    r"|\bvolumes?\s+\d+\s*[-–&]\s*\d+"
    r"|\b\d+[-\s]book\s+(?:collection|set|bundle|omnibus|series)\b"
    r"|\b(?:two|three|four|five|six|seven|eight|nine|ten)[-\s]book\s+(?:collection|set|bundle|box|omnibus|series)\b"
    r"|\b\d+\s+books?\s+in\s+(?:\d+|one)\b"
    r"|\bcomplete\s+(?:series|saga|collection|trilogy|set)\b"
    r"|\bthe\s+complete\s+\w+(?:\s+\w+)?\s+(?:series|saga|trilogy|collection)\b"
    r"|\bfull\s+series\b|\bseries\s+set\b|\bbook\s+set\b|\bbooks\s+set\b|\bcollection\s+set\b"
    r"|\bset\s+of\s+\d+\b|\b\d+\s+set\)"
    r"|\bbind-?up\b|\bstarter\s+pack\b|\bbook\s+pack\b|\bbundle\b"
    r"|\btrilogy\b(?!\s*[,:(]?\s*(?:book|#|vol|volume|part|\d))"
)
# "… Conclusion to the Last Cyborg Trilogy", "Book 2 of the X Trilogy": a
# book in a trilogy, not the set.
_RX_IN_A_TRILOGY = re.compile(
    r"(?i)\b(?:of|to|in|from|for)\s+the\s+(?:[\w'-]+\s+){0,5}trilogy\b"
    r"|\([^)]*trilogy[^)]*$"                       # "(The Land of Nowhere Trilogy)": its series
    r"|:\s*(?:the\s+)?(?:[\w'-]+\s+){0,5}trilogy\b"  # "Otherlife Dreams: The Selfless Hero Trilogy"
)
# Two or more titles joined by " / " or two ";": several books in one listing.
_RX_JOINED_TITLES = re.compile(r"\S\s+/\s+\S")

_RX_ANTHOLOGY = re.compile(
    r"(?i)\banthology\b|\banthologies\b|\byear'?s\s+best\b|\bbest\s+(?:new\s+)?(?:science\s+fiction|sf|fantasy|horror|short\s+stories)\b"
    r"|\bmagazine\b|\bedited\s+by\b|\bstories\s+(?:from|of|by|in)\b|\bshort\s+stories\b"
    r"|\bcollected\s+(?:stories|fiction|tales|works)\b|\bselected\s+(?:stories|fiction|tales)\b"
    r"|\bnebula\s+awards?\b|\bhugo\s+(?:awards?|winners)\b|\banalog\s+science\b|\basimov'?s\b"
    r"|\b(?:a|an)\s+collection\s+of\b|\b(?:stories|tales)\s+collection\b"
    r"|\b(?:volume|issue|vol\.?)\s*\d+\s*,\s*(?:number|no\.?)\s*\d+"   # "Volume 9, Number 2"
    r"|\bissue\s+#?\d+\s*,\s*(?:january|february|march|april|may|june|july|august|september|october|november|december)\b"
    r"|\b(?:january|february|march|april|may|june|july|august|september|october|november|december|spring|summer|autumn|fall|winter)\s+\d{4}\s*(?:#\d+)?$"
)
_RX_ANTHOLOGY_DESC = re.compile(
    r"(?i)\banthology\b(?!\s+series)|\bedited\s+by\b|\bstories\s+by\b|\bfeaturing\s+(?:stories|tales)\s+(?:by|from)\b"
    r"|\bcollection\s+of\s+(?:\w+\s+){0,3}(?:stories|tales|novellas|novelettes)\b"
)
# A standalone story's blurb names the collection it came from ("originally
# published in the anthology On the Prowl", "part of Forward, a collection
# of six stories"): that isn't this book.
_RX_PARENT_COLLECTION = re.compile(
    r"(?i)\b(?:part\s+of|published\s+in|appeared\s+in|appears\s+in|included\s+in|featured\s+in|"
    r"from\s+the|creator\s+of|editor\s+of|contributor\s+to|originally\s+in)\b[^.!?]{0,60}$"
)


def _description_says_anthology(description: str) -> bool:
    for m in _RX_ANTHOLOGY_DESC.finditer(description or ""):
        if not _RX_PARENT_COLLECTION.search(description[max(0, m.start() - 80):m.start()]):
            return True
    return False
ANTHOLOGY_MIN_AUTHORS = 4

_RX_SPLIT = re.compile(
    r"(?i)\bpart\s+\d+\s+of\s+\d+\b|\bvol(?:ume)?\.?\s*\d+\s+of\s+\d+\b"
    r"|[,:]\s*chapters?\s+\d+(?:\s*[-–]\s*\d+)?\s*(?:\([^)]*\)?\s*)?$"
    r"|\bseason\s+\d+,?\s+episode\s+\d+\b|\bepisode\s+\d+\s+of\s+\d+\b|\bdisc\s+\d+\b|\bdisk\s+\d+\b"
)
_RX_SAMPLER = re.compile(
    r"(?i)\bexcerpt\b|\bsampler\b|\bsneak\s+peek\b|\bfree\s+(?:sample|preview|chapters?)\b"
    r"|\bpreview\b|\bfirst\s+\d+\s+chapters\b|\b(?:chapter|book)\s+teaser\b|\bteaser\s+(?:chapter|excerpt)\b"
    r"|\bbonus\s+(?:chapter|content)\b"
)
_RX_LISTING = re.compile(
    r"(?i)\[(?:[^\]]*\b(?:paperback|hardcover|hardback|mass\s+market|audio\s*cd|audiobook|unabridged|abridged|kindle|ebook|large\s+print)\b[^\]]*)\]?"
    r"|\((?:paperback|hardcover|hardback|mass\s+market\s+paperback|mass\s+market|audio\s*cd|large\s+print)\)"
    r"|\bsigned\b|\bunabridged\s+cd\b|\bcd\s+audiobook\b|\b\d+(?:st|nd|rd|th)\s+edition\b"
    r"|\*\s*[^*]*edition\s*\*"
    r"|\b(?:special|collector'?s|deluxe|limited|gift|anniversary|book\s+club|mass\s+market|large\s+print|movie\s+tie-in|tie-in)\s+edition\b"
    r"|\bedition\s+(?:hardcover|paperback)\b|\b(?:hardcover|paperback)\s+edition\b"
    r"|^\s*by\s+[^-–]+[-–]\s+\S"                                    # "By Cassandra Clare - City of …"
    r"|\(\s*author\s*\)"                                             # "BY Derting, Kimberly ( Author )"
    r"|\bby\s+\w+(?:\s+\w\.)*\s+\w+\s+\(\d{4}\)\s+(?:paperback|hardcover)\b"   # "… by Asher, Neal (2013) Paperback"
    r"|,\s*\w+\s*\(\d{4}\)\s+(?:paperback|hardcover)\b"
)
_RX_STUDY_GUIDE = re.compile(
    r"(?i)\bsummary\s+(?:of|and|&)\b|^summary\b|\bstudy\s+guide\b|\bsparknotes\b|\bcliffs\s*notes\b"
    r"|\banalysis\s+of\b|\bkey\s+takeaways\b|\bquiz\s+book\b|\btrivia\b|\bconversation\s+starters\b"
    r"|\breader'?s\s+guide\b|\bteacher'?s\s+guide\b|\blesson\s+plans?\b|\bgradesaver\b|\bbook\s+notes\b"
)
_RX_NOT_A_BOOK = re.compile(
    r"(?i)\bcolou?ring\s+books?\b|\bnotebooks?\b|\bcalendar\b|\bplanner\b|\bposter\s+book\b|\bkeepsake\s+journal\b"
    r"|\bsticker\s+book\b|\bactivity\s+book\b|\bpuzzle\s+book\b|\bcrossword\b|\bsudoku\b"
    r"|\b(?:blank|lined|guided|dot\s+grid)\s+journal\b|\bjournal\s*(?:\(|:)?\s*(?:blank|lined|notebook)\b"
    r"|\b(?:tarot|oracle)\s+(?:deck|cards)\b|\bdeck\s+and\s+guidebook\b|\bcard\s+game\b|\bboard\s+game\b"
    r"|\b(?:fantasy\s+)?role-?playing\s+game\b|(?<!lit\s)(?<!lit-)\brpg\s+(?:sourcebook|supplement|core\s+rulebook|module|adventure)\b"
    r"|\bsourcebook\b|\bcore\s+rule\s*book\b|\bplaytest\b|\bgame\s+master'?s?\s+(?:guide|screen)\b"
    r"|\bpathfinder\s+(?:2e|second\s+edition|roleplaying|rpg|adventure|module)\b|\b(?:ad&d|d&d)\b"
    r"|\bprepack\b|\benamel\s+pins?\b|\bbookmarks?\b|\bpostcards?\b"
)
# "The Expanse #3", "New Novella #2": nothing but a series name before the
# number (a real title with its series, "Organ Donor: Afterlife of Ebon
# Black #1", has a title part first).
_RX_SERIES_REF = re.compile(r"^\s*[^:()\[\]]+?\s+#\d+(?:\.\d+)?\s*$")
_RX_JUNK = re.compile(
    r"^[a-z]+$"                       # "first"
    r"|(?i:^series\s)|(?i:\bglossary\b)|^[A-Z]{1,3}\d+\.\S"   # "AM4.Alvin Journeyman"
    r"|(?i:^untitled\b)|(?i:\buntitled\s*$)|(?i:\btbd\b|\btba\b)"
)

# ─── Foreign titles (the language field is often empty or says English) ─

# Diacritics no English title carries (an "é" in "café" or a name is common
# in English; these pairs aren't). Non-Latin scripts are always foreign.
_RX_FOREIGN_SCRIPT = re.compile(
    r"[\u0370-\u03FF\u0400-\u04FF\u0530-\u058F\u0590-\u05FF\u0600-\u06FF\u0900-\u0DFF\u0E00-\u0E7F"
    r"\u10A0-\u10FF\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF]"
)
_RX_FOREIGN_CHARS = re.compile(r"[şğıİŞĞœœßąęłńśźżćčřšžťďňůőűãõ¿¡]")
# Function words of other languages that English titles don't use as
# words. Each one checked against the owned titles (2026-10-21).
_FOREIGN_WORDS = frozenset("""
der die das den dem des und ein eine einer eines im vom zum zur ins aus auf mit für über nicht de en
le la les du des une et aux sur dans pour avec sans
el los las del y una con por para sin
il lo gli della delle dei degli nel nella sul sulla un'
het een van voor niet ons waarom wat zijn wordt
och av för med
ve ile bir için
o os um uma dos das nas nos ao pela pelo
și din pentru cu
hamvai kapuja háborúja bosszúja przebudzenie ekspansja lewiatana babilon
""".split())
# Words too common in English titles to count alone ("die", "a", "o", "un",
# "le" as in "Le Morte"): they count only beside another foreign word.
_WEAK = frozenset({"die", "o", "le", "la", "el", "il", "un", "den", "des", "et", "van", "av", "ve", "y", "con", "sin", "dos", "do", "de", "en"})
# A hyphenated compound is one word ("Para-Military" isn't Spanish "para").
_RX_WORD = re.compile(r"[^\W\d_]+(?:['-][^\W\d_]+)*", re.UNICODE)
# Edition words: "Band 3", "Tome 2", "Teil 1", "Libro 4", "Livro 2", "Volumen"
_RX_FOREIGN_EDITION = re.compile(
    r"(?i)\b(?:band|teil|tome|tomo|libro|livro|volumen|coffret|ausgabe|edizione|edición|edição|wydanie|kitap|roman|"
    r"buch|bok|romanzo|novela)\s+\d+\b|\b(?:coffret|edizione|wydanie|broché|taschenbuch|tascabile)\b"
    r"|\w*ausgabe\b"                               # "Gesamtausgabe", "Sonderausgabe"
)
# Romanian / Portuguese / Italian word shapes English doesn't make.
_RX_FOREIGN_SHAPES = re.compile(r"(?i)\b\w+(?:ului|ção|ções|zione|zioni|ské|ského|ość)\b")
_RX_ACCENTS = re.compile(r"[àáâäåèéêëìíîïòóôöùúûüñçșțăÀÁÂÄÅÈÉÊËÌÍÎÏÒÓÔÖÙÚÛÜÑÇ]")
# English function words: a title with as many of these as foreign ones is
# English ("Der Werwolf: The Annals of Veight").
_ENGLISH_WORDS = frozenset("the of and a an to in on for with from at by is my your his her its our their".split())


def is_foreign_title(title: str) -> bool:
    t = title or ""
    if _RX_FOREIGN_SCRIPT.search(t) or _RX_FOREIGN_CHARS.search(t):
        return True
    if _RX_FOREIGN_EDITION.search(t):
        return True
    words = [w.lower() for w in _RX_WORD.findall(t)]
    hits = [w for w in words if w in _FOREIGN_WORDS]
    strong = [w for w in hits if w not in _WEAK]
    english = sum(1 for w in words if w in _ENGLISH_WORDS)
    if (strong or len(hits) >= 2) and len(hits) > english:
        return True
    if english == 0 and _RX_ACCENTS.search(t):
        return True          # "L'Empire caché", "Soleils éclatés"; not "The Tides of Bára"
    return bool(_RX_FOREIGN_SHAPES.search(t)) and english == 0 and len(words) <= 6


# ─── The verdict ─────────────────────────────────────────────


def _box_set(t: str) -> bool:
    if _RX_JOINED_TITLES.search(t) or t.count(";") >= 2:
        return True
    for m in _RX_BOX_SET.finditer(t):
        if m.group(0).lower().startswith("trilogy") and _RX_IN_A_TRILOGY.search(t[:m.end()]):
            continue
        return True
    return False


def _head(title: str) -> str:
    """The title before its first subtitle, tagline or parenthetical: a
    superhero named in a tagline ("The Malevolent Seven: 'Terry Pratchett
    meets Deadpool'") doesn't make the book a comic."""
    return re.split(r"\s*(?::|\s-\s|\(|\"|“)", title, maxsplit=1)[0]


def series_ref_stem(title: str) -> Optional[str]:
    """"The Last Paladin" for "The Last Paladin #7": a title that is only a
    series name and a number (C22: junk, unless the author owns a book of the
    same stem, as with The Last Paladin)."""
    t = (title or "").strip()
    if ":" in t or not _RX_SERIES_REF.match(t):
        return None
    return re.sub(r"\s+#\d+(?:\.\d+)?\s*$", "", t).strip().lower()


def _byline(title: str, authors: Iterable[str]) -> bool:
    """"Abaddon's Gate by James S. A. Corey …": a seller's listing names
    the author in the title."""
    tl = (title or "").lower()
    for name in authors or ():
        n = (name or "").strip().lower()
        if len(n) > 4 and re.search(r"\bby\s+" + re.escape(n) + r"\b", tl):
            return True
        last = n.split()[-1] if n.split() else ""
        if len(last) > 3 and re.search(r"\bby\s+" + re.escape(last) + r",\s*\w", tl):
            return True                  # "by Asher, Neal"
    return False


# A comic issue: "Red Rising: Sons of Ares #2" is 30 pages; "Organ Donor:
# Afterlife of Ebon Black #1", the same shape, is a 230-page novel (C24).
COMIC_ISSUE_MAX_PAGES = 48
_RX_ISSUE_NUMBER = re.compile(r"#\d+\s*$")


def filter_reason(
    title: str,
    *,
    description: Optional[str] = None,
    n_authors: int = 0,
    authors: Iterable[str] = (),
    series_name: Optional[str] = None,
    page_count: Optional[int] = None,
) -> Optional[str]:
    """The family a source's book belongs to, or None for a book to list.

    `n_authors`: the source's credited author-role contributors (an
    anthology has 4+, F7); `authors`: names a seller's listing would put in
    the title; `series_name`: the book's series, a comic's run often named
    only there ("Lucifer, Volume 1: Cold Heaven" in "Lucifer (2015-2017)").
    """
    t = (title or "").strip()
    if not t:
        return JUNK
    if is_foreign_title(t):
        return FOREIGN
    if _RX_JUNK.search(t) or len(t) <= 2:
        return JUNK
    if _RX_NOT_A_BOOK.search(t):
        return NOT_A_BOOK
    if _RX_STUDY_GUIDE.search(t):
        return STUDY_GUIDE
    if not _RX_LIGHT_NOVEL.search(t) and (
            _RX_COMIC.search(t)
            or (_RX_HERO.search(_head(t)) and not _RX_PROSE_TIE_IN.search(t))
            or (series_name and _RX_COMIC.search(series_name) and not _RX_LIGHT_NOVEL.search(series_name))
            or (page_count and 0 < page_count <= COMIC_ISSUE_MAX_PAGES and _RX_ISSUE_NUMBER.search(t))):
        return COMIC
    if series_ref_stem(t) is not None:
        return JUNK
    if _box_set(t):
        return BOX_SET
    if _RX_SPLIT.search(t):
        return SPLIT
    if _RX_SAMPLER.search(t):
        return SAMPLER
    if (_RX_ANTHOLOGY.search(t)
            or (description and _description_says_anthology(description))
            or n_authors >= ANTHOLOGY_MIN_AUTHORS):
        return ANTHOLOGY
    if _RX_LISTING.search(t) or _byline(t, authors):
        return LISTING
    return None


def book_class(title: str, *, authors: Iterable[str] = ()) -> Optional[str]:
    """A book's family for matching (G197), read from its title alone, the
    same way for a stored row and an incoming book (C21). What else can mark
    a family depends on the source: a stored row has no contributor list, its
    blurb often names the anthology a standalone story came from, and two
    sources name a comic's series differently ("The Golden Age of the Sith"
    vs "… (1996-1997)") or give no page count. Those signals decide only
    whether a new row is created hidden, never what it matches."""
    return filter_reason(title, authors=authors)


def author_role_count(contributors) -> int:
    """Credited authors on a source's book (translators, narrators and the
    like don't count)."""
    from app.discovery.sources.base import contributor_is_author
    return sum(1 for c in contributors or () if contributor_is_author(getattr(c, "role", None)))


__all__ = [
    "ANTHOLOGY", "AUTHOR_OFF", "BOX_SET", "COMIC", "FAMILIES", "FILTER_REASONS",
    "FOREIGN", "JUNK", "LISTING", "NOT_A_BOOK", "SAMPLER", "SETTING_FOR", "SHOWN",
    "SHORT_UNDER_PAGES", "SPLIT", "STUDY_GUIDE", "author_role_count", "book_class", "filter_reason",
    "hides", "is_foreign_title",
]
