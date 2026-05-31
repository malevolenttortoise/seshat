# Active replacement

Active replacement is the opt-in upgrade path: when a freshly-grabbed torrent scores higher quality than an owned book of the same work, Seshat can soft-delete the owned file and let the library app pick up the higher-quality grab through its normal ingest. The old file is moved aside — not destroyed — so the operator can reverse the swap within a retention window. The whole feature is off by default and turned on per [library](../../CONTEXT.md#library-identity-and-sync); the rest of this chapter explains the gates, the multi-axis quality comparison that produces an "opportunity," the soft-delete layout, the sink-specific paths, and how to restore.

This is post-v2.27.0 behavior. Pre-v2.27.0 installs had no replacement path at all, so an upgrade introduces the feature in its disabled-by-default state — no existing library starts replacing on its own.

## What replacement is, and why it's opt-in

Replacement only fires when both sides exist: an owned book in a library Seshat manages, and an incoming grab that the [quality scorer](#how-quality-is-scored) ranks strictly better. The work of "is this the same book?" uses the same [dedup key](../../CONTEXT.md#bundles-and-dedup) the rest of the pipeline uses, scoped to the library's content type. The scorer's tuple comparison is strict — ties don't qualify — so a candidate has to actually beat the owned edition on a real axis (better tier on format, higher bitrate band, more channels) before an opportunity is even logged.

The feature is opt-in because the destructive step touches the on-disk file the library reads. Even though the move is reversible during the retention window, the library app (Calibre, CWA, or Audiobookshelf) sees the row disappear and the new row arrive — there is no in-place edit. Operators choose to enable that per library because the right answer depends on workflow: a library where a human curates edition choices in Calibre should not be replaced automatically; an archival audiobook library where "newest highest-bitrate copy wins" is the policy benefits from auto-enact.

## How quality is scored

Replacement decisions sit on top of Seshat's quality model, which generalizes the v2.9.0 format-priority rule into a tuple-comparison over an ordered list of axes. The rule is "lower tuple wins, lexicographic." The [format-priority](../../CONTEXT.md#quality-and-replacement) axis is always primary; numeric axes act as tiebreakers when format ties.

For audiobooks the default axis stack is:

1. **Format** — the per-library format-priority list (e.g. `m4b > mp3` for the audiobook profile).
2. **Audio bitrate (kbps)** — banded into tiers; rip-quality matters but encoder-specific 256-vs-320 micro-differences don't.
3. **Audio channels** — stereo beats mono.

For ebooks the format axis is the entire ranking — there are no numeric axes, because the per-edition snapshot Seshat captures has nothing useful below the format level.

Snapshot inputs come from a single per-torrent row in `torrent_quality_metadata`, populated at grab time from MAM's `mediainfo` payload and the tags line, plus general fields (file count, byte total, seeders) that survive across both content types. The raw mediainfo and tags blobs are persisted alongside the parsed columns so future axes can be added without re-fetching MAM. Missing axis data ranks one step worse than the worst declared tier — known data always beats unknown — but a fully-unknown candidate never silently outranks a fully-known incumbent. The table is named so operators can inspect it directly via Database Manager when a scoring decision needs auditing; you should never need to edit it by hand.

Per-library overrides are merged on top of the global profile. An override that defines `format_priority.audiobook` for one library replaces the global audiobook list outright for that library; one that omits a key falls through. The settings UI surfaces this under the **Quality Metadata** panel; the **Active Replacement** panel only governs whether scored opportunities act on a library, not how they're scored.

## Safety gates

Before an opportunity can become an enactment, three gates must pass — re-evaluated at enact time, not just at detection.

**The library safety classification.** Seshat compares the library's on-disk root against its view of qBit's download path (`local_path_prefix`, the same value the [path-aliasing](../../CONTEXT.md#cross-cutting) translation uses). The classification is:

| Classification | Meaning                                                                                          |
| -------------- | ------------------------------------------------------------------------------------------------ |
| `safe`         | Both paths exist and neither is a prefix of the other. Per-library opt-in decides.               |
| `overlap`      | One path is a subpath of (or equal to) the other. Replacement is hard-disabled regardless of the toggle. |
| `unknown`      | One or both paths are missing/unresolvable. Per-library opt-in decides; the UI surfaces a warning. |

Overlap matters because the on-disk file that backs an owned book *is* the file qBit is seeding when the library reads from the download folder. Soft-deleting that file would break the seed and de-sync the torrent. Seshat refuses to act in that case even if the toggle is on; the operator has to fix the path layout (separate library tree from download tree) before the gate will pass. `unknown` exists for setups where Calibre and ABS containers mount the host directory at a different container path than Seshat sees — Seshat can't see across container boundaries, so it accepts the operator's attestation when the toggle is enabled, with a UI warning.

**The per-library opt-in.** A `slug`-keyed map (`active_replacement_enabled_by_slug`) decides whether opportunities can be enacted in that library at all. Default false everywhere. Toggling this on enables detection + manual enact buttons.

**The per-library auto-enact opt-in.** A second `slug`-keyed map (`active_replacement_auto_enact_by_slug`) controls whether a freshly-detected opportunity also acts on its own without operator intervention. Default false. The auto-enact gate is compound: the master gate must also pass, so flipping master off implicitly disables auto-enact for that library; the UI clears the orphaned bool so the row doesn't reactivate when master is flipped back on.

The detection-time check uses the same master gate. An opportunity is only ever inserted for a library where the master gate passes at the moment a grab completes — but the gate is re-checked at enact time, so a toggle flipped off between detection and a manual enact click blocks the enact cleanly rather than acting on stale permission.

## The soft-delete model

When an opportunity is enacted (manually or via auto-enact), Seshat performs the destructive step in this order:

1. Re-validate the opportunity (status still `detected`, master gate still true, library still discovered).
2. Resolve the owned book's on-disk directory: Calibre via a read-only join against the library's `metadata.db`; ABS via the `/api/items/{id}` endpoint.
3. Create a timestamped subdir under `<library_path>/.seshat-replaced/<YYYYMMDD-HHMMSS>/` and `shutil.move` the owned book's directory into it.
4. Record a row in `replacement_enactments` (the audit log) with both the before and after paths and the owned size in bytes.
5. Call the appropriate sink's `remove()` to drop the now-missing row from the library app's database.
6. On sink success: flip the opportunity to `enacted` and write the sink's reply text into the audit row.

If the sink call fails after the soft-delete landed, Seshat rolls back: the directory is moved back to its original location, the audit row is stamped `failed_at`/`failed_reason`, and the opportunity stays `detected` so the operator can retry once the sink is reachable.

The `.seshat-replaced/` directory lives inside the library root with a dot prefix so backup tools capture it but library-scanning tools ignore it. Each enact gets its own timestamp folder; multiple enacts on the same library are independently distinguishable and independently restorable. Tooling on top should not consolidate them — the audit row's `owned_path_after` points at the timestamped subdir as a stable URL, and a flat layout would break it.

## Sink path divergence

The "tell the library app the file is gone" half of an enact is sink-specific because the three sinks have radically different interfaces.

| Sink                 | Used by                                              | Remove path                                                                                  |
| -------------------- | ---------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `CalibreSink`        | Full image (calibredb on PATH)                       | `calibredb remove --library-path <root> <book_id>` against the Calibre library directly.     |
| `CWASink`            | Slim image (no calibredb; CWA admin creds configured)| HTTP DELETE against Calibre-Web-Automated's admin API using `cwa_base_url` + `cwa_username`. |
| `AudiobookshelfSink` | Audiobookshelf libraries                             | Filesystem move (already done in step 3) + `POST /api/libraries/<id>/scan` so ABS reconciles.|

Sink selection for Calibre prefers `calibredb` when it's on PATH and falls back to the CWA admin API when it isn't. Slim-image deployments (Mark's prod and any operator running the no-Calibre container) route exclusively through CWA — so a slim install with `cwa_base_url` or `cwa_username` blank produces a `no_sink` result and refuses to act. The full image will pick `calibredb` unconditionally; CWA is not consulted there.

The CWA path additionally requires the per-library books row to carry a `calibre_id`. Pre-Seshat owned books without a calibre_id (rare, but possible for libraries restored from older Seshat installs) trigger a `failed` result with a clear reason and a soft-delete rollback. The full-image `calibredb` path can fall back to an `identifiers:mam_torrent_id:<id>` search when the calibre_id is missing.

ABS has no per-book "delete this item" API — the filesystem is authoritative, and a scan reconciles the database — so the `remove()` for that sink is essentially the scan trigger; the actual delete happened in step 3.

## Reversibility — how to restore

Restore is the inverse of enact, available while the timestamped directory still exists under `.seshat-replaced/` and before the retention sweep purges it. There are two ways in.

**From the UI.** Open Settings → Active Replacement → **Detected Opportunities** and click **View**. On the **Enacted** tab, each row carries a **Restore** button. The flow is:

1. Look up the most recent active enactment for that opportunity.
2. Move the directory from `owned_path_after` back to `owned_path_before`. If the original path is now occupied (a manual reconcile happened in the meantime), the restore refuses and surfaces a "manually inspect before retrying" error rather than overwriting.
3. Re-register with the library app: Calibre/CWA call `sink.deliver()` with the first file in the restored directory, and ABS triggers a rescan.
4. Stamp `restored_at` on the audit row and flip the opportunity back to `detected`.

The opportunity goes back to a state where the operator can either enact again later (e.g. after the candidate's quality issue is investigated) or **Dismiss** it to stop the row from re-suggesting itself.

**By hand.** If the UI flow is unavailable (e.g. the library has since been removed from settings), the recipe is:

1. Find the timestamped directory: `ls <library_path>/.seshat-replaced/`. The format is `YYYYMMDD-HHMMSS` local time; the matching audit row's `owned_path_after` is the exact location.
2. Move the book directory back to where it was: `mv <library_path>/.seshat-replaced/<ts>/<book_dir> <original_path>`. The `owned_path_before` field in `replacement_enactments` carries the original path verbatim.
3. Trigger a library scan so the app re-registers the file. For Calibre / CWA that's a normal **Reingest** through the operator's usual ingest path; for ABS hit "Scan library" in the ABS web UI or POST `/api/libraries/<id>/scan` directly.

A hand restore does **not** stamp `restored_at` on the audit row, so the row stays "active" from Seshat's point of view until the operator either marks it manually in the database or accepts the inconsistency. Prefer the UI flow when it's available.

## Retention window and the hygiene sweeper

Soft-deleted directories don't live forever. The retention window is controlled by the `active_replacement_soft_delete_retention_days` setting (default **30 days**, range 1–3650, global — not per-library); the hygiene sweep enforces it.

The sweep is the **Soft-delete retention sweep** job in [`./hygiene-jobs.md`](./hygiene-jobs.md) — see that chapter for the job's catalogue entry and run cadence. The sweep walks every discovered library's `.seshat-replaced/` directory, parses each timestamped subdir's name back into a unix time, and `shutil.rmtree`s any subtree older than the retention window. Malformed directory names (anything that doesn't parse as `YYYYMMDD-HHMMSS`) are skipped, not purged — operators occasionally create their own folders here and the sweep won't blast them. The audit row in `replacement_enactments` survives the purge; its `owned_path_after` becomes a dangling pointer, and the restore endpoint reports "soft-delete file is gone (retention sweeper may have purged it)" when it's invoked against a purged enactment.

Retention is measured against the directory's name (the time the enact happened), not against the file's mtime — operators copying these directories around for backup won't accidentally extend retention.

## Operator settings reference

Everything user-facing lives under **Settings → Pipeline → Active Replacement**.

- **Detected Opportunities** — running count of detected / dismissed / enacted opportunities, plus a **View** button that opens the queue page. The queue page is where per-row Enact, Bulk Enact, Dismiss, and Restore actions live; it's also reachable directly via the navigation menu.
- **Replaced-file retention** — the retention window in days (default 30). Hand-edited `settings.json` values below 1 are clamped at runtime back to the default rather than silently accepting zero retention.
- **Per-library Safety + Opt-in** — one row per discovered library. Each row shows the library name, content type, on-disk path, the safety classification badge (SAFE / OVERLAP / UNKNOWN), the master toggle (Enable replacement), and the auto-enact toggle. Overlap rows render the toggles in a disabled state with the hard-block reason visible. The first time the auto-enact toggle is enabled for a library, a confirmation modal explains that detected opportunities will fire without further operator action.

Operators driving the queue page directly (without the settings panel) should be aware that an opportunity's status transitions are: `detected → enacted` (Enact), `detected → dismissed` (Dismiss), `dismissed → detected` (Restore on a dismissed row), `enacted → detected` (Restore on an enacted row, with file move). The two Restore actions are different code paths; the queue page surfaces both as a button labeled **Restore**.

Active replacement does not currently emit dedicated [notification](./notifications.md) events — opportunities are surfaced through the queue + the count badge on the settings panel rather than through the notification routing system. The hygiene sweep contributes its purge count to the standard hygiene toast (`-N expired soft-deletes`) at the end of every hygiene run; that toast is also the only routine signal that retention is doing work.
