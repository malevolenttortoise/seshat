# Phase 5b — Replacement opportunity enactment (file swap)

**Status:** Decisions LOCKED 2026-05-24. Ship target: **v2.27.0** (scope grew past patch with auto-enact + bulk-enact + restore all in 5b).

## Scope

Take a `replacement_opportunities` row with `status='detected'` and:

1. Verify the candidate is still actionable (path still exists, opportunity still gated allowed).
2. Move the lower-quality owned file out of the library to a soft-delete folder.
3. Remove the owned book row from the library (Calibre via `calibredb remove`, ABS via re-scan after file removal).
4. Mark opportunity `status='enacted'`.
5. Write an audit-trail row capturing what changed (file paths, sizes, timestamps).

**The candidate is already in the library** by the time the opportunity is detected — Send-to-Pipeline delivered it via the sink layer post-STATE_COMPLETE. So "swap" means *remove the owned duplicate*, not *move the candidate*. The library will then have only the higher-quality copy.

**The qBit-seeding copy is never touched.** That's the v2.26.0 safety guarantee: paths don't overlap with qBit's download folder. Defense-in-depth: re-verify at enact-time.

## Safety defaults (top-level requirement)

Phase 5b is a **destructive** feature. Every gate is **opt-in by default**; no destructive action can fire without the user explicitly enabling it for the specific library it affects.

| Setting | Default | Effect |
|---|---|---|
| `active_replacement_enabled_by_slug.<slug>` (v2.26.0, reused) | `false` per library | Master gate. False → no detection, no enact, no auto-enact, no restore for this library. |
| `active_replacement_auto_enact_by_slug.<slug>` (new in 5b) | `false` per library | When False, opportunities sit in 'detected' until user clicks Enact. When True, detection triggers immediate enact (still gated by the master + safety classification). |
| `active_replacement_soft_delete_retention_days` (new in 5b) | `30` | Days the moved owned file stays in `.seshat-replaced/` before the hygiene sweeper purges. Restore must run within this window. |

**Compound gate** for any destructive op: `library.enabled AND library.safety != OVERLAP AND (manual-click OR (library.auto_enact AND post-detection))`.

**No global "enable everything" toggle.** Per-library only — operator must explicitly toggle each library they want active replacement on. OVERLAP-classified libraries hard-disable regardless of the toggles.

### Settings UI exposure

All toggles live in **Settings → Active Replacement** (panel already exists from v2.26.0 — extending it):

- Existing per-library row gets a second toggle next to "Enable active replacement": **"Auto-enact detected upgrades"**. Disabled (greyed out) when master toggle is off. Confirmation modal on first enable, listing the destructive ops the user is opting into.
- Top of panel adds a section: **"Replaced-file retention"** with the days input (validates ≥ 1, default 30). One-line explanation below: "Owned files moved to `.seshat-replaced/` are purged after this many days. Restore must run within the window."
- Per-library row shows current state at a glance: badge for `safety`, badge for `enabled`, badge for `auto_enact`, count of `detected` + `enacted` opportunities.

The Upgrades page also gets a per-row indicator showing which gate each row hangs on so the user can see why an opportunity isn't actionable (e.g., "Library not opted in" or "Library marked OVERLAP").

## Locked decisions (2026-05-24)

1. **Soft delete, 30-day retention.** Owned file moves to `<library>/.seshat-replaced/<YYYYMMDD-HHMMSS>/<filename>`. Hygiene-job sweeper purges expired entries. Default retention configurable via `active_replacement_soft_delete_retention_days`.

2. **New `replacement_enactments` table** in main DB. FK'd to opportunities; preserves history across enact → restore → re-enact cycles. Shape:
   ```
   id, opportunity_id (FK), enacted_at, acted_by,
   owned_path_before, owned_path_after, owned_size_bytes,
   candidate_path, candidate_size_bytes,
   library_slug, owned_book_id_before,
   sink_result TEXT, restored_at, restored_by,
   failed_at, failed_reason
   ```

3. **Calibre lookup:** `calibredb list --search "identifiers:mam_torrent_id:<X>"` with path-search fallback for pre-Seshat books. No Calibre schema changes; no Seshat-side stamping at delivery time.

4. **ABS coordination:** file removal followed by triggering an ABS library scan via the existing API client. ABS is file-system-of-truth; its DB catches up on scan.

5. **API surface:**
   - `POST /api/quality/replacement-opportunities/{id}/enact` — single enact
   - `POST /api/quality/replacement-opportunities/enact-bulk` — multi-enact, body `{ids: [...]}`
   - `POST /api/quality/replacement-opportunities/{id}/restore` — single restore
   - Existing PATCH continues to reject `status='enacted'`.
   - All synchronous; return audit row(s) + updated opportunity(ies).

6. **Manual + auto-enact opt-in BOTH in this release.** Per-library `active_replacement_auto_enact_by_slug` setting (default all off). Auto-enact runs as a post-detection step that respects all the same safety re-checks as manual enact. UI toggle lives next to the existing safety badge in the Active Replacement panel.

7. **Bulk-enact ships in this release.** Multi-select on the Upgrades page + bulk action. Mixed safety: skip per-row unsafe entries, enact the safe ones, return per-item results. Partial-failure path matches Decision 9 (rollback per item).

