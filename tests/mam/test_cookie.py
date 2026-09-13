"""
Unit tests for the MAM cookie module.

Three layers:

  1. **Pure helpers** — `build_headers` shape, validate's no-token
     short-circuit. No fixture needed.
  2. **Fake-MAM driven** — `register_ip`, `verify_session`, and the
     full `validate` flow against the in-memory `httpx.MockTransport`
     fixture. Real MAM is never contacted.
  3. **Cookie capture** — assertions that the production code is
     attaching the right `mam_id` cookie to outgoing requests.
"""
import pytest

from app.mam.cookie import (
    _do_get,
    _extract_mam_id_from_response,
    build_headers,
    get_current_token,
    register_ip,
    set_current_token,
    set_rotation_callback,
    validate,
    verify_session,
)
from tests.fake_mam import HTML_LOGIN_PAGE


# ─── Pure helper tests (no fixture needed) ───────────────────


class TestBuildHeaders:
    def test_includes_cookie_with_mam_id_prefix(self):
        headers = build_headers("abc123")
        assert headers["Cookie"] == "mam_id=abc123"

    def test_user_agent_is_curl_8(self):
        # The curl/8.0 UA is load-bearing — MAM has been observed to
        # subtly reject other UAs. Pin the value down so a "tidy up
        # the headers" refactor can't quietly break production.
        headers = build_headers("abc123")
        assert headers["User-Agent"] == "curl/8.0"

    def test_content_type_is_json(self):
        headers = build_headers("abc123")
        assert headers["Content-Type"] == "application/json"

    def test_token_substituted_verbatim(self):
        # No URL-encoding, no quoting, no transformation — the cookie
        # is whatever MAM emitted on the security page.
        headers = build_headers("a:b/c+d=e")
        assert headers["Cookie"] == "mam_id=a:b/c+d=e"


class TestValidateNoToken:
    async def test_empty_token_returns_clear_failure_without_network(self):
        # The validate() function MUST short-circuit on empty token
        # rather than firing an HTTP request.
        result = await validate("")
        assert result["success"] is False
        assert "no mam session" in result["message"].lower()
        assert result["ip_result"] is None
        assert result["search_result"] is None


# ─── verify_session against fake MAM ─────────────────────────


class TestVerifySession:
    async def test_success(self, fake_mam):
        # Default fake response: HTTP 200 with a non-empty JSON body.
        result = await verify_session("good_token")
        assert result["success"] is True
        assert "successful" in result["message"].lower()

    async def test_empty_200_treated_as_invalid(self, fake_mam):
        # MAM returns HTTP 200 with an empty body when the cookie is
        # invalid — exactly the gotcha that motivated the cookie module
        # documentation. Seshat must catch this.
        fake_mam.search.body = b""
        result = await verify_session("expired_token")
        assert result["success"] is False
        assert "empty" in result["message"].lower() or "invalid" in result["message"].lower()

    async def test_403_treated_as_rejected(self, fake_mam):
        fake_mam.search.status = 403
        result = await verify_session("bad_token")
        assert result["success"] is False
        assert "403" in result["message"] or "rejected" in result["message"].lower()

    async def test_unexpected_status(self, fake_mam):
        fake_mam.search.status = 502
        result = await verify_session("any_token")
        assert result["success"] is False
        assert "502" in result["message"]

    async def test_attaches_cookie_to_request(self, fake_mam):
        await verify_session("my_session_value")
        assert "my_session_value" in fake_mam.cookies_seen()


# ─── register_ip against fake MAM ────────────────────────────


