"""
MAM IRC client.

A minimal, single-purpose IRC client that connects to
`irc.myanonamouse.net`, authenticates via SASL PLAIN (or NickServ as
a fallback), joins `#announce`, listens for PRIVMSGs from MouseBot,
parses them via `mam.announce.parse_announce`, and dispatches each
parsed `Announce` to a user-supplied callback.

Why a hand-rolled client instead of `pydle`?

  Seshat's IRC needs are extraordinarily narrow — one server, one
  channel, one bot we listen to, two auth modes, ping handling,
  reconnect. We use ~5% of pydle's surface area. A hand-rolled
  ~250-line client gives us full control over reconnect semantics,
  trivially testable I/O (we inject a connect_fn that returns a
  fake StreamReader/StreamWriter pair), and zero external dependency
  to fight. The IRC protocol is just lines of text — implementing
  the slice we need is smaller than the test scaffolding pydle would
  require.

Reconnect strategy is lifted from the Autobrr research findings:

  - Exponential backoff starting at 15 seconds, capped at 10 minutes
  - Up to 25 back-to-back reconnect attempts before giving up
  - 4-minute read timeout (matches Autobrr's KeepAlive)
  - The "manual-stop guard" from Autobrr issue #1239: when a stop
    signal arrives DURING a reconnect backoff, break out immediately
    instead of completing the wait and trying to reconnect.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from app.filter.gate import Announce
from app.mam.announce import parse_announce

_log = logging.getLogger("seshat.mam.irc")
# Dedicated announce channel — the Logs page "Announces" tab filters
# on this logger name (plus seshat.orchestrator.dispatch) so users
# can see every parsed announce without wading through IRC PING/PONG
# traffic. Writing at INFO so the filter captures it cleanly; the
# raw PRIVMSG stays at DEBUG on the main _log.
_announce_log = logging.getLogger("seshat.mam.announce")


class _NickInUse(Exception):
    """Internal: raised by _expect when 433 is received and nick_suffix_max > 0.

    The handshake catches this and retries with a suffixed nick.
    """

    def __init__(self, attempted_nick: str, message: str = ""):
        self.attempted_nick = attempted_nick
        super().__init__(message or f"nick '{attempted_nick}' in use")


class IrcFatalConfigError(Exception):
    """An IRC connection failed in a way that no amount of reconnecting
    will fix — wrong credentials, nick already claimed by another
    client, SASL not supported, etc. The supervised loop catches this
    distinctly from generic ConnectionError and stops the listener
    entirely instead of backing off and retrying. The user has to
    fix the config and restart Seshat (or the lifespan will rebuild
    the dispatcher when settings change in Phase 3).
    """


# ─── Config ──────────────────────────────────────────────────


@dataclass(frozen=True)
class IrcConfig:
    """Everything the IRC client needs to know to talk to MAM.

    Defaults are MAM-specific because Seshat is MAM-only by design.
    The fields are exposed so tests can override anything (especially
    timeouts and reconnect counts) without monkey-patching.
    """

    server: str = "irc.myanonamouse.net"
    port: int = 6697
    tls: bool = True
    # MAM IRC has historically used a self-signed-ish cert that
    # Autobrr's config tells you to skip-verify. We default to that
    # behavior for the same compat reason; can be flipped via Settings.
    tls_verify: bool = False

    # The bot identity registered with NickServ on MAM IRC.
    nick: str = ""
    user: str = "seshat"
    realname: str = "Seshat courier bot"

    # SASL/NickServ account credentials. For SASL PLAIN, `account` is
    # the authcid (your NickServ account name); `password` is the
    # NickServ password. For NickServ identify, `password` is sent in
    # a PRIVMSG to NickServ after the welcome.
    account: str = ""
    password: str = ""

    # "sasl" → CAP/SASL PLAIN handshake (preferred, what OP uses)
    # "nickserv" → plain NICK/USER then PRIVMSG NickServ IDENTIFY
    # "none" → no auth (test mode, won't work against real MAM)
    auth_mode: str = "sasl"

    channel: str = "#announce"
    # Only PRIVMSGs from this nick in `channel` are treated as
    # announces. Anything else is logged at debug and ignored.
    announcer_nick: str = "MouseBot"

    # ── Reconnect / liveness (Autobrr-derived) ──────────────
    initial_backoff_seconds: float = 15.0
    max_backoff_seconds: float = 600.0
    max_reconnect_attempts: int = 25

    # The read loop now uses a short per-iteration timeout (so we
    # can wake up periodically and send client-side keepalives) plus
    # a longer "actual dead connection" deadline based on how long
    # it's been since we saw ANY server traffic. The 4-minute MAM IRC
    # PING interval mentioned in earlier docs turned out to be wrong
    # in practice: in production we observed MAM IRC going 4+ minutes
    # without sending us anything during quiet periods, which would
    # silently kill our connection if we relied on a single read
    # timeout for liveness.
    #
    # `read_iter_timeout_seconds` is how long each individual readline
    # waits before checking the keepalive timer. Short = responsive,
    # but no shorter than necessary because every wakeup is wasted CPU.
    read_iter_timeout_seconds: float = 30.0

    # `keepalive_interval_seconds` is how often Seshat sends a
    # client-initiated PING to the server during silence. Standard
    # IRC bot practice is ~60s. The server will reply with PONG, which
    # both refreshes our `last_traffic_at` clock and keeps the server
    # from idle-disconnecting US.
    keepalive_interval_seconds: float = 60.0

    # `dead_connection_seconds` is the actual liveness deadline. If
    # we go this long without ANY server traffic (including PONGs to
    # our keepalives), we treat the connection as dead and reconnect.
    # 600s = 10 minutes — well past the 60s keepalive cadence so any
    # genuine network drop will fire this within ~9-10 minutes max.
    dead_connection_seconds: float = 600.0

    # Per-handshake-step timeout. If SASL auth or channel join hangs
    # this long, we abort and reconnect.
    handshake_timeout_seconds: float = 30.0

    # 433 auto-suffix: when the configured nick is already in use,
    # try nick_2, nick_3, ... up to this many attempts before giving up.
    # 0 = fail-fast on first collision (original behavior).
    nick_suffix_max: int = 3


# ─── IRC line parser ─────────────────────────────────────────


@dataclass
class IrcMessage:
    """One parsed IRC protocol line."""

    raw: str
    prefix: str = ""        # everything between : and the first space (host or nick!user@host)
    nick: str = ""          # extracted from prefix, if present
    command: str = ""       # uppercased command or 3-digit numeric
    params: list[str] = field(default_factory=list)
    trailing: str = ""      # the post-" :" text


_PREFIX_NICK_RX = re.compile(r"^([^!@\s]+)")


def parse_irc_line(line: str) -> Optional[IrcMessage]:
    """Parse one IRC protocol line into an IrcMessage.

    Returns None for empty input. Tolerant of malformed lines — IRC
    has been around long enough that everything in the wild is at
    least *almost* well-formed, and the read loop should drop bad
    lines silently rather than crashing.
    """
    if not line:
        return None
    msg = IrcMessage(raw=line)
    rest = line

    # Optional prefix: ":sender ..."
    if rest.startswith(":"):
        space = rest.find(" ")
        if space < 0:
            return None
        msg.prefix = rest[1:space]
        m = _PREFIX_NICK_RX.match(msg.prefix)
        if m:
            msg.nick = m.group(1)
        rest = rest[space + 1:]

    # Trailing parameter starts with " :" — everything after the
    # delimiter is one big param including spaces.
    trailing_idx = rest.find(" :")
    if trailing_idx >= 0:
        msg.trailing = rest[trailing_idx + 2:]
        head = rest[:trailing_idx]
    else:
        head = rest

    parts = head.split(" ")
    if not parts or not parts[0]:
        return None
    msg.command = parts[0].upper()
    msg.params = [p for p in parts[1:] if p]
    return msg


# ─── The client ──────────────────────────────────────────────


# Type alias for the connection factory the client uses to open a
# socket. Production code uses the default `_real_connect`. Tests
# inject a fake that returns an in-memory reader/writer pair.
ConnectFn = Callable[
    [],
    Awaitable[tuple[asyncio.StreamReader, Any]],
]


class IrcClient:
    """Connects to MAM IRC, parses announces, dispatches to a callback.

    Lifecycle:
      - `await client.run_forever()` runs until `await client.stop()`
        is called from another task. Internally manages the
        connect → auth → join → read loop and reconnects on failure.
      - `client.connected`, `client.authenticated`, `client.joined`,
        `client.last_error`, `client.announces_seen`,
        `client.announces_dispatched` are read-only status fields the
        dashboard polls.

    The `on_announce` callback is awaited for every successfully-parsed
    announce. Parser failures (announces that don't match the regex)
    are logged at debug and dropped — they're typically MouseBot
    status messages or non-torrent PRIVMSGs we don't care about.

    `connect_fn` is a test hook. Production code leaves it None and
    the client uses asyncio's real network transport.
    """

    def __init__(
        self,
        config: IrcConfig,
        on_announce: Callable[[Announce], Awaitable[None]],
        *,
        connect_fn: Optional[ConnectFn] = None,
    ) -> None:
        self.config = config
        self.on_announce = on_announce
        self._connect_fn = connect_fn

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Any = None
        self._stop = asyncio.Event()
        self._current_nick = config.nick
        self._nick_attempt = 0

        # Status fields (read by app.state mirror + dashboard).
        self.connected = False
        self.authenticated = False
        self.joined = False
        self.last_error = ""
        self.announces_seen = 0
        self.announces_dispatched = 0

    # ─── Public API ──────────────────────────────────────────

    async def run_forever(self) -> None:
        """Connect, run, reconnect on failure, until stop() is called."""
        attempt = 0
        while not self._stop.is_set():
            try:
                await self._run_once()
                # If _run_once returned cleanly (server disconnected
                # us), reset the backoff counter — we made it through
                # a real connection cycle, so the next attempt is a
                # "first attempt" again.
                attempt = 0
            except asyncio.CancelledError:
                raise
            except IrcFatalConfigError as e:
                # Bail out entirely. Reconnecting on a config error
                # would just hit the same wall on the next handshake.
                # The user has to fix their settings and restart
                # Seshat — there is no remediation we can do from
                # inside the loop. Logged at ERROR so it stands out
                # in the dashboard / log scroll.
                self.last_error = f"FATAL: {e}"
                _log.error(
                    f"IRC listener stopped permanently — {e}. "
                    f"Fix your settings and restart Seshat to retry."
                )
                self._reset_status()
                break
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                _log.warning(f"IRC connection error: {self.last_error}")

            self._reset_status()

            if self._stop.is_set():
                _log.info("IRC stop requested; not reconnecting")
                break

            attempt += 1
            if attempt > self.config.max_reconnect_attempts:
                _log.error(
                    f"IRC giving up after {attempt - 1} reconnect attempts; "
                    f"last error: {self.last_error}"
                )
                break

            delay = self._compute_backoff(attempt)
            _log.info(
                f"IRC reconnecting in {delay:.0f}s "
                f"(attempt {attempt}/{self.config.max_reconnect_attempts})"
            )

            # Manual-stop guard (Autobrr issue #1239): wait for either
            # the backoff timer OR a stop signal. If stop fires during
            # the wait, break immediately instead of attempting another
            # connection that we'd just have to tear down.
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                # If wait_for returned without timeout, stop is set.
                _log.info("IRC stop signaled during reconnect backoff")
                break
            except asyncio.TimeoutError:
                pass  # delay elapsed normally; fall through to retry

    async def stop(self) -> None:
        """Signal the run loop to exit and tear down the open connection."""
        self._stop.set()
        await self._close_connection()

    # ─── Connection lifecycle ────────────────────────────────

    async def _run_once(self) -> None:
        """One full connect → auth → join → read cycle.

        Returns normally on a clean disconnect; raises on any failure
        the run_forever loop should treat as "try reconnecting."
        """
        await self._open_connection()
        try:
            if self.config.auth_mode == "sasl":
                await asyncio.wait_for(
                    self._sasl_handshake(),
                    timeout=self.config.handshake_timeout_seconds,
                )
            else:
                await self._send_nick_user()

            await asyncio.wait_for(
                self._wait_for_welcome(),
                timeout=self.config.handshake_timeout_seconds,
            )

            if self.config.auth_mode == "nickserv":
                await self._nickserv_identify()

            await asyncio.wait_for(
                self._join_channel(),
                timeout=self.config.handshake_timeout_seconds,
            )

            self.authenticated = True
            self.joined = True
            await self._read_loop()
        finally:
            await self._close_connection()

    async def _open_connection(self) -> None:
        if self._connect_fn is not None:
            self._reader, self._writer = await self._connect_fn()
        else:
            self._reader, self._writer = await self._real_connect()
        self.connected = True
        _log.info(f"IRC connected to {self.config.server}:{self.config.port}")

    async def _real_connect(self):
        ssl_ctx: Optional[ssl.SSLContext] = None
        if self.config.tls:
            ssl_ctx = ssl.create_default_context()
            if not self.config.tls_verify:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
        return await asyncio.open_connection(
            self.config.server, self.config.port, ssl=ssl_ctx
        )

    async def _close_connection(self) -> None:
        writer = self._writer
        if writer is None:
            return
        self._writer = None
        self._reader = None
        try:
            writer.close()
            wait_closed = getattr(writer, "wait_closed", None)
            if wait_closed is not None:
                await wait_closed()
        except Exception as e:
            _log.debug(f"IRC writer close raised: {e}")

    def _reset_status(self) -> None:
        self.connected = False
        self.authenticated = False
        self.joined = False

    # ─── Wire I/O ────────────────────────────────────────────

    async def _send(self, line: str) -> None:
        """Write one CRLF-terminated line to the server."""
        if self._writer is None:
            raise ConnectionError("send: writer is closed")
        # Strip any embedded CR/LF for safety — IRC injection via
        # newline-in-channel-name is a real bug class.
        clean = line.replace("\r", "").replace("\n", "")
        # Don't log auth payloads at INFO — they contain credentials.
        upper = clean.upper()
        if upper.startswith("AUTHENTICATE ") and clean != "AUTHENTICATE PLAIN":
            _log.debug("IRC > AUTHENTICATE <redacted>")
        elif upper.startswith("PRIVMSG NICKSERV"):
            _log.debug("IRC > PRIVMSG NickServ <redacted>")
        elif upper.startswith("PASS "):
            _log.debug("IRC > PASS <redacted>")
        elif upper.startswith("OPER "):
            _log.debug("IRC > OPER <redacted>")
        else:
            _log.debug(f"IRC > {clean}")
        self._writer.write((clean + "\r\n").encode("utf-8", errors="replace"))
        drain = getattr(self._writer, "drain", None)
        if drain is not None:
            await drain()

    async def _read_line(self, timeout: float) -> Optional[str]:
        """Read one CRLF-terminated line from the server.

        Returns None on clean EOF. Raises asyncio.TimeoutError if no
        data arrives within `timeout`.
        """
        if self._reader is None:
            return None
        raw = await asyncio.wait_for(self._reader.readline(), timeout=timeout)
        if not raw:
            return None
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        _log.debug(f"IRC < {line}")
        return line

    async def _expect(
        self,
        *commands: str,
        timeout: Optional[float] = None,
    ) -> IrcMessage:
        """Read until we see a message whose command matches any given.

        Handles PINGs transparently along the way (responds with
        PONG and keeps reading) so callers don't have to. Also
        detects fatal handshake errors (433 nick in use, 432
        erroneous nick, 462 already registered) and raises
        `IrcFatalConfigError` so the supervised loop can stop the
        listener entirely instead of reconnecting in a loop the
        user can't fix without changing settings.
        """
        if timeout is None:
            timeout = self.config.handshake_timeout_seconds
        deadline = time.monotonic() + timeout
        wanted = {c.upper() for c in commands}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError(
                    f"timeout waiting for IRC command(s) {sorted(wanted)}"
                )
            line = await self._read_line(timeout=remaining)
            if line is None:
                raise ConnectionError("connection closed during handshake")
            msg = parse_irc_line(line)
            if msg is None:
                continue
            if msg.command == "PING":
                await self._send(f"PONG :{msg.trailing or (msg.params[0] if msg.params else '')}")
                continue

            # Fatal config errors during handshake. Detected here
            # rather than at the call sites because all of them can
            # arrive at any handshake `_expect` between sending NICK
            # and getting RPL_WELCOME — putting the check in one
            # place means we never miss them no matter where in the
            # flow they show up.
            if msg.command == "433":
                # Real-world example from MAM IRC:
                #   :irc1.myanonamouse.net 433 * Turtles81_arrbot :Nickname is already in use.
                attempted = msg.params[1] if len(msg.params) > 1 else self._current_nick
                if self._nick_attempt < self.config.nick_suffix_max:
                    # Try a suffixed nick: nick_2, nick_3, etc.
                    raise _NickInUse(attempted)
                raise IrcFatalConfigError(
                    f"IRC nickname '{attempted}' is already in use "
                    f"(tried {self._nick_attempt} suffix(es)) — "
                    f"either another client is connected as this nick "
                    f"(check Autobrr, etc.) or pick a different "
                    f"mam_irc_nick in settings.json"
                )
            if msg.command == "432":
                attempted = msg.params[1] if len(msg.params) > 1 else self.config.nick
                raise IrcFatalConfigError(
                    f"IRC server rejected nickname '{attempted}' as "
                    f"erroneous: {msg.trailing}"
                )
            if msg.command == "462":
                # "You may not reregister" — usually means the
                # handshake replayed somehow. Treat as fatal because
                # reconnecting won't fix it without state cleanup.
                raise IrcFatalConfigError(
                    f"IRC server says we're already registered: {msg.trailing}"
                )

            if msg.command in wanted:
                return msg

    # ─── Handshake stages ────────────────────────────────────

    async def _send_nick_user(self) -> None:
        self._current_nick = self.config.nick
        self._nick_attempt = 0
        await self._send(f"NICK {self._current_nick}")
        await self._send(
            f"USER {self.config.user} 0 * :{self.config.realname}"
        )

    async def _retry_nick(self) -> None:
        """Send a suffixed NICK after a 433 collision."""
        self._nick_attempt += 1
        self._current_nick = f"{self.config.nick}_{self._nick_attempt + 1}"
        _log.info(
            "IRC nick collision, trying suffix: %s (attempt %d/%d)",
            self._current_nick, self._nick_attempt, self.config.nick_suffix_max,
        )
        await self._send(f"NICK {self._current_nick}")

    async def _sasl_handshake(self) -> None:
        """IRCv3 SASL PLAIN handshake.

        Order matters: CAP LS → CAP REQ → NICK/USER → AUTHENTICATE
        flow → CAP END. The NICK/USER pair has to be sent BEFORE we
        finish CAP negotiation, or the server will close us with
        "ERROR :Connection registration timed out".

        If a 433 (nick in use) is received and nick_suffix_max > 0,
        the handshake retries with a suffixed nick (nick_2, nick_3, ...)
        before giving up.
        """
        await self._send("CAP LS 302")
        await self._expect("CAP")  # CAP * LS :sasl ...

        await self._send("CAP REQ :sasl")
        await self._send_nick_user()

        # The CAP ACK and subsequent steps can receive a 433 at any
        # point if the nick is already in use. We retry with suffixed
        # nicks up to nick_suffix_max times.
        while True:
            try:
                ack = await self._expect("CAP")  # CAP * ACK :sasl
                break
            except _NickInUse:
                await self._retry_nick()

        if "sasl" not in ack.trailing.lower():
            raise ConnectionError(
                f"server NAKed SASL: {ack.raw}"
            )

        await self._send("AUTHENTICATE PLAIN")
        await self._expect("AUTHENTICATE")

        auth_string = f"\0{self.config.account}\0{self.config.password}"
        encoded = base64.b64encode(auth_string.encode("utf-8")).decode("ascii")
        await self._send(f"AUTHENTICATE {encoded}")

        result = await self._expect("903", "904", "905", "906")
        if result.command != "903":
            raise ConnectionError(
                f"SASL authentication failed: {result.command} {result.raw}"
            )

        await self._send("CAP END")

    async def _wait_for_welcome(self) -> None:
        """Wait for the server's 001 numeric (RPL_WELCOME).

        A 433 can still arrive here if the nick collision happens
        after CAP END but before 001.
        """
        while True:
            try:
                await self._expect("001")
                return
            except _NickInUse:
                await self._retry_nick()

    async def _nickserv_identify(self) -> None:
        """Send PRIVMSG NickServ :IDENTIFY <password>.

        Used in the `nickserv` auth mode (fallback). Doesn't wait for
        a response — NickServ replies are NOTICEs, and the read loop
        ignores them. The server is already letting us join channels
        at this point so identification proceeds in parallel.
        """
        await self._send(
            f"PRIVMSG NickServ :IDENTIFY {self.config.password}"
        )

    async def _join_channel(self) -> None:
        await self._send(f"JOIN {self.config.channel}")
        # Look for either the JOIN echo or 366 (RPL_ENDOFNAMES). Some
        # servers send 332/333/353 between, which `_expect` ignores
        # because we're only matching on JOIN/366.
        await self._expect("JOIN", "366")

    # ─── Read loop ───────────────────────────────────────────

    async def _read_loop(self) -> None:
        """Run until the connection drops or stop is signaled.

        Two timing layers in play:

          - **Per-iteration `readline()` timeout** (short, ~30s) so
            we wake up frequently to check whether it's time to send
            a client-side keepalive PING.

          - **Cumulative liveness deadline** based on
            `last_traffic_at` (long, ~10 minutes) — if we haven't
            seen ANY server traffic in this window (not even PONGs
            to our keepalives), the connection is dead and we
            reconnect.

        Why client-initiated keepalives matter:
        Some MAM IRC servers + quiet channels can go several
        minutes without sending anything. Without our own PING
        every 60s, those quiet windows would trip a dumb single-
        timer read timeout and trigger an unnecessary reconnect
        BEFORE the channel even ramps up to its normal traffic
        rate. The first production smoke test caught us in exactly
        this loop: connect → handshake → JOIN → 4 minutes silence
        → reconnect → repeat → never receive a single announce.

        Handles PING/PONG transparently. Routes PRIVMSGs in our
        channel from the configured announcer nick through
        `parse_announce` and dispatches results to the user
        callback. Everything else is dropped silently.
        """
        last_traffic_at = time.monotonic()
        last_keepalive_at = time.monotonic()
        keepalive_pending = False  # True between sending PING and seeing any reply

        while not self._stop.is_set():
            now = time.monotonic()

            # Liveness deadline: have we seen ANYTHING from the
            # server in dead_connection_seconds? If not, treat the
            # connection as dead and let run_forever reconnect.
            since_traffic = now - last_traffic_at
            if since_traffic > self.config.dead_connection_seconds:
                _log.warning(
                    f"IRC connection appears dead "
                    f"({since_traffic:.0f}s since last server traffic, "
                    f"keepalive_pending={keepalive_pending}); "
                    f"forcing reconnect"
                )
                raise ConnectionError(
                    f"no server traffic for {since_traffic:.0f}s"
                )

            # Keepalive cadence: send a client-initiated PING every
            # keepalive_interval_seconds to make sure the server
            # knows we're alive AND to give us something to receive
            # (the PONG reply) so quiet channels don't look like
            # dead connections.
            since_keepalive = now - last_keepalive_at
            if since_keepalive > self.config.keepalive_interval_seconds:
                try:
                    await self._send("PING :seshat-keepalive")
                except Exception as e:
                    _log.warning(f"IRC keepalive PING failed: {e}")
                    raise ConnectionError(
                        f"keepalive PING send failed: {e}"
                    )
                last_keepalive_at = now
                keepalive_pending = True

            # Short read with per-iteration timeout. On timeout we
            # just loop back to the keepalive check above — that's
            # the whole reason this timeout is short.
            try:
                line = await self._read_line(
                    timeout=self.config.read_iter_timeout_seconds
                )
            except asyncio.TimeoutError:
                continue

            if line is None:
                _log.info("IRC server closed the connection")
                return

            # Any received bytes counts as traffic for the liveness
            # clock — including the line we're about to process.
            last_traffic_at = time.monotonic()
            keepalive_pending = False

            msg = parse_irc_line(line)
            if msg is None:
                continue

            if msg.command == "PING":
                # Server pinged us. Reply with PONG. The PING token
                # we got is whatever the server wants echoed back.
                await self._send(
                    f"PONG :{msg.trailing or (msg.params[0] if msg.params else '')}"
                )
                continue

            if msg.command == "PONG":
                # Either a reply to our keepalive PING or a stray
                # server PONG. Either way, the receipt has already
                # refreshed `last_traffic_at` above; nothing else
                # to do.
                continue

            if msg.command == "PRIVMSG":
                await self._handle_privmsg(msg)
                continue

            if msg.command == "ERROR":
                # Server-initiated disconnect (k-line, server restart,
                # etc). Treat as a connection failure so the run loop
                # cycles through reconnect.
                raise ConnectionError(f"server ERROR: {msg.trailing}")

    async def _handle_privmsg(self, msg: IrcMessage) -> None:
        # PRIVMSG params: [target] [text]
        # Target is the channel; text is the trailing.
        if not msg.params:
            return
        target = msg.params[0]
        if target.lower() != self.config.channel.lower():
            return
        if msg.nick.lower() != self.config.announcer_nick.lower():
            return

        self.announces_seen += 1
        announce = parse_announce(msg.trailing)
        if announce is None:
            _log.debug(f"IRC PRIVMSG didn't parse as announce: {msg.trailing[:80]}")
            return

        # Log every parsed announce to the dedicated announce channel
        # so the Logs page Announces tab has something to show. Keep
        # the line compact — torrent name, category, format, VIP flag.
        # Downstream dispatcher logs the accept/drop decision against
        # seshat.orchestrator.dispatch which the Announces tab also
        # surfaces. Uses the gate.py Announce dataclass field names
        # (torrent_name + filetype) — NOT `.name` / `.format` which
        # don't exist on this object and produced AttributeError
        # before the fix.
        _announce_log.info(
            "announce tid=%s %r cat=%s fmt=%s vip=%s",
            announce.torrent_id, announce.torrent_name,
            announce.category or "?", announce.filetype or "?",
            announce.vip,
        )

        try:
            await self.on_announce(announce)
            self.announces_dispatched += 1
        except Exception:
            _log.exception(
                f"on_announce callback raised for tid={announce.torrent_id}"
            )

    # ─── Backoff ─────────────────────────────────────────────

    def _compute_backoff(self, attempt: int) -> float:
        """Exponential backoff capped at max_backoff_seconds.

        attempt 1 → initial; 2 → 2×; 3 → 4×; etc. Capped so a long
        outage doesn't push the next retry an hour into the future.
        """
        delay = self.config.initial_backoff_seconds * (2 ** (attempt - 1))
        return min(delay, self.config.max_backoff_seconds)
