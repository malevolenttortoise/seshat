"""
MAM's torrent `size`, as bytes.

The search API returns `size` as a human-readable string — "658.5 MiB",
"1,023.2 KiB", "2.6 GiB" (`torrent_quality_metadata.total_size_bytes`
in prod holds the parsed values) — although MAM's own API reference
shows bare bytes ("6324306932"). Both parse here. Everything that needs
a torrent's size in bytes (the buffer gate, the economy preflight,
Manual Grab, quality metadata) goes through `parse_size_to_bytes`;
`int(info.size)` silently failed on every real torrent until 2026-10-06.
"""
from __future__ import annotations

import re
from typing import Optional

_SIZE_RE = re.compile(
    r"^\s*([\d,]+(?:\.\d+)?)\s*(B|KiB|MiB|GiB|TiB|KB|MB|GB|TB)?\s*$",
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
    """Parse MAM's size ("658.5 MiB", or bare bytes) into bytes.

    Returns None on any parse failure rather than guessing.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    m = _SIZE_RE.match(value)
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (m.group(2) or "b").lower()
    return int(n * _SIZE_UNITS[unit])
