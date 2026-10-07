"""
The dispatcher.

Two public functions, both following the same shape:

  - `handle_announce(deps, announce)` — called by the IRC listener
    for every parsed announce. Runs the filter, evaluates the rate
    limiter, fetches the .torrent file (if allowed), submits to
    the download client (if budget allows), and updates all the
    persistence layers in the right order.

  - `inject_grab(deps, torrent_id, ...)` — called by the manual-
    inject HTTP endpoint. Skips the filter (the user already
    decided they want this) but still goes through the rate
    limiter so a manually-injected grab respects the snatch budget.

The `Dispatcher` dataclass below is the dependency container —
everything the dispatcher needs is passed in explicitly so the
tests can construct one with fakes and verify the orchestration
without any global state. In production, `main.py`'s lifespan
builds a singleton Dispatcher with real implementations and
hands it to the IRC listener and the inject router.

State transitions written by this module:

    decide=submit, fetch ok, client ok       → STATE_SUBMITTED
    decide=submit, fetch=cookie_expired    → STATE_FAILED_COOKIE_EXPIRED
    decide=submit, fetch=torrent_not_found → STATE_FAILED_TORRENT_GONE
    decide=submit, fetch=other failure     → STATE_FAILED_UNKNOWN
    decide=submit, fetch ok, client reject → STATE_FAILED_QBIT_REJECTED
    decide=submit, fetch ok, client auth   → STATE_PENDING_QUEUE (queued for retry)
    decide=queue,  fetch ok               → STATE_PENDING_QUEUE (queued)
    decide=queue,  fetch failure          → same as submit-failure
    decide=drop                           → no grab row, only audit
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import weakref
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Protocol

import aiosqlite

from app.clients.base import AddResult, TorrentClient
from app.filter.gate import Announce, Decision, FilterConfig, evaluate_announce
from app.mam.cookie import fingerprint as cookie_fingerprint
from app.mam.grab import GrabResult
from app.mam.torrent_meta import BencodeError, info_hash, read_mam_comment
from app.mam.torrent_id import extract_torrent_id
from app.mam.torrent_info import (
    TorrentInfo,
    TorrentInfoError,
    TorrentNotFoundError,
    get_torrent_info,
)
from app.mam.user_status import UserStatusError, get_user_status
from app.orchestrator.auto_train import train_authors_from_torrent_info
from app.orchestrator import torrent_store
from app.orchestrator.delayed import rotate_oldest_to_delayed
from app.orchestrator.download_folders import (
    compute_download_folder,
    ensure_folder_exists,
    translate_path,
)
from app.policy.engine import (
    EconomicContext,
    PolicyConfig,
    evaluate_policy,
)
from app.rate_limit import decide_grab_action
from app.rate_limit import ledger as ledger_mod
from app.rate_limit import queue as queue_mod
from app.storage import economy_audit
from app.storage import grabs as grabs_storage
from app.storage import holds as holds_storage
from app.storage import tentative as tentative_storage
from app.orchestrator.format_dedup import (
    evaluate_format_dedup,
    lookup_dedup_siblings,
    media_type_from_category,
    normalize_dedup_key,
)

_log = logging.getLogger("seshat.orchestrator.dispatch")


# Rolling-6h-window ntfy throttle for buffer-gate blocks, keyed by
# trigger (IRC autograb vs user grab). In-memory, resets on process
# restart — a restart right after a notify doesn't cost anything
# worse than one extra message if the buffer is still tight. Writing
# this to the DB would persist it across restarts but isn't worth
# the complexity for a soft "don't spam" throttle.
_BUFFER_GATE_NOTIFY_WINDOW_SECONDS = 6 * 3600
_last_buffer_gate_notify_at: dict[str, float] = {}


# Module-level wallclock + lock for qBit add-torrent staggering.
# qBit fires one tracker announce per POST /api/v2/torrents/add, and
# MAM's tracker throttles announces per IP. A burst of grabs (autograb
# + manual inject + author allow-list catching multiple in quick
# succession) can land 8+ adds in 3 seconds, tripping the throttle
# and turning every subsequent announce into a sticky ~15-minute
# timeout for every torrent on the user's account. Diagnosed on
# 2026-05-22 — the per-IP throttle is invisible from inside the
# qBit container (curl shows 200 OK 426ms) but real for bursty
# patterns.
#
# `_stagger_qbit_add()` reads `qbit_add_stagger_s` + `_jitter_s` from
# live settings on every call and sleeps before the actual add so
# consecutive calls are at least the configured gap apart. The lock
# serializes concurrent dispatcher tasks through the gap correctly —
# without it, two parallel grabs could both see "last add was 5s ago"
# and both proceed immediately.
_qbit_add_lock = asyncio.Lock()
_last_qbit_add_at: float = 0.0

# Snatch safety: MAM sees one download per torrent, ever. The guard
# re-checks for a blocking grab and inserts the new grab row under this
# lock, so two concurrent grabs of the same torrent ID (an IRC announce
# racing a manual inject, a double-clicked Grab, the cookie-retry job)
# can't both pass the check before either row exists. One process, one
# event loop (single uvicorn worker), so an asyncio lock is enough.
# Kept per event loop rather than as one module-level Lock: a Lock binds
# to the first loop that contends for it, and the test suite runs a
# fresh loop per test. Production has exactly one loop, so one lock.
_grab_claim_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def grab_claim_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _grab_claim_locks.get(loop)
    if lock is None:
        lock = _grab_claim_locks[loop] = asyncio.Lock()
    return lock


# Waiting for MAM's index (D37, D38). A fresh upload reaches #announce
# about a second after MAM adds it, before MAM's search API lists it,
# so the lookup every grab decision leans on (free status for a wedge,
# the uploader, `my_snatched`, the size) used to come back "not found"
# on nearly every autograb. An allowed announce MAM doesn't list yet is
# held: a background task polls on this schedule (cumulative seconds
# after the announce: 5, 15, 30, 60, 120, 180, 300, 420, 600) and grabs
# once MAM lists it. If MAM still doesn't by the end, the grab goes
# ahead on the announce's word (VIP|Normal). In memory only: a restart
# mid-wait loses the grab, though its announce row is already written.
_INDEX_WAIT_DELAYS_S: tuple[float, ...] = (5, 10, 15, 30, 60, 60, 120, 120, 180)
_index_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep  # test seam
# Strong refs: the event loop only keeps weak ones to running tasks.
_held_grabs: set[asyncio.Task] = set()
# Held grabs finish in the background, so two could otherwise run the
# grab path at once and both slip past the format-dedup gate (each sees
# no in-flight sibling yet). Every allowed IRC announce's grab runs
# under this lock, as they did when the IRC loop ran them one by one.
_announce_grab_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def _announce_grab_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _announce_grab_locks.get(loop)
    if lock is None:
        lock = _announce_grab_locks[loop] = asyncio.Lock()
    return lock


async def release_write_lock(db: aiosqlite.Connection) -> None:
    """Commit anything pending on `db` before waiting on the claim lock.

    Whatever is pending would be committed by this connection's next
    commit anyway; committing now only stops it holding SQLite's write
    lock while it waits. Every claim-lock site calls this first.
    """
    if db.in_transaction:
        _log.warning(
            "grab claim: connection had an open transaction before the "
            "claim lock; committing it so the lock holder can write"
        )
        await db.commit()


async def _stagger_qbit_add() -> float:
    """Sleep before the next qBit add to space out tracker announces.

    Reads `qbit_add_stagger_s` (default 2.0) and
    `qbit_add_stagger_jitter_s` (default 0.5) from settings on every
    call so the operator can disable or tune the stagger without
    restarting. `qbit_add_stagger_s <= 0` disables.

    Returns the actual sleep duration (mainly for logs + tests).
    Holds `_qbit_add_lock` across the sleep AND the timer update so
    concurrent callers serialize through the gap rather than racing
    on a stale `_last_qbit_add_at`.
    """
    from app.config import load_settings
    s = load_settings()
    stagger_s = float(s.get("qbit_add_stagger_s", 2.0) or 0.0)
    jitter_s = float(s.get("qbit_add_stagger_jitter_s", 0.5) or 0.0)
    if stagger_s <= 0:
        return 0.0

    global _last_qbit_add_at
    async with _qbit_add_lock:
        target_gap = max(0.0, stagger_s + random.uniform(-jitter_s, jitter_s))
        elapsed = time.monotonic() - _last_qbit_add_at
        sleep_s = max(0.0, target_gap - elapsed)
        if sleep_s > 0:
            await asyncio.sleep(sleep_s)
        _last_qbit_add_at = time.monotonic()
        return sleep_s


# ─── Dependency container ────────────────────────────────────


# Type aliases for the injectable callables. Production code uses
# `app.mam.grab.fetch_torrent` and a `QbitClient` instance; tests
# pass in fakes that record what they were called with.
GrabFetchFn = Callable[[str, str], Awaitable[GrabResult]]


class _DbProvider(Protocol):
    """Anything that can hand back an aiosqlite.Connection on demand.

    Defined as a Protocol so the test fixture can pass a simple
    `lambda: get_db()` factory and production code can pass the
    same factory bound to the real APP_DB_PATH.
    """

    async def __call__(self) -> aiosqlite.Connection: ...


@dataclass
class DispatcherDeps:
    """Bag of injected dependencies for the dispatcher functions.

    Tests construct one of these with fakes and pass it to
    `handle_announce` / `inject_grab` directly. The dispatcher
    never reaches into module globals — every effect goes through
    one of these fields.
    """

    # Read-only knobs (required — no defaults)
    filter_config: FilterConfig
    mam_token: str
    qbit_category: str
    budget_cap: int
    queue_max: int
    queue_mode_enabled: bool
    seed_seconds_required: int

    # Behavior (required)
    db_factory: _DbProvider
    fetch_torrent: GrabFetchFn
    qbit: TorrentClient

    # ── Fields with defaults below this line ────────────────

    # Dry-run mode: run filter + policy but never fetch or submit.
    dry_run: bool = False

    # Uploaders whose torrents should never be grabbed. Prevents
    # downloading your own uploads (MAM counts that as a re-snatch).
    # Case-insensitive match against the `ownership` field from the
    # search API. Checked after the torrent_info lookup.
    excluded_uploaders: frozenset[str] = field(default_factory=frozenset)

    # Policy engine config. Defaults to permissive (grab everything).
    policy_config: PolicyConfig = field(default_factory=PolicyConfig)

    # Tag list to apply to every torrent Seshat submits to qBit.
    qbit_tags: list[str] = field(default_factory=list)

    # Download folder organization.
    qbit_download_path: str = ""
    download_folder_structure: str = "monthly"  # "monthly" | "yearly" | "author" | "flat" | "template"
    # Format string used when `download_folder_structure == "template"`.
    # Tokens: {author}, {series}, {title}. Empty defaults to "{author}"
    # (matches legacy "author" mode). See app/orchestrator/download_folders.py.
    download_folder_template: str = ""

    # Path translation between qBit and Seshat containers.
    # qBit reports paths like "/data/[mam-complete]" but Seshat
    # mounts that host directory at "/downloads/[mam-complete]".
    qbit_path_prefix: str = "/data"
    local_path_prefix: str = "/downloads"

    # Delayed-torrents folder: when the queue is full and a new
    # grab arrives, the oldest queued grab gets rotated out into
    # this directory as a raw .torrent file. FIFO eviction keeps
    # the queue moving. Empty path disables the feature — new
    # grabs that hit a full queue will drop as before.
    delayed_torrents_path: str = ""

    # Phase 2 pipeline settings.
    staging_path: str = ""
    review_queue_enabled: bool = True
    review_staging_path: str = ""
    metadata_review_timeout_days: int = 14
    # Orphan adoption cutoff — only qBit torrents with
    # `added_on >= qbit_orphan_adoption_since` get adopted. Defaults
    # to 0 for tests / backward compat (no filter); production sets it
    # from settings at dispatcher-build time.
    qbit_orphan_adoption_since: float = 0.0
    # Audiobook format priority — ordered list like ["m4b", "m4a",
    # "mp3"]. Used by file_copier to pick the primary file in
    # mixed-format torrents. None/empty disables the priority sort
    # (largest-file wins, which matches pre-Phase-7 behaviour).
    audiobook_format_priority: list[str] = field(default_factory=list)
    # Ebook format priority — symmetric counterpart to the audiobook
    # field above, sourced from `mam_format_priority` in settings.
    # UAT canary 2026-05-11: a torrent containing both EPUB and PDF
    # picked the PDF (largest-first baseline) despite EPUB being the
    # user's preferred format. Mirrors the audiobook-priority sort
    # for the ebook side.
    ebook_format_priority: list[str] = field(default_factory=list)

    # Tier 4 metadata enrichment. The enricher instance is built
    # at startup from settings and passed through here so the
    # pipeline doesn't need a global. None disables enrichment.
    metadata_enricher: Optional[object] = None
    default_sink: str = "calibre"
    calibre_library_path: str = ""
    folder_sink_path: str = ""
    audiobookshelf_library_path: str = ""
    # Audiobookshelf API hookup — optional, and only consulted by the
    # audiobookshelf sink. All three must be set together for the
    # post-drop library-scan POST to fire; any missing one degrades
    # gracefully to "drop and let ABS's watcher find it".
    abs_base_url: str = ""
    abs_api_key: str = ""
    abs_library_id: str = ""
    cwa_ingest_path: str = ""
    # Minimum gap (seconds) between successive deliveries to the same
    # CWA ingest path — works around a CWA cps wedge when overlapping
    # imports trigger the post-import duplicate scan. See
    # `app/sinks/_cwa_throttle.py`. 0 disables the throttle.
    cwa_min_inter_book_seconds: float = 10.0
    category_routing: dict = field(default_factory=dict)
    ntfy_url: str = ""
    ntfy_topic: str = "seshat"
    per_event_notifications: bool = False
    auto_train_enabled: bool = True

    # v2.9.0 — format-priority dedup. `format_priority` is the dict
    # of per-media-type priority lists from settings; an empty dict
    # disables dedup entirely (every announce that passes the filter
    # gate just grabs). `format_dedup_hold_seconds` is how long to
    # park a disabled-format announce in `pending_holds` waiting for
    # a higher-priority sibling. See app/orchestrator/format_dedup.py.
    format_priority: dict = field(default_factory=dict)
    format_dedup_hold_seconds: int = 600
    # v2.26.0 — numeric quality axes (bitrate/channels for audiobook;
    # empty for ebook by default). Layered on top of `format_priority`
    # by app/quality/scoring.py::resolve_profile_from_settings to form
    # the QualityProfile the dedup gate scores against. Empty dict
    # preserves v2.9.0 format-only behavior verbatim.
    quality_axes: dict = field(default_factory=dict)

    # Optional: an audit hook for tests / future observability.
    on_event: Optional[Callable[[str, dict], None]] = None

    # ── Live credential accessors ───────────────────────────

    def live_mam_token(self) -> str:
        """Resolve the MAM session cookie, preferring the live value.

        NEVER read `self.mam_token` directly at a call site that talks
        to MAM. That field is a snapshot taken when the dispatcher was
        built. Before the 2026-10 audit the long-lived background loops
        (budget watcher, cookie retry, IRC listener) captured a
        DispatcherDeps *once* at startup and held it forever, so before
        v3.10.1 they kept replaying a dead token: grabs and the
        cookie-retry job failed with HTTP 401 until the container was
        restarted, even though the cookie had been updated correctly.
        The loops now resolve `state.dispatcher` per tick, but a rotated
        cookie never rebuilds the dispatcher at all, so the snapshot is
        still stale between saves.

        `app.mam.cookie` holds the live token — it is updated both by
        rotation capture and by the Settings save path — so resolving
        through it makes every consumer, however stale its deps
        snapshot, pick the change up on the next tick. The field stays
        as the fallback for tests and for the window before the first
        seed.
        """
        from app.mam.cookie import get_current_token

        return get_current_token() or self.mam_token


# ─── Result type ─────────────────────────────────────────────


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of a single dispatch call.

    `action` mirrors the rate-limit decision (`submit`/`queue`/`drop`)
    when the filter allowed the announce, or `"skip"` when the filter
    rejected it. `grab_id` is the row id in `grabs` (None for drop
    and most skips; an `already_grabbed` skip carries the EXISTING
    grab's id so the UI can link to it). `error` is set when fetching
    or submitting failed, and on the snatch-safety skips
    (`already_grabbed`, `already_snatched_on_mam`,
    `torrent_removed_from_mam`) as a message the user can act on.
    `"hold"` means an IRC announce is waiting for MAM's search index;
    its grab runs later in the background (`_grab_once_indexed`).
    """

    action: str               # "skip" | "submit" | "queue" | "drop" | "hold"
    reason: str               # human-readable + machine-stable
    announce_id: int          # always set — every dispatch produces an audit row
    grab_id: Optional[int] = None
    qbit_hash: Optional[str] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class TorrentLookup:
    """One grab's search-API lookup of its torrent, made once and passed
    down to the snatch guard, co-author auto-train and the economic
    context. `get_torrent_info` caches only successes, so before this a
    failing lookup (MAM unreachable, not listed yet) ran three times per
    grab (audit L1-14). Exactly one of `info` / `error` is set."""

    info: Optional[TorrentInfo] = None
    error: Optional[TorrentInfoError] = None