class TestRegisterIp:
    async def test_skip_short_circuits_without_network(self, fake_mam):
        result = await register_ip("any_token", skip_ip_update=True)
        assert result["success"] is True
        assert "asn-locked" in result["message"].lower() or "skipped" in result["message"].lower()
        # Confirm no HTTP request was made
        assert len(fake_mam.requests) == 0

    async def test_success_returns_ip_and_asn(self, fake_mam):
        # Default fake response is the happy-path JSON.
        result = await register_ip("good_token", skip_ip_update=False)
        assert result["success"] is True
        assert result["ip"] == "192.0.2.1"
        assert result["asn"] == 64500

    async def test_html_response_means_token_expired(self, fake_mam):
        fake_mam.dynip.body = HTML_LOGIN_PAGE
        fake_mam.dynip.headers = {"content-type": "text/html"}
        result = await register_ip("expired_token", skip_ip_update=False)
        assert result["success"] is False
        assert "html" in result["message"].lower() or "expired" in result["message"].lower()

    async def test_no_session_cookie_msg(self, fake_mam):
        fake_mam.dynip.body = b'{"Success":false,"msg":"No Session Cookie"}'
        result = await register_ip("bad_token", skip_ip_update=False)
        assert result["success"] is False
        assert "not recognized" in result["message"].lower() or "not recognised" in result["message"].lower()

    async def test_ip_mismatch_msg(self, fake_mam):
        fake_mam.dynip.body = b'{"Success":false,"msg":"Invalid session - IP mismatch"}'
        result = await register_ip("bad_token", skip_ip_update=False)
        assert result["success"] is False
        assert "different network" in result["message"].lower()

    async def test_too_recent_msg(self, fake_mam):
        fake_mam.dynip.body = b'{"Success":false,"msg":"Last Change too recent"}'
        result = await register_ip("good_token", skip_ip_update=False)
        assert result["success"] is False
        assert "rate-limited" in result["message"].lower()

    async def test_asn_locked_session_msg_treated_as_success(self, fake_mam):
        # The "incorrect session type" branch — an ASN-locked session
        # called with skip_ip_update=False. Seshat treats this as
        # success since the cookie is fine, just doesn't need IP register.
        fake_mam.dynip.body = b'{"Success":false,"msg":"Incorrect session type for this endpoint"}'
        result = await register_ip("good_token", skip_ip_update=False)
        assert result["success"] is True


# ─── Full validate() flow ────────────────────────────────────


class TestValidate:
    async def test_full_happy_path(self, fake_mam):
        result = await validate("good_token", skip_ip_update=True)
        assert result["success"] is True
        assert result["ip_result"] is not None
        assert result["search_result"] is not None
        assert result["search_result"]["success"] is True

    async def test_full_happy_path_with_ip_register(self, fake_mam):
        result = await validate("good_token", skip_ip_update=False)
        assert result["success"] is True
        assert result["ip_result"]["success"] is True
        assert result["search_result"]["success"] is True

    async def test_search_failure_propagates(self, fake_mam):
        fake_mam.simulate_cookie_rejected_403()
        result = await validate("bad_token", skip_ip_update=True)
        assert result["success"] is False
        assert result["search_result"] is not None
        assert result["search_result"]["success"] is False

    async def test_ip_register_failure_short_circuits_search(self, fake_mam):
        # If IP register fails, validate must NOT proceed to search —
        # there's no point and we want a clear "this step failed" UI.
        fake_mam.dynip.body = b'{"Success":false,"msg":"Invalid session - IP mismatch"}'
        result = await validate("bad_token", skip_ip_update=False)
        assert result["success"] is False
        assert result["ip_result"] is not None
        assert result["ip_result"]["success"] is False
        assert result["search_result"] is None  # never called
        # And the fake MAM should never have seen a search request
        assert not any(
            "loadSearchJSONbasic.php" in str(req.url)
            for req in fake_mam.requests
        )

    async def test_empty_token_short_circuits_without_network(self, fake_mam):
        result = await validate("", skip_ip_update=True)
        assert result["success"] is False
        assert len(fake_mam.requests) == 0


# ─── Cookie auto-rotation ────────────────────────────────────


