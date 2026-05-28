# 0001. SemVer policy: loose within v2.3, strict from v2.4.0

- Status: Accepted
- Date: 2026-05-07

## Context

Seshat has no external consumers — nothing depends on its version range, so the version number's only real job is to narrate delivery. The question of how strictly to follow SemVer came up after v2.3.3 shipped, mid-way through the v2.3 initiative arc (dual-source-of-truth metadata + Series Manager + Metadata Manager + push-back).

## Decision

- **Within the v2.3 arc (v2.3.0 → v2.3.6):** loose SemVer. PATCH-level bumps even when adding new features, because the arc is one coherent initiative and the v2.2 → v2.3 MINOR bump already signalled "new initiative." Splitting the story across v2.3 and v2.4 mid-arc buys nothing.
- **From v2.4.0 onward:** strict SemVer 2.0.0 — MINOR for backwards-compatible new features, PATCH for bug fixes/regressions only.
- **Borderline "feature or fix?" cases:** default to MINOR. Over-bumping costs far less than under-bumping a real feature.
- **Don't renumber retroactively.**
- **Tags don't move.** One documented exception (v2.12.1, 2026-05-14): a tag pointing at a commit whose CI image build failed, with no downstream consumer, was force-moved to the fix commit. The rule for future failed-build tags: if no deployment occurred and the fix is <1h out, move the tag and document it in the annotated message; otherwise bump to the next patch.

## Consequences

- Version history stays narrative and auditable; readers can trust that v2.4.0+ MINOR/PATCH semantics are meaningful.
- The "tags don't move" default keeps GHCR digests and Unraid pulls reproducible. Any exception must be recorded in the tag's annotated message.

## Related

- [0007](0007-development-main-release-flow.md) — the release flow that places these tags.