8. **Restore ships in this release.** Inverse of enact: move soft-deleted file back to library, re-add via `calibredb add` / ABS scan, flip opportunity status back to `detected`. Audit row gets `restored_at` + `restored_by`. Gated by soft-delete file still existing (retention sweeper may have purged).

9. **Mid-enact failure rollback:** if sink-remove fails after the soft-delete has been performed, move the file back from `.seshat-replaced/` to original location, write audit row with `failed_at` + `failed_reason`, return HTTP 500. Opportunity status stays `detected` so user can retry once the sink is reachable.

10. **Soft-delete folder lives inside the library root**: `<library>/.seshat-replaced/`. Dotfile so library tools ignore it; lives with the data so backups capture it. Not configurable (one less knob).

## Phased plan

1. **Schema**: add `replacement_enactments` table + index on `(opportunity_id, restored_at)`. Migration entry in `app/database.py::MIGRATIONS`.

2. **Sink-inverse helpers**:
   - `app/sinks/calibre.py::CalibreSink.remove(book_id_or_path, metadata) -> SinkResult` wrapping `calibredb remove`.
   - `app/sinks/audiobookshelf.py::ABSSink.remove(path) -> SinkResult` — file-system delete + library re-scan.
   - Both must accept paths that may not exist (idempotency).

3. **Orchestrator entrypoint**: `app/orchestrator/active_replacement.py::enact_opportunity(opportunity_id) -> EnactmentResult`. Re-runs `is_replacement_allowed`, re-verifies path-overlap, performs the soft-delete + sink remove + DB updates inside one connection transaction.

4. **API endpoints**:
   - `POST /api/quality/replacement-opportunities/{id}/enact` → calls (3).
   - `POST /api/quality/replacement-opportunities/{id}/restore` → inverse.
   - Both return the audit row + updated opportunity.

5. **UI** (Settings → Active Replacement + Upgrades page):
   - Per-row **Enact** button on detected opportunities (with confirmation modal showing exact paths + sizes that will change).
   - Per-row **Restore** button on enacted opportunities (only while soft-delete file still exists).
   - Status badge updates.

6. **Retention sweeper**: hygiene-job step that walks `.seshat-replaced/` folders, removes anything older than `active_replacement_soft_delete_retention_days` (default 30).

7. **Tests**:
   - Path-overlap re-check still rejects at enact-time (gate-was-on-at-detect, gate-now-off scenario).
   - Soft-delete creates file in expected location, retains original mtime.
   - `calibredb remove` mocked: success path + library-not-found + book-not-found.
   - ABS re-scan triggered correctly.
   - Audit row captures all required fields.
   - Idempotent: enacting an already-enacted opportunity is a 409, not a duplicate file op.
   - Restore restores file + library row + opportunity status, audit row updated.
   - Retention sweeper purges expired soft-deletes.

## Open questions

1. **Two libraries with the same dedup_key.** If both libraries have an owned copy and the candidate is grabbed once, the detector records one opportunity per library. Enacting on library A doesn't affect library B. Confirm this is desired (it almost certainly is — per-library opt-in semantics).

2. **Pre-Seshat owned books with no `mam_torrent_id`.** The detector already handles them (owned_mam_torrent_id is NULL in the opportunity row). Enact must support path-based Calibre/ABS removal without an mam_torrent_id anchor.

3. **What about partial bundle candidates?** If the candidate grab is a bundle and the opportunity was for one book in that bundle, the bundle's other files are unrelated. Enact only touches the owned file in the library; the bundle itself stays seeding intact. Verify this is consistent with the per-book opportunity row shape.

4. **Soft-delete folder location**: inside the library root (e.g. `<library>/.seshat-replaced/`) keeps backups capturing it; outside keeps library-scanning tools from seeing it. Recommend inside + dotfile (most library tools ignore dotfiles).

5. **Failure mid-enact**: candidate move succeeded, sink remove failed (e.g. Calibre not reachable). Recommend: roll back the soft-delete (move back), mark opportunity status unchanged, return 500. Audit row records the attempted enactment with `failed_at` set.

6. **What if the candidate file itself has been moved/deleted** (e.g. user manually cleaned up the library)? Detect at enact-time, return 409 with detail, don't touch anything.

## Related code anchors

- v2.26.0 detection: `app/quality/replacement_detector.py::detect_for_grab`
- v2.26.0 safety gate: `app/orchestrator/active_replacement.py::is_replacement_allowed`
- v2.26.0 opportunities storage: `app/quality/opportunities.py`
- v2.26.0 router (rejects `enacted` PATCH): `app/routers/quality.py:174-203`
- Calibre sink (where remove inverse goes): `app/sinks/calibre.py:97-`
- ABS sink (where remove inverse goes): `app/sinks/audiobookshelf.py:40-`
- qBit path aliasing (already used by safety gate): see `feedback_seshat_qbit_path_aliasing`

## Observation period

v2.26.0 just shipped (2026-05-24). Recommend ≥1–2 weeks of detection-only running in Mark's prod before locking 5b. The audit goal is to confirm the detected opportunities are actually upgrades a user would want (no false positives, no surprising flags on bundle children, etc.) before any code can delete a file.
