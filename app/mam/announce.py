"""
MAM IRC announce parser.

Turns one raw announce line from MouseBot in `#announce` on
`irc.myanonamouse.net` into a fully populated `Announce` object that the
filter can evaluate.

MAM changed the `#announce` format on 2026-08-11 19:15 UTC to carry the
new multi-category taxonomy (8 media types + 61 content tags, the same
one already snapshotted in `categories_v2.json`). Staff announcement:

    Title By: Author, Author,... [Lang] [Media Type] [Fiction/Non-Fiction]
    [filetype, filetype,...] [size] - Category, Category,... - link VIP|Normal

Real line (color codes stripped):

    Yield Under Great Persuasion By: Alexandra Rowland [English] [Audiobook] [Fiction] [m4b] [575.62 MiB] - Fantasy,  LGBTQIA+,  Romance - https://www.myanonamouse.net/t/1263153 VIP

The **old** format — which this parser handled via Autobrr's regex —
was a single flat category and is now permanently gone:

    New Torrent: The Demon King By: Peter V Brett Category: ( Audiobooks - Fantasy ) Size: ( 921.91 MiB ) Filetype: ( m4b ) Language: ( English ) Link: ( https://www.myanonamouse.net/t/1233592 ) VIP

Two structural changes drive the parsing strategy:

  * **Everything is anchored on the trailing URL**, never on separators.
    Both the title and the content-tag list can legitimately contain
    `" - "` — real examples are the title "I Want to Hold Aono-kun So
    Badly I Could Die - Full Series" and the tag "Complete Editions -
    Music". Splitting on the separator would corrupt both.
  * **Categories and filetypes are lists.** `[cbz, pdf]` is real (comic
    and manga bundles). The single-valued `category` / `filetype` fields
    are preserved as synthesized legacy values so the ~15 downstream
    consumers that parse the old `"Ebooks - Fantasy"` shape keep working
    untouched; `categories` / `filetypes` carry the full truth.

This module is intentionally pure — no I/O, no logging, no state.
Database persistence and side effects are the caller's job.
"""
from __future__ import annotations

import re
from typing import Optional

from app.filter.gate import Announce


# The post-2026-08-11 announce grammar.
#
# Anchoring notes (see module docstring for why this matters):
#   * `title` is greedy up to the LAST " By: " so a title containing
#     "By:" resolves to the title, matching the old parser's behavior.
#   * `authors` is lazy up to the first bracket group.
#   * `categories` is lazy, bounded by the ` - <url>` tail rather than
#     by a separator split, so tags containing " - " survive intact.
#   * The five-bracket run is required. A PRIVMSG that doesn't carry it
#     isn't an announce (MouseBot also emits status chatter), so the
#     parser returns None and the listener ignores the line.
_ANNOUNCE_RX = re.compile(
    r"^\s*(?P<title>.+) By:\s*(?P<authors>.+?)\s*"
    r"\[(?P<language>[^\]]*)\]\s*"
    r"\[(?P<media_type>[^\]]*)\]\s*"
    r"\[(?P<fiction>[^\]]*)\]\s*"
    r"\[(?P<filetypes>[^\]]*)\]\s*"
    r"\[(?P<size>[^\]]*)\]\s*"
    r"-\s*(?P<categories>.*?)\s*"
    r"-\s*(?P<base_url>https?://[^/\s]+/)t/(?P<torrent_id>\d+)"
    r"\s*(?P<vip>VIP|Normal)?\s*$",
    re.IGNORECASE,
)

# New-format media type → the legacy `"<Format> - <Sub>"` prefix that
# `filter.normalize.extract_format`, `format_dedup.media_type_from_category`
# and the `LOWER(g.category) LIKE 'audiobook%'` SQL in
# `discovery.acquisition_linkback` all still key on.
#
# The plural is load-bearing: MAM's new feed says "Ebook"/"Audiobook"
# (singular) but every existing consumer — and the user's saved
# `allowed_formats` / `allowed_categories` settings — expects
# "ebooks"/"audiobooks". Synthesizing the plural is what makes this a
# parser-only change instead of a settings migration.
_MEDIA_TYPE_TO_LEGACY_FORMAT: dict[str, str] = {
    "audiobook": "Audiobooks",
    "ebook": "Ebooks",
    "musicology": "Musicology",
    "radio": "Radio",
    "manga": "Manga",
    "comic book / graphic novel": "Comics/Graphic novels",
    "periodical ebook": "Periodical Ebooks",
    "periodical audiobook": "Periodical Audiobooks",
}

# mIRC formatting codes that real MAM IRC traffic includes inline.
# Without stripping these, the regex above silently fails to match
# every real announce — `04New Torrent:14` (color 4 / 14 wrapping
# the literal text) is not the same as `New Torrent:` to a regex.
# Caught the hard way during the first production smoke test: the
# unit-test fixtures we had were the DECOLORED form Autobrr serves
# in its logs, but raw IRC traffic carries the color bytes.
#
#   \x02  bold
#   \x03  color (followed by NN[,MM] digits)
#   \x0f  reset
#   \x16  reverse
#   \x1d  italic
#   \x1e  strikethrough
#   \x1f  underline
#
# The color sequence `\x03NN` or `\x03NN,MM` is the special case
# because it has a numeric payload following the marker byte. The
# others are single-byte tokens that we can drop directly.
_COLOR_CODE_RX = re.compile(r"\x03(?:\d{1,2}(?:,\d{1,2})?)?")
_FORMATTING_CODES = str.maketrans(
    "", "", "\x02\x0f\x16\x1d\x1e\x1f"
)


