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
from functools import lru_cache
from pathlib import Path
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

# G208: a Goodreads page's genres, most-shelved first. A comic in its top
# two is a comic ("Germ Warfare: A Very Graphic History"); at third it
# also caught a prose novel ("Final Fantasy XIII-2: Fragments After":
# fiction, gaming, manga). A light novel genre anywhere means prose (F12).
_COMIC_GENRES = frozenset({
    "comics", "comic", "comic book", "comic books", "graphic novels", "graphic novel",
    "graphic novels comics", "comics manga", "graphic novels manga", "manga",
    "manhwa", "manhua", "webcomic", "webcomics", "webtoon",
})
_LIGHT_NOVEL_GENRES = frozenset({"light novel", "light novels"})
COMIC_GENRE_TOP = 2


def genres_say_comic(genres: Iterable[str]) -> bool:
    g = [str(x).strip().lower() for x in genres or ()]
    if any(x in _LIGHT_NOVEL_GENRES for x in g):
        return False
    return any(x in _COMIC_GENRES for x in g[:COMIC_GENRE_TOP])

_RX_BOX_SET = re.compile(
    r"(?i)\bbox(?:ed)?\s*set\b|\bboxset\b|\bomnibus\b|\bcompilation\b"
    r"|\bbooks?\s+#?\d+\s*(?:[-–&+]|to|and|through)\s*#?\d+"   # "Books 1-3", "Book 1 & 2"
    r"|\bbooks?\s+\d+(?:\s*,\s*\d+)+(?:\s*,?\s*(?:and|&)\s*\d+)?\b"   # "Books 1, 2, and 3" (G261)
    r"|\bbooks?\s+(?:one|two|three|four|five|six|i{1,3}|iv|v|vi)\s*(?:[-–&+]|to|and|through|thru)\s*(?:books?\s+)?"
    r"(?:two|three|four|five|six|seven|eight|nine|ten|i{1,3}|iv|v|vi{1,3}|ix|x|\d{1,2})\b"   # "Books I-V", "Books One Through Six"
    r"|#\d+(?:\.\d+)?\s*[-–]\s*\d{1,2}(?:\.\d+)?\b"           # "(Alpha & Omega, #0.5-3)" (not "#2-1976")
    r"|\bvolumes?\s+\d+\s*[-–&]\s*\d+"
    r"|\b(?:series|episodes?)\s+\d{1,3}\s*[-–]\s*\d{1,3}\b"   # "Star Force: Origin Series 13-16" (G254)
    r"|\b\d+[-\s]book\s+(?:collection|set|bundle|omnibus|series)\b"
    r"|\b(?:two|three|four|five|six|seven|eight|nine|ten)[-\s]book\s+(?:collection|set|bundle|box|omnibus|series)\b"
    r"|\b\d+\s+books?\s+in\s+(?:\d+|one)\b"
    r"|\bcomplete\s+(?:series|saga|collection|trilogy|set)\b"
    r"|\bthe\s+complete\s+\w+(?:\s+\w+)?\s+(?:series|saga|trilogy|collection)\b"
    r"|\bfull\s+series\b|\bseries\s+set\b|\bbook\s+set\b|\bbooks\s+set\b|\bcollection\s+set\b"
    r"|\bset\s+of\s+\d+\b|\b\d+\s+set\)"
    r"|\bbind-?up\b|\bstarter\s+pack\b|\bbook\s+pack\b|\bbundle\b"
    r"|\b(?:trilogy|duology|quartet|quintet|tetralogy|pentalogy|hexalogy|septology)\b(?!\s*[,:(]?\s*(?:book|#|vol|volume|part|\d))"
)
_SET_NAMES = r"(?:trilogy|duology|quartet|quintet|tetralogy|pentalogy|hexalogy|septology)"
# "… Conclusion to the Last Cyborg Trilogy", "Book 2 of the X Trilogy": a
# book in a trilogy, not the set.
_RX_IN_A_TRILOGY = re.compile(
    r"(?i)\b(?:of|to|in|from|for)\s+(?:the|an?)\s+(?:[\w'-]+\s+){0,5}" + _SET_NAMES + r"\b"   # "Part I of an Epic … Duology"
    r"|\([^)]*" + _SET_NAMES + r"[^)]*$"                       # "(The Land of Nowhere Trilogy)": its series
    r"|:\s*(?:the\s+)?(?:[\w'-]+\s+){0,5}" + _SET_NAMES + r"\b"  # "Otherlife Dreams: The Selfless Hero Trilogy"
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
    r"|\bchapters?\s+\d{1,4}\s*[-–]\s*\d{1,4}\b"                   # "Kumo Desu ga, Nani ka? Chapter 001-050"
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
# "Dracula : by Bram Stoker": a last part that is only a byline, whoever it
# names (that row was Holly Black's). "by" in lower case: a title-case
# subtitle starts "By" ("Learn German: By Reading Urban Fantasy").
_RX_BYLINE_PART = re.compile(r"(?:\s[-–—:]|:)\s*by\s+[A-Z][\w.'’-]*(?:\s+[A-Z][\w.'’-]*){1,3}\s*$")
# "Birds of Prey - Bisexual Edition (Pinwheel Book 20)": an author's other
# cut of a book you own one cut of (G246). Hidden at creation and by the
# cleanup only; matching reads the title without it (`book_class`).
_RX_ORIENTATION_EDITION = re.compile(
    r"(?i)\b(?:bisexual|bi|gay|lesbian|queer|hetero(?:sexual)?|straight)\s+edition\b"
)
# "Backyard Dungeon: UK Edition 7" beside the owned Backyard Dungeon 7 ebook:
# another cut, in the ebook library only (G252; the audiobook library's UK
# Edition audiobooks are the ones Mark collects).
_RX_REGIONAL_EDITION = re.compile(r"(?i)\b(?:uk|u\.k\.|us|u\.s\.)\s+edition\b")
_RX_STUDY_GUIDE = re.compile(
    r"(?i)\bsummary\s+(?:of|and|&)\b|^summary\b|\bstudy\s+guide\b|\bsparknotes\b|\bcliffs\s*notes\b"
    r"|\banalysis\s+of\b|\bkey\s+takeaways\b|\bquiz\s+book\b|\btrivia\b|\bconversation\s+starters\b"
    r"|\breader'?s\s+guide\b|\bteacher'?s\s+guide\b|\blesson\s+plans?\b|\bgradesaver\b|\bbook\s+notes\b"
)
_RX_NOT_A_BOOK = re.compile(
    r"(?i)\bcolou?ring\s+books?\b|\bnotebooks?\b|\bcalendar\b|\bplanner\b|\bposter\s+book\b|\bkeepsake\s+journal\b"
    r"|\bsticker\s+book\b|\bactivity\s+book\b|\bpuzzle\s+book\b|\bcrossword\b|\bsudoku\b|\bword\s*search\b"
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
    # Amazon's own marker, on titles with no foreign word to read ("Stahllied
    # (German Edition)"): 0 owned titles carry one, 497 cached Amazon titles do.
    r"|\(\s*(?:german|french|spanish|italian|portuguese|brazilian\s+portuguese|dutch|polish|russian|"
    r"japanese|chinese|korean|turkish|swedish|norwegian|danish|finnish|czech|hungarian|romanian|"
    r"greek|hebrew|catalan|ukrainian)\s+edition\s*\)"
)
# Romanian / Portuguese / Italian word shapes English doesn't make.
_RX_FOREIGN_SHAPES = re.compile(r"(?i)\b\w+(?:ului|ção|ções|zione|zioni|ské|ského|ość)\b")
_RX_ACCENTS = re.compile(r"[àáâäåèéêëìíîïòóôöùúûüñçșțăÀÁÂÄÅÈÉÊËÌÍÎÏÒÓÔÖÙÚÛÜÑÇ]")
# English function words: a title with as many of these as foreign ones is
# English ("Der Werwolf: The Annals of Veight").
_ENGLISH_WORDS = frozenset("the of and a an to in on for with from at by is my your his her its our their".split())


# G233 (wave D): more languages' words, each checked against the owned
# titles (none of them may hit). The vocabulary test below catches what no
# list does ("Gefallene Sonnen", "Vangin valtaistuin").
_MORE_FOREIGN_WORDS = frozenset("""
della dello delle sono senza questo quella questa degli fuoco sangue mondo ultimo ultima
como pero sobre entre hasta desde unos unas
pelo pela uma umas uns
meine mein dein deine ihre dieser diese dieses oder nicht ist sind welt alles einer anderen nach
przez dla przy oraz jest nie
podle
""".split())
_MORE_WEAK = frozenset({"di", "da", "nos", "nas", "sem", "zu", "na", "od", "po"})

# A word the reference library's English blurbs never use (G233):
# `app/discovery/wordlists/english_words.txt`, built by
# `tools/build_english_words.py`. A missing file turns the test off.
_ENGLISH_VOCAB_FILE = Path(__file__).parent / "wordlists" / "english_words.txt"
_RX_TITLE_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.UNICODE)


