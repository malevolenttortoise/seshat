"""
Unit tests for the MAM announce parser.

Two layers of testing:

  1. **Hand-written cases** — pin down specific behaviors (the
     "and N more" stripping, the typographic apostrophe, VIP vs Normal,
     multi-category/multi-filetype handling, malformed input rejection).
     These are the regression tests that catch deliberate changes.

  2. **Real fixture sweep** — every line in
     `tests/fixtures/real_announces_v2.txt` (35 captures pulled straight
     from the production container log, mIRC color bytes intact) MUST
     parse cleanly. This is the safety net for the next format change:
     the previous one shipped silently and cost three days of announces
     because `parse_announce` returning None is indistinguishable from
     "not an announce" at the call site.

`real_announces.txt` holds the pre-2026-08-11 captures. Those are kept
deliberately — not as a parser target but as the fixture proving the
old grammar is gone, so nobody "restores" it later.
"""
from pathlib import Path

from app.filter.gate import Announce
from app.mam.announce import (
    _strip_and_n_more,
    _strip_irc_formatting,
    build_download_url,
    parse_announce,
)


_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
_FIXTURES_PATH = _FIXTURES_DIR / "real_announces_v2.txt"
_LEGACY_FIXTURES_PATH = _FIXTURES_DIR / "real_announces.txt"


