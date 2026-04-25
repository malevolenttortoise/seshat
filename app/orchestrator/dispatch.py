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

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Protocol

import aiosqlite

from app.clients.base import AddResult, TorrentClient
from app.filter.gate import Announce, Decision, FilterConfig, evaluate_announce
from app.mam.grab import GrabResult
from app.mam.torrent_meta import BencodeError, info_hash
from app.mam.torrent_info import TorrentInfoError, get_torrent_info
from app.mam.user_status import UserStatusError, get_user_status
from app.orchestrator.auto_train import train_author
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
from app.storage import tentative as tentative_storage

_log = logging.getLogger("seshat.orchestrator.dispatch")


# Rolling-6h-window ntfy throttle for buffer-gate blocks, keyed by
# trigger (IRC autograb vs user grab). In-memory, resets on process
# restart — a restart right after a notify doesn't cost anything
# worse than one extra message if the buffer is still tight. Writing
# this to the DB would persist it across restarts but isn't worth
# the complexity for a soft "don't spam" throttle.
_BUFFER_GATE_NOTIFY_WINDOW_SECONDS = 6 * 3600
_last_buffer_gate_notify_at: dict[str, float] = {}


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
    category_routing: dict = field(default_factory=dict)
    ntfy_url: str = ""
    ntfy_topic: str = "seshat"
    per_event_notifications: bool = False
    auto_train_enabled: bool = True

    # Optional: an audit hook for tests / future observability.
    on_event: Optional[Callable[[str, dict], None]] = None


# ─── Result type ─────────────────────────────────────────────


