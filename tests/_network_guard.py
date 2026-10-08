"""The test suite never reaches a real host (audit issue 08, L1-04).

`tests/conftest.py` installs this for the whole session. It refuses, at
the lowest level Python code can reach:

  - DNS: `socket.getaddrinfo` for anything but loopback;
  - TCP: `socket.socket.connect` / `connect_ex` to a non-loopback
    address (a hostname passed straight to `connect` is resolved in C,
    past `getaddrinfo`, so only `localhost` is let through there);
  - curl_cffi, which runs libcurl's own DNS and sockets:
    `Session.request` / `AsyncSession.request`.

A refused call raises (so nothing leaves the machine) and is recorded;
the per-test check in conftest then fails the test naming the host,
even if the code under test caught the error and failed open. Blocking
here, below httpx, is what makes it trustworthy: a spy on
`httpx.AsyncClient.send` also sees requests that respx or a test
transport answer, and reports them as escapes (the 2026-10-06 list).

Loopback stays open: local fake servers, ASGI clients (no socket at
all), asyncio's own self-pipe.
"""
from __future__ import annotations

import ipaddress
import socket

attempts: list[str] = []


class RealNetworkAttempt(socket.gaierror):
    """Raised instead of a real DNS lookup or connection. An OSError, so
    code under test handles it the way it handles a network outage."""


def _name(host) -> str:
    if isinstance(host, bytes):
        return host.decode("ascii", "replace")
    return str(host)


def _is_local_host(host) -> bool:
    if host is None:
        return True
    host = _name(host).strip("[]")
    if host in ("", "localhost", "localhost.localdomain"):
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return host.endswith(".localhost")
    return ip.is_loopback or ip.is_unspecified


def _refuse(what: str):
    attempts.append(what)
    raise RealNetworkAttempt(f"test suite tried to reach the real network: {what}")


def _check_address(sock: socket.socket, address) -> None:
    if sock.family not in (socket.AF_INET, socket.AF_INET6):
        return  # AF_UNIX and friends never leave the machine
    host, port = (address[0], address[1]) if isinstance(address, tuple) else (address, "?")
    if not _is_local_host(host):
        _refuse(f"connect to {_name(host)}:{port}")


def install(mp) -> None:
    """Patch through `mp` (a `pytest.MonkeyPatch`), so `mp.undo()` removes it."""
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def getaddrinfo(host, *args, **kwargs):
        if not _is_local_host(host):
            _refuse(f"DNS lookup of {_name(host)}")
        return real_getaddrinfo(host, *args, **kwargs)

    def connect(self, address):
        _check_address(self, address)
        return real_connect(self, address)

    def connect_ex(self, address):
        _check_address(self, address)
        return real_connect_ex(self, address)

    mp.setattr(socket, "getaddrinfo", getaddrinfo)
    mp.setattr(socket.socket, "connect", connect)
    mp.setattr(socket.socket, "connect_ex", connect_ex)

    try:
        from curl_cffi import requests as cffi
    except ImportError:
        return

    def _cffi_refusal(method, url):
        from urllib.parse import urlsplit
        host = urlsplit(str(url)).hostname
        if not _is_local_host(host):
            _refuse(f"curl_cffi {method} {host}")

    real_sync = cffi.Session.request
    real_async = cffi.AsyncSession.request

    def sync_request(self, method, url, *args, **kwargs):
        _cffi_refusal(method, url)
        return real_sync(self, method, url, *args, **kwargs)

    async def async_request(self, method, url, *args, **kwargs):
        _cffi_refusal(method, url)
        return await real_async(self, method, url, *args, **kwargs)

    mp.setattr(cffi.Session, "request", sync_request)
    mp.setattr(cffi.AsyncSession, "request", async_request)
