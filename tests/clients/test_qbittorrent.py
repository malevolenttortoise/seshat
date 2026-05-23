"""
Unit tests for the qBittorrent WebUI client.

Layered the same way as the MAM tests:

  1. **Login flow** — happy path, bad creds, transport error,
     credential capture.
  2. **Add torrent** — happy path, auth-failed retry, rejected file,
     server error, request shape (multipart, category, save_path).
  3. **List / get** — empty list, category filter, hash lookup, 403
     drops the session and returns empty.
  4. **Lifecycle** — aclose() is idempotent.

The fake-qBit fixture from `tests/conftest.py` provides a fresh
`FakeQbit` per test. Tests construct a real `QbitClient` pointed at
the fake's transport — there's no monkey-patching of module state.
"""
import httpx

from app.clients.qbittorrent import QbitClient
from tests.fake_mam import MINIMAL_BENCODED_TORRENT


# ─── Helpers ─────────────────────────────────────────────────


def _make_client(fake_qbit, **overrides) -> QbitClient:
    """Build a QbitClient pointed at the fake transport.

    Defaults match the fake's default credentials so login succeeds
    out of the box. Tests override `username`/`password` to drive
    auth failure scenarios.
    """
    kwargs = {
        "base_url": "http://fake.qbit.local:8080",
        "username": "admin",
        "password": "adminadmin",
        "transport": fake_qbit.transport(),
    }
    kwargs.update(overrides)
    return QbitClient(**kwargs)


# ─── Login ───────────────────────────────────────────────────


class TestLogin:
    async def test_happy_path(self, fake_qbit):
        client = _make_client(fake_qbit)
        try:
            assert await client.login() is True
            assert client._logged_in is True
        finally:
            await client.aclose()

    async def test_bad_credentials_returns_false(self, fake_qbit):
        client = _make_client(fake_qbit, username="wrong", password="wrong")
        try:
            assert await client.login() is False
            assert client._logged_in is False
        finally:
            await client.aclose()

    async def test_login_request_uses_form_encoded_credentials(self, fake_qbit):
        client = _make_client(fake_qbit)
        try:
            await client.login()
        finally:
            await client.aclose()

        login_reqs = [r for r in fake_qbit.requests if r.url.path == "/api/v2/auth/login"]
        assert len(login_reqs) == 1
        body = login_reqs[0].content.decode("utf-8")
        assert "username=admin" in body
        assert "password=adminadmin" in body

    async def test_login_attaches_referer_header(self, fake_qbit):
        # qBit docs say the Referer header is required. Some setups
        # enforce it; pin the behavior down so a "tidy up" refactor
        # can't quietly drop it.
        client = _make_client(fake_qbit)
        try:
            await client.login()
        finally:
            await client.aclose()

        login_req = next(
            r for r in fake_qbit.requests if r.url.path == "/api/v2/auth/login"
        )
        assert "referer" in {k.lower() for k in login_req.headers.keys()}

    async def test_qbit_403_marks_not_logged_in(self, fake_qbit):
        # Rate-limited / IP-banned scenario.
        fake_qbit.login_status = 403
        client = _make_client(fake_qbit)
        try:
            assert await client.login() is False
            assert client._logged_in is False
        finally:
            await client.aclose()

    async def test_qbit_204_treated_as_success(self, fake_qbit):
        # qBit 5.2+ returns 204 No Content when the caller's IP is in
        # AuthSubnetWhitelist — credentials are ignored, subnet match
        # is authoritative. Must be accepted as success or Seshat will
        # bounce off the bypass and report "not authenticated" even
        # though qBit is granting access on every request.
        fake_qbit.login_status = 204
        client = _make_client(fake_qbit)
        try:
            assert await client.login() is True
            assert client._logged_in is True
        finally:
            await client.aclose()


# ─── Add torrent ─────────────────────────────────────────────


