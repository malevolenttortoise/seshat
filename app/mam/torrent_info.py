"""
MAM torrent-info lookup.

`get_torrent_info()` queries MAM's search API for a single torrent by
ID to retrieve economic metadata that the IRC announce doesn't always
carry:

  - vip: bool         — permanent or temporary VIP (download is free)
  - free: bool        — global freeleech
  - fl_vip: bool      — freeleech OR VIP (convenience union flag)
  - personal_freeleech: bool — user has already bought FL for this torrent

The IRC announce only carries a `(VIP)` suffix for VIP torrents.
Freeleech status and wedge applicability require this API lookup.

Routes through `cookie._do_post` so cookie auto-rotation fires on
every response.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from app.mam.cookie import MAM_SEARCH_URL, _do_post

_log = logging.getLogger("seshat.mam")

# Cache TTL in seconds (2 minutes — shorter than user_status because
# VIP/FL status can change when site-wide freeleech events start/end).
_CACHE_TTL = 120


@dataclass(frozen=True)
class TorrentInfo:
    """Metadata for a single MAM torrent.

    The "economic" fields (vip/free/fl_vip/personal_freeleech) drive
    the policy engine. The "bibliographic" fields (authors, narrators,
    series, tags, description, language, filetype) are available from
    the same search API call for zero extra cost — the enricher can
    use them as a first-pass metadata source that's faster and more
    authoritative than external scrapers.
    """

    torrent_id: str
    vip: bool
    free: bool
    fl_vip: bool
    personal_freeleech: bool
    category: str       # e.g. "Audiobooks - Urban Fantasy"
    title: str
    size: str           # e.g. "6324306932" (bytes as string)
    # Bibliographic fields — populated from the same search response.
    authors: dict[str, str] = field(default_factory=dict)    # {mam_id: name}
    narrators: dict[str, str] = field(default_factory=dict)  # {mam_id: name}
    series: dict[str, list] = field(default_factory=dict)    # {mam_id: [name, index]}
    tags: str = ""
    description: str = ""
    language_id: str = ""
    filetype: str = ""
    uploader_id: int = 0
    uploader_name: str = ""
    # v2.13.2: ISBN/ASIN extracted from MAM's optional ISBN/ASIN
    # upload-form field. The MAM search API returns a single `isbn`
    # value where uploaders prefix ASINs with "ASIN:" (per the
    # upload form's help text) and leave ISBNs un-prefixed. Empty
    # string when the uploader didn't set the field.
    isbn: str = ""
    asin: str = ""
    # v2.25.0 — raw mediainfo + general-fields for quality extraction.
    # `mediainfo` is the stringified JSON MAM returns when the
    # `mediaInfo: True` payload flag is set. Empty `"{}"` for ebooks
    # (no audio container) and for older audiobook uploads (pre-2024).
    # Downstream consumer is `app/quality/extract.py::extract_quality()`.
    # `size` is already defined above (required positional); these are
    # the additional general-field fallbacks the extractor reads.
    mediainfo: str = ""
    numfiles: int = 0
    seeders: int = 0
    times_completed: int = 0
    added: str = ""             # MAM-formatted upload timestamp


# ─── In-memory cache ────────────────────────────────────────

_cache: dict[str, tuple[float, TorrentInfo]] = {}


def invalidate_cache() -> None:
    """Clear the torrent-info cache."""
    _cache.clear()


# ─── Public API ─────────────────────────────────────────────


async def get_torrent_info(
    torrent_id: str,
    token: Optional[str] = None,
    ttl: int = _CACHE_TTL,
) -> TorrentInfo:
    """Look up a single torrent's economic metadata from MAM.

    Returns a cached result if one exists within `ttl` seconds.
    Raises `TorrentInfoError` on any failure.

    Args:
        torrent_id: The numeric MAM torrent ID (string).
        token: Explicit mam_id cookie value. If None, uses the
               module-level current token from cookie.py.
        ttl: Cache lifetime in seconds. Pass 0 to force a fresh fetch.
    """
    now = time.monotonic()

    if ttl > 0 and torrent_id in _cache:
        cached_at, cached_info = _cache[torrent_id]
        if now - cached_at < ttl:
            _log.debug("torrent_info cache hit for tid=%s", torrent_id)
            return cached_info

    _log.info("Fetching MAM torrent info for tid=%s", torrent_id)

    # MAM's loadSearchJSONbasic.php uses payload-root flags to opt
    # into optional fields. The v2.18.2 probe missed `mediaInfo`;
    # v2.25.0 confirmed it works via the API reference + live probe.
    # See `feedback_seshat_mam_search_api_flags.md` memory.
    #
    # `isbn: True` (v2.13.2) — returns the uploader's optional
    # ISBN/ASIN field. Empty string when not filled. ASINs are
    # prefixed "ASIN:" per the upload form's help text.
    #
    # `description: True` (v2.18.2) — returns the full uploader-
    # written synopsis as HTML. Normalized to plain text downstream
    # by description_to_plain_text. Without this flag MAM returns
    # NO description at all (the key is absent), which let a
    # Goodreads boilerplate string ("Discover and share books you
    # love on Goodreads.") win the longest-wins merge on Spirit
    # Blade (tid=1243620).
    #
    # `mediaInfo: True` (v2.25.0) — returns the `mediainfo` field
    # carrying stringified JSON with audio container metadata
    # (bitrate, codec, channels, sample rate, chapters). Drives
    # the quality-metadata extraction pipeline. Empty `"{}"` for
    # ebooks and older audiobook uploads where the uploader didn't
    # include mediainfo.
    payload = json.dumps({
        "tor": {
            "id": torrent_id,
            "searchType": "all",
            "searchIn": "torrents",
            "cat": ["0"],
            "sortType": "default",
            "startNumber": "0",
        },
        "perpage": 1,
        "isbn": True,
        "description": True,
        "mediaInfo": True,
    })

    try:
        resp = await _do_post(MAM_SEARCH_URL, token=token, payload=payload, timeout=15)
    except Exception as exc:
        raise TorrentInfoError(f"network error: {exc}") from exc

    if resp.status_code != 200:
        raise TorrentInfoError(f"HTTP {resp.status_code} from search API")

    if not resp.text:
        raise TorrentInfoError("empty response from search API — cookie may be invalid")

    try:
        data = resp.json()
    except Exception as exc:
        raise TorrentInfoError(f"invalid JSON: {resp.text[:200]}") from exc

    items = data.get("data", [])
    if not items:
        raise TorrentInfoError(f"torrent {torrent_id} not found in search results")

    item = items[0]

    parsed_isbn, parsed_asin = _classify_identifier(item.get("isbn"))

    info = TorrentInfo(
        torrent_id=str(item.get("id", torrent_id)),
        vip=_to_bool(item.get("vip")),
        free=_to_bool(item.get("free")),
        fl_vip=_to_bool(item.get("fl_vip")),
        personal_freeleech=_to_bool(item.get("personal_freeleech")),
        category=str(item.get("catname", "")),
        title=str(item.get("title", item.get("name", ""))),
        size=str(item.get("size", "")),
        authors=_parse_json_field(item.get("author_info")),
        narrators=_parse_json_field(item.get("narrator_info")),
        series=_parse_json_field(item.get("series_info")),
        tags=str(item.get("tags", "")),
        description=str(item.get("description", "")),
        language_id=str(item.get("language", "")),
        filetype=str(item.get("filetype", "")),
        uploader_id=_parse_ownership_id(item.get("ownership")),
        uploader_name=_parse_ownership_name(item.get("ownership")),
        isbn=parsed_isbn,
        asin=parsed_asin,
        mediainfo=str(item.get("mediainfo", "")),
        numfiles=int(item.get("numfiles") or 0),
        seeders=int(item.get("seeders") or 0),
        times_completed=int(item.get("times_completed") or 0),
        added=str(item.get("added", "")),
    )

    _cache[torrent_id] = (now, info)
    _log.info(
        "MAM torrent tid=%s: vip=%s, free=%s, fl_vip=%s, pfl=%s",
        torrent_id,
        info.vip,
        info.free,
        info.fl_vip,
        info.personal_freeleech,
    )
    return info


def _parse_json_field(value) -> dict:
    """Decode author_info / narrator_info / series_info.

    MAM returns these as JSON-encoded strings inside the JSON response:
      "author_info": "{\"8234\": \"Kerrelyn Sparks\"}"
      "series_info": "{\"67\": [\"Love at Stake\", \"01-16, 13.5\"]}"
    Returns an empty dict on any parse failure.
    """
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _parse_ownership_id(value) -> int:
    """Extract the uploader user ID from the `ownership` field.

    MAM returns ownership as `[user_id, "username"]`.
    """
    if isinstance(value, list) and len(value) >= 1:
        try:
            return int(value[0])
        except (ValueError, TypeError):
            pass
    return 0


def _parse_ownership_name(value) -> str:
    """Extract the uploader username from the `ownership` field."""
    if isinstance(value, list) and len(value) >= 2:
        return str(value[1])
    return ""


def _to_bool(value) -> bool:
    """Coerce MAM's mixed boolean representations to Python bool.

    MAM's search API returns booleans as strings ("0"/"1"), integers,
    or actual booleans depending on the field and the response format.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return False


