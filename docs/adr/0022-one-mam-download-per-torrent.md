# 0022. One MAM download per torrent, ever

- Status: Accepted
- Date: 2026-10-06

## Context

MAM counts every `.torrent` download, and downloading the same torrent twice can look like an attempt to game the seeding system. Recon on 2026-10-06, before Manual Grab added the first user-driven way to submit the same torrent ID twice, found nothing enforcing this:

- `grabs.find_grab_by_torrent_id` was written for exactly this check and had **no callers**. The only pre-fetch dedup was format-priority dedup (title/author based, and overridable).
- Queued grabs dropped their bytes and re-fetched at pop time; delayed rotation and delayed reinject fetched again; the cookie-retry job re-fetched rows MAM had already served.
- The live database already held nine torrent IDs grabbed more than once.

Grab-state names alone can't tell "MAM served the bytes" from "the fetch never happened": `failed_torrent_gone` is both a download 404 *and* the download watcher's "absent from qBit for 12h", and `failed_unknown` covers both sides of the fetch. `grabs.qbit_hash` is the reliable marker — it is only ever computed from bytes MAM handed over.

## Decision

Every path into the dispatcher refuses, before any fetch:

| Skip reason | When | Override |
| --- | --- | --- |
| `already_grabbed` | A prior grab of this ID has `qbit_hash` set, or is in `grabs.BLOCKING_STATES` (in flight, or a state only reachable after a successful fetch) | **None.** A torrent Seshat fetched is never fetched again |
| `already_snatched_on_mam` | MAM's `my_snatched` flag on the search-API result | `override_mam_snatched` — an explicit user confirm. Otherwise point the user at Reingest from disk |
| `torrent_removed_from_mam` | Search API says not found ([0006](0006-mam-not-found-is-permanent.md)), on user/programmatic grabs | None. IRC announces fail open: a fresh upload can beat the search index |

Pre-fetch failures (cookie expired, 404 on download, network error — no `qbit_hash`) stay retryable. The `already_grabbed` check re-runs under a per-event-loop asyncio lock immediately before the grab row is inserted, and the cookie-retry job claims its row under the same lock, so concurrent grabs of one ID cannot both pass. Seshat runs one uvicorn worker, so an in-process lock is sufficient.

Every other torrent-info lookup failure fails open: the database guard still covers everything Seshat itself fetched.

**The bytes are kept.** Once MAM has served a `.torrent`, Seshat never asks for it again, so every later step works from the bytes it holds:

- A grab that can't reach qBit yet saves its bytes to `<data>/queued-torrents/<grab_id>.torrent` before it is queued; a failed save fails the grab instead of queueing it.
- Queue pops, delayed rotation and delayed reinject use those bytes (reinject through the bytes-in path `submit_torrent_bytes`). A missing file fails the grab loudly (`grab.failed`); it is never re-fetched.
- Before saved bytes go to qBit, one search-API call by ID confirms the torrent is still on MAM. Removed → fail as torrent-gone. Unreachable → hold (the queue stops draining until the next tick; a reinject returns "try again").
- Files are deleted on submit or any terminal state, and a startup sweep removes any whose grab is no longer queued. They carry the passkey, as the delayed folder always has.

## Consequences

- A grab that MAM served but that never landed (qBit rejected it, its files were deleted) cannot be re-grabbed through Seshat; the user handles it on MAM directly.
- A `failed_*` row with a `qbit_hash` is permanently blocking; this is intended.
- A grab row stuck in `fetched` after a crash mid-fetch blocks its torrent ID until someone clears it, because it can't be known whether MAM served it.
- Any future code that fetches from MAM must go through `_dispatch_with_decision` or take `grab_claim_lock()` and consult `find_blocking_grab` itself. Code that already holds bytes (an upload) goes through `submit_torrent_bytes` after claiming its grab row.
- Never wait on `grab_claim_lock()` with an open write transaction: the holder needs SQLite's write lock. Each claim site calls `release_write_lock(db)` first (a 30s deadlock in CI taught this).
- An expired cookie stalls the queue (the liveness check can't run), even though submitting saved bytes would not need it. Accepted: nothing reaches qBit unchecked.
