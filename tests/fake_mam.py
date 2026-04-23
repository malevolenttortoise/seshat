"""
Fake MAM HTTP server for unit tests.

We deliberately do NOT hit real MAM in unit tests — every request
costs real-world side effects (snatch budget, IP register rate limits,
plus the obvious "we shouldn't be hammering MAM from CI"). Instead,
we install an `httpx.MockTransport` into the cookie module's shared
client and intercept every request that the production code would have
made.

The `FakeMAM` class is a programmable response builder. Each test
constructs an instance, tweaks the fields it cares about, and the
`fake_mam` pytest fixture wires it into `cookie._client` for the
duration of the test. After the test the original client is restored.

Five endpoints are simulated, matching the real MAM surface:

  - loadSearchJSONbasic.php   — search probe (cookie.verify_session)
                                AND torrent-by-id lookup (torrent_info)
  - dynamicSeedbox.php        — IP register     (cookie.register_ip)
  - download.php              — .torrent fetch  (grab.fetch_torrent)
  - jsonLoad.php              — user status      (user_status)
  - bonusBuy.php              — VIP / upload / personalFL purchases
                                (bonus_buy)

Each endpoint has independently configurable status code, body, and
headers, plus a request log for assertions ("did the code under test
actually call this endpoint with the right cookie?").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import httpx


# A minimally valid bencoded torrent file: a top-level dict with
# `announce` and `info` keys. The info dict contains the minimum
# required fields (`name`, `piece length`, `pieces`) so it's a real
# torrent file shape — not just a marker. The string lengths must be
# byte-accurate or the bencode parser in `app.mam.torrent_meta` will
# misread the structure (it bit us once already; the announce URL is
# 31 bytes, not 30).
MINIMAL_BENCODED_TORRENT = (
    b"d8:announce31:http://tracker.example/announce4:infod"
    b"4:name8:test.txt12:piece lengthi16384e6:pieces20:" + b"\x00" * 20 + b"ee"
)

# A representative HTML login page response — what MAM serves when
# the cookie has been rotated/expired and they redirect you to log in.
# The actual page is much longer; we only need the `<html` token at the
# top so the body sniffer triggers.
HTML_LOGIN_PAGE = (
    b"<!DOCTYPE html>\n<html>\n<head><title>MyAnonamouse - Login</title>"
    b"</head>\n<body>Please log in.</body></html>"
)

# Default jsonLoad.php response — realistic user account data.
DEFAULT_USER_STATUS_BODY = (
    b'{"classname":"Elite VIP","country_code":"us",'
    b'"country_name":"United States","downloaded":"91.71 MiB",'
    b'"downloaded_bytes":96160650,"ratio":91184.8,"seedbonus":71088,'
    b'"uid":224285,"uploaded":"7.975 TiB",'
    b'"uploaded_bytes":8768386723586,"username":"Turtles81","wedges":462}'
)

# Default bonusBuy.php success response — shape is from a real upload
# buy response captured against live MAM 2026-04-24. Notable details:
# `seedbonus` is a float (jsonLoad.php reports integers but bonusBuy
# returns fractional values); `ratio` is a bare float on this endpoint
# (our earlier memory had it as a `{source, parsedValue}` dict, which
# was wrong — `_parse_ratio` handles both shapes regardless). The
# uploadFancy/downloadFancy fields are display-only and ignored by
# our parser but kept here so the fixture matches MAM byte-for-byte.
DEFAULT_BONUS_BUY_BODY = (
    b'{"success":true,"type":"upload","amount":50,'
    b'"seedbonus":26512.091,"uploaded":9094496082151,'
    b'"downloaded":96557899,"uploadFancy":"8.271 TiB",'
    b'"downloadFancy":"92.08 MiB",'
    b'"ratio":94186.97}'
)

# Default search-by-id response — a single torrent result with
# VIP/free/fl_vip fields.
DEFAULT_TORRENT_INFO_BODY = (
    b'{"perpage":1,"start":0,"found":1,"data":[{'
    b'"id":"965093","language":"1","main_cat":"14","category":"63",'
    b'"catname":"Ebooks - Fantasy","size":"5242880","numfiles":"1",'
    b'"vip":"0","free":"0","fl_vip":"0","personal_freeleech":"0",'
    b'"title":"Test Book","name":"Test Book",'
    b'"author_info":"{\\"1234\\": \\"Test Author\\"}","seeders":"5",'
    b'"leechers":"0","times_completed":"42"}]}'
)


@dataclass
class _EndpointConfig:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeMAM:
    """Programmable fake for the three MAM HTTP endpoints we touch.

    Defaults model the happy path: every endpoint returns a successful
    response. Tests override the fields they care about — e.g.

        fake.download.status = 403
        fake.download.body = HTML_LOGIN_PAGE

    The `requests` list captures every intercepted request in order so
    tests can assert on URLs, headers (Cookie), method, and content.

    **Cookie rotation simulation.** Real MAM rotates the `mam_id`
    cookie on every API call by sending a new value in a `Set-Cookie`
    response header. Tests exercise this by setting
    `rotate_cookie_to` — every response produced by the fake from
    that point on will include a `Set-Cookie: mam_id=<value>` header,
    letting tests verify Seshat captures and persists the new
    value. Set to None (the default) to disable rotation and keep
    responses cookie-free.
    """

    search: _EndpointConfig = field(
        default_factory=lambda: _EndpointConfig(
            status=200,
            body=b'{"perpage":5,"start":0,"found":0,"data":[]}',
            headers={"content-type": "application/json"},
        )
    )
    dynip: _EndpointConfig = field(
        default_factory=lambda: _EndpointConfig(
            status=200,
            body=(
                b'{"Success":true,"msg":"Completed","ip":"192.0.2.1",'
                b'"ASN":64500,"AS":"Test ISP"}'
            ),
            headers={"content-type": "application/json"},
        )
    )
    download: _EndpointConfig = field(
        default_factory=lambda: _EndpointConfig(
            status=200,
            body=MINIMAL_BENCODED_TORRENT,
            headers={"content-type": "application/x-bittorrent"},
        )
    )
    user_status: _EndpointConfig = field(
        default_factory=lambda: _EndpointConfig(
            status=200,
            body=DEFAULT_USER_STATUS_BODY,
            headers={"content-type": "application/json"},
        )
    )
    bonus_buy: _EndpointConfig = field(
        default_factory=lambda: _EndpointConfig(
            status=200,
            body=DEFAULT_BONUS_BUY_BODY,
            headers={"content-type": "application/json"},
        )
    )

    # If set, every response produced by the fake includes a
    # `Set-Cookie: mam_id=<value>` header. Tests use this to
    # simulate MAM's automatic cookie rotation.
    rotate_cookie_to: Optional[str] = None

    requests: list[httpx.Request] = field(default_factory=list)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)

        if "jsonLoad.php" in url:
            cfg = self.user_status
        elif "bonusBuy.php" in url:
            cfg = self.bonus_buy
        elif "loadSearchJSONbasic.php" in url:
            cfg = self.search
        elif "dynamicSeedbox.php" in url:
            cfg = self.dynip
        elif "download.php" in url:
            cfg = self.download
        else:
            return httpx.Response(
                404,
                content=b"unknown fake-MAM endpoint",
                headers={"content-type": "text/plain"},
            )

        # Merge rotation cookie header into the response headers.
        # Using httpx's `headers=` list-of-tuples form so we can
        # add `set-cookie` alongside whatever the endpoint config
        # already sets (content-type, etc.) — httpx allows the
        # same header name multiple times this way.
        response_headers = list(cfg.headers.items())
        if self.rotate_cookie_to is not None:
            response_headers.append(
                (
                    "set-cookie",
                    f"mam_id={self.rotate_cookie_to}; Path=/; HttpOnly",
                )
            )

        return httpx.Response(
            cfg.status,
            content=cfg.body,
            headers=response_headers,
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handler)

    # ─── Convenience helpers for common failure scenarios ────

    def simulate_cookie_expired_html(self) -> None:
        """All endpoints return 200 + HTML login page (MAM's typical
        response when a cookie has been rotated or expired)."""
        for cfg in (
            self.search, self.dynip, self.download,
            self.user_status, self.bonus_buy,
        ):
            cfg.status = 200
            cfg.body = HTML_LOGIN_PAGE
            cfg.headers = {"content-type": "text/html"}

    def simulate_cookie_rejected_403(self) -> None:
        """All endpoints return 403 Forbidden — what MAM does when
        the auth header is wrong but they bother to use a real status code."""
        for cfg in (
            self.search, self.dynip, self.download,
            self.user_status, self.bonus_buy,
        ):
            cfg.status = 403
            cfg.body = b"forbidden"
            cfg.headers = {"content-type": "text/plain"}

    def simulate_torrent_not_found(self) -> None:
        """The download endpoint returns 404 — torrent removed from MAM."""
        self.download.status = 404
        self.download.body = b"not found"

    def simulate_server_error(self) -> None:
        """The download endpoint returns 500 — transient MAM-side issue."""
        self.download.status = 500
        self.download.body = b"internal server error"

    def cookies_seen(self) -> list[str]:
        """Extract every `mam_id=...` cookie value from captured requests."""
        out = []
        for req in self.requests:
            cookie_header = req.headers.get("cookie", "")
            if cookie_header.startswith("mam_id="):
                out.append(cookie_header[len("mam_id=") :])
        return out
