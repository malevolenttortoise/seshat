"""
Parse a MAM torrent ID out of whatever the user pasted.

Accepts a bare ID (`1274788`), a torrent page link
(`https://www.myanonamouse.net/t/1274788`) or a download link
(`.../tor/download.php?tid=1274788`). One home for the parser that
`inject-batch`, Discovery's send-to-pipeline and Manual Grab all use.
"""
from __future__ import annotations

import re
from typing import Optional

_BARE_ID_RX = re.compile(r"^\d+$")
_PAGE_RX = re.compile(r"/t/(\d+)")
_DOWNLOAD_RX = re.compile(r"[?&]tid=(\d+)")


def extract_torrent_id(url_or_id: str) -> Optional[str]:
    s = (url_or_id or "").strip()
    if _BARE_ID_RX.match(s):
        return s
    for rx in (_PAGE_RX, _DOWNLOAD_RX):
        m = rx.search(s)
        if m:
            return m.group(1)
    return None
