"""
Quality-metadata extraction from MAM's torrent-info API response.

Three-tier fallback chain so we capture quality even on uploads where
the modern `mediaInfo` field is empty (older torrents pre-2024):

  1. Parse the `mediainfo` JSON field (modern uploads — exact data)
  2. Parse `description` for known structured blocks (inAudible-style)
  3. Parse the `tags` line for kbps/format keywords

Each `QualitySnapshot` carries a `source` tag identifying which tier
produced its data. Future re-extractions can target weak rows first.

Sample data shapes (captured via live probe 2026-05-23, files under
`/home/mbaker/Documents/Projects/files/mam-mediainfo-*.json`):

  modern audiobook → `mediainfo` populated with Audio1/General/menu
  modern ebook     → `mediainfo` = "{}"  (no audio to describe)
  old audiobook    → `mediainfo` = "{}"  but description has
                     inAudible "Media Information" block with
                     Codec/Sample Rate/Channels/Bitrate fields
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

_log = logging.getLogger("seshat.quality.extract")


# ─── Output dataclass ─────────────────────────────────────────


@dataclass(frozen=True)
class QualitySnapshot:
    """Extracted quality data for one MAM torrent.

    Audio fields are None for ebook torrents (no audio container).
    `source` identifies which extraction tier produced the data:
        - 'mediainfo'   — parsed from MAM's mediainfo JSON
        - 'description' — parsed from inAudible-style block
        - 'tags'        — parsed from tags line
        - 'mixed'       — multiple sources contributed
        - 'none'        — no quality axes could be filled
    """
    mam_torrent_id: str
    source: str  # 'mediainfo' | 'description' | 'tags' | 'mixed' | 'none'

    # Audio fields.
    audio_format: Optional[str] = None        # 'AAC', 'MP3', 'FLAC'
    audio_bitrate_kbps: Optional[int] = None  # 126
    audio_channels: Optional[int] = None      # 2
    audio_bitrate_mode: Optional[str] = None  # 'CBR' | 'VBR'
    audio_sample_rate: Optional[int] = None   # 44100
    audio_compression: Optional[str] = None   # 'Lossy' | 'Lossless'
    audio_codec_id: Optional[str] = None      # '2 / 40 / mp4a-40-2'
    audio_duration_sec: Optional[int] = None
    audio_chapter_count: Optional[int] = None
    container_format: Optional[str] = None    # 'MPEG-4'

    # General fields (audio + ebook).
    num_files: Optional[int] = None
    total_size_bytes: Optional[int] = None
    seeders: Optional[int] = None
    times_completed: Optional[int] = None
    torrent_added_at: Optional[str] = None

    # Raw payloads — persisted so future axes (not yet designed) can be
    # parsed without re-calling MAM.
    raw_mediainfo: Optional[str] = None
    raw_tags: Optional[str] = None


# ─── Primary tier: mediainfo JSON ─────────────────────────────


def _parse_mediainfo_json(raw: str) -> dict:
    """Decode MAM's stringified mediainfo field.

    Returns {} for empty / missing / unparseable input. Mirrors the
    pattern from `_parse_json_field()` in app/mam/torrent_info.py.
    """
    if not raw or not isinstance(raw, str):
        return {}
    raw = raw.strip()
    if not raw or raw == "{}":
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _bitrate_str_to_kbps(value) -> Optional[int]:
    """Coerce MAM's mediainfo BitRate strings to integer kbps.

    Seen forms (modern uploads):
        "126k"     → 126
        "192 kbps" → 192
        "128000"   → 128 (assume bits-per-second when bare int)
        "1.5M"     → 1500
        128        → 128 (assume kbps for bare int from JSON int)
    """
    if value is None:
        return None
    if isinstance(value, int):
        # mediainfo's BitRate is typically already kbps when bare int.
        # Some uploaders emit raw bps — cap-detect: >50000 is bps.
        return value // 1000 if value >= 50000 else value
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if not s:
        return None
    # Strip "kbps" / "kbit/s" suffix variants.
    s = re.sub(r"\s*(kbps|kbit/s|kbits/s|kbits)\b", "", s)
    m = re.match(r"^([\d.]+)\s*([kKmM])?\s*$", s)
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    if unit == "m":
        return int(n * 1000)
    return int(n)


def _sample_rate_to_hz(value) -> Optional[int]:
    """Coerce sample rate strings to Hz.

    Seen forms:
        "44.1kHz"  → 44100
        "44.1 kHz" → 44100
        "48000"    → 48000
        "22050 Hz" → 22050
    """
    if value is None:
        return None
    if isinstance(value, int):
        # Bare int may already be Hz (44100) or kHz (44).
        return value * 1000 if value < 1000 else value
    if not isinstance(value, str):
        return None
    s = value.strip().lower()
    if not s:
        return None
    s = re.sub(r"\s*(hz|hertz)\s*$", "", s)
    m = re.match(r"^([\d.]+)\s*(k?)\s*$", s)
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    if m.group(2) == "k":
        return int(n * 1000)
    return int(n)


def _duration_to_seconds(value) -> Optional[int]:
    """Parse "HH:MM:SS" or "H:MM:SS" duration into seconds.

    Mediainfo emits "12:05:07" — three colon-separated integer parts.
    Tolerant of leading/trailing whitespace.
    """
    if value is None or not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    return h * 3600 + m * 60 + s


def parse_mediainfo(
    mam_torrent_id: str,
    raw_mediainfo: str,
) -> Optional[dict]:
    """Extract audio axes from a parsed mediainfo block.

    Returns a partial QualitySnapshot dict (kwargs), or None when the
    mediainfo is empty / unparseable / contains no Audio1 stream.

    Caller merges with general-field extraction + sets the `source`
    field appropriately.
    """
    parsed = _parse_mediainfo_json(raw_mediainfo)
    if not parsed:
        return None

    audio = parsed.get("Audio1")
    if not isinstance(audio, dict):
        return None

    general = parsed.get("General") or {}
    menu = parsed.get("menu") or {}
    menu_extra = menu.get("extra") if isinstance(menu, dict) else None
    chapter_count: Optional[int] = None
    if isinstance(menu_extra, list):
        chapter_count = len(menu_extra)

    return {
        "audio_format": str(audio.get("Format") or "").strip() or None,
        "audio_bitrate_kbps": _bitrate_str_to_kbps(audio.get("BitRate")),
        "audio_channels": _coerce_int(audio.get("Channels")),
        "audio_bitrate_mode": str(audio.get("BitRate_Mode") or "").strip() or None,
        "audio_sample_rate": _sample_rate_to_hz(audio.get("SamplingRate")),
        "audio_compression": str(audio.get("Compression_Mode") or "").strip() or None,
        "audio_codec_id": str(audio.get("CodecID") or "").strip() or None,
        "audio_duration_sec": _duration_to_seconds(general.get("Duration")),
        "audio_chapter_count": chapter_count,
        "container_format": str(general.get("Format") or "").strip() or None,
    }


def _coerce_int(value) -> Optional[int]:
    """Coerce mixed int/string/float values to int, or None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