# v2.13.2: ASINs on Amazon's modern catalog (Kindle) start with "B0" +
# 8 alphanumerics. The MAM upload form's help text explicitly tells
# uploaders to prefix ASIN values with "ASIN:" (because the autofill
# button doesn't handle ASIN). But uploaders sometimes forget the
# prefix — defensive ASIN sniffing catches the bare-B0 case.
import re as _re
_ASIN_BARE_RX = _re.compile(r"^B0[A-Z0-9]{8}$")


def _classify_identifier(raw) -> tuple[str, str]:
    """Split MAM's `isbn` field value into `(isbn, asin)`.

    MAM stores one optional identifier per torrent in a single
    free-text field. Uploaders follow a convention documented on the
    upload form: prefix ASINs with `ASIN:` (the autofill button only
    handles ISBN). v2.13.2: probe confirmed the same field also
    sometimes contains bare ISBN-13 with or without dashes (e.g.
    `9798902092261`, `979-8895615560`) and prefixed ASIN entries
    (e.g. `ASIN:B0H1XKSFHQ`).

    Note (v2.13.2 UAT): MAM serializes bare-digit ISBNs as JSON
    INTEGERS (no quotes), so this accepts int inputs too. ASINs
    always arrive as strings because they contain letters.

    Classification:
      - None / empty / dict / list / bool → ("", "")
      - bare int                          → treat as ISBN (digits-only)
      - case-insensitive "ASIN:" prefix   → ("", stripped uppercase)
      - bare 10-char `B0XXXXXXXX` pattern → ("", uppercased) — defensive
      - case-insensitive "ISBN:" prefix   → (stripped digits-only, "")
      - anything else                     → treat as ISBN, digits-only
    """
    if isinstance(raw, bool):
        # bool is a subclass of int — explicitly reject it before the
        # int branch below promotes True to "1".
        return "", ""
    if isinstance(raw, int):
        raw = str(raw)
    if not isinstance(raw, str):
        return "", ""
    text = raw.strip()
    if not text:
        return "", ""

    low = text.lower()
    if low.startswith("asin:"):
        return "", text[5:].strip().upper()
    if low.startswith("isbn:"):
        digits = _re.sub(r"[^0-9Xx]", "", text[5:])
        return digits.upper(), ""

    # Bare ASIN-shaped values (uploader forgot the prefix).
    upper = text.upper()
    if _ASIN_BARE_RX.match(upper):
        return "", upper

    # Default: treat as ISBN. Strip dashes / spaces; preserve trailing
    # `X` checksum character on ISBN-10.
    digits = _re.sub(r"[^0-9Xx]", "", text)
    if not digits:
        return "", ""
    return digits.upper(), ""


def mam_cover_url(torrent_id: str) -> str:
    """Build the CDN cover image URL for a torrent.

    MAM serves poster images at a CDN endpoint that requires:
      - the mam_id cookie (same as all MAM API calls)
      - a current-epoch timestamp as a cache-buster segment
      - the torrent ID

    The image is typically JPEG. Returns the URL string — the
    caller fetches it through the cookie-aware HTTP client.
    """
    import time
    ts = int(time.time())
    return f"https://cdn.myanonamouse.net/t/p/{ts}/large/{torrent_id}.jpeg"


class TorrentInfoError(Exception):
    """Raised when the torrent-info lookup fails."""
