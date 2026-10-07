"""MAM's torrent size, as bytes: the human-readable strings the search
API really sends, and the bare bytes its own reference documents."""
import pytest

from app.mam.size import parse_size_to_bytes
from app.mam.torrent_info import TorrentInfo


@pytest.mark.parametrize("raw,expected", [
    ("2.6 GiB", int(2.6 * 1024 ** 3)),
    ("856.3 MiB", int(856.3 * 1024 ** 2)),
    ("1,023.2 KiB", int(1023.2 * 1024)),
    ("1 MiB", 1024 ** 2),
    ("100 MB", 100_000_000),
    ("6324306932", 6324306932),
    (" 512 B ", 512),
    (1234, 1234),
])
def test_parses(raw, expected):
    assert parse_size_to_bytes(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "a lot", "1.2 parsecs", True, 1.5])
def test_refuses_to_guess(raw):
    assert parse_size_to_bytes(raw) is None


def test_torrent_info_size_bytes():
    info = TorrentInfo(
        torrent_id="1", vip=False, free=False, fl_vip=False,
        personal_freeleech=False, category="", title="", size="1.2 GiB",
    )
    assert info.size_bytes == int(1.2 * 1024 ** 3)