@lru_cache(maxsize=1)
def english_vocabulary() -> frozenset:
    try:
        lines = _ENGLISH_VOCAB_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return frozenset()
    return frozenset(w for w in lines if w and not w.startswith("#"))


def _unseen_words(title: str, authors: Iterable[str]) -> tuple[list[str], list[str], int]:
    """(the title's words no English blurb uses, its words one does, its
    English function words): parentheticals, author names, possessives and
    short words left out."""
    vocab = english_vocabulary()
    if not vocab:
        return [], [], 1
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", title or "")
    names = {w.lower() for name in authors or () for w in _RX_TITLE_WORD.findall(name or "")}
    words = [re.sub(r"'s$", "", w.lower()) for w in _RX_TITLE_WORD.findall(text)]
    english = sum(1 for w in words if w in _ENGLISH_WORDS)
    unseen = [w for w in words if len(w) >= 3 and w not in names and w not in _ENGLISH_WORDS and w not in vocab]
    seen = [w for w in words if len(w) >= 3 and w not in names and w not in _ENGLISH_WORDS and w in vocab]
    return unseen, seen, english


# Place names English titles use, whose first word is a foreign article
# ("Viva, Las Vega IX", "Dirty Magick: Los Angeles"; wave D's F–Z sort).
_RX_PLACE_NAMES = re.compile(
    r"(?i)\b(?:las\s+vegas?|los\s+(?:angeles|alamos)|el\s+(?:paso|dorado)|la\s+(?:jolla|paz))\b"
)