# ─── Second tier: description text (inAudible block) ──────────


# inAudible's ripper outputs a structured "Media Information" block
# that's been a de facto standard among MAM audiobook uploaders since
# at least 2014. Real example from torrent 332910 (Golden Son, 2016):
#
#   Encoded Codec: AAC / M4B
#   Encoded Sample Rate: 22050 Hz
#   Encoded Channels: 2
#   Encoded Bitrate: 63 kbits
#   Lossless Encode: Yes
#   Chapters: 51
#   Duration: 19 hours, 3 minutes, 23 seconds
#
# These regexes target the "Encoded *" labels (the post-rip values
# Seshat actually cares about; the "Source *" values describe the
# pre-rip source which isn't the file we get). Tolerant of <br /> and
# whitespace inside MAM's HTML-flavored description format.
_DESC_PATTERNS = {
    "audio_format": re.compile(
        r"Encoded\s*Codec\s*:\s*([A-Za-z0-9 /+]+)", re.IGNORECASE
    ),
    "audio_bitrate_kbps_text": re.compile(
        r"Encoded\s*Bitrate\s*:\s*(\d+)\s*kb(?:its|ps)?", re.IGNORECASE
    ),
    "audio_channels_text": re.compile(
        r"Encoded\s*Channels\s*:\s*(\d+)", re.IGNORECASE
    ),
    "audio_sample_rate_text": re.compile(
        r"Encoded\s*Sample\s*Rate\s*:\s*(\d+)\s*Hz", re.IGNORECASE
    ),
    "audio_chapter_count_text": re.compile(
        r"Chapters?\s*:\s*(\d+)", re.IGNORECASE
    ),
    "audio_compression_text": re.compile(
        r"Lossless\s*Encode\s*:\s*(Yes|No|True|False)", re.IGNORECASE
    ),
}