async def _look_up_torrent(deps: "DispatcherDeps", torrent_id: str) -> TorrentLookup:
    try:
        return TorrentLookup(
            info=await get_torrent_info(torrent_id, token=deps.live_mam_token()),
        )
    except TorrentInfoError as e:
        return TorrentLookup(error=e)


# ─── Public surface ──────────────────────────────────────────


async def handle_announce(
    deps: DispatcherDeps, announce: Announce, *, raw_line: str = ""
) -> DispatchResult:
    # IRC announces never force a wedge — that decision is scoped to
    # the manual-inject router's "use a wedge for this one" checkbox,
    # and `force_fl_wedge=False` here preserves whatever the policy
    # engine decided.
    """Process one announce end-to-end.

    Called by the IRC listener's `on_announce` callback. Runs the
    full pipeline:

      1. Evaluate the filter
      2. Always write the audit row in `announces`
      3. If filter says skip → return "skip"
      4. v2.9.0: evaluate the format-priority dedup gate
         (skip / hold / allow with optional preempts)
      5. If filter says allow → consult the rate limiter
      6. If decision is drop → return "drop" (no grab row, only audit)
      7. Fetch the .torrent file
      8. If fetch fails → write a failed grab row, return failure
      9. If decision is submit → submit to qBit, record in ledger
      10. If decision is queue → enqueue (file already fetched)

    Returns a `DispatchResult` describing the outcome. Never raises
    on the happy or expected-failure paths — the IRC listener
    iterates over many announces and a single bad one shouldn't
    take down the loop.

    Live-read kill switches (v2.21.2). Reads `dry_run` and
    `mam_irc_enabled` from settings on every call so the operator's
    runtime toggles take effect on the NEXT announce instead of
    waiting for a container restart. Pre-v2.21.2, both were read
    only at dispatcher build time / lifespan startup, so flipping
    them on a running container had no effect until the next reboot
    — a critical gap that meant the UI "kill switches" weren't
    actually kill switches. Cheap on every call: `load_settings()`
    is mtime-cached.
    """
    # Live-read kill-switch evaluation (v2.21.2). Both `dry_run` and
    # `mam_irc_enabled` route through synthetic skip decisions so the
    # audit row still gets written (preserving the `announce_id is
    # always set` contract) but nothing downstream actually fetches
    # or submits the torrent. Live dispatch path is the cheapest
    # place to enforce — pre-fix these toggles only took effect at
    # container restart, which was a critical safety gap.
    live = _live_kill_switch_state()
    if not live["irc_enabled"]:
        _log.info(
            "IRC announce ignored: mam_irc_enabled=False "
            "(torrent_id=%s, name=%r)",
            announce.torrent_id, announce.torrent_name,
        )
        return await _dispatch_with_decision(
            deps,
            announce=announce,
            raw_line=raw_line,
            filter_decision=Decision(
                action="skip", reason="irc_listener_disabled",
            ),
            skip_filter=False,
            force_fl_wedge=False,
            apply_format_dedup=False,
        )
    if live["dry_run"] and not deps.dry_run:
        # Live setting flipped to dry_run while the dispatcher's
        # `deps.dry_run` is still False (dispatcher was built before
        # the toggle flipped on a running container). Honor the
        # live setting — this is the v2.21.2 contract that the
        # toggle takes effect on the next announce. The original
        # `if deps.dry_run` path inside `_dispatch_with_decision`
        # still works for tests / startup-time-configured dry_run.
        _log.info(
            "Live dry_run engaged: skipping IRC announce "
            "(torrent_id=%s, name=%r)",
            announce.torrent_id, announce.torrent_name,
        )
        return await _dispatch_with_decision(
            deps,
            announce=announce,
            raw_line=raw_line,
            filter_decision=Decision(
                action="skip", reason="dry_run_live",
            ),
            skip_filter=False,
            force_fl_wedge=False,
            apply_format_dedup=False,
        )

    decision = evaluate_announce(announce, deps.filter_config)
    if decision.action != "allow":
        return await _dispatch_with_decision(
            deps,
            announce=announce,
            raw_line=raw_line,
            filter_decision=decision,
            skip_filter=False,
            force_fl_wedge=False,
            apply_format_dedup=True,
        )
    # Not listed by MAM's search yet → hold for the index (D37). Any
    # other lookup error (MAM unreachable) means the grab goes ahead now
    # and fails open, as every lookup error does; the lookup is passed
    # down so the grab path doesn't repeat it (L1-14).
    lookup = None
    if deps.live_mam_token() and announce.torrent_id:
        lookup = await _look_up_torrent(deps, announce.torrent_id)
        if isinstance(lookup.error, TorrentNotFoundError):
            return await _hold_for_index(
                deps, announce, raw_line=raw_line, decision=decision,
            )
    async with _announce_grab_lock():
        return await _dispatch_with_decision(
            deps,
            announce=announce,
            raw_line=raw_line,
            filter_decision=decision,
            skip_filter=False,
            force_fl_wedge=False,
            apply_format_dedup=True,
            lookup=lookup,
        )