class TestAddTorrent:
    async def test_happy_path(self, fake_qbit):
        client = _make_client(fake_qbit)
        try:
            result = await client.add_torrent(
                MINIMAL_BENCODED_TORRENT,
                category="mam-complete",
                save_path="/downloads",
            )
        finally:
            await client.aclose()

        assert result.success is True
        assert result.failure_kind is None
        assert len(fake_qbit.added_torrents) == 1
        assert fake_qbit.added_torrents[0]["category"] == "mam-complete"
        assert fake_qbit.added_torrents[0]["save_path"] == "/downloads"
        assert fake_qbit.added_torrents[0]["torrent_size"] == len(MINIMAL_BENCODED_TORRENT)

    async def test_auto_login_on_first_add(self, fake_qbit):
        # add_torrent should call login() automatically if we haven't
        # already.
        client = _make_client(fake_qbit)
        try:
            result = await client.add_torrent(MINIMAL_BENCODED_TORRENT)
        finally:
            await client.aclose()

        assert result.success is True
        # The fake should have seen one login + one add request.
        login_reqs = [r for r in fake_qbit.requests if r.url.path == "/api/v2/auth/login"]
        add_reqs = [r for r in fake_qbit.requests if r.url.path == "/api/v2/torrents/add"]
        assert len(login_reqs) == 1
        assert len(add_reqs) == 1

    async def test_empty_bytes_rejected_without_network(self, fake_qbit):
        client = _make_client(fake_qbit)
        try:
            result = await client.add_torrent(b"")
        finally:
            await client.aclose()

        assert result.success is False
        assert result.failure_kind == "rejected"
        assert "empty" in result.failure_detail.lower()
        assert len(fake_qbit.requests) == 0  # never hit the wire

    async def test_login_failure_returns_auth_failed(self, fake_qbit):
        client = _make_client(fake_qbit, password="wrong")
        try:
            result = await client.add_torrent(MINIMAL_BENCODED_TORRENT)
        finally:
            await client.aclose()

        assert result.success is False
        assert result.failure_kind == "auth_failed"

    async def test_session_expired_triggers_relogin_retry(self, fake_qbit):
        # Tests the auto-relogin path: first add fails with 403
        # (session expired), client re-logs in and retries, second
        # attempt succeeds.
        #
        # We simulate this by having the FIRST add fail then revert.
        # Easier: temporarily disable auth, make the add return 403,
        # then flip it back.
        client = _make_client(fake_qbit)
        try:
            # Login normally first.
            assert await client.login() is True

            # Now invalidate the session by changing the SID; the
            # client will get 403 on its first add attempt, re-login,
            # and retry.
            fake_qbit.sid_cookie = "newSID67890"
            # Track how many adds we've seen so we can flip the
            # require_auth flag after the first failure.
            add_call_count = {"n": 0}
            real_handler = fake_qbit._handle_add

            def patched_add(req):
                add_call_count["n"] += 1
                if add_call_count["n"] == 1:
                    # First call: pretend session is expired.
                    return httpx.Response(403, content=b"forbidden")
                return real_handler(req)

            fake_qbit._handle_add = patched_add  # type: ignore[assignment]

            result = await client.add_torrent(MINIMAL_BENCODED_TORRENT)
        finally:
            await client.aclose()

        assert result.success is True
        # We should have seen: login → add (403) → relogin → add (200)
        login_reqs = [r for r in fake_qbit.requests if r.url.path == "/api/v2/auth/login"]
        assert len(login_reqs) == 2
        assert add_call_count["n"] == 2

    async def test_persistent_403_returns_auth_failed(self, fake_qbit):
        # If even the relogin retry fails with 403, the result must
        # be auth_failed (not success), so the caller knows.
        client = _make_client(fake_qbit)
        try:
            assert await client.login() is True
            # Permanently 403 the add endpoint.
            fake_qbit.add_status = 403
            fake_qbit.add_body = b"forbidden"
            result = await client.add_torrent(MINIMAL_BENCODED_TORRENT)
        finally:
            await client.aclose()

        assert result.success is False
        assert result.failure_kind == "auth_failed"

    async def test_415_marks_rejected(self, fake_qbit):
        # qBit returns 415 when the .torrent file is unparseable.
        client = _make_client(fake_qbit)
        try:
            assert await client.login() is True
            fake_qbit.add_status = 415
            fake_qbit.add_body = b"unsupported media"
            result = await client.add_torrent(b"\x00\x01\x02 not a torrent")
        finally:
            await client.aclose()

        assert result.success is False
        assert result.failure_kind == "rejected"

    async def test_500_marks_unknown(self, fake_qbit):
        client = _make_client(fake_qbit)
        try:
            assert await client.login() is True
            fake_qbit.add_status = 500
            result = await client.add_torrent(MINIMAL_BENCODED_TORRENT)
        finally:
            await client.aclose()

        assert result.success is False
        assert result.failure_kind == "unknown"
        assert "server error" in result.failure_detail.lower()

    async def test_tags_passed_through_as_comma_separated(self, fake_qbit):
        # Single tag — most common case (Seshat's `seshat-seed`).
        client = _make_client(fake_qbit)
        try:
            await client.add_torrent(
                MINIMAL_BENCODED_TORRENT,
                category="mam-complete",
                tags=["seshat-seed"],
            )
        finally:
            await client.aclose()

        assert len(fake_qbit.added_torrents) == 1
        assert fake_qbit.added_torrents[0]["tags"] == "seshat-seed"

    async def test_multiple_tags_joined_no_spaces(self, fake_qbit):
        # qBit's API splits the `tags` form field strictly on commas
        # with NO whitespace tolerated. Tag values themselves can
        # contain spaces, but the SEPARATOR cannot.
        client = _make_client(fake_qbit)
        try:
            await client.add_torrent(
                MINIMAL_BENCODED_TORRENT,
                tags=["seshat-seed", "vip", "freeleech"],
            )
        finally:
            await client.aclose()

        assert fake_qbit.added_torrents[0]["tags"] == "seshat-seed,vip,freeleech"

    async def test_empty_tag_strings_dropped(self, fake_qbit):
        # Defensive: a tag list with empty strings shouldn't produce
        # literal-empty tags in qBit (which renders as a phantom
        # blank tag in the UI).
        client = _make_client(fake_qbit)
        try:
            await client.add_torrent(
                MINIMAL_BENCODED_TORRENT,
                tags=["seshat-seed", "", "vip"],
            )
        finally:
            await client.aclose()

        assert fake_qbit.added_torrents[0]["tags"] == "seshat-seed,vip"

    async def test_no_tags_omits_form_field_entirely(self, fake_qbit):
        # Backwards compat: existing add_torrent calls without the
        # tags parameter should NOT send a tags=<empty> form field.
        # qBit treats an empty tags field as "remove all tags" on
        # update endpoints, and we want zero ambiguity here.
        client = _make_client(fake_qbit)
        try:
            await client.add_torrent(MINIMAL_BENCODED_TORRENT)
        finally:
            await client.aclose()

        assert fake_qbit.added_torrents[0]["tags"] is None

    async def test_fails_body_marks_duplicate(self, fake_qbit):
        # qBit's standard "torrent already in client" response is
        # HTTP 200 with body literally "Fails.". Easy to misclassify
        # as `unknown` because the status code is 200 — and we did
        # exactly that until a real production deploy hit it. Pin
        # the recognition down so it can't regress.
        client = _make_client(fake_qbit)
        try:
            assert await client.login() is True
            fake_qbit.add_body = b"Fails."
            result = await client.add_torrent(MINIMAL_BENCODED_TORRENT)
        finally:
            await client.aclose()

        assert result.success is False
        assert result.failure_kind == "duplicate"
        assert "already exists" in result.failure_detail.lower()

    # ─── qBit 5.2+ JSON response shape (post-v2.21.0 hotfix) ───
    #
    # Discovered 2026-05-22 after the v2.21.0 deploy. qBit 5.2's
    # hotio image returns JSON
    #   {"added_torrent_ids": ["<hash>"], "failure_count": 0}
    # from POST /api/v2/torrents/add instead of the legacy
    # "Ok." / "Fails." plain text. Pre-fix, every successful add
    # got classified `failed_unknown` even though qBit was actually
    # downloading the torrent — Mark's two stuck audiobooks
    # (tids 1244201 + 1244208) were the prod evidence. Tests pin
    # both the success and rejection JSON shapes.

    async def test_v52_json_success_response_marks_added(self, fake_qbit):
        client = _make_client(fake_qbit)
        try:
            assert await client.login() is True
            fake_qbit.add_body = (
                b'{"added_torrent_ids":["e00501ba351b73e6e09014f1689a7aa4483b113d"],'
                b'"failure_count":0}'
            )
            result = await client.add_torrent(MINIMAL_BENCODED_TORRENT)
        finally:
            await client.aclose()

        assert result.success is True
        assert result.failure_kind is None
        assert not result.failure_detail

    async def test_v52_json_failure_response_marks_rejected(self, fake_qbit):
        client = _make_client(fake_qbit)
        try:
            assert await client.login() is True
            fake_qbit.add_body = (
                b'{"added_torrent_ids":[],"failure_count":1}'
            )
            result = await client.add_torrent(MINIMAL_BENCODED_TORRENT)
        finally:
            await client.aclose()

        assert result.success is False
        assert result.failure_kind == "rejected"
        assert "failure_count=1" in result.failure_detail

    async def test_v52_json_mixed_batch_treats_added_as_authoritative(
        self, fake_qbit,
    ):
        # qBit's batch endpoint could theoretically return added > 0 AND
        # failure_count > 0 for a multi-torrent post, but Seshat only
        # ever sends one torrent. If the response says it was added,
        # we trust that — better than classifying a successful add as
        # a failure and re-grabbing later (which would burn a slot).
        client = _make_client(fake_qbit)
        try:
            assert await client.login() is True
            fake_qbit.add_body = (
                b'{"added_torrent_ids":["abc123"],"failure_count":1}'
            )
            result = await client.add_torrent(MINIMAL_BENCODED_TORRENT)
        finally:
            await client.aclose()

        assert result.success is True

    async def test_v52_json_with_unknown_shape_falls_through(self, fake_qbit):
        # If qBit ships another response variant in a future version,
        # we want the client to surface it cleanly as "unexpected
        # response" rather than silently swallowing it. Empty JSON
        # object is a good probe.
        client = _make_client(fake_qbit)
        try:
            assert await client.login() is True
            fake_qbit.add_body = b'{}'
            result = await client.add_torrent(MINIMAL_BENCODED_TORRENT)
        finally:
            await client.aclose()

        assert result.success is False
        assert result.failure_kind == "unknown"
        assert "unexpected qBit response" in result.failure_detail

    async def test_v52_malformed_json_falls_through(self, fake_qbit):
        # A body that LOOKS like JSON but won't parse must NOT crash
        # the client. Falls back to the generic "unexpected" path.
        client = _make_client(fake_qbit)
        try:
            assert await client.login() is True
            fake_qbit.add_body = b'{"added_torrent_ids": ['
            result = await client.add_torrent(MINIMAL_BENCODED_TORRENT)
        finally:
            await client.aclose()

        assert result.success is False
        assert result.failure_kind == "unknown"


