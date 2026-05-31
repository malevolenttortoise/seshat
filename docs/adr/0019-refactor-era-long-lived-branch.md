# 0019. Refactor eras use a long-lived scope-bounded branch

- Status: Accepted
- Date: 2026-05-31 (effective from v3.5.x refactor era kickoff)

## Context

[ADR-0007](0007-development-main-release-flow.md) established the default branch flow: all work lands on `development`, `main` receives only PR merge commits + version tags. That flow has carried Seshat from v2.23.0 through v3.4.1 with a single permanent staging branch — every UAT-discovered bug rolls into `:development-slim` without inflating version tags, every release ships through a single `development → main` PR.

ADR-0007 envisioned **sequential** arcs: one minor at a time, each arc's scope cleanly delimited by its release. That assumption breaks down for the v3.5.x **refactor era**:

| Property | Sequential arcs (v3.0.0 through v3.4.1) | Refactor era (v3.5.x) |
|---|---|---|
| Duration | Days to weeks per release | Weeks to months across multiple releases |
| Per-slice scope | New behavior, contained to its arc | Restructuring existing code, touches large files repeatedly |
| Conflict surface with parallel work | Low — new code in new files | High — same files (`lookup.py`, `_merge_result`) other work may also touch |
| Failure mode if slice misjudged | Revert the slice's commits on `development` | Same — but a long-lived series of slices makes "abandon the whole era" a meaningful escape hatch |
| Parallel-work blocking | No — sequential by design | Yes if single-branch flow is enforced: hotfix on a refactor-in-flight file races every sync |

The v3.5.0 first slice targets `app/discovery/lookup.py` — Seshat's largest backend file (4216 LOC) carrying 8 ADR cross-references (0008/0009/0012/0014/0015/0016/0017/0018). Any hotfix to that file landing on `development` mid-slice would conflict with every subsequent slice's structural changes. Forcing serialization (freeze `development` for the era's duration) blocks all other work and is unacceptable. Forcing merge-debt (carry refactor changes on `development` while accepting parallel work) makes the conflict-resolution burden cumulative — by slice 3, every sync is a manual rebuild.

Mark named the safety concern explicitly during the v3.5.x kickoff grill (2026-05-31): "if severe issues arise other work can still be worked on the development branch while this is happening." The risk surface for a refactor era is exactly the asymmetric case ADR-0007 doesn't cover.

## Decision

Refactor eras use a **dedicated long-lived scope-bounded branch** parallel to `development`, released through `main` directly.

1. **Branch name and lifecycle.** The era's branch is `refactor/<version-line>` — for the v3.5.x era, `refactor/v3.5.x`. Cut from `main` at the era's start SHA. Retired (deleted) at era close. Scope-bounded by version-line, NOT by individual slice — every slice in the era lands on the same branch.

2. **Slice landing pattern.** Each AFK structural slice lands as a PR from a feature branch (e.g. `refactor/v3.5.0-merge-books`) into `refactor/v3.5.x`. The HITL converge slice runs against `refactor/v3.5.x` HEAD. The v3.5.x release ships as a PR `refactor/v3.5.x → main` (NOT through `development`), tagged on `main`.

3. **Post-release sync.** After each v3.5.x release merges to `main` + tag lands, **both** `development` AND `refactor/v3.5.x` fast-forward to `main`. Refactor branch stays one step ahead of `main` for the next slice's baseline; `development` stays aligned with `main` per existing release flow.

4. **In-flight sync direction: `development → refactor/v3.5.x` only, on-demand, via merge commit (not rebase).** Trigger before each new slice's first issue (resets baseline), or when accumulated conflict makes the next sync painful. Rebase is rejected because ADR-0007 commits to merge-method-merge for per-commit history preservation; that property must hold for the refactor branch too. The merge direction is intentionally one-way: refactor branch absorbs `development`'s changes (so it can ship correctly merged code to `main`), but does NOT push refactor work onto `development` mid-era (that would defeat the isolation point).

5. **CI image tagging.** A new image tag `:refactor-slim` rolls on every push to `refactor/v3.5.x`. Lets Mark UAT the refactor branch in a dedicated container without affecting `:development-slim` (his dev-stack baseline) or `:latest-slim` (his prod baseline). One workflow line addition; reused for any future refactor era (the tag is generic, not era-specific).

6. **Rollback paths.** One slice failing HITL converge: revert the offending commits on `refactor/v3.5.x`, era continues. Entire era turning out to be a mistake: abandon `refactor/v3.5.x` outright. Zero impact on `main`/`development` in either case — this isolation is the ADR's load-bearing safety property.

This is an **explicit exception to ADR-0007 for refactor eras only**. The default rule (all work on `development`, `main` gets merges + tags only) remains for every non-refactor minor. The exception's trigger is the version-line shape: `refactor/v<MAJOR>.<MINOR>.x` is the era marker; absence of a `refactor/...` branch means the default ADR-0007 flow.

## Consequences

- **Non-refactor work unblocked.** Hotfixes, non-refactor minors, and feature work continue on `development → main` during the refactor era. The two branches are intentionally independent; Mark can release a v3.6.0 feature minor from `development` mid-refactor-era without touching `refactor/v3.5.x`.
- **Merge debt is real, paid by the refactor branch, bounded by sync cadence.** Every change `development` makes to a file `refactor/v3.5.x` is restructuring will conflict on the next sync. Cost is per-conflict, not per-commit — small targeted hotfixes are usually mechanical to merge; arc-scale parallel work on the same files would be painful. The refactor era's scope discipline (one subsystem at a time) keeps the conflict surface bounded.
- **`seshat-release` skill behavior for v3.5.x releases changes the source branch.** Step 3 ("PR draft") opens from `refactor/v3.5.x` not `development`. Step 8 ("sync `development` to `main`") additionally fast-forwards `refactor/v3.5.x` to `main`. The skill's other steps (tag, GitHub Release, CI verification) are unchanged.
- **Two deployment images during the era.** `:refactor-slim` for refactor UAT, `:development-slim` for parallel `development` work, `:latest-slim` for prod. Mark's existing Unraid → Force Update model handles all three.
- **Era end retires the branch.** At v3.5.x close (the end-of-era review decides this per the PRD's Q2), `refactor/v3.5.x` is deleted from origin. Its history is preserved in `main` via the v3.5.x release merge commits.
- **Default flow unaffected for everything else.** Non-refactor work doesn't see this ADR. Only future refactor eras (if any — Mark may never run another) inherit the pattern. The CI workflow line addition stays in place permanently as cheap infrastructure for that possibility.

## Supersedes / superseded by

- Carves out an **explicit exception** to [ADR-0007](0007-development-main-release-flow.md) for refactor eras only. ADR-0007 remains the default rule; ADR-0019 applies only when a `refactor/<version-line>` branch exists.
- Builds on [ADR-0001](0001-semver-policy.md)'s strict SemVer (the refactor era ships as v3.5.x minors, may or may not eventually promote to v4.0 — see the v3.5.x PRD §1 cadence decision).
