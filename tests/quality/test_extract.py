"""
Unit tests for the quality-metadata extraction module.

Three input tiers tested against real captured MAM responses
(probed 2026-05-23, files preserved under `tests/quality/fixtures/`):

  - modern audiobook (Dungeon Diving 302) → mediainfo populated
  - modern ebook (Blood Bound)           → mediainfo == "{}"
  - old audiobook (Golden Son, 2016)     → mediainfo == "{}", but
                                            description has structured
                                            inAudible "Media Information"
                                            block
"""
from __future__ import annotations

import pytest

from app.quality.extract import (
    QualitySnapshot,
    _bitrate_str_to_kbps,
    _duration_to_seconds,
    _sample_rate_to_hz,
    extract_quality,
    parse_description,
    parse_mediainfo,
    parse_size_to_bytes,
    parse_tags,
)


# ─── Helper coercion tests ───────────────────────────────────


class TestBitrateCoercion:
    def test_k_suffix_int_string(self):
        assert _bitrate_str_to_kbps("126k") == 126

    def test_kbps_suffix(self):
        assert _bitrate_str_to_kbps("192 kbps") == 192

    def test_bare_int_under_50000_is_kbps(self):
        """`128` in BitRate is already kbps in mediainfo's JSON."""
        assert _bitrate_str_to_kbps(128) == 128

    def test_bare_int_over_50000_is_bps(self):
        """`128000` from a raw-bps stream rolls over to 128 kbps."""
        assert _bitrate_str_to_kbps(128000) == 128

    def test_m_suffix(self):
        assert _bitrate_str_to_kbps("1.5M") == 1500

    def test_none(self):
        assert _bitrate_str_to_kbps(None) is None

    def test_unparseable(self):
        assert _bitrate_str_to_kbps("variable") is None


class TestSampleRateCoercion:
    def test_khz_format(self):
        assert _sample_rate_to_hz("44.1kHz") == 44100

    def test_khz_with_space(self):
        assert _sample_rate_to_hz("44.1 kHz") == 44100

    def test_hz_suffix(self):
        assert _sample_rate_to_hz("22050 Hz") == 22050

    def test_bare_hz_int(self):
        assert _sample_rate_to_hz(48000) == 48000

    def test_bare_khz_int(self):
        """Bare 44 → 44000 Hz (assume kHz when small)."""
        assert _sample_rate_to_hz(44) == 44000


class TestDurationCoercion:
    def test_hms_format(self):
        assert _duration_to_seconds("12:05:07") == 12 * 3600 + 5 * 60 + 7

    def test_zero_pads(self):
        assert _duration_to_seconds("01:00:00") == 3600

    def test_missing_parts(self):
        """Only HH:MM:SS supported; HH:MM or bare seconds → None."""
        assert _duration_to_seconds("12:05") is None
        assert _duration_to_seconds("65") is None

    def test_none(self):
        assert _duration_to_seconds(None) is None


class TestSizeParsing:
    def test_mib(self):
        # 658.5 MiB from the Dungeon Diving probe.
        assert parse_size_to_bytes("658.5 MiB") == int(658.5 * 1024 ** 2)

    def test_kib_with_comma(self):
        # "1,023.2 KiB" from the Blood Bound probe.
        assert parse_size_to_bytes("1,023.2 KiB") == int(1023.2 * 1024)

    def test_mb_decimal(self):
        # Decimal megabyte (1000^2) vs MiB (1024^2).
        assert parse_size_to_bytes("100 MB") == 100_000_000

    def test_bare_int(self):
        assert parse_size_to_bytes(1234) == 1234

    def test_unparseable(self):
        assert parse_size_to_bytes("a lot") is None


# ─── Mediainfo parsing (modern audiobook) ────────────────────


# Captured live from MAM 2026-05-23 for torrent 1237094 (Dungeon
# Diving 302). The mediainfo field is stringified JSON — paste in
# as a Python triple-quoted string and the test parses it the same
# way the real code does.
MODERN_AUDIOBOOK_MEDIAINFO = (
    '{"menu":{"extra":[" Opening Credits "," The Story Thus Far '
    '"," Chapter 1 "," Chapter 2 "," Chapter 3 "]},'
    '"Audio1":{"Format":"AAC","BitRate":"126k",'
    '"CodecID":"2 / 40 / mp4a-40-2","Channels":2,'
    '"BitRate_Mode":"CBR","SamplingRate":"44.1kHz",'
    '"Compression_Mode":"Lossy"},'
    '"General":{"Title":"Dungeon Diving 302 (Unabridged)",'
    '"Format":"MPEG-4","Duration":"12:05:07"}}'
)