# ─── List / get torrents ─────────────────────────────────────


class TestListTorrents:
    async def test_empty_list(self, fake_qbit):
        client = _make_client(fake_qbit)
        try:
            await client.login()
            result = await client.list_torrents()
        finally:
            await client.aclose()

        assert result == []

    async def test_returns_added_torrents(self, fake_qbit):
        client = _make_client(fake_qbit)
        try:
            await client.add_torrent(
                MINIMAL_BENCODED_TORRENT,
                category="mam-complete",
                save_path="/downloads",
            )
            result = await client.list_torrents()
        finally:
            await client.aclose()

        assert len(result) == 1
        assert result[0].category == "mam-complete"
        assert result[0].save_path == "/downloads"
        assert result[0].state == "downloading"
        assert result[0].seeding_seconds == 0

    async def test_category_filter(self, fake_qbit):
        # Pre-populate the fake with two torrents in different categories.
        fake_qbit.torrents = [
            {
                "hash": "a" * 40,
                "name": "in-category",
                "category": "mam-complete",
                "state": "uploading",
                "seeding_time": 100,
                "save_path": "/x",
                "added_on": 1,
            },
            {
                "hash": "b" * 40,
                "name": "other",
                "category": "other",
                "state": "uploading",
                "seeding_time": 200,
                "save_path": "/y",
                "added_on": 2,
            },
        ]
        client = _make_client(fake_qbit)
        try:
            await client.login()
            result = await client.list_torrents(category="mam-complete")
        finally:
            await client.aclose()

        assert len(result) == 1
        assert result[0].name == "in-category"

    async def test_403_returns_empty_and_drops_session(self, fake_qbit):
        client = _make_client(fake_qbit)
        try:
            await client.login()
            assert client._logged_in is True
            fake_qbit.info_status = 403
            result = await client.list_torrents()
        finally:
            await client.aclose()

        assert result == []
        # Session must be dropped so the next call re-authenticates.
        assert client._logged_in is False

    async def test_transport_error_drops_session(self, fake_qbit):
        # When the qBit container is stopped / network is partitioned,
        # httpx raises a transport error. We must flip _logged_in so
        # the budget watcher's reachability check (which reads the
        # flag) reports false — that's what drives the SSE
        # `client-status` event + the Downloader pill in the UI.
        import httpx as _httpx

        class _FailingTransport(_httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                raise _httpx.ConnectError("simulated qBit down")

        client = _make_client(fake_qbit)
        try:
            await client.login()
            assert client._logged_in is True
            # Swap in a failing transport for the list call.
            await client._client.aclose()
            client._client = _httpx.AsyncClient(
                base_url="http://fake.qbit.local:8080",
                transport=_FailingTransport(),
            )
            result = await client.list_torrents()
        finally:
            await client.aclose()

        assert result == []
        assert client._logged_in is False

    async def test_500_status_drops_session(self, fake_qbit):
        # A 5xx from qBit (or from a reverse proxy in front of it
        # with qBit down) also means we can't trust the session.
        client = _make_client(fake_qbit)
        try:
            await client.login()
            assert client._logged_in is True
            fake_qbit.info_status = 500
            result = await client.list_torrents()
        finally:
            await client.aclose()

        assert result == []
        assert client._logged_in is False

    async def test_seeding_time_parsed_correctly(self, fake_qbit):
        fake_qbit.torrents = [
            {
                "hash": "c" * 40,
                "name": "seeding",
                "category": "mam-complete",
                "state": "uploading",
                "seeding_time": 259200,  # 72h in seconds — the budget threshold
                "save_path": "/z",
                "added_on": 1,
            }
        ]
        client = _make_client(fake_qbit)
        try:
            await client.login()
            result = await client.list_torrents()
        finally:
            await client.aclose()

        assert result[0].seeding_seconds == 259200


class TestGetTorrent:
    async def test_found(self, fake_qbit):
        fake_qbit.torrents = [
            {
                "hash": "d" * 40,
                "name": "target",
                "category": "mam-complete",
                "state": "uploading",
                "seeding_time": 50,
                "save_path": "/p",
                "added_on": 1,
            }
        ]
        client = _make_client(fake_qbit)
        try:
            await client.login()
            result = await client.get_torrent("d" * 40)
        finally:
            await client.aclose()

        assert result is not None
        assert result.name == "target"

    async def test_not_found_returns_none(self, fake_qbit):
        client = _make_client(fake_qbit)
        try:
            await client.login()
            result = await client.get_torrent("nonexistent")
        finally:
            await client.aclose()

        assert result is None

    async def test_empty_hash_returns_none_without_network(self, fake_qbit):
        client = _make_client(fake_qbit)
        try:
            result = await client.get_torrent("")
        finally:
            await client.aclose()

        assert result is None
        assert len(fake_qbit.requests) == 0


# ─── Lifecycle ───────────────────────────────────────────────


class TestLifecycle:
    async def test_aclose_idempotent(self, fake_qbit):
        client = _make_client(fake_qbit)
        await client.aclose()
        await client.aclose()  # second call must not raise