def _read_lines(path: Path) -> list[str]:
    return [
        line.rstrip("\n")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _load_real_announces() -> list[str]:
    return _read_lines(_FIXTURES_PATH)


# ─── Real-fixture sweep ──────────────────────────────────────


class TestRealFixturesParse:
    """Every captured production announce must parse cleanly."""

    def test_all_fixtures_parse(self):
        announces = _load_real_announces()
        assert len(announces) >= 34, "Fixture file shrunk unexpectedly"
        for line in announces:
            result = parse_announce(line)
            assert result is not None, f"Failed to parse: {line!r}"
            assert isinstance(result, Announce)
            assert result.torrent_id, f"Missing torrent_id in: {line!r}"
            assert result.torrent_name, f"Missing torrent_name in: {line!r}"
            assert result.category, f"Missing category in: {line!r}"
            assert result.categories, f"Missing categories in: {line!r}"
            assert result.author_blob, f"Missing author_blob in: {line!r}"
            assert result.filetypes, f"Missing filetypes in: {line!r}"
            assert result.language, f"Missing language in: {line!r}"
            assert result.media_type, f"Missing media_type in: {line!r}"

    def test_fixtures_cover_both_vip_and_normal(self):
        # "Normal" is MAM's explicit non-VIP marker. A regex that only
        # knew about an optional trailing "VIP" would still match these
        # lines while silently mis-flagging them, so the corpus has to
        # contain both to make that failure visible.
        parsed = [parse_announce(l) for l in _load_real_announces()]
        flags = {p.vip for p in parsed if p is not None}
        assert flags == {True, False}

    def test_fixtures_cover_multiple_media_types(self):
        parsed = [parse_announce(l) for l in _load_real_announces()]
        media = {p.media_type for p in parsed if p is not None}
        assert {"Ebook", "Audiobook"} <= media
        assert len(media) >= 4, f"Corpus lost media-type variety: {media}"

    def test_legacy_format_no_longer_parses(self):
        # MAM retired this grammar on 2026-08-11. Pinned so the old
        # regex can't be reintroduced without this test going red.
        legacy = _read_lines(_LEGACY_FIXTURES_PATH)
        assert len(legacy) >= 18
        assert all(parse_announce(line) is None for line in legacy)

    def test_fixture_torrent_ids_are_unique_and_numeric(self):
        announces = _load_real_announces()
        ids = []
        for line in announces:
            result = parse_announce(line)
            assert result is not None
            assert result.torrent_id.isdigit()
            ids.append(result.torrent_id)
        assert len(set(ids)) == len(ids), "Fixture file has duplicate torrent IDs"

    def test_fixture_info_urls_are_well_formed(self):
        announces = _load_real_announces()
        for line in announces:
            result = parse_announce(line)
            assert result is not None
            assert result.info_url.startswith("https://www.myanonamouse.net/")
            assert result.info_url.endswith(f"/t/{result.torrent_id}")


# ─── Hand-written cases — specific behaviors ─────────────────


class TestParseAnnounce:
    def test_basic_single_author_with_vip(self):
        line = (
            "The Demon King By: Peter V Brett [English] [Audiobook] "
            "[Fiction] [m4b] [921.91 MiB] - Fantasy - "
            "https://www.myanonamouse.net/t/1233592 VIP"
        )
        result = parse_announce(line)
        assert result is not None
        assert result.torrent_id == "1233592"
        assert result.torrent_name == "The Demon King"
        assert result.title == "The Demon King"
        assert result.author_blob == "Peter V Brett"
        assert result.media_type == "Audiobook"
        assert result.categories == ("Fantasy",)
        assert result.size == "921.91 MiB"
        assert result.filetype == "m4b"
        assert result.filetypes == ("m4b",)
        assert result.language == "English"
        assert result.vip is True
        assert result.info_url == "https://www.myanonamouse.net/t/1233592"

    def test_legacy_category_string_is_synthesized(self):
        # The single `category` field keeps the pre-2026-08-11
        # "<Format> - <Sub>" shape because extract_format,
        # media_type_from_category and the acquisition-linkback SQL all
        # still parse it. Note the PLURAL: MAM now says "Audiobook",
        # every consumer and every saved setting says "audiobooks".
        result = parse_announce(
            "The Demon King By: Peter V Brett [English] [Audiobook] "
            "[Fiction] [m4b] [921.91 MiB] - Fantasy - "
            "https://www.myanonamouse.net/t/1233592 VIP"
        )
        assert result is not None
        assert result.category == "Audiobooks - Fantasy"

        ebook = parse_announce(
            "New Earth By: M R Forbes [English] [Ebook] [Fiction] [epub] "
            "[1.10 MiB] - Science Fiction - "
            "https://www.myanonamouse.net/t/1233593 Normal"
        )
        assert ebook is not None
        assert ebook.category == "Ebooks - Science Fiction"

    def test_normal_marker_is_not_vip(self):
        # "Normal" replaced "no trailing token" as the non-VIP marker.
        line = (
            "The Path of Ascension 11 By: C Mantis [English] [Audiobook] "
            "[Fiction] [m4b] [761.20 MiB] - Fantasy - "
            "https://www.myanonamouse.net/t/1233620 Normal"
        )
        result = parse_announce(line)
        assert result is not None
        assert result.vip is False
        assert result.torrent_id == "1233620"

    def test_multiple_categories_preserved_in_order(self):
        line = (
            "Yield Under Great Persuasion By: Alexandra Rowland [English] "
            "[Audiobook] [Fiction] [m4b] [575.62 MiB] - "
            "Fantasy,  LGBTQIA+,  Romance - "
            "https://www.myanonamouse.net/t/1263153 VIP"
        )
        result = parse_announce(line)
        assert result is not None
        # The live feed double-spaces after commas; the staff post
        # showed single. Neither is trusted — whitespace is collapsed.
        assert result.categories == ("Fantasy", "LGBTQIA+", "Romance")
        # First tag stands in for the old single subcategory.
        assert result.category == "Audiobooks - Fantasy"

    def test_multiple_filetypes(self):
        # Real comic/manga bundles announce as "[cbz, pdf]".
        line = (
            "Ultimate Marvel Team-Up By: Matt Wagner [English] "
            "[Comic Book / Graphic Novel] [Fiction] [cbz, pdf] "
            "[1.20 GiB] - Superheroes - "
            "https://www.myanonamouse.net/t/1262899 VIP"
        )
        result = parse_announce(line)
        assert result is not None
        assert result.filetypes == ("cbz", "pdf")
        assert result.filetype == "cbz, pdf"

    def test_title_containing_separator(self):
        # This title carries " - ", which also delimits the category
        # list. Anchoring on the trailing URL is what keeps it from
        # being truncated at the first separator.
        result = parse_announce(
            "I Want to Hold Aono-kun So Badly I Could Die - Full Series "
            "By: Umi Shiina [English] [Manga] [Fiction] [cbz] "
            "[4.59 GiB] - Fantasy - "
            "https://www.myanonamouse.net/t/1262900 VIP"
        )
        assert result is not None
        assert result.title == (
            "I Want to Hold Aono-kun So Badly I Could Die - Full Series"
        )
        assert result.author_blob == "Umi Shiina"

    def test_category_containing_separator(self):
        # "Complete Editions - Music" is a real MAM content tag, so the
        # category region itself can contain the delimiter.
        result = parse_announce(
            "Guitar Anthology By: Various [English] [Musicology] "
            "[Non-Fiction] [pdf] [88.10 MiB] - Complete Editions - Music,  "
            "Music Book - https://www.myanonamouse.net/t/1262901 Normal"
        )
        assert result is not None
        assert result.categories == ("Complete Editions - Music", "Music Book")
        assert result.torrent_id == "1262901"

    def test_unknown_media_type_passes_through(self):
        # A media type MAM adds later should degrade to "unrecognized
        # format" at the gate rather than losing the category entirely.
        result = parse_announce(
            "Some Thing By: A N Other [English] [Hologram] [Fiction] "
            "[bin] [1.00 MiB] - Fantasy - "
            "https://www.myanonamouse.net/t/1299999 VIP"
        )
        assert result is not None
        assert result.media_type == "Hologram"
        assert result.category == "Hologram - Fantasy"

    def test_and_n_more_stripped(self):
        # Real-world MAM truncation when there are too many co-authors.
        line = (
            "The Hardboiled Mystery MEGAPACK "
            "By: Stephen Marlowe, John Roeburt, Ed Lacy, and 1 more "
            "[English] [Ebook] [Fiction] [epub] [743.58 KiB] - Mystery - "
            "https://www.myanonamouse.net/t/1233596 VIP"
        )
        result = parse_announce(line)
        assert result is not None
        # The "and 1 more" suffix must be removed so the splitter
        # produces 3 authors, not 4 with a phantom "1 more".
        assert "more" not in result.author_blob.lower()
        assert result.author_blob == "Stephen Marlowe, John Roeburt, Ed Lacy"

    def test_typographic_apostrophe_in_title_preserved(self):
        # The parser preserves the title verbatim — apostrophe
        # normalization happens in the filter layer when comparing
        # against author lists, not in the parser.
        line = (
            "I Won\u2019t Let Mistress Suck My Blood, Vol. 1 "
            "By: Paderapollonorio [English] [Comic Book / Graphic Novel] "
            "[Fiction] [cbz] [62.93 MiB] - Comedy - "
            "https://www.myanonamouse.net/t/1233619 VIP"
        )
        result = parse_announce(line)
        assert result is not None
        assert "\u2019" in result.title

    def test_title_with_colon(self):
        # "Classroom of the Elite: Year 2, Vol. 12.5" — the colon in the
        # title shouldn't confuse the regex (it's a real fixture).
        line = (
            "Classroom of the Elite: Year 2, Vol. 12.5 "
            "By: Syougo Kinugasa [English] [Audiobook] [Fiction] [m4b] "
            "[472.16 MiB] - Young Adult - "
            "https://www.myanonamouse.net/t/1233608 VIP"
        )
        result = parse_announce(line)
        assert result is not None
        assert result.title == "Classroom of the Elite: Year 2, Vol. 12.5"
        assert result.author_blob == "Syougo Kinugasa"

    def test_title_with_comma(self):
        # "Sea of Wind, Shore of the Labyrinth" — comma in title
        line = (
            "Sea of Wind, Shore of the Labyrinth "
            "By: Fuyumi Ono [English] [Audiobook] [Fiction] [m4b] "
            "[401.33 MiB] - Fantasy - "
            "https://www.myanonamouse.net/t/1233605 VIP"
        )
        result = parse_announce(line)
        assert result is not None
        assert result.title == "Sea of Wind, Shore of the Labyrinth"

    def test_category_with_slash(self):
        # "Action/Adventure", "Thriller/Suspense" — slashes in the
        # category are common and shouldn't be eaten by the regex.
        line = (
            "God's Eye By: Robert Rapoza [English] [Ebook] [Fiction] "
            "[epub] [1.49 MiB] - Action/Adventure - "
            "https://www.myanonamouse.net/t/1233601 VIP"
        )
        result = parse_announce(line)
        assert result is not None
        assert result.categories == ("Action/Adventure",)
        assert result.category == "Ebooks - Action/Adventure"

    # ─── _strip_irc_formatting direct tests ──────────────────

    def test_strip_irc_formatting_color_with_two_digits(self):
        # The most common shape: \x03 + two-digit color code +
        # actual text. The grey color used by MouseBot is `14`.
        assert _strip_irc_formatting("\x0314hello") == "hello"

    def test_strip_irc_formatting_color_with_one_digit(self):
        # IRC color codes are 1-2 digits. \x031 (color 1) is valid.
        assert _strip_irc_formatting("\x031hi") == "hi"

    def test_strip_irc_formatting_color_with_background(self):
        # \x03NN,MM is "foreground color NN, background color MM".
        assert _strip_irc_formatting("\x0304,00text") == "text"

    def test_strip_irc_formatting_bare_color_reset(self):
        # \x03 alone with no digits resets to default colors.
        assert _strip_irc_formatting("hello\x03world") == "helloworld"

    def test_strip_irc_formatting_bold(self):
        assert _strip_irc_formatting("\x02bold\x02 text") == "bold text"

    def test_strip_irc_formatting_underline(self):
        assert _strip_irc_formatting("\x1funderlined\x1f") == "underlined"

    def test_strip_irc_formatting_italic(self):
        assert _strip_irc_formatting("\x1ditalic\x1d") == "italic"

    def test_strip_irc_formatting_reverse(self):
        assert _strip_irc_formatting("\x16reverse\x16") == "reverse"

    def test_strip_irc_formatting_strikethrough(self):
        assert _strip_irc_formatting("\x1estrikethrough\x1e") == "strikethrough"

    def test_strip_irc_formatting_reset(self):
        assert _strip_irc_formatting("\x0fafter reset") == "after reset"

    def test_strip_irc_formatting_multiple_codes(self):
        # All formatting codes together — common in styled bot output
        assert (
            _strip_irc_formatting("\x02\x0304bold red\x0f normal")
            == "bold red normal"
        )

    def test_strip_irc_formatting_preserves_plain_digits(self):
        # Critical: digits NOT preceded by \x03 must be preserved.
        # The torrent ID 1233678 in a real announce is just digits;
        # if we accidentally consume it, parse_announce dies.
        assert _strip_irc_formatting("torrent 1233678") == "torrent 1233678"

    def test_strip_irc_formatting_empty(self):
        assert _strip_irc_formatting("") == ""

    def test_strip_irc_formatting_no_codes_passthrough(self):
        # Non-colored input should pass through unchanged.
        line = "New Torrent: Foo By: Bar Category: ( Ebooks - Fantasy )"
        assert _strip_irc_formatting(line) == line

    def test_real_colored_privmsg_parses_after_stripping(self):
        # The actual on-the-wire shape MAM IRC sends, captured from the
        # production container log. Color codes (\x03 followed by 1-2
        # digits) wrap most fields — without the formatting stripper the
        # regex silently doesn't match and Seshat looks like it's
        # working but never grabs anything.
        line = (
            "The Winds of Change... and Other Stories\x0304 By:\x0303 "
            "Isaac Asimov\x0304 [\x0314English\x0304] [Ebook] [Fiction] "
            "[\x0314epub\x0304] [\x0303301.58 KiB\x0304] -\x03 "
            "Science Fiction\x0304 - \x0314"
            "https://www.myanonamouse.net/t/1263152\x0304 VIP"
        )
        result = parse_announce(line)
        assert result is not None
        assert result.torrent_id == "1263152"
        assert result.torrent_name == "The Winds of Change... and Other Stories"
        assert result.author_blob == "Isaac Asimov"
        assert result.media_type == "Ebook"
        assert result.categories == ("Science Fiction",)
        assert result.category == "Ebooks - Science Fiction"
        assert result.filetype == "epub"
        assert result.vip is True

    def test_real_colored_privmsg_multi_category(self):
        # Colored variant carrying several tags and the "Normal"
        # non-VIP marker.
        line = (
            "RuinForged Architect\x0304 By:\x0303 Malik Mark\x0304 "
            "[\x0314English\x0304] [Audiobook] [Fiction] "
            "[\x0314m4b\x0304] [\x0303689.70 MiB\x0304] -\x03 "
            "Action/Adventure,  Fantasy,  LitRPG,  Progression Fantasy"
            "\x0304 - \x0314"
            "https://www.myanonamouse.net/t/1262830\x0304 Normal"
        )
        result = parse_announce(line)
        assert result is not None
        assert result.torrent_id == "1262830"
        assert result.vip is False
        assert result.categories == (
            "Action/Adventure",
            "Fantasy",
            "LitRPG",
            "Progression Fantasy",
        )

    def test_returns_none_on_unrelated_line(self):
        # The IRC channel emits other PRIVMSGs (status, errors, etc).
        # Anything that doesn't match returns None — never raises.
        assert parse_announce("MouseBot: server restart in 5 minutes") is None
        assert parse_announce("") is None
        assert parse_announce("just some random text") is None

    def test_returns_none_on_partial_match(self):
        # Truncated / malformed announce — must NOT half-fill an Announce.
        assert parse_announce(
            "The Demon King By: Peter V Brett [English] [Audiobook]"
        ) is None

    def test_returns_none_on_cutover_malformed_line(self):
        # MAM's own first line under the new format was malformed —
        # missing the space before "By:", a stray space in the size
        # bracket, and ")" where the URL separator belongs. Rejecting it
        # is correct; it's pinned so a future "be more lenient" change
        # has to be deliberate.
        line = (
            " Frost HungerBy:\x0303 Indigo Frey\x0304 [\x0314English\x0304] "
            "[Ebook] [Fiction] [\x0314epub\x0304] [1.07 MiB ]  - "
            "Fantasy,  LGBTQIA+,  Erotica/Sexual Content,  Romance ) "
            "https://www.myanonamouse.net/t/1262654 VIP"
        )
        assert parse_announce(line) is None


# ─── _strip_and_n_more directly ──────────────────────────────


class TestStripAndNMore:
    def test_no_marker(self):
        assert _strip_and_n_more("A, B, C") == "A, B, C"

    def test_and_n_more(self):
        assert (
            _strip_and_n_more("Stephen Marlowe, John Roeburt, Ed Lacy, and 1 more")
            == "Stephen Marlowe, John Roeburt, Ed Lacy"
        )

    def test_and_2_more(self):
        assert (
            _strip_and_n_more("Author A, Author B, and 2 more")
            == "Author A, Author B"
        )

    def test_n_more_no_and(self):
        # Defensive — handle ", 3 more" without the "and" connector too.
        assert _strip_and_n_more("Author A, Author B, 3 more") == "Author A, Author B"

    def test_case_insensitive(self):
        assert _strip_and_n_more("Author A, AND 5 MORE") == "Author A"


# ─── build_download_url ──────────────────────────────────────


class TestBuildDownloadUrl:
    def test_format(self):
        assert (
            build_download_url("1233592")
            == "https://www.myanonamouse.net/tor/download.php?tid=1233592"
        )

    def test_with_announce_roundtrip(self):
        # The torrent_id captured from a real announce should produce
        # a valid download URL when passed back to build_download_url.
        line = (
            "The Demon King By: Peter V Brett [English] [Audiobook] "
            "[Fiction] [m4b] [921.91 MiB] - Fantasy - "
            "https://www.myanonamouse.net/t/1233592 VIP"
        )
        result = parse_announce(line)
        assert result is not None
        url = build_download_url(result.torrent_id)
        assert url == "https://www.myanonamouse.net/tor/download.php?tid=1233592"
