# 0027. Settings keys no code knows are swept on load; a registry and a CI scan keep "known" true

- Status: Accepted
- Date: 2026-10-09 (2026-10 audit, wave 5a, issue 25; G10, G13, G126, G127)

## Context

`settings.json` only ever grew. `load_settings` merges the file over `DEFAULT_SETTINGS`, so a key the code stopped
reading stayed on disk forever: prod had 205 keys, 27 of them unknown to the defaults and the runtime-state list. 25
of those were dead (the per-source `<source>_enabled` / `rate_<source>` keys Phase 7 folded into `metadata_sources`,
`policy_lookup_torrent_info`, three `pipeline_*_enabled` toggles, `weekly_audit_*`, …), but 2 were live state written
by background code outside both lists: a one-time backfill's sentinel (`v2_12_1_dual_row_backfill_done`; dropping it
re-runs the backfill at the next boot) and the cache worker's stall debounce (`metadata_cache.<source>.stall_notified_at`).

Two settings were also dead in the code itself and part of observable shapes:

- `mam_economy_fl_wedge_offer_enabled` showed a "buy personal FL" checkbox until 2026-10-07, when MAM turned out to
  refuse `spendtype=personalFL` via the API. Kept in `DEFAULT_SETTINGS` "so existing settings files still load", it was
  still in `GET /api/v1/economy/config`'s response and its PATCH list.
- The `sync.mam_cookie_rotated` notification event (legacy key `ntfy_on_mam_cookie_rotated`) was listed on the
  Notifications page but nothing ever emitted it; MAM rotates the cookie about 430 times in 72 hours, so wiring it
  would be noise.

## Decision

1. **Retire both.** `mam_economy_fl_wedge_offer_enabled` leaves `DEFAULT_SETTINGS`, the `/economy/config` GET and PATCH
   key list and the frontend type. The `sync.mam_cookie_rotated` event leaves the registry (so the Notifications page and
   `/api/v1/notifications` no longer list it) with its toggle and `ntfy_on_mam_cookie_rotated`.
2. **`load_settings` sweeps unknown top-level keys**, after the legacy-shape migrations have read theirs, and saves.
   "Known" = `DEFAULT_SETTINGS` ∪ runtime keys written by background code ∪ read-only knobs (read with a default,
   written by nothing) ∪ the secret keys whose plaintext copies the boot migration moves ∪ keys a legacy migration reads
   ∪ fnmatch patterns for keys built at run time (`config.is_known_settings_key`). Nested dicts (`metadata_sources`,
   `metadata_cache`, …) are not swept.
3. **Nothing is swept before `metadata_sources` exists in the file**: an install old enough not to have it still needs
   its legacy per-source keys for the startup migration that builds it, which runs after the first load.
4. **Before the first sweep that drops anything, the file is copied to `settings.json.pre-sweep-<build>`** (the image's
   git SHA, else the date), once per build; each dropped key is logged at INFO.
5. **"Known" is kept true by CI** (`tests/test_settings_registry.py`): a static scan of `app/` finds every key read or
   written on `load_settings()` (or a name bound to it), resolving module constants, local strings, f-strings (as
   patterns), ternaries and loops over literal tuples, and fails on a key the registry doesn't know. A site whose key it
   can't resolve must be listed with the test that covers its keys (the event legacy keys, the runtime-state keys, the
   economy key lists, the secret keys).

## Consequences

- **Observable changes:** `GET /api/v1/economy/config` no longer returns `mam_economy_fl_wedge_offer_enabled` (a PATCH
  naming it is ignored, as for any unknown key); the "MAM cookie rotated" toggle and event are gone; on the first start of
  this version a typical install loses its dead keys (prod: 205 → 179) and gains one backup file.
- **Downgrade:** an older image reading a swept key gets its default (Mark accepted, G10); the backup holds the old file.
- **New code must register a settings key** it writes outside `DEFAULT_SETTINGS`, or the test fails; a key that slips
  past the scan (a key from data it can't follow) is deleted on the next load. Write keys through `DEFAULT_SETTINGS`
  where possible.
- Found by the scan: `reingest` read `ebook_format_priority`, a key nothing writes (the pipeline uses
  `mam_format_priority`), so a multi-format reingest ignored the ebook format priority. Fixed with this ADR (G134).

## Related

- [0001](0001-semver-policy.md) strict SemVer (the response-shape change needs this ADR). Issue
  `.scratch/codebase-audit-2026-10/issues/25-settings-retire-and-sweep.md`; findings L5-02.