def is_foreign_title(title: str) -> bool:
    t = title or ""
    if _RX_FOREIGN_SCRIPT.search(t) or _RX_FOREIGN_CHARS.search(t):
        return True
    if _RX_FOREIGN_EDITION.search(t):
        return True
    words = [w.lower() for w in _RX_WORD.findall(_RX_PLACE_NAMES.sub(" ", t))]
    hits = [w for w in words if w in _FOREIGN_WORDS or w in _MORE_FOREIGN_WORDS or w in _MORE_WEAK]
    strong = [w for w in hits if w not in _WEAK and w not in _MORE_WEAK]
    english = sum(1 for w in words if w in _ENGLISH_WORDS)
    if (strong or len(hits) >= 2) and len(hits) > english:
        return True
    if english == 0 and _RX_ACCENTS.search(t):
        return True          # "L'Empire caché", "Soleils éclatés"; not "The Tides of Bára"
    return bool(_RX_FOREIGN_SHAPES.search(t)) and english == 0 and len(words) <= 6


def _unseen_title(title: str, authors: Iterable[str] = ()) -> bool:
    """G233: every word is one no English blurb uses (two or more of them):
    "Gefallene Sonnen", "Vangin valtaistuin"."""
    unseen, seen, english = _unseen_words(title, authors)
    return len(unseen) >= 2 and not seen and english == 0


def is_foreign_language_title(title: str, authors: Iterable[str] = ()) -> bool:
    """A title in another language by any title rule (the Goodreads worker's
    check before an autocomplete, G234)."""
    return is_foreign_title(title) or _unseen_title(title, authors)


def _blurb_says_foreign(title: str, description: Optional[str], authors: Iterable[str]) -> bool:
    """G233: a blurb in another language, and a title with a word no English
    blurb uses ("Hechicero", "Preludio a Dune"). The blurb alone isn't
    enough: Hardcover files English titles with a translation's blurb ("The
    Wrath of Mulgarath")."""
    if not description:
        return False
    from app.discovery.goodreads_filters import snippet_language
    lang = snippet_language(description)
    if not lang or lang == "en":
        return False
    return bool(_unseen_words(title, authors)[0])