def _strip_irc_formatting(line: str) -> str:
    """Strip mIRC color/formatting codes from a raw IRC PRIVMSG body.

    Order matters: handle the variable-length color sequences with a
    regex first, THEN drop the single-byte formatting tokens with a
    translate. Doing it in the other order would leave orphan digit
    bytes from a color marker that's been partially consumed.
    """
    if not line:
        return line
    cleaned = _COLOR_CODE_RX.sub("", line)
    cleaned = cleaned.translate(_FORMATTING_CODES)
    return cleaned

# MAM truncates the author list when there are too many co-authors,
# appending "and N more" (or just ", N more"). Without stripping this
# the splitter would happily produce a phantom author named "1 more"
# and that author would never match anything in the allow/ignore lists.
# Real example from autobrr.log:
#   "Stephen Marlowe, John Roeburt, Ed Lacy, and 1 more"
# Strips trailing ", and N more" / ", N more" / "and N more".
_AND_N_MORE_RX = re.compile(
    r"\s*,?\s*(?:and\s+)?\d+\s+more\s*$",
    re.IGNORECASE,
)


def _strip_and_n_more(blob: str) -> str:
    """Remove a trailing 'and N more' truncation marker from an author blob."""
    cleaned = _AND_N_MORE_RX.sub("", blob).rstrip().rstrip(",").rstrip()
    return cleaned


def _split_list_field(blob: str) -> tuple[str, ...]:
    """Split a comma-separated announce field into clean parts.

    The live IRC feed pads with double spaces after commas
    (`"Fantasy,  LGBTQIA+,  Romance"`) where the staff announcement
    showed single — so normalize whitespace rather than trusting either.
    Empty parts are dropped so a trailing comma can't yield a `""` tag.
    """
    if not blob:
        return ()
    parts = (re.sub(r"\s+", " ", p).strip() for p in blob.split(","))
    return tuple(p for p in parts if p)


def parse_announce(line: str) -> Optional[Announce]:
    """Parse one IRC announce line into an `Announce`, or None if it doesn't match.

    The MAM IRC channel emits a steady stream of torrent PRIVMSGs plus
    the occasional unrelated bot message. Anything that doesn't match
    the announce grammar returns None — the caller treats None as
    "ignore this line, not for us."

    No exceptions are raised for malformed input. The contract is
    Optional[Announce], not "raises on bad input" — the IRC listener
    runs in a tight loop and exception handling per line would just be
    silently absorbed `try/except` boilerplate at the call site.
    """
    if not line:
        return None

    # Strip mIRC color and formatting codes BEFORE running the regex.
    # MAM's MouseBot wraps fields in color codes (`\x0314English\x0304`)
    # that the unit-test fixtures didn't have because they came from
    # Autobrr's already-decolored log dump.
    cleaned = _strip_irc_formatting(line)

    m = _ANNOUNCE_RX.search(cleaned)
    if not m:
        return None

    title = m.group("title").strip()
    raw_author = m.group("authors").strip()
    language = m.group("language").strip()
    media_type = m.group("media_type").strip()
    size = m.group("size").strip()
    torrent_id = m.group("torrent_id")
    base_url = m.group("base_url")

    categories = _split_list_field(m.group("categories"))
    filetypes = _split_list_field(m.group("filetypes"))

    # "Normal" is MAM's explicit non-VIP marker, not a missing field.
    vip = (m.group("vip") or "").strip().lower() == "vip"

    author_blob = _strip_and_n_more(raw_author)

    # Synthesize the legacy `"<Format> - <Subcategory>"` category string.
    # The first content tag is the stand-in for the old single
    # subcategory; the full list rides along in `categories` and is what
    # the filter gate actually evaluates, so nothing is lost by the
    # first-tag choice here. Unknown media types pass through verbatim
    # rather than being dropped — a new MAM media type should degrade to
    # "unrecognized format" at the gate, not to "no category at all".
    legacy_format = _MEDIA_TYPE_TO_LEGACY_FORMAT.get(
        media_type.lower(), media_type
    )
    category = f"{legacy_format} - {categories[0]}" if categories else legacy_format

    # Reconstruct the canonical info URL from the captured base + ID.
    # MAM's torrent landing page URL is `<base>t/<id>`. Building it from
    # captures rather than copying the raw match guarantees the URL we
    # store is well-formed even if MAM ever changes the path slightly.
    info_url = f"{base_url}t/{torrent_id}"

    return Announce(
        torrent_id=torrent_id,
        torrent_name=title,
        category=category,
        categories=categories,
        author_blob=author_blob,
        title=title,
        info_url=info_url,
        size=size,
        filetype=", ".join(filetypes),
        filetypes=filetypes,
        language=language,
        media_type=media_type,
        vip=vip,
    )


def build_download_url(torrent_id: str, *, use_fl_wedge: bool = False) -> str:
    """Construct the .torrent file download URL for a given MAM torrent ID.

    Used by the grab path. Kept here next to the parser because the
    URL shape is part of the same MAM-specific API surface, and the
    caller already has the parsed `torrent_id` field handy.

    The URL is the same one Autobrr uses (confirmed from its
    `myanonamouse.yaml`) — `/tor/download.php?tid=<id>`. Authentication
    is via the `mam_id` cookie attached as an HTTP header at fetch time;
    the URL itself carries no token.

    When `use_fl_wedge=True`, appends `&fl=1` to spend a freeleech
    wedge on this torrent, making the download free. The policy engine
    decides whether to set this flag.
    """
    url = f"https://www.myanonamouse.net/tor/download.php?tid={torrent_id}"
    if use_fl_wedge:
        url += "&fl=1"
    return url