@dataclass(frozen=True)
class DispatchResult:
    """Outcome of a single dispatch call.

    `action` mirrors the rate-limit decision (`submit`/`queue`/`drop`)
    when the filter allowed the announce, or `"skip"` when the filter
    rejected it. `grab_id` is the row id in `grabs` (None for skip
    and drop). `error` is set when fetching or submitting failed.
    """

    action: str               # "skip" | "submit" | "queue" | "drop"
    reason: str               # human-readable + machine-stable
    announce_id: int          # always set — every dispatch produces an audit row
    grab_id: Optional[int] = None
    qbit_hash: Optional[str] = None
    error: Optional[str] = None


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
      4. If filter says allow → consult the rate limiter
      5. If decision is drop → return "drop" (no grab row, only audit)
      6. Fetch the .torrent file
      7. If fetch fails → write a failed grab row, return failure
      8. If decision is submit → submit to qBit, record in ledger
      9. If decision is queue → enqueue (file already fetched)

    Returns a `DispatchResult` describing the outcome. Never raises
    on the happy or expected-failure paths — the IRC listener
    iterates over many announces and a single bad one shouldn't
    take down the loop.
    """
    decision = evaluate_announce(announce, deps.filter_config)
    return await _dispatch_with_decision(
        deps,
        announce=announce,
        raw_line=raw_line,
        filter_decision=decision,
        skip_filter=False,
        force_fl_wedge=False,
    )


async def inject_grab(
    deps: DispatcherDeps,
    *,
    torrent_id: str,
    torrent_name: str = "",
    category: str = "",
    author_blob: str = "",
    series_name: str = "",
    book_title: str = "",
    raw_line: str = "manual_inject",
    force_fl_wedge: bool = False,
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
    """
    fake_announce = Announce(
        torrent_id=torrent_id,
        torrent_name=torrent_name or f"manual_inject_{torrent_id}",
        category=category,
        author_blob=author_blob,
        series_name=series_name,
        book_title=book_title,
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
) -> DispatchResult:
    """The shared pipeline body used by both handle_announce and
    inject_grab. The only thing they differ on is whether the filter
    decision came from `evaluate_announce` or was synthesized.
    """
    db = await deps.db_factory()
    try:
        announce_id = await grabs_storage.record_announce(
            db,
            raw=raw_line,
            torrent_id=announce.torrent_id,
            torrent_name=announce.torrent_name,
            category=announce.category,
            author_blob=announce.author_blob,
            decision=filter_decision,
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
            # what they're turning down.
            elif filter_decision.reason == "ignored_author":
                try:
                    cover_path = await _fetch_mam_cover_for_skip(
                        deps, announce.torrent_id
                    )
                    await tentative_storage.record_ignored_seen(
                        db,
                        mam_torrent_id=announce.torrent_id,
                        torrent_name=announce.torrent_name,
                        author_blob=announce.author_blob
                            or filter_decision.primary_log_author
                            or "",
                        category=announce.category,
                        info_url=announce.info_url or None,
                        cover_path=cover_path,
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

        # Co-author auto-train: if the filter allowed this announce
        # because one co-author matched, add the unknown co-authors
        # to the allow list too. They're collaborating with a known-
        # good author, so they're likely relevant to the user's taste.
        if (
            filter_decision.reason == "allowed_author"
            and filter_decision.all_authors
            and not skip_filter
        ):
            for raw_author in filter_decision.all_authors:
                try:
                    await train_author(db, raw_author, source="coauthor_train")
                except Exception:
                    pass  # best-effort, don't block the grab

        # Filter said allow (or we're injecting). Build the economic
        # context for the policy engine. Start with what we already
        # know from the announce, then enrich with the MAM APIs (both
        # cached, both fail-safe).
        eco_ctx = await _build_economic_context(deps, announce)

        # Uploader exclusion check. Uses the cached torrent_info (zero
        # extra cost) to see if this torrent was uploaded by someone on
        # the excluded list. Prevents downloading your own uploads.
        if deps.excluded_uploaders and deps.mam_token and announce.torrent_id:
            try:
                info = await get_torrent_info(
                    announce.torrent_id, token=deps.mam_token, ttl=300
                )
                if info.uploader_name and info.uploader_name.lower() in deps.excluded_uploaders:
                    _emit(deps, "excluded_uploader", {
                        "torrent_id": announce.torrent_id,
                        "uploader": info.uploader_name,
                    })
                    return DispatchResult(
                        action="skip",
                        reason=f"excluded_uploader:{info.uploader_name}",
                        announce_id=announce_id,
                    )
            except TorrentInfoError:
                pass  # fail-open: if we can't check, allow the grab

        policy_decision = evaluate_policy(eco_ctx, deps.policy_config)

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
                    fetch_torrent=deps.fetch_torrent,
                    mam_token=deps.mam_token,
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
        grab_id = await grabs_storage.create_grab(
            db,
            announce_id=announce_id,
            mam_torrent_id=announce.torrent_id,
            torrent_name=announce.torrent_name,
            category=announce.category,
            author_blob=announce.author_blob,
            state=initial_state,
        )

        # `force_fl_wedge` is the manual-inject override — the user
        # explicitly asked for `&fl=1` on this grab, irrespective of
        # what the policy engine decided. Either path alone is
        # enough to flip the wedge on.
        fetch_result = await deps.fetch_torrent(
            announce.torrent_id, deps.mam_token,
            use_fl_wedge=policy_decision.use_wedge or force_fl_wedge,
        )

        if not fetch_result.success:
            failed_state = _grab_failure_state(fetch_result)
            await grabs_storage.set_state(
                db,
                grab_id,
                failed_state,
                failed_reason=fetch_result.failure_detail,
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

        if rate_decision.action == "queue":
            # Park the grab in the pending queue. The .torrent bytes
            # ARE NOT persisted to disk in Phase 1 — the queue holds
            # the grab id; the budget watcher (in a later phase)
            # re-fetches when popping. This is intentional: keeping
            # bytes only in memory means a crash loses queued grabs
            # but never leaves stale .torrent files lying around.
            # The Phase 2 follow-up will add disk persistence.
            await queue_mod.enqueue(db, grab_id)
            await grabs_storage.set_state(
                db,
                grab_id,
                grabs_storage.STATE_PENDING_QUEUE,
                qbit_hash=qbit_hash,
            )
            _emit(deps, "queued", {"grab_id": grab_id})
            return DispatchResult(
                action="queue",
                reason=rate_decision.reason,
                announce_id=announce_id,
                grab_id=grab_id,
                qbit_hash=qbit_hash,
            )

        # Submit path: compute the save path (monthly folder if enabled).
        # The save_path we send to qBit uses qBit's mount namespace
        # (e.g. /data/[mam-complete]/[2026-04]). qBit can't auto-create
        # folders with bracket characters, so we pre-create the folder
        # using OUR mount namespace (e.g. /downloads/[mam-complete]/...)
        # before passing the path to qBit.
        save_path = None
        if deps.qbit_download_path:
            save_path = compute_download_folder(
                deps.qbit_download_path,
                deps.download_folder_structure,
                author_name=announce.author_blob,
                series_name=announce.series_name,
                book_title=announce.book_title,
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

        add_result = await deps.qbit.add_torrent(
            torrent_bytes,
            category=deps.qbit_category,
            save_path=save_path,
            tags=deps.qbit_tags or None,
        )

        if not add_result.success:
            # If the client is unreachable or auth failed, queue the
            # grab so it can be retried when the client comes back.
            # We already fetched the .torrent from MAM — losing it
            # would waste a snatch. Only permanent failures (rejected,
            # duplicate) stay as failed.
            retriable = add_result.failure_kind in ("auth_failed", "network_error")
            if retriable and deps.queue_mode_enabled:
                await queue_mod.enqueue(db, grab_id)
                await grabs_storage.set_state(
                    db, grab_id, grabs_storage.STATE_PENDING_QUEUE,
                    qbit_hash=qbit_hash,
                    failed_reason=f"client unreachable, queued for retry: {add_result.failure_detail}",
                )
                _emit(deps, "queued_on_client_failure", {
                    "grab_id": grab_id, "kind": add_result.failure_kind,
                })
                _log.info(
                    "download client unreachable for grab_id=%d — queued for retry (%s)",
                    grab_id, add_result.failure_kind,
                )
                return DispatchResult(
                    action="queue",
                    reason=f"client_unreachable:{add_result.failure_kind}",
                    announce_id=announce_id,
                    grab_id=grab_id,
                    qbit_hash=qbit_hash,
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

        if deps.per_event_notifications and deps.ntfy_url:
            try:
                from app.notify import ntfy as _ntfy
                await _ntfy.notify_grab(
                    deps.ntfy_url, deps.ntfy_topic,
                    announce.torrent_name,
                    announce.author_blob,
                    announce.category,
                )
            except Exception:
                _log.exception("per-event notify_grab failed (non-fatal)")
        return DispatchResult(
            action="submit",
            reason="ok",
            announce_id=announce_id,
            grab_id=grab_id,
            qbit_hash=qbit_hash,
        )
    finally:
        await db.close()


async def _build_economic_context(
    deps: DispatcherDeps, announce: Announce
) -> EconomicContext:
    """Build the EconomicContext for the policy engine.

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
    """
    ctx_kwargs: dict = {"announce_vip": announce.vip}

    # Torrent-info is needed when any gate branches on per-torrent
    # economics OR when the buffer gate needs the torrent size.
    needs_torrent_info = (
        deps.mam_token
        and announce.torrent_id
        and (
            deps.policy_config.free_only
            or deps.policy_config.use_wedge
            or deps.policy_config.ratio_floor > 0
            or deps.policy_config.buffer_gate_enabled
        )
    )
    if needs_torrent_info:
        try:
            info = await get_torrent_info(announce.torrent_id, token=deps.mam_token)
            ctx_kwargs["torrent_vip"] = info.vip
            ctx_kwargs["torrent_free"] = info.free
            ctx_kwargs["torrent_fl_vip"] = info.fl_vip
            ctx_kwargs["personal_freeleech"] = info.personal_freeleech
            # info.size is a string of bytes — parse defensively
            # because MAM has been known to send an empty string on
            # edge cases. Malformed values fall through to None so
            # the policy engine fails open on the buffer gate.
            try:
                ctx_kwargs["torrent_size_bytes"] = int(info.size) if info.size else None
            except (TypeError, ValueError):
                ctx_kwargs["torrent_size_bytes"] = None
        except TorrentInfoError as e:
            _log.debug("torrent_info lookup failed for tid=%s: %s",
                         announce.torrent_id, e)

    # User-status is needed when any gate branches on ratio/wedges
    # OR when the buffer gate needs the upload_buffer.
    needs_user_status = (
        deps.mam_token
        and (
            deps.policy_config.use_wedge
            or deps.policy_config.ratio_floor > 0
            or deps.policy_config.buffer_gate_enabled
        )
    )
    if needs_user_status:
        try:
            status = await get_user_status(token=deps.mam_token)
            ctx_kwargs["user_ratio"] = status.ratio
            ctx_kwargs["user_wedges"] = status.wedges
            ctx_kwargs["user_upload_buffer_bytes"] = status.upload_buffer_bytes
        except UserStatusError as e:
            _log.debug("user_status lookup failed: %s", e)

    return EconomicContext(**ctx_kwargs)


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
        from app.notify import ntfy as _ntfy
        await _ntfy.notify_buffer_gate_block(
            deps.ntfy_url, deps.ntfy_topic,
            announce.torrent_name or f"tid={announce.torrent_id}",
            size_gb, buffer_gb,
        )
    except Exception:
        _log.exception("buffer-gate ntfy failed (non-fatal)")


def _grab_failure_state(result: GrabResult) -> str:
    """Map a GrabResult.failure_kind to a `grabs.state` value."""
    kind = result.failure_kind
    if kind == "cookie_expired":
        return grabs_storage.STATE_FAILED_COOKIE_EXPIRED
    if kind == "torrent_not_found":
        return grabs_storage.STATE_FAILED_TORRENT_GONE
    return grabs_storage.STATE_FAILED_UNKNOWN


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
    """Best-effort MAM cover fetch for tentative/ignored captures.

    Downloads the MAM poster to a temp directory and returns the path.
    Returns None on any failure — never blocks the dispatch loop.
    The cover is stored alongside the tentative/ignored-seen DB row
    so the review UI can show it.
    """
    if not deps.mam_token or not torrent_id:
        return None
    try:
        from pathlib import Path
        import tempfile
        from app.metadata.covers import fetch_mam_cover

        # Store covers in a predictable location under staging_path
        # (or a temp dir if staging isn't configured).
        base = Path(deps.staging_path) if deps.staging_path else Path(tempfile.gettempdir())
        cover_dir = base / "tentative-covers" / f"tid-{torrent_id}"
        path = await fetch_mam_cover(
            torrent_id,
            dest_dir=cover_dir,
            basename="cover-mam",
            token=deps.mam_token,
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
