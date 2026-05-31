# Notifications

Seshat ships notifications through a single dotted-name event taxonomy and a
per-event routing layer on top of [ntfy](https://ntfy.sh). Every site in the
app that pushes a notification goes through one dispatcher
(`bus.emit(event_type, …)`), and every event it can emit is catalogued in a
single registry. Operators tune *what fires*, *where it goes*, and *when it
stays quiet* from one Settings panel — not from a wall of `notify_on_*`
booleans.

This chapter is the canonical home for notification routing decisions. The
legacy `notify_on_metadata_cache_*` block in DEPLOY's Step 6 still works (see
[Migration from pre-v2.28.0](#migration-from-pre-v2280-operators) below), but
new routing configuration belongs here.

## Pre-requisite — ntfy setup

Without an ntfy server configured, every event resolves to "no destination"
and is silently dropped. Two settings are required:

| Setting | Purpose |
| --- | --- |
| `ntfy_url` | Base URL of your ntfy server (e.g. `https://ntfy.example.com`). A path component is tolerated for backwards compatibility — its first segment is treated as the default topic. |
| `ntfy_topic` | Default topic for events that have no per-event topic override. |

Both keys live at the top level of `settings.json` and are surfaced under
**Settings → Notifications** above the routing panel. From this point on the
chapter assumes both are set.

## The event registry

Every event Seshat can emit is declared in `app/notifications/events.py` and
exposed to the Settings UI through `GET /api/v1/notifications/events`. The
catalogue currently holds **21 events** across six prefixes. Each event
declares a default priority (1 = lowest, 5 = highest — ntfy's scale), a
default tag list (ntfy renders some tags as emoji), and whether it is
**suppressible during quiet hours**.

Events flagged "bypasses quiet hours" in the registry always fire through —
the panel marks them with a warning row. The intent is "quiet hours mute
routine successes, not failures."

### `grab.*` — autograb pipeline

| Event | Fires when | Default priority | Default tags | Quiet-hours suppressible |
| --- | --- | --- | --- | --- |
| `grab.success` | A torrent was grabbed (autograb or manual). | 3 | `books` | yes |
| `grab.buffer_blocked` | An autograb was refused by the buffer gate. | 4 | `no_entry_sign` | **no** — bypasses |

### `pipeline.*` — post-download, pre-library

| Event | Fires when | Default priority | Default tags | Quiet-hours suppressible |
| --- | --- | --- | --- | --- |
| `pipeline.download_complete` | A torrent download finished. | 3 | `white_check_mark` | yes |
| `pipeline.review_queued` | A downloaded book entered the review queue. | 3 | `books`, `white_check_mark` | yes |
| `pipeline.library_ingest` | A book landed in a library (Calibre / CWA / Audiobookshelf). | 3 | `books`, `white_check_mark` | yes |
| `pipeline.error` | The post-download pipeline hit a fatal error. | 4 | `warning` | **no** — bypasses |

### `discovery.*` — source scanning and MAM matching

| Event | Fires when | Default priority | Default tags | Quiet-hours suppressible |
| --- | --- | --- | --- | --- |
| `discovery.scan_complete` | A source or bulk scan finished. | 3 | `books`, `mag` | yes |
| `discovery.new_books` | Per-author new-books summary inside a bulk scan. | 3 | `books`, `sparkles` | yes |
| `discovery.mam_complete` | A MAM scan finished (found / possible / not-found summary). | 3 | `mag` | yes |
| `discovery.pipeline_sent` | Books were sent from discovery to the pipeline. | 3 | `arrow_down`, `books` | yes |

### `sync.*` — library and cookie maintenance

| Event | Fires when | Default priority | Default tags | Quiet-hours suppressible |
| --- | --- | --- | --- | --- |
| `sync.library` | A library finished syncing (Calibre / Audiobookshelf). | 3 | `books` | yes |
| `sync.mam_cookie_rotated` | The MAM session cookie was automatically refreshed. | 2 | `key` | yes |

### `source.*` — source health and metadata-cache worker

These events report on the per-source health surfaces — Goodreads weekly
canary, the Amazon and Goodreads
[metadata cache](../../CONTEXT.md#metadata-caching) workers. The *triggers*
themselves live in [`./metadata-cache.md`](./metadata-cache.md); the events
they emit are catalogued here.

| Event | Fires when | Default priority | Default tags | Quiet-hours suppressible |
| --- | --- | --- | --- | --- |
| `source.goodreads_canary_failed` | The weekly Goodreads canary detected a Cloudflare soft-block. | 4 | `warning` | **no** — bypasses |
| `source.metadata_cache_error` | The metadata-cache worker hit a fatal error. | 4 | `warning` | **no** — bypasses |
| `source.metadata_cache_warning` | The metadata-cache worker logged a recoverable warning. | 3 | `warning` | yes |
| `source.metadata_cache_daily_summary` | Daily summary of the metadata-cache worker's activity. | 3 | `books`, `calendar` | yes |
| `source.metadata_cache_new_book` | The metadata-cache worker discovered a previously-unseen book. | 3 | `books`, `sparkles` | yes |

### `digest.*` — scheduled summaries

Digests are timer-driven, not event-driven, but they're catalogued through
the same registry so the routing panel can toggle them uniformly.

| Event | Fires when | Default priority | Default tags | Quiet-hours suppressible |
| --- | --- | --- | --- | --- |
| `digest.daily_accepted` | Daily digest of accepted books. | 3 | `books` | yes |
| `digest.daily_tentative` | Daily digest of books awaiting tentative-review approval. | 3 | `books`, `mag` | yes |
| `digest.daily_ignored` | Daily digest of ignored torrents. | 3 | `books` | yes |
| `digest.weekly` | Weekly digest (author promotions + Calibre summary). | 3 | `books`, `calendar` | yes |

> [`./hygiene-jobs.md`](./hygiene-jobs.md) describes the scheduled
> hygiene-job machinery; hygiene runs surface through
> `source.metadata_cache_*` (for cache-touching jobs) and the
> `digest.*` events (for queue-shape summaries). There is no dedicated
> `hygiene.*` prefix today.

## Hierarchical-dotted taxonomy

Event names are read left-to-right as **prefix → subsystem → specific
event**: `source.goodreads_canary_failed` is a `source` event, scoped to the
Goodreads source-health concern, naming the canary failure. Concretely the
naming pays off in two places:

- **Prefix routing.** `source.*` matches every source-health event in one
  rule. Adding a future `source.amazon.cooldown_escalated` event would route
  under the same rule with no config change. (Names today are flat
  beneath the prefix; the matcher tolerates arbitrarily deep dots.)
- **Cross-reference clarity.** Sibling chapters refer to events by name
  rather than by the producing call site — the registry is the contract.

## Routing model

Routing rules live under `notifications.events` in `settings.json` and
override the registry's defaults. Each rule may set any of three fields per
event key — `enabled`, `topic`, `priority`:

```json
{
  "notifications": {
    "master_enabled": true,
    "events": {
      "grab.success":        { "enabled": true,  "topic": "seshat-grabs" },
      "source.*":            { "topic": "seshat-scrapers" },
      "digest.daily_ignored":{ "enabled": false },
      "*":                   { "topic": "seshat-misc" }
    }
  }
}
```

Three kinds of key:

- **Exact event name** (`grab.success`) — matches that one event.
- **Prefix wildcard** (`source.*`) — matches the prefix itself and any
  deeper dotted name beneath it.
- **Universal** (`*`) — matches every event. Wildcards must be either
  `prefix.*` or the bare `*`; mid-name and suffix wildcards are not
  supported.

### Resolution order

For any field, the bus picks the value from the most specific matching key:

1. **Exact key** wins unconditionally.
2. **Longest matching prefix wildcard** wins among wildcards
   (`source.metadata_cache.*` would beat `source.*` for a
   `source.metadata_cache_error` event, were such a key configured).
3. **Universal `*`** applies only when no prefix wildcard matched.
4. The **registry default** (or caller-supplied default for `topic`,
   `priority`) applies if nothing routed.

Resolution is per-field. A rule that only sets `topic` lets `priority` and
`enabled` continue to resolve normally — they may come from a different
(less specific) rule or from the registry default.

### Master toggle

`notifications.master_enabled` is the global kill switch. When explicitly
`false` every event is suppressed regardless of per-event configuration. Use
this as the panic button; use per-event `enabled: false` (or an
`enabled: false` wildcard) to mute individual subsystems.

## Per-event priority and tags

Priority precedence at send time (highest → lowest):

1. An explicit `priority=` kwarg on the producing `bus.emit()` call.
2. A `priority` value resolved through the routing config
   (exact / wildcard / `*`).
3. The event's `default_priority` from the registry.

Operators only see precedences 2 and 3 — bumping a single event's priority
is a one-line override under that event's key. Bumping a whole subsystem
uses a `prefix.*` rule.

Default **tags** are baked into each registry entry — they're the ntfy tag
list (which renders as emoji on most clients). Tags are not routable today;
overriding tags requires changing the call site.

## Quiet hours

Quiet hours are a single window during which suppressible events are
**silently dropped** — not deferred, not batched, not retried later. The
window is configured under `notifications.quiet_hours`:

```json
{
  "notifications": {
    "quiet_hours": {
      "enabled": true,
      "start": "23:00",
      "end":   "07:00",
      "timezone": "America/New_York"
    }
  }
}
```

Behavior:

- **Overnight windows** (start > end, e.g. `23:00 → 07:00`) are supported —
  the window is treated as `[start, 24:00) ∪ [00:00, end)`.
- **Zero-length windows** (start == end) are treated as "always off" so a
  misconfiguration cannot silently mute the entire app forever.
- **Malformed config** (invalid `HH:MM`, unknown IANA timezone) silently
  degrades to "quiet hours off". Notification logic must never crash a
  producing call site.
- **Timezone** uses IANA names (`America/New_York`, `Europe/London`). Leave
  blank for the container's system local time.

Per-event opt-out is set in the registry, not the routing config —
`grab.buffer_blocked`, `pipeline.error`, `source.goodreads_canary_failed`,
and `source.metadata_cache_error` carry
`suppressible_during_quiet_hours=False` and fire through unconditionally.
The routing panel flags these rows with a "⚠ Bypasses quiet hours" note.

## Settings UI walkthrough

The routing panel lives at **Settings → Notifications → Advanced Routing &
Quiet Hours**. It has three sections:

1. **Master enabled.** A single checkbox. Unchecking it mutes everything,
   regardless of per-event state.
2. **Quiet hours.** Enable toggle, `Start` / `End` time inputs, and an
   IANA `Timezone` field. The summary line shows the active window when
   enabled (`Quiet hours (23:00 → 07:00)`) or `(off)` when not.
3. **Per-event Overrides.** A collapsible section that lists:
   - A **wildcard rule editor** at the top — add `prefix.*` or `*` rules,
     each with their own `Enabled` / `Topic override` / `Priority` controls
     and a `Remove` button.
   - A **registry-driven table** with one row per catalogued event. Each
     row shows the event name, its description, a "⚠ Bypasses quiet hours"
     badge if applicable, and three controls: `Enabled`
     (`Default` / `On` / `Off`), `Topic Override` (free-text; empty = use
     default), and `Priority` (`Default (N)` or `1`–`5`).

The table is rendered from the live `/api/v1/notifications/events`
response, so it always reflects the registry the running container ships —
no frontend hardcoding of event names.

Saving works through the existing **Save** button at the top of the
Settings page; the panel buffers changes into the parent settings dict and
the page issues a single `PATCH /api/v1/settings` for the whole tree.

## Examples

### Route every source-health event to a "scrapers" topic

Wildcard rule under **Per-event Overrides → Wildcard rules**:

```
source.*  →  topic: seshat-scrapers
```

Every `source.goodreads_canary_failed`,
`source.metadata_cache_error`, etc. now fires to the `seshat-scrapers`
topic instead of the default `ntfy_topic`. Anything *not* under
`source.*` continues to use the default.

### Mute the ignored-books daily digest during quiet hours

`digest.daily_ignored` is already suppressible during quiet hours by
default — enabling the quiet-hours window in section 2 of the panel is
enough to mute it nightly without touching its per-event override. To
silence it entirely (not just at night), set its `Enabled` column to `Off`
in the per-event table.

### Bump the priority on `pipeline.error`

`pipeline.error` already defaults to priority 4 and bypasses quiet hours,
but if you want it pinned at the top of your phone's notification feed,
set its `Priority` column in the per-event table to `5`. The event keeps
its default tags and topic.

## Migration from pre-v2.28.0 operators

Operators upgrading from a Seshat version older than v2.28.0 don't need
to do anything. The legacy flat keys — `notify_on_grab`,
`ntfy_on_scan_complete`, `notify_on_metadata_cache_*`, etc. — still work as
a fallback. For each event:

1. If `notifications.master_enabled` is explicitly `false`, the event is
   suppressed.
2. If any routing rule (exact, wildcard, or `*`) supplies an `enabled` value
   for the event, that wins.
3. Otherwise, the bus consults the event's legacy key. Events flagged
   `legacy_requires_master` additionally require the
   `per_event_notifications` master toggle — preserving the pre-v2.28.0
   orchestrator gating model.

As soon as you configure a routing rule that covers a given event, the
legacy key is silently ignored for that event. Mixing legacy keys and
routing rules is safe — they don't fight, because legacy is consulted only
when routing is silent.

### The DEPLOY.md `notify_on_metadata_cache_*` block

DEPLOY's Step 6 still documents the pre-v2.28.0
`notify_on_metadata_cache_*` keys. Those keys continue to function as
described there. **Don't edit the DEPLOY block** — it stays as the legacy
escape hatch — but treat *this chapter* as the canonical home for new
routing decisions. Configure new behavior under `notifications.events`
instead of adding more flat `notify_on_*` keys.

## Related

- [`./metadata-cache.md`](./metadata-cache.md) — what triggers the
  `source.metadata_cache_*` and `source.goodreads_canary_failed` events.
- [`./hygiene-jobs.md`](./hygiene-jobs.md) — the hygiene jobs whose
  outputs surface through `source.*` and `digest.*`.
- [`./multi-author-and-series.md`](./multi-author-and-series.md) — series
  recomputation runs inside the scan/sync paths and surfaces (where it
  surfaces at all) through `discovery.scan_complete` and `sync.library`;
  there is no dedicated series event in the registry today.