class TestExtractMamId:
    """The Set-Cookie parser is the bedrock of the rotation feature.
    Pin down its behavior on every shape MAM is known to send (or
    has been observed to send by other clients) so a future
    refactor doesn't silently regress."""

    def _make_response(self, headers: list) -> object:
        """Build a real httpx.Response with a bound request.

        httpx's `.cookies` accessor lazily walks the request URL when
        deciding which Set-Cookie headers apply, so a Response built
        without a request raises on `.cookies` access. The bound
        request only needs a URL — none of the rest of the request
        machinery is touched by the cookie jar walk.
        """
        import httpx as _httpx

        request = _httpx.Request(
            "GET", "https://www.myanonamouse.net/tor/js/loadSearchJSONbasic.php"
        )
        return _httpx.Response(
            200, headers=headers, content=b"x", request=request
        )

    def test_extracts_from_jar(self):
        # The most common path: httpx parses the Set-Cookie into
        # its own cookies dict and we read it from there.
        resp = self._make_response(
            [("set-cookie", "mam_id=NEW_VALUE_123; Path=/; HttpOnly")]
        )
        assert _extract_mam_id_from_response(resp) == "NEW_VALUE_123"

    def test_returns_none_when_no_cookie(self):
        resp = self._make_response([("content-type", "text/plain")])
        assert _extract_mam_id_from_response(resp) is None

    def test_extracts_unrelated_cookies_returns_none(self):
        # Some other cookie set, but not mam_id.
        resp = self._make_response(
            [("set-cookie", "csrftoken=ABC; Path=/")]
        )
        assert _extract_mam_id_from_response(resp) is None

    def test_returns_first_mam_id_when_multiple(self):
        # Pathological case: MAM somehow sets two mam_id cookies.
        # The httpx jar will pick one (last-write-wins per RFC), and
        # we just trust whichever it picked.
        resp = self._make_response(
            [
                ("set-cookie", "mam_id=FIRST; Path=/"),
                ("set-cookie", "mam_id=SECOND; Path=/"),
            ]
        )
        # Either is acceptable as long as we return SOMETHING.
        result = _extract_mam_id_from_response(resp)
        assert result in ("FIRST", "SECOND")

    def test_max_age_zero_deletion_returns_none(self):
        # MAM uses Set-Cookie: mam_id=deleted; Max-Age=0 to terminate
        # a session (observed when a filelist fetch with mismatched
        # cookie pair triggers MAM's cross-session-defense logout).
        # The jar correctly drops Max-Age=0 cookies; we must return
        # None so the rotation handler doesn't capture "deleted" as
        # a fresh value and persist it to the encrypted store. This
        # was the 2026-05-09 corruption bug — see cookie.py docstring.
        resp = self._make_response(
            [(
                "set-cookie",
                "mam_id=deleted; expires=Thu, 01 Jan 1970 00:00:01 GMT; "
                "Max-Age=0; path=/; domain=.myanonamouse.net",
            )]
        )
        assert _extract_mam_id_from_response(resp) is None

    def test_expires_in_past_deletion_returns_none(self):
        # Belt + suspenders: same test but relying only on the
        # `Expires=` attribute (no Max-Age). Either is enough for
        # the jar to drop the cookie under RFC 6265.
        resp = self._make_response(
            [(
                "set-cookie",
                "mam_id=deleted; expires=Thu, 01 Jan 1970 00:00:01 GMT; "
                "path=/; domain=.myanonamouse.net",
            )]
        )
        assert _extract_mam_id_from_response(resp) is None


