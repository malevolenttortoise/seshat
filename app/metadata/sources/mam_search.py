"""
MAM search API metadata source.

Uses the same `get_torrent_info()` call that the policy engine
already makes — no extra API hit. The torrent_info response carries
structured author/narrator/series/tags/description data that is
more authoritative than any external scraper because MAM is the
actual source of truth for the torrent's metadata.

Priority: this source should run BEFORE external scrapers in the
enricher priority list. It's free (cached from the policy lookup),
fast, and the highest-confidence match possible (the torrent ID is
an exact key, not a fuzzy text search).

The search_book method takes a title + author like every other
source, but internally it looks up the torrent by ID if one is
available on the enricher's context. When called without a torrent
ID, it falls back to a keyword search (less precise but still MAM-
authoritative).
"""
from __future__ import annotations

import logging
from typing import Optional

from app.metadata.record import MetaRecord
from app.metadata.sources.base import MetaSource
from app.mam.torrent_info import TorrentInfo, TorrentInfoError, get_torrent_info

_log = logging.getLogger("seshat.metadata.sources.mam_search")


class MamSearchSource(MetaSource):
    """Metadata from MAM's own search API.

    Unlike other sources, this one can be initialized with a
    `mam_token` and an optional `torrent_id` for exact lookups.
    If torrent_id is set, search_book ignores the title/author
    args and does a direct ID lookup (guaranteed single result).
    """

    name = "mam"
    default_timeout = 15.0

    def __init__(
        self,
        *,
        mam_token: str = "",
        torrent_id: str = "",
        rate_limit: float = 0,
    ):
        super().__init__(rate_limit=rate_limit)
        self._token = mam_token
        self._torrent_id = torrent_id

    async def search_book(
        self, title: str, author: str, **_,
    ) -> Optional[MetaRecord]:
        if not self._token:
            return None
        if not self._torrent_id:
            return None  # keyword search not implemented yet

        try:
            info = await get_torrent_info(
                self._torrent_id, token=self._token, ttl=300
            )
        except TorrentInfoError as e:
            _log.info("mam_search: lookup failed for tid=%s: %s",
                      self._torrent_id, e)
            return None

        return _info_to_record(info)


def _info_to_record(info: TorrentInfo) -> MetaRecord:
    """Convert a TorrentInfo (with rich fields) to a MetaRecord."""
    authors = list(info.authors.values()) if info.authors else []
    narrators = list(info.narrators.values()) if info.narrators else []

    series_name = None
    series_index = None
    if info.series:
        for _sid, sdata in info.series.items():
            if isinstance(sdata, list) and len(sdata) >= 1:
                series_name = str(sdata[0])
                if len(sdata) >= 2:
                    idx_str = str(sdata[1])
                    try:
                        series_index = float(idx_str)
                    except (ValueError, TypeError):
                        pass
                break

    # Strip BBCode from the description for plain-text display.
    description = _strip_bbcode(info.description) if info.description else None

    # Tags from MAM carry genre + format info as a space-separated string.
    tags = [t.strip() for t in info.tags.split() if t.strip()] if info.tags else []

    return MetaRecord(
        title=info.title or "",
        authors=authors,
        series=series_name,
        series_index=series_index,
        description=description,
        language=None,  # info.language_id is a numeric ID, not a name
        tags=tags,
        # v2.13.2: surface MAM's optional ISBN/ASIN upload-form field
        # so downstream sources (GoodreadsSource T1-T3, Hardcover,
        # OpenLibrary) can use the identifier for direct lookup.
        # Empty when the uploader didn't fill the form's ISBN/ASIN box.
        isbn=info.isbn or None,
        asin=info.asin or None,
        source="mam",
        source_url=f"https://www.myanonamouse.net/t/{info.torrent_id}",
        external_id=info.torrent_id,
        confidence=1.0,  # exact torrent ID lookup — can't get better
    )


def _strip_bbcode(text: str) -> str:
    """Rough BBCode → plain text. Good enough for review queue display."""
    import re
    text = re.sub(r'\[/?(?:b|i|u|s|size|color|url|img|quote|code|spoiler|list|\*)(?:=[^\]]*)?]', '', text)
    text = re.sub(r'\[hr]', '\n', text)
    text = re.sub(r'\r\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()