class TestParseMediainfo:
    def test_modern_audiobook_extracts_full_audio_data(self):
        result = parse_mediainfo("1237094", MODERN_AUDIOBOOK_MEDIAINFO)
        assert result is not None
        assert result["audio_format"] == "AAC"
        assert result["audio_bitrate_kbps"] == 126
        assert result["audio_channels"] == 2
        assert result["audio_bitrate_mode"] == "CBR"
        assert result["audio_sample_rate"] == 44100
        assert result["audio_compression"] == "Lossy"
        assert result["audio_codec_id"] == "2 / 40 / mp4a-40-2"
        assert result["audio_duration_sec"] == 12 * 3600 + 5 * 60 + 7
        assert result["audio_chapter_count"] == 5  # menu.extra length
        assert result["container_format"] == "MPEG-4"

    def test_empty_object_returns_none(self):
        """The exact string MAM returns for ebooks + older audiobooks."""
        assert parse_mediainfo("any", "{}") is None

    def test_empty_string_returns_none(self):
        assert parse_mediainfo("any", "") is None

    def test_malformed_json_returns_none(self):
        assert parse_mediainfo("any", "not json {") is None

    def test_missing_audio1_returns_none(self):
        """No Audio1 stream → no audio extraction possible."""
        no_audio = '{"General": {"Format": "MPEG-4", "Duration": "1:00:00"}}'
        assert parse_mediainfo("any", no_audio) is None


# ─── Description parsing (inAudible block) ───────────────────


# Captured from torrent 332910 (Golden Son, 2016). The "Encoded *"
# values are the post-rip ones that match the file Seshat receives.
OLD_AUDIOBOOK_DESCRIPTION = """General Information
===================
 Title: Golden Son
 Duration: 19 hours, 3 minutes, 23 seconds
 Chapters: 51

Media Information
=================
 Source Format: Audible AAX
 Source Sample Rate: 22050 Hz
 Source Channels: 2
 Source Bitrate: 63 kbits

 Lossless Encode: Yes
 Encoded Codec: AAC / M4B
 Encoded Sample Rate: 22050 Hz
 Encoded Channels: 2
 Encoded Bitrate: 63 kbits

 Ripper: inAudible 1.75
"""


class TestParseDescription:
    def test_inaudible_block_extracts_encoded_values(self):
        """We pull the ENCODED values (the file we actually receive),
        not the SOURCE values (the pre-rip Audible source)."""
        result = parse_description(OLD_AUDIOBOOK_DESCRIPTION)
        assert result is not None
        assert result["audio_format"] == "AAC"  # before the slash
        assert result["audio_bitrate_kbps"] == 63
        assert result["audio_channels"] == 2
        assert result["audio_sample_rate"] == 22050
        assert result["audio_chapter_count"] == 51
        assert result["audio_compression"] == "Lossless"

    def test_empty_description_returns_none(self):
        assert parse_description("") is None

    def test_no_matching_patterns_returns_none(self):
        assert parse_description("Just prose about the book.") is None

    def test_partial_match(self):
        """Only some fields match; others stay absent."""
        partial = "Encoded Codec: MP3 ... no bitrate field here ..."
        result = parse_description(partial)
        assert result is not None
        assert result["audio_format"] == "MP3"
        assert "audio_bitrate_kbps" not in result


# ─── Tags parsing ────────────────────────────────────────────


class TestParseTags:
    def test_modern_audiobook_tags(self):
        """Real tags from the Dungeon Diving probe."""
        tags = "126 kbps m4b with chapters | Release Date 04-23-26 | Listening Length 12 hrs"
        result = parse_tags(tags)
        assert result is not None
        assert result["audio_bitrate_kbps"] == 126
        assert result["audio_format"] == "M4b"  # capitalize() on lookup

    def test_old_audiobook_tags(self):
        """Real tags from the Golden Son probe — sparse."""
        tags = "Red Rising, 0345539834, m4b"
        result = parse_tags(tags)
        assert result is not None
        assert result["audio_format"] == "M4b"
        assert "audio_bitrate_kbps" not in result

    def test_ebook_tags_no_audio_fields(self):
        """Ebook tags don't carry audio info, so nothing matches."""
        tags = "Published Nov 2007 | Fae, Fantasy, Vampires"
        assert parse_tags(tags) is None


