"""The suite-wide "no real network" guard itself (audit issue 08).

Each refusal is checked, then cleared from the record so this test
doesn't trip the per-test check in conftest that it is proving.
"""
from __future__ import annotations

import asyncio
import socket

import httpx
import pytest
from fastapi import FastAPI

from tests import _network_guard


@pytest.fixture
def expect_refusal():
    yield _network_guard.attempts
    _network_guard.attempts.clear()


def test_dns_for_a_real_host_is_refused(expect_refusal):
    with pytest.raises(OSError, match="example.com"):
        socket.getaddrinfo("example.com", 443)
    assert expect_refusal == ["DNS lookup of example.com"]


def test_connect_to_a_real_address_is_refused(expect_refusal):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(OSError, match="93.184.215.14"):
            s.connect(("93.184.215.14", 80))
    assert expect_refusal == ["connect to 93.184.215.14:80"]


def test_connect_by_hostname_is_refused(expect_refusal):
    # connect() with a hostname resolves in C, past getaddrinfo.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        with pytest.raises(OSError):
            s.connect(("example.com", 80))
    assert expect_refusal == ["connect to example.com:80"]


async def test_httpx_to_a_real_host_is_refused(expect_refusal):
    async with httpx.AsyncClient() as c:
        with pytest.raises(httpx.ConnectError):
            await c.get("https://example.com/")
    assert expect_refusal == ["DNS lookup of example.com"]


async def test_curl_cffi_to_a_real_host_is_refused(expect_refusal):
    cffi = pytest.importorskip("curl_cffi.requests")
    async with cffi.AsyncSession() as s:
        with pytest.raises(OSError, match="example.com"):
            await s.get("https://example.com/")
    assert expect_refusal == ["curl_cffi GET example.com"]


async def test_a_swallowed_refusal_is_still_recorded(expect_refusal):
    # Code that fails open on network errors must not hide the attempt.
    try:
        socket.getaddrinfo("goodreads.com", 443)
    except OSError:
        pass
    assert expect_refusal == ["DNS lookup of goodreads.com"]


async def test_loopback_still_works():
    async def echo(reader, writer):
        writer.write(await reader.read(5))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        for host in ("127.0.0.1", "localhost"):
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(b"hello")
            await writer.drain()
            assert await reader.read(5) == b"hello"
            writer.close()
    finally:
        server.close()
        await server.wait_closed()
    assert _network_guard.attempts == []


async def test_asgi_client_still_works():
    app = FastAPI()

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as c:
        assert (await c.get("/ping")).json() == {"ok": True}
    assert _network_guard.attempts == []
