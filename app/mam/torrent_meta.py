"""
Torrent file metadata extraction.

The single use case: compute the SHA1 info hash of a `.torrent` file
so the dispatcher can pass that hash to `ledger.record_grab` at
submission time. qBittorrent's `/api/v2/torrents/add` endpoint
doesn't return the hash for newly-added torrents, so without this
we'd be racing list_torrents calls to find the new entry — fragile
and hard to test.

The info hash IS the SHA1 of the bencoded `info` dictionary, raw
bytes from the file. The trick is finding the byte range of the
info dict in the bencoded blob without disturbing it (re-encoding
breaks the hash because bencode allows multiple representations of
the same data). We use a minimal bencode walker that returns the
byte ranges of every value it parses.

Bencode is one of the simplest binary serialization formats:

  - integers:  i<n>e            (e.g. i42e)
  - strings:   <len>:<bytes>    (e.g. 4:spam)
  - lists:     l<items>e
  - dicts:     d<key><val>...e  (keys are byte strings, sorted)

This module implements just enough of the spec to find the `info`
key inside the top-level dict and return its value's byte slice.
~80 lines, no dependencies.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional, Tuple


class BencodeError(ValueError):
    """Raised when a torrent file isn't valid bencode."""


def info_hash(torrent_bytes: bytes) -> str:
    """Compute the lowercase-hex SHA1 info hash of a .torrent file.

    Returns a 40-character lowercase hex string. The format matches
    what qBittorrent reports in its `hash` field, so the result can
    be compared directly against `TorrentInfo.hash` from a
    `qbit.list_torrents` call.

    Raises:
        BencodeError: if the input isn't valid bencode or doesn't
            have a top-level dict with an `info` key.
    """
    if not torrent_bytes:
        raise BencodeError("empty input")

    if torrent_bytes[:1] != b"d":
        raise BencodeError("torrent file must start with a top-level dict")

    # Walk the top-level dict looking for the `info` key. We need
    # the BYTE RANGE of its value (not the parsed structure) so we
    # can hash the original bytes verbatim.
    pos = 1  # skip leading 'd'
    while pos < len(torrent_bytes):
        if torrent_bytes[pos:pos + 1] == b"e":
            break  # end of top-level dict — no info key found
        key, pos = _read_string(torrent_bytes, pos)
        value_start = pos
        pos = _walk(torrent_bytes, pos)
        if key == b"info":
            return hashlib.sha1(torrent_bytes[value_start:pos]).hexdigest()

    raise BencodeError("no `info` key in top-level dict")


def top_level_string(torrent_bytes: bytes, key: bytes) -> Optional[bytes]:
    """The raw value of a top-level string key (e.g. `comment`), or None.

    Raises BencodeError if the input isn't a bencoded dict.
    """
    if not torrent_bytes or torrent_bytes[:1] != b"d":
        raise BencodeError("torrent file must start with a top-level dict")
    pos = 1
    while pos < len(torrent_bytes):
        if torrent_bytes[pos:pos + 1] == b"e":
            return None
        k, pos = _read_string(torrent_bytes, pos)
        if k == key and torrent_bytes[pos:pos + 1].isdigit():
            value, _ = _read_string(torrent_bytes, pos)
            return value
        pos = _walk(torrent_bytes, pos)
    raise BencodeError("unterminated top-level dict")


@dataclass(frozen=True)
class MamComment:
    torrent_id: str
    uid: Optional[int]


_MID_RX = re.compile(rb"\bMID=(\d+)")
_UID_RX = re.compile(rb"\bUID=(\d+)")


def read_mam_comment(torrent_bytes: bytes) -> Optional[MamComment]:
    """MAM's stamp on a .torrent it served: `comment = "MID=<torrent
    id>,UID=<downloading account>"` (every one of 388 grabbed files
    checked, 2026-10-06). None when there's no MID — not a MAM file.

    Raises BencodeError if the input isn't a bencoded dict.
    """
    comment = top_level_string(torrent_bytes, b"comment") or b""
    mid = _MID_RX.search(comment)
    if mid is None:
        return None
    uid = _UID_RX.search(comment)
    return MamComment(
        torrent_id=mid.group(1).decode(),
        uid=int(uid.group(1)) if uid else None,
    )


# ─── Minimal bencode walker ──────────────────────────────────
# Each `_walk_*` function takes the buffer + a start index and
# returns the index ONE PAST the end of the value it parsed. We
# don't decode the values themselves — only locate their byte
# ranges so the info-dict slice can be sliced cleanly.


def _walk(data: bytes, pos: int) -> int:
    if pos >= len(data):
        raise BencodeError("unexpected end of input")
    head = data[pos:pos + 1]
    if head == b"d":
        return _walk_dict(data, pos)
    if head == b"l":
        return _walk_list(data, pos)
    if head == b"i":
        return _walk_int(data, pos)
    if head.isdigit():
        return _skip_string(data, pos)
    raise BencodeError(f"unexpected byte at offset {pos}: {head!r}")


def _walk_dict(data: bytes, pos: int) -> int:
    pos += 1  # skip 'd'
    while pos < len(data) and data[pos:pos + 1] != b"e":
        _, pos = _read_string(data, pos)
        pos = _walk(data, pos)
    if pos >= len(data):
        raise BencodeError("unterminated dict")
    return pos + 1  # skip closing 'e'


def _walk_list(data: bytes, pos: int) -> int:
    pos += 1  # skip 'l'
    while pos < len(data) and data[pos:pos + 1] != b"e":
        pos = _walk(data, pos)
    if pos >= len(data):
        raise BencodeError("unterminated list")
    return pos + 1  # skip closing 'e'


def _walk_int(data: bytes, pos: int) -> int:
    end = data.find(b"e", pos)
    if end < 0:
        raise BencodeError("unterminated int")
    return end + 1


def _read_string(data: bytes, pos: int) -> Tuple[bytes, int]:
    """Read a `<len>:<bytes>` bencoded string. Returns (value, new_pos)."""
    colon = data.find(b":", pos)
    if colon < 0:
        raise BencodeError("string missing length delimiter")
    try:
        length = int(data[pos:colon])
    except ValueError as e:
        raise BencodeError(f"invalid string length at {pos}: {e}")
    start = colon + 1
    end = start + length
    if end > len(data):
        raise BencodeError("string length exceeds buffer")
    return data[start:end], end


def _skip_string(data: bytes, pos: int) -> int:
    _, new_pos = _read_string(data, pos)
    return new_pos