# ─── The verdict ─────────────────────────────────────────────


# "The Complete Morgaine (Morgaine Cycle Book 1)", "The Complete Wheel of
# Time" (G235): "Complete" before a name, not a guide or a book of something.
_RX_COMPLETE_NAME = re.compile(
    r"^\s*(?:[Tt]he\s+)?[Cc]omplete\s+(?![Bb]ut\b|[Gg]uide\b|[Bb]ook\b|[Hh]andbook\b|[Ii]diot|[Bb]eginner|[Mm]anual\b|[Cc]ourse\b)[A-Z]"
)
_RX_LIST_GENRE = re.compile(
    r"(?i)\b(?:litrpg|gamelit|fantasy|progression|romance|thriller|sci-fi|scifi|science\s+fiction|"
    r"adventure|mystery|horror|harem|isekai|dystopian|cultivation|urban|paranormal|ya)\b"
)
_LIST_HEAD_WORDS = re.compile(r"(?i)\b(?:series|collection|novels|books|set|saga|" + _SET_NAMES[3:-1] + r")\b")


def _title_list(t: str) -> bool:
    """G236: a list of titles after the last colon: "Mistborn: The Wax &
    Wayne Series: Alloy of Law, Shadows of Self, Bands of Mourning…". Three
    or more comma-separated titles, two of them 2+ words, or what comes
    before names a set ("Mass Effect: The Complete Novels: Revelation,
    Ascension, …"). "Home: Habitat, Range, Niche, Territory" is one book; a
    description's list ("A Paranormal Romance with Forced Proximity,
    Reluctant Allies, and …") and volume marks ("…, Book 1") aren't titles."""
    text = t.strip()
    while True:                          # "… Series (The X Series: Tales of …)": not this colon
        trimmed = re.sub(r"\s*\([^()]*\)\s*$", "", text)
        if trimmed == text or ":" not in trimmed or not trimmed.rsplit(":", 1)[1].strip():
            break
        text = trimmed
    if ":" not in text:
        return False
    head, tail = text.rsplit(":", 1)
    tail = tail.strip()
    if re.search(r"\bby\b", tail):
        return False                     # a byline: "… by Card, Orson Scott, Johnston, Aaron"
    items = [i.strip() for i in re.split(r",\s*(?:and\s+)?", tail) if i.strip()]
    if items and re.match(r"(?i)(?:a|an)\s", items[0]):
        return False
    if any(
        re.search(r"[!?]|[^.]\.\s*$|^[a-z0-9]|\band\s+more\b", i) or _RX_LIST_GENRE.search(i) for i in items
    ):
        return False                     # a description: "LitRPG, Progression Fantasy, …", "Fight, … Lena!"
    titles = [
        i for i in items
        if re.search(r"[A-Za-z]{2}", i)
        and not re.fullmatch(r"(?i)(?:book|books|vol\.?|volume|part|no\.?)\s*\S+|(?:&\s*|and\s+)?more\W*", i)
    ]
    if len(titles) < 3:
        return False
    return sum(1 for i in titles if len(i.split()) >= 2) >= 2 or bool(_LIST_HEAD_WORDS.search(head))


def _box_set(t: str) -> bool:
    if _RX_JOINED_TITLES.search(t) or t.count(";") >= 2:
        return True
    if (_RX_COMPLETE_NAME.search(t) or _title_list(t)) and not (
            _RX_ANTHOLOGY.search(t) or _RX_SAMPLER.search(t) or _RX_LISTING.search(t)):
        return True
    for m in _RX_BOX_SET.finditer(t):
        if re.fullmatch(_SET_NAMES, m.group(0).lower()) and _RX_IN_A_TRILOGY.search(t[:m.end()]):
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


# "Moon Called Vol. 1"; Amazon's lone "(Volume 1)" tag is no mark of a
# comic ("Matt Forbeck's Brave New World: Revolution (Volume 1)", a novel).
_RX_VOLUME_MARK = re.compile(r"(?i)\bvol(?:ume)?\.?\s*\d")
_RX_LONE_VOLUME_TAG = re.compile(r"(?i)\(\s*volume\s+\d+\s*\)")