async def _hold_for_index(
    deps: DispatcherDeps,
    announce: Announce,
    *,
    raw_line: str,
    decision: Decision,
) -> DispatchResult:
    """Write the announce row now; grab in the background once MAM lists it."""
    db = await deps.db_factory()
    try:
        announce_id = await grabs_storage.record_announce(
            db,
            raw=raw_line,
            torrent_id=announce.torrent_id,
            torrent_name=announce.torrent_name,
            category=announce.category,
            author_blob=announce.author_blob,
            decision=decision,
            filetype=(announce.filetype or "").lower().strip(),
        )
    finally:
        await db.close()
    _emit(deps, "announce_recorded", {"announce_id": announce_id})
    _log.info(
        "tid=%s %r isn't in MAM's search yet; grabbing once it is "
        "(waiting up to %ds)",
        announce.torrent_id, announce.torrent_name,
        int(sum(_INDEX_WAIT_DELAYS_S)),
    )
    task = asyncio.create_task(_grab_once_indexed(
        deps, announce,
        raw_line=raw_line, decision=decision, announce_id=announce_id,
    ))
    _held_grabs.add(task)
    task.add_done_callback(_held_grabs.discard)
    return DispatchResult(
        action="hold", reason="waiting_for_mam_index", announce_id=announce_id,
    )


async def _wait_for_index(deps: DispatcherDeps, torrent_id: str) -> bool:
    """Poll MAM's search until it lists `torrent_id`. False if it never did."""
    waited = 0.0
    for delay in _INDEX_WAIT_DELAYS_S:
        await _index_sleep(delay)
        waited += delay
        try:
            await get_torrent_info(torrent_id, token=deps.live_mam_token())
        except TorrentInfoError as e:
            _log.debug("tid=%s not in MAM's search after %ds: %s",
                       torrent_id, int(waited), e)
            continue
        _log.info("tid=%s is in MAM's search after %ds", torrent_id, int(waited))
        return True
    return False


async def _grab_once_indexed(
    deps: DispatcherDeps,
    announce: Announce,
    *,
    raw_line: str,
    decision: Decision,
    announce_id: int,
) -> None:
    """The held grab: wait for MAM's index, then run the grab path."""
    try:
        indexed = await _wait_for_index(deps, announce.torrent_id)
        # The wait can run for minutes: a settings save may have
        # replaced the dispatcher (grab policy, excluded uploaders,
        # budget), and the kill switches may have flipped.
        deps = _current_dispatcher(deps)
        live = _live_kill_switch_state()
        if not live["irc_enabled"] or (live["dry_run"] and not deps.dry_run):
            _log.info(
                "held grab of tid=%s dropped: IRC grabbing was switched "
                "off or dry run on during the wait", announce.torrent_id,
            )
            return
        if not indexed:
            _log.info(
                "tid=%s still not in MAM's search after %ds; grabbing on "
                "the announce's word (%s)", announce.torrent_id,
                int(sum(_INDEX_WAIT_DELAYS_S)),
                "VIP" if announce.vip else "Normal",
            )
        async with _announce_grab_lock():
            await _dispatch_with_decision(
                deps,
                announce=announce,
                raw_line=raw_line,
                filter_decision=decision,
                skip_filter=False,
                force_fl_wedge=False,
                apply_format_dedup=True,
                announce_id=announce_id,
                trust_announce=not indexed,
            )
    except Exception:
        _log.exception("held grab of tid=%s failed", announce.torrent_id)


def _current_dispatcher(held: DispatcherDeps) -> DispatcherDeps:
    """`state.dispatcher` as it is now, for work that outlives its announce.

    `held` is the dispatcher the announce arrived with; it is used only
    when none is published (unit tests that drive `handle_announce`
    directly).
    """
    from app import state
    return state.dispatcher if state.dispatcher is not None else held


def _live_kill_switch_state() -> dict[str, bool]:
    """Read the runtime kill-switch settings on every call.

    Settings are mtime-cached in `app.config.load_settings`, so a
    per-announce call is effectively free. Defaults match the
    "permissive" lifespan defaults so a missing field never
    accidentally disables grabs.
    """
    from app.config import load_settings
    s = load_settings()
    return {
        "irc_enabled": bool(s.get("mam_irc_enabled", True)),
        "dry_run": bool(s.get("dry_run", False)),
    }


async def inject_grab(
    deps: DispatcherDeps,
    *,
    torrent_id: str,
    torrent_name: str = "",
    category: str = "",
    author_blob: str = "",
    series_name: str = "",
    book_title: str = "",
    filetype: str = "",
    raw_line: str = "manual_inject",
    force_fl_wedge: bool = False,
    apply_format_dedup: bool = True,
    override_mam_snatched: bool = False,
    apply_claim_for_owned: bool = True,
) -> DispatchResult:
    """Manually queue a grab by torrent ID.

    Skips the filter (the user already decided they want this) but
    DOES go through the rate limiter — a manually-injected grab
    still counts against the snatch budget like any other.

    Used by:
      - the manual-inject HTTP endpoint
      - the cookie-rotation manual test recipe
      - the external grabs endpoint (`/api/v1/grabs/inject-batch`)
      - the discovery domain's send-to-pipeline flow

    The metadata fields (`torrent_name`, `category`, `author_blob`)
    are only used for audit-log readability — the dispatcher doesn't
    need them to operate. Callers that have the data should pass it;
    the inject endpoint passes them as empty strings when called
    with just a torrent ID.

    `force_fl_wedge=True` forces `&fl=1` on the download URL
    regardless of what the policy engine decided. Used by the
    manual-inject router when the user checks "use a wedge for this
    one" — drains one wedge from the pool for this single grab
    without needing to flip the global `policy_use_wedge` setting.

    `override_mam_snatched=True` lets the grab through when MAM says
    this account already snatched the torrent (`my_snatched`) — the
    user confirmed they want a second download. It never overrides
    `already_grabbed`: a torrent Seshat itself fetched is never
    fetched again.

    `apply_claim_for_owned=False` skips the claim-for-owned gate. Manual
    Grab passes it (with `apply_format_dedup=False`): its preview already
    showed the user what they own, so ticking the row is the decision to
    grab anyway (ADR-0023).

    Live `dry_run` kill-switch (v2.21.2). Unlike the IRC-enabled
    toggle (which doesn't apply here — manual injects are user-
    initiated regardless of the IRC listener state), the live
    `dry_run` setting IS honored on every manual inject. Mirrors
    the same contract `handle_announce` uses.

    `torrent_id` is normalised first (`extract_torrent_id`: whitespace,
    leading zeros, a pasted link), so ADR-0022's `already_grabbed`
    check compares one form. Anything that isn't a torrent ID is
    refused before an audit row or a MAM call (`invalid_torrent_id`).
    """
    tid = extract_torrent_id(torrent_id)
    if tid is None:
        return DispatchResult(
            action="skip", reason="invalid_torrent_id", announce_id=0,
            error=f"could not parse torrent ID from: {torrent_id}",
        )
    torrent_id = tid
    live = _live_kill_switch_state()
    fake_announce = Announce(
        torrent_id=torrent_id,
        torrent_name=torrent_name or f"manual_inject_{torrent_id}",
        category=category,
        author_blob=author_blob,
        series_name=series_name,
        book_title=book_title,
        filetype=(filetype or "").lower(),
    )
    if live["dry_run"] and not deps.dry_run:
        _log.info(
            "Live dry_run engaged: skipping manual inject "
            "(torrent_id=%s, name=%r)",
            torrent_id, torrent_name,
        )
        return await _dispatch_with_decision(
            deps,
            announce=fake_announce,
            raw_line=raw_line,
            filter_decision=Decision(
                action="skip", reason="dry_run_live",
            ),
            skip_filter=False,
            force_fl_wedge=False,
            apply_format_dedup=False,
        )

    # Synthetic "allow" decision so the audit row reflects that this
    # was a manual override (reason `manual_inject` rather than the
    # filter's allowed_author / category_not_allowed / etc.).
    fake_decision = Decision(
        action="allow",
        reason="manual_inject",
        matched_author=author_blob,
    )
    return await _dispatch_with_decision(
        deps,
        announce=fake_announce,
        raw_line=raw_line,
        filter_decision=fake_decision,
        skip_filter=True,
        force_fl_wedge=force_fl_wedge,
        apply_format_dedup=apply_format_dedup,
        override_mam_snatched=override_mam_snatched,
        apply_claim_for_owned=apply_claim_for_owned,
    )


# ─── Internals ───────────────────────────────────────────────


