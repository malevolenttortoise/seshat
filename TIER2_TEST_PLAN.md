# Tier 2 SSE Live Events — Manual Test Plan

Covers the MouseSearch-port Tier 2 work shipped across commits
`5fda43f`..`dfef9b8`. Tier 1 (MAM economy) regression is covered
by `TIER1_TEST_PLAN.md`; this plan focuses only on the SSE
plumbing + its consumers.

Run against a real Seshat instance (GHCR `seshat:latest` pulled
onto Unraid — `http://10.0.10.20:8789/`). All five sections should
pass before Tier 2 is considered UAT-complete.

Prerequisites:
- MAM cookie configured + validating.
- qBittorrent reachable.
- At least one active torrent in the watched category (so
  `torrent-progress` has something to emit).

---

## Section A — Backend smoke (curl the event stream)

Quick sanity check that the endpoint is mounted and serving events.

1. From the Seshat host (SSH to `deepstonecrypt`), pipe the event
   stream to stdout:
   ```sh
   curl -N http://localhost:8789/api/v1/events
   ```
   Expected: immediately receives `: ping` keepalive lines every
   ~15s; no body terminates unless you ^C.

2. In a second terminal, publish a test toast via a short Python
   snippet inside the container:
   ```sh
   docker exec -it Seshat python -c \
     "import asyncio; \
      from app.orchestrator.sse_publishers import publish_toast; \
      asyncio.run(publish_toast('info', 'Tier 2 smoke test'))"
   ```
   Expected in the curl output:
   ```
   event: toast
   id: 1
   data: {"level": "info", "message": "Tier 2 smoke test"}
   ```

3. Trigger a real backend event:
   - Click "Scan Sources" on the Dashboard, wait for completion.
   - The curl stream should emit `event: toast` with a
     "Source scan complete" message payload.

**Pass criterion:** The three events arrive in order; no 500s,
no disconnects, no malformed bodies.

---

## Section B — End-to-end MamPage live updates

Validates `mam-stats` wiring on the MamPage.

1. Open the MamPage in a browser tab.
2. Note the current ratio / seedbonus / wedge counts.
3. From another tab or via the Tier 1 economy UI, trigger a
   small upload-credit buy (e.g. 50 GB in dry-run mode).
4. Expected on the MamPage within ~1 second of the buy:
   - Seedbonus drops by 25,000 BP (50 GB × 500 BP/GB).
   - "Recent activity" audit row appears on next periodic refresh.
5. Toggle the tab hidden (switch windows), wait 30s, swap back.
   Expected: ratio/seedbonus reflects the newest state immediately,
   without a full page refresh.

**Pass criterion:** Economic fields update live without the page
being refreshed or polled.

---

## Section C — Dashboard live updates + toast routing

Validates `UnifiedDashboard` subscribers.

1. Open the Dashboard (`/`, default route).
2. Trigger a source scan from the Command Center.
   Expected: a toast "Source scan complete: N new books across M
   author(s)" appears within a second of the scan finishing.
3. Trigger a scheduled auto-buy (either wait for the natural
   tick or tweak the interval to fire immediately in Settings
   → Auto-buy).
   Expected: toast "Auto-buy: VIP 4 weeks (cost 5000 BP)" or the
   upload-credit equivalent.
4. Stop the qBittorrent container (`docker stop qBittorrent` on
   Unraid) and wait ~90 seconds (one budget watcher cycle).
   Expected: toast "qBittorrent unreachable — check logs".
5. Restart qBittorrent. Wait for another budget watcher cycle.
   Expected: toast "qBittorrent reachable".

**Pass criterion:** All four toasts fire at the right moments.
No duplicate toasts, no missed transitions.

---

## Section D — Visibility + reconnect

Validates `useVisibleEventSource` lifecycle.

1. Open the Dashboard, watch the Network panel. Filter to
   `events`.
2. Expected: one active connection to `/api/v1/events`.
3. Switch to a different browser tab, leave Dashboard in the
   background. After ~2 seconds, return to Network.
   Expected: the events request has been closed (status: cancelled).
4. Swap back to the Dashboard tab.
   Expected: a new events request opens immediately.
5. From the Seshat host, restart the container:
   ```sh
   docker restart Seshat
   ```
   Expected in the browser:
   - Existing events request fails (status: error / 502).
   - The hook retries on exponential backoff — first retry after
     ~500ms, then 1s, 2s, ..., capped at 30s.
   - Once Seshat comes back up, a successful reconnect establishes
     and `open` event count ticks up.
   - No toast spam during the reconnect attempts.

**Pass criterion:** Connection follows visibility + survives
container restart without user intervention.

---

## Section E — Multi-tab consistency

Validates the per-client fanout architecture.

1. Open the Dashboard in two separate tabs (or one tab + one
   browser window).
2. Trigger a source scan.
3. Expected: both tabs receive the "Source scan complete" toast
   within ~1s of each other. (They share a single backend qBit
   snapshot; the fanout delivers to both.)
4. Close one tab; keep the other.
5. Trigger another scan.
6. Expected: only the remaining tab toasts — the backend's
   subscriber count went from 2 → 1 when the tab closed.

**Pass criterion:** Events reach every connected tab; closed
tabs stop receiving events and don't leak a backend subscriber.

---

## Out-of-scope follow-ups

These are deliberately not wired in this Tier 2 batch; they
consume the same SSE stream and can be layered on later:

- **BookSidebar torrent-progress** — show live download %
  inline for a book that has an in-flight grab. Requires
  resolving book → qbit_hash at sidebar open time.
- **DiscMAMPage torrent-progress** — highlight torrents currently
  being downloaded on the search result grid.
- **Ping frequency / heartbeat tuning** — 15s default matches most
  reverse proxy idle timeouts, but a deployment behind a stricter
  nginx may want it tighter.