def parse_description(description: str) -> Optional[dict]:
    """Extract audio axes from inAudible-style description blocks.

    Returns a partial QualitySnapshot dict (kwargs), or None if no
    pattern matched. Caller merges + tags as 'description' source.
    """
    if not description or not isinstance(description, str):
        return None
    out: dict = {}

    m = _DESC_PATTERNS["audio_format"].search(description)
    if m:
        # "AAC / M4B" → take the first token; M4B is the container.
        token = m.group(1).strip().split("/")[0].strip()
        out["audio_format"] = token or None

    m = _DESC_PATTERNS["audio_bitrate_kbps_text"].search(description)
    if m:
        try:
            out["audio_bitrate_kbps"] = int(m.group(1))
        except ValueError:
            pass

    m = _DESC_PATTERNS["audio_channels_text"].search(description)
    if m:
        try:
            out["audio_channels"] = int(m.group(1))
        except ValueError:
            pass

    m = _DESC_PATTERNS["audio_sample_rate_text"].search(description)
    if m:
        try:
            out["audio_sample_rate"] = int(m.group(1))
        except ValueError:
            pass

    m = _DESC_PATTERNS["audio_chapter_count_text"].search(description)
    if m:
        try:
            out["audio_chapter_count"] = int(m.group(1))
        except ValueError:
            pass

    m = _DESC_PATTERNS["audio_compression_text"].search(description)
    if m:
        word = m.group(1).strip().lower()
        out["audio_compression"] = "Lossless" if word in ("yes", "true") else "Lossy"

    return out or None


# ─── Third tier: tags line ────────────────────────────────────


# Tags line examples seen in probe data:
#   "126 kbps m4b with chapters | Release Date 04-23-26 | ..."  (modern audiobook)
#   "Red Rising, 0345539834, m4b"                              (old audiobook)
#   "Published Nov 2007 | Fae, Fantasy, ... | Vampires"        (ebook)
#
# The signal we can pull from tags is mostly "<int> kbps" + the format
# token (m4b/mp3/flac/aac). Worse than description parsing but useful
# as last-resort for very old uploads.
_TAGS_BITRATE = re.compile(
    r"(\d+)\s*kbps?\b", re.IGNORECASE
)
_TAGS_FORMAT_TOKENS = ("flac", "m4b", "m4a", "mp3", "aac", "ogg", "opus")