async def _dispatch_with_decision(
    deps: DispatcherDeps,
    *,
    announce: Announce,
    raw_line: str,
    filter_decision: Decision,
    skip_filter: bool,
    force_fl_wedge: bool = False,
    apply_format_dedup: bool = True,
    override_mam_snatched: bool = False,
    apply_claim_for_owned: bool = True,
    announce_id: Optional[int] = None,
    trust_announce: bool = False,
    lookup: Optional[TorrentLookup] = None,
) -> DispatchResult:
    """The shared pipeline body used by both handle_announce and
    inject_grab. The only thing they differ on is whether the filter
    decision came from `evaluate_announce` or was synthesized.

    `apply_format_dedup` (v2.9.0) gates whether the format-priority
    dedup runs after the filter says allow. Default True; manual-
    inject callers can pass False from a UI override checkbox to
    force a grab regardless of in-flight/owned siblings.

    `override_mam_snatched` and `apply_claim_for_owned` — see
    `inject_grab`.

    `announce_id` and `trust_announce` come from a held grab
    (`_grab_once_indexed`): its announce row already exists, and
    `trust_announce` says MAM's search never listed the torrent, so
    the announce's VIP|Normal is its free status.

    `lookup` is a torrent-info lookup the caller already made
    (`handle_announce`'s index check); otherwise the snatch guard makes
    one. Either way it is the grab's only lookup.
    """
    # Computed once at the top so the dedup gate, the announce row,
    # and (if we reach the grab branch) the grab row all carry the
    # same values. Empty strings collapse to no-ops downstream.
    book_format = (announce.filetype or "").lower().strip()
    dedup_key = normalize_dedup_key(
        announce.torrent_name or announce.title or "",
        announce.author_blob or "",
    )

    db = await deps.db_factory()
    try:
        if announce_id is None:
            announce_id = await grabs_storage.record_announce(
                db,
                raw=raw_line,
                torrent_id=announce.torrent_id,
                torrent_name=announce.torrent_name,
                category=announce.category,
                author_blob=announce.author_blob,
                decision=filter_decision,
                filetype=book_format,
            )
            _emit(deps, "announce_recorded", {"announce_id": announce_id})

        if filter_decision.action == "skip":
            _emit(
                deps,
                "filter_skip",
                {
                    "torrent_id": announce.torrent_id,
                    "reason": filter_decision.reason,
                },
            )

            # Tier 2 routing: if the ONLY reason the filter said skip
            # was the author allow list, capture the torrent as a
            # "tentative" — the user may want it even though nobody
            # on the allow list wrote it. No .torrent is fetched until
            # the user approves via /api/v1/tentative/{id}/approve.
            if filter_decision.reason == "author_not_allowlisted":
                try:
                    # Fetch MAM cover for the tentative review UI.
                    cover_path = await _fetch_mam_cover_for_skip(
                        deps, announce.torrent_id
                    )
                    await tentative_storage.upsert_tentative(
                        db,
                        mam_torrent_id=announce.torrent_id,
                        torrent_name=announce.torrent_name,
                        author_blob=announce.author_blob
                            or filter_decision.primary_log_author
                            or "",
                        category=announce.category,
                        language=announce.language,
                        format=announce.filetype,
                        vip=announce.vip,
                        scraped_metadata=None,
                        cover_path=cover_path,
                    )
                    _emit(deps, "tentative_captured",
                          {"torrent_id": announce.torrent_id})
                except Exception:
                    _log.exception(
                        "failed to capture tentative torrent tid=%s",
                        announce.torrent_id,
                    )

            # Tier 2 routing: if the author was on the ignored list,
            # stash a seen-row so the weekly review can show the user
            # what they're turning down. No cover: nothing displays an
            # ignored-seen cover (the digest lists authors), and fetching
            # one cost a MAM CDN request per ignored announce (L1-15).
            elif filter_decision.reason == "ignored_author":
                try:
                    await tentative_storage.record_ignored_seen(
                        db,
                        mam_torrent_id=announce.torrent_id,
                        torrent_name=announce.torrent_name,
                        author_blob=announce.author_blob
                            or filter_decision.primary_log_author
                            or "",
                        category=announce.category,
                        info_url=announce.info_url or None,
                        cover_path=None,
                    )
                except Exception:
                    _log.exception(
                        "failed to record ignored-seen tid=%s",
                        announce.torrent_id,
                    )

            return DispatchResult(
                action="skip",
                reason=filter_decision.reason,
                announce_id=announce_id,
            )

        # Snatch safety (Phase 0 S1): never fetch a torrent Seshat has
        # already fetched or is fetching. No override — see
        # `find_blocking_grab` for which rows block. Runs first because
        # it's one indexed query and the most specific answer; re-run
        # under `grab_claim_lock` just before the grab row is created.
        prior = await grabs_storage.find_blocking_grab(db, announce.torrent_id)
        if prior is not None:
            return await _skip_already_grabbed(
                db, deps, announce=announce, announce_id=announce_id,
                prior=prior,
            )

        # v2.17.7 — claim-for-owned gate. If the announce matches a
        # book already in a library with no confirmed MAM URL, claim
        # the torrent_id for that owned row and skip the grab. Closes
        # the duplicate-download path for books the user owned before
        # the upload existed on MAM. Runs BEFORE format-dedup so we
        # don't even consider holding/queuing a torrent we don't want
        # the file for. Failure to claim falls through silently.
        # Manual Grab turns it off: the user saw the owned copy in the
        # preview and chose to grab anyway (ADR-0023).
        claim_result = None
        if apply_claim_for_owned:
            try:
                from app.orchestrator.owned_announce_claim import (
                    try_claim_announce_for_owned,
                )
                claim_result = await try_claim_announce_for_owned(
                    announce=announce,
                )
            except Exception:
                _log.exception(
                    "claim-for-owned: crashed during lookup for tid=%s "
                    "(falling through to normal grab)",
                    announce.torrent_id,
                )
                claim_result = None
        if claim_result is not None and claim_result.claimed:
            await grabs_storage.update_announce_decision(
                db, announce_id=announce_id,
                action="skip", reason="claimed_for_owned",
            )
            _emit(deps, "claimed_for_owned", {
                "torrent_id": announce.torrent_id,
                "library_slug": claim_result.library_slug,
                "book_id": claim_result.book_id,
                "book_title": claim_result.book_title,
            })
            return DispatchResult(
                action="skip",
                reason="claimed_for_owned",
                announce_id=announce_id,
            )

        # v2.9.0 — format-priority dedup gate. Runs only when the
        # caller asked us to AND the user has any priority list
        # configured. Three possible outcomes:
        #   skip → another format of this book is owned / racing at
        #          higher priority; update the audit row and return.
        #   hold → no immediate blocker but this format is disabled;
        #          park in pending_holds for the configured window.
        #   allow → grab normally; preempt any held lower-priority
        #          siblings as a side-effect.
        # `apply_format_dedup=False` is the manual-inject override
        # checkbox path — user explicitly bypasses dedup.
        if apply_format_dedup and deps.format_priority:
            try:
                siblings = await lookup_dedup_siblings(
                    dedup_key=dedup_key,
                    media_type=media_type_from_category(announce.category) or "",
                )
            except Exception:
                _log.exception(
                    "format_dedup: sibling lookup failed; failing open "
                    "(grab proceeds) for announce_id=%s", announce_id,
                )
                siblings = []

            dedup_decision = evaluate_format_dedup(
                announce=announce,
                format_priority=deps.format_priority,
                hold_seconds=deps.format_dedup_hold_seconds,
                siblings=siblings,
                quality_axes=deps.quality_axes,
            )

            # Preempt held lower-priority siblings regardless of outcome
            # (the allow + hold branches may have populated this; skip
            # never does but the call is harmless on empty input).
            if dedup_decision.preempt_hold_ids:
                try:
                    await holds_storage.drop_holds(
                        db, dedup_decision.preempt_hold_ids,
                        reason=f"preempted_by_{dedup_decision.reason}",
                    )
                except Exception:
                    _log.exception(
                        "format_dedup: preempt failed for hold_ids=%s",
                        dedup_decision.preempt_hold_ids,
                    )

            if dedup_decision.action == "skip":
                await grabs_storage.update_announce_decision(
                    db, announce_id=announce_id,
                    action="skip", reason=dedup_decision.reason,
                )
                _emit(deps, "format_dedup_skip", {
                    "torrent_id": announce.torrent_id,
                    "reason": dedup_decision.reason,
                    "dedup_key": dedup_decision.dedup_key,
                    "book_format": dedup_decision.book_format,
                })
                return DispatchResult(
                    action="skip",
                    reason=dedup_decision.reason,
                    announce_id=announce_id,
                )

            if dedup_decision.action == "hold":
                await grabs_storage.update_announce_decision(
                    db, announce_id=announce_id,
                    action="hold", reason=dedup_decision.reason,
                )
                try:
                    hold_id = await holds_storage.create_hold(
                        db,
                        announce_id=announce_id,
                        dedup_key=dedup_decision.dedup_key,
                        media_type=dedup_decision.media_type or "",
                        book_format=dedup_decision.book_format,
                        torrent_id=announce.torrent_id,
                        torrent_name=announce.torrent_name,
                        category=announce.category,
                        author_blob=announce.author_blob,
                        hold_seconds=dedup_decision.hold_seconds
                            or deps.format_dedup_hold_seconds,
                    )
                except Exception:
                    _log.exception(
                        "format_dedup: hold insert failed; failing open "
                        "(grab proceeds) for announce_id=%s", announce_id,
                    )
                else:
                    _emit(deps, "format_dedup_hold", {
                        "torrent_id": announce.torrent_id,
                        "hold_id": hold_id,
                        "release_seconds": dedup_decision.hold_seconds,
                        "dedup_key": dedup_decision.dedup_key,
                        "book_format": dedup_decision.book_format,
                    })
                    return DispatchResult(
                        action="skip",
                        reason=dedup_decision.reason,
                        announce_id=announce_id,
                    )

            # dedup_decision.action == "allow" — fall through to the
            # normal grab path. The grab-create call below stamps
            # book_format + dedup_key on the new row.

        # Snatch safety (Phase 0 S1): MAM's own view of the torrent, from
        # the same cached search-API lookup the auto-train and economic
        # context below reuse (one call per grab, usually a cache hit).
        # Runs before auto-train so a refused grab trains nothing.
        #   - removed from MAM → skip without fetching. User/programmatic
        #     grabs only: they act on an ID that may have aged (a
        #     tentative approved days later, a hold released). An IRC
        #     announce only gets here once MAM lists it, or after the
        #     index wait ran out (D37), so for IRC a not-found stays
        #     fail-open like every other lookup error.
        #   - `my_snatched` → skip unless the user confirmed the override.
        #   - uploader on the excluded list → skip (your own uploads).
        # Any other lookup failure fails open: the DB guard above still
        # covers everything Seshat itself fetched.
        if deps.live_mam_token() and announce.torrent_id:
            if lookup is None:
                lookup = await _look_up_torrent(deps, announce.torrent_id)
            info = lookup.info
            if isinstance(lookup.error, TorrentNotFoundError):
                if skip_filter:
                    return await _refuse_grab(
                        db, deps,
                        announce=announce, announce_id=announce_id,
                        reason="torrent_removed_from_mam",
                        message=(
                            f"MAM torrent {announce.torrent_id} is no "
                            "longer on MAM (removed, deleted or trumped); "
                            "nothing was downloaded."
                        ),
                    )
            elif lookup.error is not None:
                _log.debug(
                    "snatch safety: torrent_info lookup failed for tid=%s "
                    "(failing open): %s", announce.torrent_id, lookup.error,
                )

            if info is not None and info.my_snatched and not override_mam_snatched:
                return await _refuse_grab(
                    db, deps,
                    announce=announce, announce_id=announce_id,
                    reason="already_snatched_on_mam",
                    message=(
                        f"MAM says this account already snatched torrent "
                        f"{announce.torrent_id}. Use Reingest from disk to "
                        "bring the existing files into Seshat; downloading "
                        "it again needs an explicit override."
                    ),
                )

            if (
                info is not None
                and deps.excluded_uploaders
                and info.uploader_name
                and info.uploader_name.lower() in deps.excluded_uploaders
            ):
                _emit(deps, "excluded_uploader", {
                    "torrent_id": announce.torrent_id,
                    "uploader": info.uploader_name,
                })
                # Through `_refuse_grab` like the other snatch-safety
                # skips, so the announce row says skip, not allow (L2-07).
                return await _refuse_grab(
                    db, deps,
                    announce=announce, announce_id=announce_id,
                    reason=f"excluded_uploader:{info.uploader_name}",
                    message="The uploader is on your excluded-uploaders list.",
                )

        # Co-author auto-train (v3.0.0 Phase 10, ITEM 1): train the
        # AUTHORITATIVE MAM authorlist for this grab into the allow list, so
        # future announces by any co-author of a book we acquired pass the
        # filter. Fires when the filter allowed because a known author
        # matched, OR for any programmatic/manual grab (skip_filter — covers
        # inject / tentative-approve / hold-release / delayed / discovery
        # send-to-pipeline). One cached-or-fetched torrent_info call per grab
        # (grabs are rare + deliberate); the announce blob is the fail-safe
        # fallback. Trains `authors_allowed` only — the owned book's
        # book_authors come from Calibre/ABS sync (Phase 2). Best-effort:
        # never block the grab.
        if (
            filter_decision.reason == "allowed_author" or skip_filter
        ) and announce.torrent_id:
            try:
                await train_authors_from_torrent_info(
                    db,
                    announce.torrent_id,
                    token=deps.live_mam_token(),
                    fallback_blob=announce.author_blob or "",
                    source="coauthor_train",
                    looked_up=lookup is not None,
                    info=lookup.info if lookup is not None else None,
                )
            except Exception:
                pass  # best-effort, don't block the grab

        # Filter said allow (or we're injecting). Build the economic
        # context for the policy engine. Start with what we already
        # know from the announce, then enrich with the MAM APIs (both
        # cached, both fail-safe).
        eco_ctx = await _build_economic_context(
            deps, announce, wedge_requested=force_fl_wedge,
            trust_announce=trust_announce, lookup=lookup,
        )

        policy_decision = evaluate_policy(eco_ctx, deps.policy_config)
        if (
            deps.policy_config.use_wedge
            and policy_decision.tier == "normal"
            and not eco_ctx.free_status_known
        ):
            _log.info(
                "no wedge on tid=%s: MAM's search didn't say whether it's "
                "already free, so it's grabbed paid", announce.torrent_id,
            )

        if policy_decision.action == "skip":
            _emit(
                deps,
                "policy_skip",
                {
                    "torrent_id": announce.torrent_id,
                    "tier": policy_decision.tier,
                },
            )
            # Buffer-gate blocks are the one policy-skip outcome that
            # users have asked to be visible — they represent "I
            # would have grabbed this but can't afford it right now",
            # which is an actionable signal. Write an audit row and
            # (throttled) fire a ntfy so the user knows the feed went
            # quiet on purpose, not because Seshat crashed.
            if policy_decision.tier == "buffer_insufficient":
                await _record_buffer_gate_block(
                    db, deps,
                    announce=announce,
                    eco_ctx=eco_ctx,
                    from_user_grab=skip_filter,
                )
            return DispatchResult(
                action="skip",
                reason=f"policy:{policy_decision.tier}",
                announce_id=announce_id,
            )

        # Policy said grab. Consult the rate limiter — read current
        # budget + queue counters from the DB.
        budget_used = await ledger_mod.count_effective(db)
        queue_size = await queue_mod.size(db)

        rate_decision = decide_grab_action(
            budget_used=budget_used,
            budget_cap=deps.budget_cap,
            queue_size=queue_size,
            queue_max=deps.queue_max,
            queue_mode_enabled=deps.queue_mode_enabled,
        )

        # Delayed-torrents rotation: if the queue is full and we
        # would otherwise drop, try to evict the oldest queued grab
        # into the delayed folder so this new grab can take its slot.
        # Only attempts when delayed_torrents_path is configured.
        if (
            rate_decision.action == "drop"
            and rate_decision.reason == "budget_full_queue_full"
            and deps.delayed_torrents_path
            and deps.queue_mode_enabled
        ):
            try:
                evicted = await rotate_oldest_to_delayed(
                    db,
                    delayed_path=deps.delayed_torrents_path,
                )
            except Exception:
                _log.exception("delayed rotation raised (non-fatal)")
                evicted = None
            if evicted is not None:
                _emit(deps, "delayed_rotated", {"evicted_grab_id": evicted})
                queue_size = await queue_mod.size(db)
                rate_decision = decide_grab_action(
                    budget_used=budget_used,
                    budget_cap=deps.budget_cap,
                    queue_size=queue_size,
                    queue_max=deps.queue_max,
                    queue_mode_enabled=deps.queue_mode_enabled,
                )

        _emit(deps, "rate_decision", {"action": rate_decision.action})

        if rate_decision.action == "drop":
            return DispatchResult(
                action="drop",
                reason=rate_decision.reason,
                announce_id=announce_id,
            )

        # Dry-run gate: filter + policy + rate-limit all ran normally,
        # but we stop here without fetching or submitting anything.
        # The audit row is already written, so dry-run logs show
        # exactly what WOULD have happened.
        if deps.dry_run:
            _log.debug(
                "DRY RUN: would %s tid=%s %s (policy=%s)",
                rate_decision.action,
                announce.torrent_id,
                announce.torrent_name,
                policy_decision.tier,
            )
            _emit(deps, "dry_run_skip", {
                "torrent_id": announce.torrent_id,
                "would_action": rate_decision.action,
                "policy_tier": policy_decision.tier,
            })
            return DispatchResult(
                action="skip",
                reason=f"dry_run:would_{rate_decision.action}",
                announce_id=announce_id,
            )

        # Submit or queue path: create the grab row, fetch the torrent.
        initial_state = (
            grabs_storage.STATE_FETCHED
            if rate_decision.action == "submit"
            else grabs_storage.STATE_PENDING_QUEUE
        )
        # Re-check + insert atomically: the early guard ran before a
        # string of awaits (MAM lookups, policy, rate limiter), any of
        # which let a concurrent grab of the same ID get this far too.
        # Once the row exists its state blocks every later check.
        # Never wait on the claim lock holding SQLite's write lock: the
        # holder of the claim lock needs it for create_grab (a 30s
        # deadlock in CI, via auto-train's un-rolled-back insert race).
        await release_write_lock(db)
        async with grab_claim_lock():
            prior = await grabs_storage.find_blocking_grab(
                db, announce.torrent_id,
            )
            if prior is not None:
                return await _skip_already_grabbed(
                    db, deps, announce=announce, announce_id=announce_id,
                    prior=prior,
                )
            grab_id = await grabs_storage.create_grab(
                db,
                announce_id=announce_id,
                mam_torrent_id=announce.torrent_id,
                torrent_name=announce.torrent_name,
                category=announce.category,
                author_blob=announce.author_blob,
                state=initial_state,
                book_format=book_format,
                dedup_key=dedup_key,
            )

        # A wedge comes from the policy or from `force_fl_wedge` (a user
        # tick: inject, send-to-pipeline, Manual Grab). Either way it's
        # only sent on a torrent MAM confirmed isn't free (`_wedge_for`).
        use_wedge, wedge_why = _wedge_for(
            announce, eco_ctx,
            policy_wedge=policy_decision.use_wedge,
            forced=force_fl_wedge,
            raw_line=raw_line,
        )
        token = deps.live_mam_token()
        fetch_result = await deps.fetch_torrent(
            announce.torrent_id, token,
            use_fl_wedge=use_wedge,
        )
        if use_wedge and fetch_result.success:
            await _audit_wedge(
                db, announce=announce, why=wedge_why,
                tier=policy_decision.tier, user_grab=skip_filter,
            )

        if not fetch_result.success:
            failed_state = _grab_failure_state(fetch_result)
            await grabs_storage.set_state(
                db,
                grab_id,
                failed_state,
                failed_reason=fetch_result.failure_detail,
                failed_with_cookie_id=failed_cookie_id(failed_state, token),
            )
            _emit(
                deps,
                "fetch_failed",
                {
                    "grab_id": grab_id,
                    "kind": fetch_result.failure_kind,
                    "detail": fetch_result.failure_detail,
                },
            )
            return DispatchResult(
                action=rate_decision.action,
                reason=f"fetch_failed:{fetch_result.failure_kind}",
                announce_id=announce_id,
                grab_id=grab_id,
                error=fetch_result.failure_detail,
            )

        # Fetch succeeded. Compute the info hash from the bytes so
        # we can record the ledger entry deterministically without
        # round-tripping qBit.
        torrent_bytes = fetch_result.torrent_bytes or b""
        try:
            qbit_hash = info_hash(torrent_bytes)
        except BencodeError as e:
            _log.warning(
                f"grab {grab_id}: torrent bytes did not parse as bencode: {e}"
            )
            await grabs_storage.set_state(
                db,
                grab_id,
                grabs_storage.STATE_FAILED_QBIT_REJECTED,
                failed_reason=f"unparseable torrent file: {e}",
            )
            return DispatchResult(
                action=rate_decision.action,
                reason="bad_torrent_file",
                announce_id=announce_id,
                grab_id=grab_id,
                error=str(e),
            )

        return await _place_torrent(
            deps, db,
            grab_id=grab_id,
            announce_id=announce_id,
            action=rate_decision.action,
            rate_reason=rate_decision.reason,
            torrent_bytes=torrent_bytes,
            qbit_hash=qbit_hash,
            torrent_name=announce.torrent_name,
            author_blob=announce.author_blob,
            category=announce.category,
            series_name=announce.series_name,
            book_title=announce.book_title,
        )
    finally:
        await db.close()