def _authors_comic_issue(title: str, authors: Iterable[str]) -> bool:
    """"Patricia Briggs' Alpha & Omega: Cry Wolf #2", "Patricia Briggs'
    Mercy Thompson: Moon Called Vol. 1": a comic adaptation sold under its
    author's name and numbered by issue or volume (C45, wave D). A prose
    tie-in carries neither ("Dean Koontz's Frankenstein, Book One"), nor
    does a magazine ("Isaac Asimov's Science Fiction Magazine, Volume 4")."""
    t = title or ""
    if not (re.search(r"#\d+", t)
            or (_RX_VOLUME_MARK.search(_RX_LONE_VOLUME_TAG.sub("", t)) and not _RX_ANTHOLOGY.search(t))):
        return False
    tl = (title or "").lower()
    return any(
        len(n.strip()) > 4 and re.match(re.escape(n.strip().lower()) + r"(?:'s|s'|')\s", tl)
        for n in authors or ()
    )


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
    genres: Iterable[str] = (),
    content_type: str = "ebook",
) -> Optional[str]:
    """The family a source's book belongs to, or None for a book to list.

    `n_authors`: the source's credited author-role contributors (an
    anthology has 4+, F7); `authors`: names a seller's listing would put in
    the title; `series_name`: the book's series, a comic's run often named
    only there ("Lucifer, Volume 1: Cold Heaven" in "Lucifer (2015-2017)");
    `genres`: the source's genres, most-shelved first (Goodreads pages);
    `content_type`: the library's ("ebook" / "audiobook").
    """
    reason = _title_family(
        title, description=description, n_authors=n_authors, authors=authors,
        series_name=series_name, page_count=page_count, genres=genres,
    )
    if reason is None and (_RX_ORIENTATION_EDITION.search(title or "") or (
            content_type != "audiobook" and _RX_REGIONAL_EDITION.search(title or ""))):
        return LISTING
    return reason


def _title_family(
    title: str,
    *,
    description: Optional[str] = None,
    n_authors: int = 0,
    authors: Iterable[str] = (),
    series_name: Optional[str] = None,
    page_count: Optional[int] = None,
    genres: Iterable[str] = (),
) -> Optional[str]:
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
            or (page_count and 0 < page_count <= COMIC_ISSUE_MAX_PAGES and _RX_ISSUE_NUMBER.search(t))
            or _authors_comic_issue(t, authors)
            or genres_say_comic(genres)):
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
    if _RX_LISTING.search(t) or _byline(t, authors) or _RX_BYLINE_PART.search(t):
        return LISTING
    # G233, last: a title no other family claims, made of words no English
    # blurb uses, or with a foreign blurb (a comic issue named "Hopcross
    # Jilly #2" stays a comic).
    if _unseen_title(t, authors) or _blurb_says_foreign(t, description, authors):
        return FOREIGN
    return None


def book_class(title: str, *, authors: Iterable[str] = ()) -> Optional[str]:
    """A book's family for matching (G197), read from its title alone, the
    same way for a stored row and an incoming book (C21). What else can mark
    a family depends on the source: a stored row has no contributor list, its
    blurb often names the anthology a standalone story came from, and two
    sources name a comic's series differently ("The Golden Age of the Sith"
    vs "… (1996-1997)") or give no page count. Those signals decide only
    whether a new row is created hidden, never what it matches; nor does an
    orientation edition (G246)."""
    return _title_family(title, authors=authors)


def author_role_count(contributors) -> int:
    """Credited authors on a source's book (translators, narrators and the
    like don't count)."""
    from app.discovery.sources.base import contributor_is_author
    return sum(1 for c in contributors or () if contributor_is_author(getattr(c, "role", None)))


__all__ = [
    "ANTHOLOGY", "AUTHOR_OFF", "BOX_SET", "COMIC", "FAMILIES", "FILTER_REASONS",
    "FOREIGN", "JUNK", "LISTING", "NOT_A_BOOK", "SAMPLER", "SETTING_FOR", "SHOWN",
    "SHORT_UNDER_PAGES", "SPLIT", "STUDY_GUIDE", "author_role_count", "book_class", "filter_reason",
    "genres_say_comic", "hides", "is_foreign_title",
]