def parse_tags(tags: str) -> Optional[dict]:
    """Best-effort extraction from the `tags` line.

    Mostly catches bitrate ("126 kbps") and format token (m4b/mp3/etc.).
    Returns None when nothing matches.
    """
    if not tags or not isinstance(tags, str):
        return None
    out: dict = {}

    m = _TAGS_BITRATE.search(tags)
    if m:
        try:
            out["audio_bitrate_kbps"] = int(m.group(1))
        except ValueError:
            pass

    low = tags.lower()
    for tok in _TAGS_FORMAT_TOKENS:
        if re.search(rf"\b{tok}\b", low):
            # `audio_format` from tags is the FILE container, not the
            # codec. We surface it as audio_format so a downstream
            # consumer at least knows what to look for; the more
            # accurate value will overwrite if mediainfo or
            # description parsing also fires.
            out["audio_format"] = tok.upper() if tok == "aac" else tok.capitalize()
            break

    return out or None


# ─── Size parsing ─────────────────────────────────────────────


# MAM's `size` field is a human-readable string like "658.5 MiB" or
# "1,023.2 KiB". We want bytes so future quality scoring can use
# size-per-duration ratios for bitrate sanity checks.
_SIZE_RE = re.compile(
    r"^\s*([\d,]+(?:\.\d+)?)\s*(B|KiB|MiB|GiB|TiB|KB|MB|GB|TB)\s*$",
    re.IGNORECASE,
)
_SIZE_UNITS = {
    "b": 1,
    "kib": 1024,
    "mib": 1024 ** 2,
    "gib": 1024 ** 3,
    "tib": 1024 ** 4,
    "kb": 1000,
    "mb": 1000 ** 2,
    "gb": 1000 ** 3,
    "tb": 1000 ** 4,
}


def parse_size_to_bytes(value) -> Optional[int]:
    """Parse MAM's human-readable size string into bytes.

    Returns None on any parse failure rather than guessing.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    m = _SIZE_RE.match(value.strip())
    if not m:
        return None
    num_str = m.group(1).replace(",", "")
    unit = m.group(2).lower()
    try:
        n = float(num_str)
    except ValueError:
        return None
    return int(n * _SIZE_UNITS.get(unit, 1))


# ─── Top-level orchestrator ───────────────────────────────────


def extract_quality(
    mam_torrent_id: str,
    raw_mediainfo: Optional[str],
    description: Optional[str],
    tags: Optional[str],
    raw_size: Optional[str] = None,
    numfiles: Optional[int] = None,
    seeders: Optional[int] = None,
    times_completed: Optional[int] = None,
    torrent_added_at: Optional[str] = None,
) -> QualitySnapshot:
    """Run all three extraction tiers and merge into a QualitySnapshot.

    Precedence: mediainfo > description > tags. Each axis is set by
    the highest-quality source that found it; later sources fill gaps
    but never overwrite. `source` reflects the highest tier that
    contributed (with 'mixed' when more than one tier did).
    """
    axes: dict = {}
    sources_used: list[str] = []

    primary = parse_mediainfo(mam_torrent_id, raw_mediainfo or "")
    if primary:
        axes.update({k: v for k, v in primary.items() if v is not None})
        sources_used.append("mediainfo")

    secondary = parse_description(description or "")
    if secondary:
        added = False
        for k, v in secondary.items():
            if v is not None and axes.get(k) is None:
                axes[k] = v
                added = True
        if added:
            sources_used.append("description")

    tertiary = parse_tags(tags or "")
    if tertiary:
        added = False
        for k, v in tertiary.items():
            if v is not None and axes.get(k) is None:
                axes[k] = v
                added = True
        if added:
            sources_used.append("tags")

    if not sources_used:
        source = "none"
    elif len(sources_used) == 1:
        source = sources_used[0]
    else:
        source = "mixed"

    return QualitySnapshot(
        mam_torrent_id=mam_torrent_id,
        source=source,
        num_files=numfiles,
        total_size_bytes=parse_size_to_bytes(raw_size),
        seeders=seeders,
        times_completed=times_completed,
        torrent_added_at=torrent_added_at,
        raw_mediainfo=raw_mediainfo,
        raw_tags=tags,
        **axes,
    )
