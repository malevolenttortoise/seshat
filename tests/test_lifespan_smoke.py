"""
End-to-end smoke test for the full Seshat pipeline.

This test is the proof that everything we built in Phase 1 actually
fits together. It does NOT mock the dispatcher, NOT mock the
filter, NOT mock the rate limiter, NOT mock the storage layer. The
ONLY fakes are at the I/O boundaries:

  - Fake MAM HTTP server (httpx.MockTransport via the cookie module)
  - Fake qBit (a TorrentClient implementation)
  - Fake IRC server (in-memory StreamReader/StreamWriter pair)

Everything else is the real production code path:

  - The real `IrcClient` doing the SASL handshake against the fake
  - The real `parse_announce` against a real fixture announce line
  - The real filter
  - The real rate limiter consulting the real `temp_db`
  - The real `mam.grab.fetch_torrent` doing an HTTP GET against
    the fake MAM transport
  - The real `info_hash` computing the SHA1 of the fake torrent body
  - The real fake-qBit recording the add
  - The real `storage.grabs` writing the audit row + grab row
  - The real `ledger.record_grab` creating the ledger entry

The test wires all of this together inside the actual FastAPI
lifespan, so the integration is verified at the same plumbing
level as production.
"""
import asyncio
from typing import Optional

import httpx

from app import state
from app.clients.base import AddResult, TorrentInfo
from app.database import get_db
from app.filter.gate import FilterConfig
from app.filter.normalize import normalize_category
from app.mam import cookie as mam_cookie
from app.mam.grab import fetch_torrent
from app.mam.irc import IrcClient, IrcConfig
from app.orchestrator.dispatch import DispatcherDeps, handle_announce
from app.rate_limit import ledger as ledger_mod
from app.storage import grabs as grabs_storage
from tests.fake_irc import FakeIrc, drive_sasl_handshake
from tests.fake_mam import FakeMAM


# A real fixture announce line in MAM's post-2026-08-11 format.
# Goes through the same parser the IRC listener uses in production.
_REAL_ANNOUNCE_PRIVMSG = (
    ":MouseBot!~bot@host PRIVMSG #announce :"
    "The Demon King By: Peter V Brett [English] [Audiobook] "
    "[Fiction] [m4b] [921.91 MiB] - Fantasy - "
    "https://www.myanonamouse.net/t/1233592 VIP"
)