async def _place_torrent(
    deps: DispatcherDeps,
    db: aiosqlite.Connection,
    *,
    grab_id: int,
    announce_id: int,
    action: str,
    rate_reason: str,
    torrent_bytes: bytes,
    qbit_hash: str,
    torrent_name: str,
    author_blob: str,
    category: str,
    series_name: str = "",
    book_title: str = "",
) -> DispatchResult:
    """Send .torrent bytes Seshat already holds to qBit, or park them
    in the queue — the shared tail of every grab once the bytes exist.

    Used by `_dispatch_with_decision` right after the MAM fetch and by
    `submit_torrent_bytes` (delayed reinject; Manual Grab's upload path
    next). Nothing here talks to MAM. `action` is the rate limiter's
    "submit" or "queue".

    Whenever the grab can't reach qBit now, the bytes are saved to
    `torrent_store` first (snatch safety, ADR-0022) — the budget
    watcher submits them later without re-fetching.
    """
    if action == "queue":
        return await _queue_with_bytes(
            deps, db,
            grab_id=grab_id, announce_id=announce_id,
            torrent_bytes=torrent_bytes, qbit_hash=qbit_hash,
            reason=rate_reason,
        )

    add_result = await add_to_client(
        deps,
        grab_id=grab_id,
        torrent_bytes=torrent_bytes,
        author_blob=author_blob,
        series_name=series_name,
        book_title=book_title,
    )

    if not add_result.success:
        # If the client is unreachable or auth failed, queue the
        # grab so it can be retried when the client comes back.
        # We already hold the .torrent — losing it would waste a
        # snatch, and MAM must never serve it twice. Only permanent
        # failures (rejected, duplicate) stay as failed.
        retriable = add_result.failure_kind in ("auth_failed", "network_error")
        if retriable and deps.queue_mode_enabled:
            _log.info(
                "download client unreachable for grab_id=%d — queued for retry (%s)",
                grab_id, add_result.failure_kind,
            )
            _emit(deps, "queued_on_client_failure", {
                "grab_id": grab_id, "kind": add_result.failure_kind,
            })
            return await _queue_with_bytes(
                deps, db,
                grab_id=grab_id, announce_id=announce_id,
                torrent_bytes=torrent_bytes, qbit_hash=qbit_hash,
                reason=f"client_unreachable:{add_result.failure_kind}",
                failed_reason=(
                    "client unreachable, queued for retry: "
                    f"{add_result.failure_detail}"
                ),
                error=add_result.failure_detail,
            )

        failed_state = _add_failure_state(add_result)
        await grabs_storage.set_state(
            db,
            grab_id,
            failed_state,
            failed_reason=add_result.failure_detail,
            qbit_hash=qbit_hash,
        )
        _emit(
            deps,
            "client_failed",
            {
                "grab_id": grab_id,
                "kind": add_result.failure_kind,
                "detail": add_result.failure_detail,
            },
        )
        return DispatchResult(
            action="submit",
            reason=f"client_failed:{add_result.failure_kind}",
            announce_id=announce_id,
            grab_id=grab_id,
            qbit_hash=qbit_hash,
            error=add_result.failure_detail,
        )

    # qBit accepted it. Record the ledger entry against our
    # computed hash. The grab is now in the active budget.
    await grabs_storage.set_state(
        db,
        grab_id,
        grabs_storage.STATE_SUBMITTED,
        qbit_hash=qbit_hash,
    )
    await ledger_mod.record_grab(db, grab_id, qbit_hash)
    _emit(
        deps,
        "submitted",
        {"grab_id": grab_id, "qbit_hash": qbit_hash},
    )

    try:
        from app.notifications import bus, events
        await bus.emit(
            events.GRAB_SUCCESS,
            title="New book grabbed",
            message=(
                f"{torrent_name}\n"
                f"by {author_blob}\n"
                f"{category}"
            ),
        )
    except Exception:
        _log.exception("grab.success bus emit failed (non-fatal)")
    return DispatchResult(
        action="submit",
        reason="ok",
        announce_id=announce_id,
        grab_id=grab_id,
        qbit_hash=qbit_hash,
    )