class TestHandleResponseCookie:
    """The handler that runs on every MAM response. Verifies it
    correctly updates _current_token and fires the registered
    callback exactly when expected (cookie changed) and never
    when not expected (no cookie / unchanged cookie)."""

    async def test_no_cookie_in_response_does_nothing(self, fake_mam):
        set_current_token("original_value")
        callback_calls: list[str] = []

        async def cb(new_token: str) -> None:
            callback_calls.append(new_token)

        set_rotation_callback(cb)
        try:
            # Drive a real round-trip with no rotation configured
            # on the fake — the response will not include Set-Cookie.
            await _do_get(
                "https://www.myanonamouse.net/tor/js/loadSearchJSONbasic.php"
            )
            assert get_current_token() == "original_value"
            assert callback_calls == []
        finally:
            set_rotation_callback(None)
            set_current_token("")

    async def test_new_cookie_updates_in_memory_and_fires_callback(
        self, fake_mam
    ):
        set_current_token("original_value")
        callback_calls: list[str] = []

        async def cb(new_token: str) -> None:
            callback_calls.append(new_token)

        set_rotation_callback(cb)
        fake_mam.rotate_cookie_to = "rotated_value_42"
        try:
            await _do_get(
                "https://www.myanonamouse.net/tor/js/loadSearchJSONbasic.php"
            )
            assert get_current_token() == "rotated_value_42"
            assert callback_calls == ["rotated_value_42"]
        finally:
            set_rotation_callback(None)
            set_current_token("")

    async def test_same_cookie_does_not_fire_callback(self, fake_mam):
        # MAM occasionally sends back the same value (within the
        # debounce window or for cached responses). Don't fire the
        # callback for no-op rotations.
        set_current_token("steady_value")
        callback_calls: list[str] = []

        async def cb(new_token: str) -> None:
            callback_calls.append(new_token)

        set_rotation_callback(cb)
        fake_mam.rotate_cookie_to = "steady_value"
        try:
            await _do_get(
                "https://www.myanonamouse.net/tor/js/loadSearchJSONbasic.php"
            )
            assert get_current_token() == "steady_value"
            assert callback_calls == []
        finally:
            set_rotation_callback(None)
            set_current_token("")

    async def test_rotation_uses_explicit_token_when_supplied(
        self, fake_mam
    ):
        # Explicit token argument should win over the in-memory one
        # for the request itself, but rotation still updates the
        # in-memory token because that's the shared state.
        set_current_token("in_memory_token")
        fake_mam.rotate_cookie_to = "after_rotation"
        try:
            await _do_get(
                "https://www.myanonamouse.net/tor/js/loadSearchJSONbasic.php",
                token="explicit_call_token",
            )
            # The OUTGOING request should have used the explicit token
            assert "explicit_call_token" in fake_mam.cookies_seen()
            # The IN-MEMORY token should have been updated by rotation
            assert get_current_token() == "after_rotation"
        finally:
            set_current_token("")

    async def test_callback_exception_does_not_break_request(
        self, fake_mam
    ):
        # If the rotation callback raises (e.g. disk full when
        # writing settings.json), the original MAM request must
        # still succeed and the in-memory token must still update.
        set_current_token("before")

        async def bad_cb(new_token: str) -> None:
            raise RuntimeError("simulated persistence failure")

        set_rotation_callback(bad_cb)
        fake_mam.rotate_cookie_to = "after"
        try:
            response = await _do_get(
                "https://www.myanonamouse.net/tor/js/loadSearchJSONbasic.php"
            )
            assert response.status_code == 200
            assert get_current_token() == "after"
        finally:
            set_rotation_callback(None)
            set_current_token("")

    async def test_chained_requests_rotate_each_time(self, fake_mam):
        # The realistic production pattern: Seshat does many MAM
        # calls, each one returns a fresh cookie, the in-memory state
        # tracks them all. Simulate by changing rotate_cookie_to
        # between calls.
        set_current_token("initial")
        try:
            fake_mam.rotate_cookie_to = "after_call_1"
            await _do_get(
                "https://www.myanonamouse.net/tor/js/loadSearchJSONbasic.php"
            )
            assert get_current_token() == "after_call_1"

            fake_mam.rotate_cookie_to = "after_call_2"
            await _do_get(
                "https://www.myanonamouse.net/tor/js/loadSearchJSONbasic.php"
            )
            assert get_current_token() == "after_call_2"

            fake_mam.rotate_cookie_to = "after_call_3"
            await _do_get(
                "https://www.myanonamouse.net/tor/js/loadSearchJSONbasic.php"
            )
            assert get_current_token() == "after_call_3"

            # Each request used the COOKIE FROM THE PREVIOUS
            # ROTATION (not the original one). This is the
            # critical assertion that proves rotation actually
            # threads through to the next request.
            seen = fake_mam.cookies_seen()
            assert seen[0] == "initial"
            assert seen[1] == "after_call_1"
            assert seen[2] == "after_call_2"
        finally:
            set_current_token("")


class TestRotationOnGrabPath:
    """The .torrent download endpoint is the most common rotation
    trigger in production — every successful grab gets a fresh
    cookie. This class verifies the grab path threads through
    cookie._do_get correctly so rotation fires there too."""

    async def test_successful_grab_rotates_cookie(self, fake_mam):
        from app.mam.grab import fetch_torrent

        set_current_token("before_grab")
        callback_calls: list[str] = []

        async def cb(new_token: str) -> None:
            callback_calls.append(new_token)

        set_rotation_callback(cb)
        fake_mam.rotate_cookie_to = "after_grab"
        try:
            result = await fetch_torrent("12345", token="before_grab")
            assert result.success is True
            assert get_current_token() == "after_grab"
            assert callback_calls == ["after_grab"]
        finally:
            set_rotation_callback(None)
            set_current_token("")

    async def test_failed_grab_still_rotates_if_cookie_present(
        self, fake_mam
    ):
        # Pathological-but-real case: MAM serves an HTML login page
        # (cookie expired) AND sets a fresh cookie in the same
        # response. We should still capture the new cookie even
        # though the body sniffer marks the grab as failed —
        # the rotation might be MAM's offer of a recovery cookie,
        # and a future grab attempt might succeed with it.
        from app.mam.grab import fetch_torrent

        set_current_token("expired")
        fake_mam.download.body = HTML_LOGIN_PAGE
        fake_mam.download.headers = {"content-type": "text/html"}
        fake_mam.rotate_cookie_to = "recovery"
        try:
            result = await fetch_torrent("12345", token="expired")
            # The grab itself should fail (HTML body)
            assert result.success is False
            assert result.failure_kind == "cookie_expired"
            # But the rotation should still have captured the new
            # cookie. This is defensive — we don't know for sure
            # if MAM actually does this, but if they do, we want
            # to take advantage of the recovery path.
            assert get_current_token() == "recovery"
        finally:
            set_current_token("")