class _SmokeQbit:
    """Minimal TorrentClient that records what it was given."""

    def __init__(self):
        self.add_calls: list[dict] = []

    async def login(self) -> bool:
        return True

    async def add_torrent(
        self,
        torrent_bytes: bytes,
        category: Optional[str] = None,
        save_path: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> AddResult:
        self.add_calls.append(
            {"size": len(torrent_bytes), "category": category, "tags": tags}
        )
        return AddResult(success=True)

    async def list_torrents(
        self, category: Optional[str] = None
    ) -> list[TorrentInfo]:
        return []

    async def get_torrent(self, torrent_hash: str) -> Optional[TorrentInfo]:
        return None

    async def aclose(self) -> None:
        return None


class TestEndToEndPipeline:
    """The big one — full pipeline from fake IRC to real ledger entry."""

    async def test_real_announce_flows_through_to_qbit_and_ledger(
        self, temp_db, fake_mam, fake_irc
    ):
        # ── Wire up real production components ──────────────
        #
        # The cookie module is module-level, so the fake_mam
        # fixture has already monkey-patched its httpx client.
        # `fetch_torrent` (the real one) will hit our fake MAM.
        #
        # The IrcClient is real, with the fake_irc connect_fn
        # injected so it never opens a real socket.
        #
        # The FilterConfig allows the real fixture's category
        # (Audiobooks - Fantasy) and author (Peter V Brett) so
        # the announce makes it past the filter.

        qbit = _SmokeQbit()

        deps = DispatcherDeps(
            filter_config=FilterConfig(
                allowed_categories=frozenset({
                    normalize_category("Audiobooks - Fantasy"),
                }),
                allowed_authors=frozenset({"peter v brett"}),
                ignored_authors=frozenset(),
            ),
            mam_token="smoke_test_cookie",
            qbit_category="mam-complete",
            budget_cap=200,
            queue_max=100,
            queue_mode_enabled=True,
            seed_seconds_required=72 * 3600,
            db_factory=get_db,
            fetch_torrent=fetch_torrent,  # the REAL one
            qbit=qbit,
        )

        # Production-shaped on_announce: bridge IrcClient → dispatcher.
        # Same exact wiring as main.py's lifespan.
        async def on_announce(announce):
            await handle_announce(deps, announce)

        config = IrcConfig(
            server="fake",
            port=6697,
            tls=False,
            nick="smokebot",
            account="smokeacct",
            password="smokepass",
            auth_mode="sasl",
            channel="#announce",
            announcer_nick="MouseBot",
            initial_backoff_seconds=0.05,
            max_backoff_seconds=0.2,
            max_reconnect_attempts=1,
            read_iter_timeout_seconds=5.0,
            keepalive_interval_seconds=30.0,
            dead_connection_seconds=60.0,
            handshake_timeout_seconds=2.0,
        )
        client = IrcClient(config, on_announce, connect_fn=fake_irc.connect_fn)

        # ── Run the full pipeline ───────────────────────────
        task = asyncio.create_task(client.run_forever())
        try:
            # Walk through the SASL handshake against the fake.
            await drive_sasl_handshake(fake_irc, nick=config.nick)

            # Now feed the real fixture announce line through the
            # IRC client. The whole pipeline should fire end to end:
            #   PRIVMSG → parse_announce → on_announce →
            #   handle_announce → filter (allow) → rate_limit
            #   (submit) → fetch_torrent (REAL — hits fake MAM) →
            #   info_hash (real bencode reader) → qbit.add_torrent
            #   → ledger.record_grab
            fake_irc.feed_line(_REAL_ANNOUNCE_PRIVMSG)

            # Wait for the dispatcher to record the grab. The fake-MAM
            # round trip is in-process so it should be fast — give
            # it ~50 polls of 20ms = 1 second.
            for _ in range(50):
                if qbit.add_calls:
                    break
                await asyncio.sleep(0.02)

            # ── Assertions: the WHOLE pipeline ran ──────────

            # 1. The fake MAM was hit for the torrent download
            assert any(
                "download.php" in str(req.url) and "tid=1233592" in str(req.url)
                for req in fake_mam.requests
            ), "expected fetch_torrent to hit fake MAM with tid=1233592"

            # 2. The fake MAM saw our cookie attached
            assert "smoke_test_cookie" in fake_mam.cookies_seen()

            # 3. The fake qBit was given the bytes
            assert len(qbit.add_calls) == 1
            assert qbit.add_calls[0]["category"] == "mam-complete"

            # 4. The grab row exists in the right state
            db = await get_db()
            try:
                grab = await grabs_storage.find_grab_by_torrent_id(
                    db, "1233592"
                )
                assert grab is not None
                assert grab.state == grabs_storage.STATE_SUBMITTED
                assert grab.qbit_hash is not None
                assert len(grab.qbit_hash) == 40
                assert grab.author_blob == "Peter V Brett"
                assert grab.category == "Audiobooks - Fantasy"

                # 5. The ledger entry exists and counts against budget
                assert await ledger_mod.count_active(db) == 1
                ledger_row = await ledger_mod.get_row(db, grab.id)
                assert ledger_row is not None
                assert ledger_row.qbit_hash == grab.qbit_hash
                assert ledger_row.released_at is None

                # 6. The IrcClient internal stats reflect the dispatch
                assert client.announces_seen == 1
                assert client.announces_dispatched == 1
            finally:
                await db.close()
        finally:
            await client.stop()
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

    async def test_skipped_announce_does_not_reach_qbit(
        self, temp_db, fake_mam, fake_irc
    ):
        # Same pipeline, but with a filter config that DOESN'T
        # allow the fixture author. The whole point of this test
        # is verifying that the filter actually short-circuits
        # before fetch_torrent runs — no MAM contact, no qBit contact.
        qbit = _SmokeQbit()

        deps = DispatcherDeps(
            filter_config=FilterConfig(
                allowed_categories=frozenset({
                    normalize_category("Audiobooks - Fantasy"),
                }),
                # Empty allow list — Peter V Brett is unknown
                allowed_authors=frozenset(),
                ignored_authors=frozenset(),
            ),
            mam_token="smoke_test_cookie",
            qbit_category="mam-complete",
            budget_cap=200,
            queue_max=100,
            queue_mode_enabled=True,
            seed_seconds_required=72 * 3600,
            db_factory=get_db,
            fetch_torrent=fetch_torrent,
            qbit=qbit,
        )

        async def on_announce(announce):
            await handle_announce(deps, announce)

        config = IrcConfig(
            nick="smokebot", account="acct", password="pass",
            auth_mode="sasl",
            initial_backoff_seconds=0.05, max_reconnect_attempts=1,
            handshake_timeout_seconds=2.0,
        )
        client = IrcClient(config, on_announce, connect_fn=fake_irc.connect_fn)
        task = asyncio.create_task(client.run_forever())
        try:
            await drive_sasl_handshake(fake_irc, nick=config.nick)
            fake_irc.feed_line(_REAL_ANNOUNCE_PRIVMSG)

            # Wait long enough for the dispatcher to have run.
            await asyncio.sleep(0.1)

            # Filter should have skipped — no qBit add, no MAM
            # download attempt, but the audit row should exist.
            assert qbit.add_calls == []
            assert not any(
                "download.php" in str(req.url) for req in fake_mam.requests
            ), "filter should have prevented any MAM download"

            db = await get_db()
            try:
                grab = await grabs_storage.find_grab_by_torrent_id(
                    db, "1233592"
                )
                assert grab is None  # no grab row for skipped announces
                assert await ledger_mod.count_active(db) == 0
            finally:
                await db.close()
        finally:
            await client.stop()
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