async def add_to_client(
    deps: DispatcherDeps,
    *,
    grab_id: int,
    torrent_bytes: bytes,
    author_blob: str,
    series_name: str = "",
    book_title: str = "",
) -> AddResult:
    """Compute + pre-create the save folder, stagger, then add to qBit.

    No state writes: callers own the grab's state transitions. Shared by
    the dispatcher and the budget watcher's queue drain.

    The save_path we send to qBit uses qBit's mount namespace
    (e.g. /data/[mam-complete]/[2026-04]). qBit can't auto-create
    folders with bracket characters, so we pre-create the folder
    using OUR mount namespace (e.g. /downloads/[mam-complete]/...)
    before passing the path to qBit.
    """
    save_path = None
    if deps.qbit_download_path:
        save_path = compute_download_folder(
            deps.qbit_download_path,
            deps.download_folder_structure,
            author_name=author_blob,
            series_name=series_name,
            book_title=book_title,
            template=deps.download_folder_template,
        )
        if save_path:
            # Translate qBit-namespace path → local-namespace path,
            # then create the folder so it exists when qBit tries to use it.
            local_save_path = translate_path(
                save_path, deps.qbit_path_prefix, deps.local_path_prefix
            )
            if not ensure_folder_exists(local_save_path):
                _log.error(
                    "failed to pre-create download folder: %s "
                    "(qBit path: %s) — submission will likely fail",
                    local_save_path, save_path,
                )

    # Space out consecutive qBit adds so MAM's per-IP tracker
    # throttle doesn't trip on bursts. See `_stagger_qbit_add()`
    # for the rationale + the 2026-05-22 incident. Disabled by
    # `qbit_add_stagger_s=0`; read live from settings.
    stagger_slept = await _stagger_qbit_add()
    if stagger_slept > 0:
        _log.info(
            "staggered qBit add by %.2fs (grab_id=%d)",
            stagger_slept, grab_id,
        )

    return await deps.qbit.add_torrent(
        torrent_bytes,
        category=deps.qbit_category,
        save_path=save_path,
        tags=deps.qbit_tags or None,
    )


async def _queue_with_bytes(
    deps: DispatcherDeps,
    db: aiosqlite.Connection,
    *,
    grab_id: int,
    announce_id: int,
    torrent_bytes: bytes,
    qbit_hash: str,
    reason: str,
    failed_reason: Optional[str] = None,
    error: Optional[str] = None,
) -> DispatchResult:
    """Save the bytes, then park the grab in the pending queue.

    Save first: the queue must never hold a grab whose bytes aren't on
    disk, because the only other source is a second MAM download. If
    the save fails the grab fails loudly instead of queueing.
    """
    try:
        path = torrent_store.save(grab_id, torrent_bytes)
    except OSError as e:
        detail = f"could not save the .torrent for the queue: {e}"
        _log.error("grab %d: %s", grab_id, detail)
        await grabs_storage.set_state(
            db, grab_id, grabs_storage.STATE_FAILED_UNKNOWN,
            failed_reason=detail, qbit_hash=qbit_hash,
        )
        await torrent_store.notify_grab_failed(grab_id, detail)
        return DispatchResult(
            action="queue",
            reason="queue_save_failed",
            announce_id=announce_id,
            grab_id=grab_id,
            qbit_hash=qbit_hash,
            error=detail,
        )
    await grabs_storage.set_state(
        db,
        grab_id,
        grabs_storage.STATE_PENDING_QUEUE,
        qbit_hash=qbit_hash,
        torrent_file_path=str(path),
        failed_reason=failed_reason,
    )
    await queue_mod.enqueue(db, grab_id)
    _emit(deps, "queued", {"grab_id": grab_id})
    return DispatchResult(
        action="queue",
        reason=reason,
        announce_id=announce_id,
        grab_id=grab_id,
        qbit_hash=qbit_hash,
        error=error,
    )


async def submit_torrent_bytes(
    deps: DispatcherDeps,
    *,
    grab_id: int,
    torrent_bytes: bytes,
) -> DispatchResult:
    """Bytes-in grab: place a .torrent Seshat already holds — never
    touches MAM's download endpoint.

    For an existing grab row whose bytes came from somewhere other than
    a fresh fetch: the delayed-torrents folder today, Manual Grab's
    upload next. Runs the same rate limiter as a fetched grab (submit
    now, or queue with the bytes saved) and honours the live dry_run
    kill-switch. The caller owns the snatch-safety checks for its source
    (liveness, `find_blocking_grab`), and should claim the grab row
    under `grab_claim_lock()` first.
    """
    live = _live_kill_switch_state()
    if live["dry_run"] or deps.dry_run:
        return DispatchResult(
            action="skip", reason="dry_run_live", announce_id=0,
            grab_id=grab_id, error="dry run is on; nothing was submitted",
        )
    try:
        qbit_hash = info_hash(torrent_bytes)
    except BencodeError as e:
        return DispatchResult(
            action="skip", reason="bad_torrent_file", announce_id=0,
            grab_id=grab_id, error=f"unparseable torrent file: {e}",
        )

    db = await deps.db_factory()
    try:
        grab = await grabs_storage.get_grab(db, grab_id)
        if grab is None:
            return DispatchResult(
                action="skip", reason="grab_not_found", announce_id=0,
                grab_id=grab_id, error=f"grab #{grab_id} not found",
            )
        announce_id = grab.announce_id or 0
        rate_decision = decide_grab_action(
            budget_used=await ledger_mod.count_effective(db),
            budget_cap=deps.budget_cap,
            queue_size=await queue_mod.size(db),
            queue_max=deps.queue_max,
            queue_mode_enabled=deps.queue_mode_enabled,
        )
        if rate_decision.action == "drop":
            return DispatchResult(
                action="drop", reason=rate_decision.reason,
                announce_id=announce_id, grab_id=grab_id,
                error="snatch budget and queue are full; try again later",
            )
        return await _place_torrent(
            deps, db,
            grab_id=grab_id,
            announce_id=announce_id,
            action=rate_decision.action,
            rate_reason=rate_decision.reason,
            torrent_bytes=torrent_bytes,
            qbit_hash=qbit_hash,
            torrent_name=grab.torrent_name,
            author_blob=grab.author_blob,
            category=grab.category,
        )
    finally:
        await db.close()


async def account_uid(deps: DispatcherDeps) -> Optional[int]:
    """This account's MAM uid (cached user status), or None if unknown."""
    token = deps.live_mam_token()
    if not token:
        return None
    try:
        status = await get_user_status(token=token)
    except UserStatusError:
        return None
    return status.uid or None