class TestIsMamUrl:
    """Host gate that protects against URL-substring sanitization
    bypass (CodeQL #25, #28) — `"myanonamouse.net" in url` was tricked
    by attacker URLs containing the literal substring elsewhere
    (path / query / fragment), which would have routed the MAM session
    cookie through `_do_get` to attacker.com."""

    def test_accepts_apex(self):
        from app.mam.cookie import _is_mam_url
        assert _is_mam_url("https://myanonamouse.net/foo") is True

    def test_accepts_www_subdomain(self):
        from app.mam.cookie import _is_mam_url
        assert _is_mam_url("https://www.myanonamouse.net/jsonLoad.php") is True

    def test_accepts_t_subdomain(self):
        # Tracker subdomain — used by MAM_DYNIP_URL.
        from app.mam.cookie import _is_mam_url
        assert _is_mam_url("https://t.myanonamouse.net/json/dynamicSeedbox.php") is True

    def test_accepts_cdn_subdomain(self):
        # CDN subdomain — used by cover URLs.
        from app.mam.cookie import _is_mam_url
        assert _is_mam_url("https://cdn.myanonamouse.net/t/p/123/large/456.jpeg") is True

    def test_rejects_substring_in_path(self):
        # The classic CodeQL bypass — substring elsewhere in the URL.
        from app.mam.cookie import _is_mam_url
        assert _is_mam_url("https://attacker.com/myanonamouse.net/cover.jpg") is False

    def test_rejects_substring_in_query(self):
        from app.mam.cookie import _is_mam_url
        assert _is_mam_url("https://attacker.com/?u=myanonamouse.net") is False

    def test_rejects_substring_in_subdomain_of_attacker(self):
        # `myanonamouse.net.attacker.com` — endswith would match if
        # we just stripped scheme. The hostname check rejects.
        from app.mam.cookie import _is_mam_url
        assert _is_mam_url("https://myanonamouse.net.attacker.com/") is False

    def test_rejects_userinfo_spoof(self):
        # `https://myanonamouse.net@attacker.com/` — `myanonamouse.net`
        # is the userinfo, real host is `attacker.com`.
        from app.mam.cookie import _is_mam_url
        assert _is_mam_url("https://myanonamouse.net@attacker.com/") is False

    def test_rejects_empty(self):
        from app.mam.cookie import _is_mam_url
        assert _is_mam_url("") is False

    def test_rejects_malformed(self):
        from app.mam.cookie import _is_mam_url
        assert _is_mam_url("not a url at all") is False

    def test_case_insensitive_host(self):
        # RFC says hostnames are case-insensitive.
        from app.mam.cookie import _is_mam_url
        assert _is_mam_url("https://WWW.MyAnonAmouse.NET/foo") is True


class TestDoGetHostGate:
    """`_do_get` rejects non-MAM URLs to prevent the session cookie
    from leaking to attacker-controlled hosts (CodeQL #26 defense)."""

    @pytest.mark.asyncio
    async def test_rejects_non_mam_url(self):
        from app.mam.cookie import _do_get
        with pytest.raises(ValueError, match="non-MAM URL"):
            await _do_get("https://attacker.com/", token="t")

    @pytest.mark.asyncio
    async def test_rejects_substring_bypass(self):
        from app.mam.cookie import _do_get
        with pytest.raises(ValueError, match="non-MAM URL"):
            await _do_get(
                "https://attacker.com/myanonamouse.net", token="t"
            )