# ─── Top-level orchestrator ──────────────────────────────────


class TestExtractQuality:
    def test_modern_audiobook_picks_mediainfo_source(self):
        snap = extract_quality(
            mam_torrent_id="1237094",
            raw_mediainfo=MODERN_AUDIOBOOK_MEDIAINFO,
            description="<p>Some prose.</p>",
            tags="126 kbps m4b | other tags",
            raw_size="658.5 MiB",
            numfiles=1,
            seeders=183,
            times_completed=249,
            torrent_added_at="2026-04-24 12:21:59",
        )
        assert snap.source == "mediainfo"
        assert snap.audio_bitrate_kbps == 126
        assert snap.audio_chapter_count == 5
        assert snap.total_size_bytes == int(658.5 * 1024 ** 2)
        assert snap.seeders == 183

    def test_old_audiobook_falls_back_to_description(self):
        """No mediainfo → description fills in."""
        snap = extract_quality(
            mam_torrent_id="332910",
            raw_mediainfo="{}",
            description=OLD_AUDIOBOOK_DESCRIPTION,
            tags="Red Rising, m4b",
            raw_size="519.2 MiB",
            numfiles=4,
            seeders=738,
            times_completed=2063,
            torrent_added_at="2016-09-25 22:10:01",
        )
        # description picked up most axes; tags also contributed
        # `audio_format` but description won because it ran first
        # and description's value (AAC) overrode nothing (tags only
        # populates m4b on empty axes).
        assert snap.source in ("description", "mixed")
        assert snap.audio_bitrate_kbps == 63
        assert snap.audio_sample_rate == 22050
        assert snap.audio_chapter_count == 51
        # Tags-only fallback for things description doesn't carry.
        assert snap.audio_format == "AAC"

    def test_ebook_has_no_audio_axes(self):
        """mediainfo `{}` + tags without bitrate → source='none'."""
        snap = extract_quality(
            mam_torrent_id="1244212",
            raw_mediainfo="{}",
            description="<p>Just a synopsis with no quality info.</p>",
            tags="Published Nov 2007 | Fae, Fantasy",
            raw_size="1,023.2 KiB",
            numfiles=2,
        )
        assert snap.source == "none"
        assert snap.audio_format is None
        assert snap.audio_bitrate_kbps is None
        # General fields still populated.
        assert snap.num_files == 2
        assert snap.total_size_bytes == int(1023.2 * 1024)

    def test_mixed_source_when_multiple_tiers_contribute(self):
        """mediainfo + tags both contributed; description didn't.

        We construct an artificially-incomplete mediainfo (only the
        General block) so the description/tags fallbacks have axes to
        fill in. With Audio1 absent, parse_mediainfo returns None, so
        no axes come from that tier — the test falls through to
        description/tags only. That makes 'source' = 'description' or
        'tags' depending on which had matches.
        """
        snap = extract_quality(
            mam_torrent_id="x",
            # mediainfo missing Audio1 → no audio extraction
            raw_mediainfo='{"General": {"Format": "MPEG-4"}}',
            # description matches a bitrate
            description="Encoded Bitrate: 128 kbits",
            # tags matches a format (description didn't)
            tags="192 kbps mp3",
            raw_size="500 MiB",
        )
        # Both description (bitrate 128) and tags (format MP3) contributed.
        assert snap.source == "mixed"
        assert snap.audio_bitrate_kbps == 128  # description wins (128, not tags' 192)
        assert snap.audio_format == "Mp3"      # only tags had format

    def test_raw_payloads_preserved(self):
        """raw_mediainfo + raw_tags persisted as-is for future re-parsing."""
        snap = extract_quality(
            mam_torrent_id="x",
            raw_mediainfo='{"some": "blob"}',
            description=None,
            tags="some tags",
        )
        assert snap.raw_mediainfo == '{"some": "blob"}'
        assert snap.raw_tags == "some tags"