async def grab_uploaded_torrent(
    deps: DispatcherDeps,
    *,
    torrent_bytes: bytes,
) -> DispatchResult:
    """Manual Grab's upload path: a .torrent the user downloaded from MAM
    themselves. Never calls MAM's download endpoint (ADR-0023).

    The file must prove it is this account's own MAM download: a
    `MID=` in its comment (else `not_mam_file`) and a `UID=` matching
    the account (else `foreign_file`; `uid_unknown` when the account
    can't be read, never fail open). Then the usual refusals, minus
    `my_snatched` (the upload IS the snatch): `already_grabbed` by
    torrent ID or by info hash, `torrent_removed_from_mam`, an excluded
    uploader. Claim-for-owned and format dedup don't run (the preview
    showed what the user owns). Auto-train and the policy gate do: qBit
    still downloads the data through MAM's tracker. No wedge, ever: an
    app can only spend one with `fl` on the .torrent download, which
    already happened, and MAM refuses "Buy as FL" via the API.

    A budget-and-queue-full drop is refused before any grab row exists.
    Otherwise the grab row is claimed under `grab_claim_lock()` and the
    bytes go through `submit_torrent_bytes`.
    """
    live = _live_kill_switch_state()
    if live["dry_run"] or deps.dry_run:
        return DispatchResult(
            action="skip", reason="dry_run_live", announce_id=0,
            error="Dry run is on; nothing was grabbed.",
        )
    try:
        qbit_hash = info_hash(torrent_bytes)
        stamp = read_mam_comment(torrent_bytes)
    except BencodeError as e:
        return DispatchResult(
            action="skip", reason="bad_torrent_file", announce_id=0,
            error=f"Not a valid .torrent file: {e}",
        )
    if stamp is None:
        return DispatchResult(
            action="skip", reason="not_mam_file", announce_id=0,
            error="Not a MAM .torrent (no MID in its comment).",
        )
    uid = await account_uid(deps)
    if uid is None:
        return DispatchResult(
            action="skip", reason="uid_unknown", announce_id=0,
            error=(
                "Can't confirm this .torrent is yours: Seshat can't read "
                "your MAM account right now. Check the cookie and try again."
            ),
        )
    if stamp.uid != uid:
        return DispatchResult(
            action="skip", reason="foreign_file", announce_id=0,
            error=(
                "This .torrent was downloaded by another MAM account; "
                "download your own copy from MAM."
            ),
        )

    tid = stamp.torrent_id
    token = deps.live_mam_token()
    try:
        info = await get_torrent_info(tid, token=token)
    except TorrentNotFoundError:
        info = None
        removed = True
    except TorrentInfoError as e:
        return DispatchResult(
            action="skip", reason="lookup_failed", announce_id=0,
            error=f"Couldn't reach MAM ({e}); nothing was grabbed. Try again.",
        )
    else:
        removed = False

    author_blob = (
        ", ".join(n for n in (info.authors or {}).values() if n) if info else ""
    )
    book_format = ""
    if info is not None:
        parts = (info.filetype or "").replace(",", " ").split()
        book_format = parts[0].lower() if parts else ""
    series_name = ""
    if info is not None:
        for v in (info.series or {}).values():
            if isinstance(v, list) and v and v[0]:
                series_name = str(v[0])
                break
    announce = Announce(
        torrent_id=tid,
        torrent_name=info.title if info else f"manual_upload_{tid}",
        category=info.category if info else "",
        author_blob=author_blob,
        series_name=series_name,
        book_title=info.title if info else "",
        filetype=book_format,
    )

    db = await deps.db_factory()
    try:
        announce_id = await grabs_storage.record_announce(
            db,
            raw=f"manual_grab:upload:{tid}",
            torrent_id=tid,
            torrent_name=announce.torrent_name,
            category=announce.category,
            author_blob=author_blob,
            decision=Decision(action="allow", reason="manual_inject", matched_author=author_blob),
            filetype=book_format,
        )

        async def blocking_grab() -> Optional[grabs_storage.GrabRow]:
            return (
                await grabs_storage.find_blocking_grab(db, tid)
                or await grabs_storage.find_grab_by_hash(db, qbit_hash)
            )

        prior = await blocking_grab()
        if prior is not None:
            return await _skip_already_grabbed(
                db, deps, announce=announce, announce_id=announce_id, prior=prior,
            )
        if removed:
            return await _refuse_grab(
                db, deps, announce=announce, announce_id=announce_id,
                reason="torrent_removed_from_mam",
                message=(
                    f"MAM torrent {tid} is no longer on MAM (removed, deleted "
                    "or trumped); nothing was grabbed."
                ),
            )
        if (
            deps.excluded_uploaders
            and info.uploader_name
            and info.uploader_name.lower() in deps.excluded_uploaders
        ):
            await grabs_storage.update_announce_decision(
                db, announce_id=announce_id, action="skip",
                reason=f"excluded_uploader:{info.uploader_name}",
            )
            return DispatchResult(
                action="skip",
                reason=f"excluded_uploader:{info.uploader_name}",
                announce_id=announce_id,
            )

        try:
            await train_authors_from_torrent_info(
                db, tid, token=token, fallback_blob=author_blob,
                source="coauthor_train",
            )
        except Exception:
            pass  # best-effort, as in dispatch

        eco_ctx = await _build_economic_context(deps, announce)
        policy_decision = evaluate_policy(eco_ctx, deps.policy_config)
        if policy_decision.action == "skip":
            await grabs_storage.update_announce_decision(
                db, announce_id=announce_id, action="skip",
                reason=f"policy:{policy_decision.tier}",
            )
            if policy_decision.tier == "buffer_insufficient":
                await _record_buffer_gate_block(
                    db, deps, announce=announce, eco_ctx=eco_ctx,
                    from_user_grab=True,
                )
            return DispatchResult(
                action="skip",
                reason=f"policy:{policy_decision.tier}",
                announce_id=announce_id,
            )

        rate_decision = decide_grab_action(
            budget_used=await ledger_mod.count_effective(db),
            budget_cap=deps.budget_cap,
            queue_size=await queue_mod.size(db),
            queue_max=deps.queue_max,
            queue_mode_enabled=deps.queue_mode_enabled,
        )
        if rate_decision.action == "drop":
            await grabs_storage.update_announce_decision(
                db, announce_id=announce_id, action="drop",
                reason=rate_decision.reason,
            )
            return DispatchResult(
                action="drop", reason=rate_decision.reason,
                announce_id=announce_id,
                error="Snatch budget and queue are both full; try again later.",
            )

        await release_write_lock(db)
        async with grab_claim_lock():
            prior = await blocking_grab()
            if prior is not None:
                return await _skip_already_grabbed(
                    db, deps, announce=announce, announce_id=announce_id,
                    prior=prior,
                )
            grab_id = await grabs_storage.create_grab(
                db,
                announce_id=announce_id,
                mam_torrent_id=tid,
                torrent_name=announce.torrent_name,
                category=announce.category,
                author_blob=author_blob,
                state=grabs_storage.STATE_FETCHED,
                book_format=book_format,
                dedup_key=normalize_dedup_key(announce.torrent_name, author_blob),
            )
    finally:
        await db.close()

    result = await submit_torrent_bytes(
        deps, grab_id=grab_id, torrent_bytes=torrent_bytes,
    )
    if result.action in ("drop", "skip") and result.qbit_hash is None:
        # The budget filled between our check and the submit (or dry run
        # flipped on): nothing reached qBit, so free the torrent ID
        # rather than leave a `fetched` row blocking it forever.
        db = await deps.db_factory()
        try:
            await grabs_storage.set_state(
                db, grab_id, grabs_storage.STATE_FAILED_UNKNOWN,
                failed_reason=result.error or result.reason,
            )
        finally:
            await db.close()
    return result


async def _refuse_grab(
    db: aiosqlite.Connection,
    deps: DispatcherDeps,
    *,
    announce: Announce,
    announce_id: int,
    reason: str,
    message: str,
    grab_id: Optional[int] = None,
) -> DispatchResult:
    """Record a snatch-safety skip on the audit row and return it.

    `message` rides in `DispatchResult.error` so every caller that
    already surfaces `error` (inject, inject-batch, send-to-pipeline,
    tentative approve) shows the user why nothing was grabbed.
    """
    await grabs_storage.update_announce_decision(
        db, announce_id=announce_id, action="skip", reason=reason,
    )
    _log.info(
        "snatch safety: skipped tid=%s (%s)", announce.torrent_id, reason,
    )
    _emit(deps, "snatch_guard_skip", {
        "torrent_id": announce.torrent_id,
        "reason": reason,
        "grab_id": grab_id,
    })
    return DispatchResult(
        action="skip",
        reason=reason,
        announce_id=announce_id,
        grab_id=grab_id,
        error=message,
    )


async def _skip_already_grabbed(
    db: aiosqlite.Connection,
    deps: DispatcherDeps,
    *,
    announce: Announce,
    announce_id: int,
    prior: grabs_storage.GrabRow,
) -> DispatchResult:
    return await _refuse_grab(
        db, deps,
        announce=announce, announce_id=announce_id,
        reason="already_grabbed",
        message=(
            f"Seshat already grabbed MAM torrent {announce.torrent_id} "
            f"(grab #{prior.id}, {prior.state}); it never downloads the "
            "same torrent twice."
        ),
        grab_id=prior.id,
    )


