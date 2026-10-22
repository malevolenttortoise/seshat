"""Transmission, Deluge and rTorrent tell a failed list from an empty one
(2026-10 audit issue 09): `list_torrents_checked` is None on failure, and
`list_torrents` still returns []."""
import pytest

from app.clients.deluge import DelugeClient
from app.clients.rtorrent import RtorrentClient
from app.clients.transmission import TransmissionClient


async def _boom(*args, **kwargs):
    raise RuntimeError("client down")


@pytest.mark.parametrize("make", [
    lambda: TransmissionClient(base_url="http://127.0.0.1:1", username="", password=""),
    lambda: DelugeClient(base_url="http://127.0.0.1:1", password=""),
])
async def test_rpc_failure_is_none(make, monkeypatch):
    client = make()
    monkeypatch.setattr(client, "_rpc", _boom)
    if hasattr(client, "_ensure_logged_in"):
        async def _yes():
            return True
        monkeypatch.setattr(client, "_ensure_logged_in", _yes)
    try:
        assert await client.list_torrents_checked() is None
        assert await client.list_torrents() == []
    finally:
        await client.aclose()


async def test_deluge_not_logged_in_is_none(monkeypatch):
    client = DelugeClient(base_url="http://127.0.0.1:1", password="")

    async def _no():
        return False

    monkeypatch.setattr(client, "_ensure_logged_in", _no)
    try:
        assert await client.list_torrents_checked() is None
    finally:
        await client.aclose()


async def test_rtorrent_failure_is_none(monkeypatch):
    client = RtorrentClient(base_url="http://127.0.0.1:1", username="", password="")

    def _broken_proxy():
        raise RuntimeError("client down")

    monkeypatch.setattr(client, "_get_proxy", _broken_proxy)
    try:
        assert await client.list_torrents_checked() is None
        assert await client.list_torrents() == []
    finally:
        await client.aclose()


def test_only_qbit_reports_progress():
    """G173: the download watcher requires 100% only from a client whose
    `TorrentInfo.progress` is real; the others leave it at 0.0 and keep
    the state rule (else nothing of theirs would ever finish)."""
    from app.clients.qbittorrent import QbitClient

    assert QbitClient.reports_progress is True
    for other in (DelugeClient, RtorrentClient, TransmissionClient):
        assert getattr(other, "reports_progress", False) is False, other.__name__
