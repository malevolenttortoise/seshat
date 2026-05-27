# 0007. All work on `development`; `main` gets merges + tags only

- Status: Accepted
- Date: 2026-05-24 (effective from v2.23.0)

## Context

Before v2.23.0, every UAT-discovered bug produced a same-day hotfix tag committed straight to `main` — the v2.22.x arc shipped five tags in one day. User-facing version numbers stopped reflecting actual feature-delivery cadence. A permanent staging branch was wanted so UAT bugs land on a `:development-slim` image without rolling `:latest-slim` or burning version numbers.

## Decision

- All feature, bugfix, and hotfix work commits to the permanent **`development`** branch — never directly to `main`. The local working copy defaults to `development`.
- `main` only ever receives **merge commits via PRs from `development`**, plus version tags on those merge commits.
- Release sequence when asked to "tag/release vX.Y.Z":
  1. Confirm `development` is clean and pushed.
  2. Confirm release *shape* if ambiguous (e.g. a stray patch that could tag separately or roll into the minor).
  3. Draft the PR body (commit map + test plan + UAT notes), matching the most recent release PR's tone.
  4. Open the PR; the PR-body approval **is** the merge authorization — do not re-gate afterward.
  5. Merge with `merge_method=merge` (never squash/rebase) to preserve per-commit history.
  6. Tag + GitHub Release in one `POST /releases` call (`target_commitish=<merge_sha>`, `make_latest=true`).
  7. Fast-forward `development` to the merge commit so the branch banner stops showing "N commits behind."
  8. Verify the tag landed and CI triggered the version-pinned + `:latest-slim` builds.
- **`development` is never deleted.**

## Consequences

- Version numbers track feature delivery, not UAT churn. UAT bugs ship on `:development-slim`.
- CI image lineage: development push → `:development-slim`; PR merge to main → `:latest-slim` (once per release); version tag → `:vX.Y.Z-slim`.
- **Exception class:** structural/CI changes that must land on `main` to bootstrap a new flow (e.g. the commit that first added `development` to the workflow triggers). Rare; confirm before doing it.

## Related

- [0001](0001-semver-policy.md) — which release number a development arc becomes.