async def _build_economic_context(
    deps: DispatcherDeps,
    announce: Announce,
    *,
    wedge_requested: bool = False,
    trust_announce: bool = False,
    lookup: Optional[TorrentLookup] = None,
) -> EconomicContext:
    """Build the EconomicContext for the policy engine.

    `lookup` is the grab's torrent-info lookup when the snatch guard
    already made it; reused instead of asking MAM again.

    Always starts with the announce VIP flag (reliable, free). Then
    enriches with two MAM API calls when the policy config requires
    them:

      1. torrent_info (search by ID) — gives vip/free/fl_vip/personal_fl
         PLUS the size in bytes (for the buffer gate).
      2. user_status (jsonLoad.php) — gives ratio, wedges, AND the
         upload buffer (for the buffer gate).

    Both are cached and fail-safe — if either errors out, the policy
    engine just runs with whatever data is available. The announce
    VIP flag is always present, so the policy never runs blind.

    `trust_announce` (a held grab MAM never listed, D37) lets the
    announce's VIP|Normal stand as the free status.
    """
    ctx_kwargs: dict = {"announce_vip": announce.vip, "trust_announce": trust_announce}

    # Torrent-info is needed when any gate branches on per-torrent
    # economics OR when the buffer gate needs the torrent size.
    needs_torrent_info = (
        deps.live_mam_token()
        and announce.torrent_id
        and (
            deps.policy_config.free_only
            or deps.policy_config.use_wedge
            or deps.policy_config.ratio_floor > 0
            or deps.policy_config.buffer_gate_enabled
            # A wedge override needs to know the torrent isn't free yet
            # (`_wedge_for`); the lookup is usually already cached.
            or wedge_requested
        )
    )
    if needs_torrent_info:
        if lookup is None:
            lookup = await _look_up_torrent(deps, announce.torrent_id)
        try:
            if lookup.error is not None:
                raise lookup.error
            info = lookup.info
            ctx_kwargs["torrent_vip"] = info.vip
            ctx_kwargs["torrent_free"] = info.free
            ctx_kwargs["torrent_fl_vip"] = info.fl_vip
            ctx_kwargs["personal_freeleech"] = info.personal_freeleech
            # MAM sends size as "1.2 GiB". `int(info.size)` failed on
            # every real torrent, so until 2026-10-06 the buffer gate
            # always failed open. Unparseable values still fail open.
            ctx_kwargs["torrent_size_bytes"] = info.size_bytes
        except TorrentInfoError as e:
            _log.debug("torrent_info lookup failed for tid=%s: %s",
                         announce.torrent_id, e)

    # User-status is needed when any gate branches on ratio/wedges
    # OR when the buffer gate needs the upload_buffer.
    needs_user_status = (
        deps.live_mam_token()
        and (
            deps.policy_config.use_wedge
            or deps.policy_config.ratio_floor > 0
            or deps.policy_config.buffer_gate_enabled
        )
    )
    if needs_user_status:
        try:
            status = await get_user_status(token=deps.live_mam_token())
            ctx_kwargs["user_ratio"] = status.ratio
            ctx_kwargs["user_wedges"] = status.wedges
            ctx_kwargs["user_upload_buffer_bytes"] = status.upload_buffer_bytes
        except UserStatusError as e:
            _log.debug("user_status lookup failed: %s", e)

    return EconomicContext(**ctx_kwargs)


def _wedge_for(
    announce: Announce,
    eco_ctx: EconomicContext,
    *,
    policy_wedge: bool,
    forced: bool,
    raw_line: str,
) -> tuple[bool, str]:
    """Whether this fetch may carry `&fl=1`, and why it was asked for.

    MAM spends a wedge on `fl` even when the torrent is already free or
    VIP, with no refund (its `download.php` doc). So a wedge goes only
    on a torrent the search API confirmed is not free, whoever asked
    for it (D28). The policy can't choose one for an unknown status
    (D29); this also stops a user's tick on a free or unknown one.
    """
    if not (policy_wedge or forced):
        return False, ""
    if raw_line.startswith("manual_grab:"):
        why = "Grab from MAM"
    elif forced:
        why = "manual tick"
    else:
        why = "grab policy"
    if not eco_ctx.free_status_known:
        _log.info(
            "wedge not used on tid=%s (%s): MAM didn't say whether it's "
            "already free", announce.torrent_id, why,
        )
        return False, why
    if eco_ctx.is_free:
        _log.info(
            "wedge not used on tid=%s (%s): already free or VIP",
            announce.torrent_id, why,
        )
        return False, why
    return True, why


async def _audit_wedge(
    db: aiosqlite.Connection,
    *,
    announce: Announce,
    why: str,
    tier: str,
    user_grab: bool,
) -> None:
    """One economy-audit row per wedge spent (D30). Best-effort."""
    _log.info("wedge used on tid=%s (%s)", announce.torrent_id, why)
    try:
        await economy_audit.record(
            db,
            action=economy_audit.ACTION_WEDGE,
            trigger=(
                economy_audit.TRIGGER_USER_GRAB if user_grab
                else economy_audit.TRIGGER_IRC_AUTOGRAB
            ),
            outcome=economy_audit.OUTCOME_SUCCESS,
            torrent_id=announce.torrent_id,
            tier=tier,
            message=f"Wedge on '{announce.torrent_name}' ({why})",
        )
    except Exception:
        _log.exception("wedge audit failed for tid=%s (non-fatal)", announce.torrent_id)


async def _record_buffer_gate_block(
    db: aiosqlite.Connection,
    deps: DispatcherDeps,
    *,
    announce: Announce,
    eco_ctx: EconomicContext,
    from_user_grab: bool,
) -> None:
    """Audit a buffer-gate block and (throttled) fire a ntfy.

    `from_user_grab` distinguishes a manual-inject block from an
    IRC autograb block — the audit table stores the trigger so the
    MamPage history view can show "your click was blocked" vs "the
    IRC feed autograb was blocked", and the ntfy throttle keeps a
    separate 6h window per trigger.
    """
    trigger = (
        economy_audit.TRIGGER_USER_GRAB
        if from_user_grab
        else economy_audit.TRIGGER_IRC_AUTOGRAB
    )

    # Compose a human-readable message with the size + buffer figures
    # so the audit row is self-explanatory without joining back to
    # the torrent_info cache (which may have expired by the time the
    # user reviews the history).
    size_bytes = eco_ctx.torrent_size_bytes or 0
    buffer_bytes = eco_ctx.user_upload_buffer_bytes or 0
    size_gb = size_bytes / 1_000_000_000.0
    buffer_gb = buffer_bytes / 1_000_000_000.0
    message = (
        f"Would need {size_gb:.2f} GB; buffer is {buffer_gb:.2f} GB"
    )

    try:
        await economy_audit.record(
            db,
            action=economy_audit.ACTION_BUFFER_GATE_BLOCK,
            trigger=trigger,
            outcome=economy_audit.OUTCOME_BUFFER_GATE_BLOCK,
            torrent_id=announce.torrent_id,
            message=message,
        )
    except Exception:
        # Audit failures must not take down the dispatch loop. Log
        # and move on — the skip itself already returned cleanly.
        _log.exception(
            "failed to record buffer_gate_block audit row tid=%s",
            announce.torrent_id,
        )

    # In-browser toast — always fire for gate blocks. Unlike ntfy's
    # per-6h throttle (phone spam protection), a toast is ephemeral
    # and per-open-tab. If the user is actively watching the
    # dashboard they should see every block in real time.
    try:
        from app.orchestrator.sse_publishers import publish_toast
        label = announce.torrent_name or f"tid={announce.torrent_id}"
        await publish_toast(
            "warn",
            f"Buffer gate blocked {label}: needs {size_gb:.2f} GB, "
            f"buffer {buffer_gb:.2f} GB",
        )
    except Exception:
        _log.exception("buffer-gate toast publish failed (non-fatal)")

    # ntfy throttle: fire at most once per rolling 6h window per
    # trigger type. The first block after a restart always notifies
    # (sentinel 0), because "feed went quiet" after a restart is
    # exactly the case we want the user to see.
    if not deps.ntfy_url:
        return
    import time as _time
    now = _time.time()
    last = _last_buffer_gate_notify_at.get(trigger, 0.0)
    if (now - last) < _BUFFER_GATE_NOTIFY_WINDOW_SECONDS:
        return
    _last_buffer_gate_notify_at[trigger] = now
    try:
        from app.notifications import bus, events
        torrent_name = announce.torrent_name or f"tid={announce.torrent_id}"
        await bus.emit(
            events.GRAB_BUFFER_BLOCKED,
            title="Buffer gate blocked a grab",
            message=(
                f"{torrent_name}\n"
                f"Size {size_gb:.1f} GB exceeds available buffer "
                f"({buffer_gb:.1f} GB). Further blocks suppressed for 6h."
            ),
        )
    except Exception:
        _log.exception("grab.buffer_blocked bus emit failed (non-fatal)")


def _grab_failure_state(result: GrabResult) -> str:
    """Map a GrabResult.failure_kind to a `grabs.state` value."""
    kind = result.failure_kind
    if kind == "cookie_expired":
        return grabs_storage.STATE_FAILED_COOKIE_EXPIRED
    if kind == "torrent_not_found":
        return grabs_storage.STATE_FAILED_TORRENT_GONE
    return grabs_storage.STATE_FAILED_UNKNOWN


def failed_cookie_id(failed_state: str, token: str) -> Optional[int]:
    """`grabs.failed_with_cookie_id` for a failed download: the refused
    cookie's fingerprint on `failed_cookie_expired`, else None. The
    cookie-retry job retries such a grab only once the live cookie's
    fingerprint differs."""
    if failed_state != grabs_storage.STATE_FAILED_COOKIE_EXPIRED:
        return None
    return cookie_fingerprint(token)


def _add_failure_state(result: AddResult) -> str:
    """Map an AddResult.failure_kind to a `grabs.state` value."""
    kind = result.failure_kind
    if kind == "rejected":
        return grabs_storage.STATE_FAILED_QBIT_REJECTED
    if kind == "duplicate":
        return grabs_storage.STATE_DUPLICATE_IN_QBIT
    return grabs_storage.STATE_FAILED_UNKNOWN


async def _fetch_mam_cover_for_skip(
    deps: DispatcherDeps, torrent_id: str
) -> Optional[str]:
    """Best-effort MAM cover fetch for tentative captures.

    Downloads the MAM poster to a temp directory and returns the path.
    Returns None on any failure — never blocks the dispatch loop.
    The cover is stored alongside the tentative DB row so the review
    UI can show it. A cover already on disk for this torrent (a
    re-announce) is reused instead of fetched again.
    """
    if not deps.live_mam_token() or not torrent_id:
        return None
    try:
        from pathlib import Path
        import tempfile
        from app.metadata.covers import fetch_mam_cover

        # Store covers in a predictable location under staging_path
        # (or a temp dir if staging isn't configured).
        base = Path(deps.staging_path) if deps.staging_path else Path(tempfile.gettempdir())
        cover_dir = base / "tentative-covers" / f"tid-{torrent_id}"
        existing = sorted(cover_dir.glob("cover-mam.*")) if cover_dir.is_dir() else []
        if existing:
            return str(existing[0])
        path = await fetch_mam_cover(
            torrent_id,
            dest_dir=cover_dir,
            basename="cover-mam",
            token=deps.live_mam_token(),
        )
        return str(path) if path else None
    except Exception:
        _log.debug("MAM cover fetch failed for tentative tid=%s", torrent_id)
        return None


def _emit(deps: DispatcherDeps, event: str, payload: dict) -> None:
    """Fire the optional observability hook, swallowing exceptions."""
    if deps.on_event is None:
        return
    try:
        deps.on_event(event, payload)
    except Exception:
        _log.exception(f"on_event hook raised for {event}")
