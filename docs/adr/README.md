# Architecture Decision Records

Durable decisions about Seshat's architecture and engineering process. Each ADR captures the context that forced a choice, the decision itself, and the consequences — so the *why* survives even when the code changes.

New decisions get the next number; supersede rather than rewrite when a decision changes.

| ADR | Decision |
| --- | --- |
| [0001](0001-semver-policy.md) | SemVer policy: loose within v2.3, strict from v2.4.0 |
| [0002](0002-multi-library-slug-routing.md) | Per-book mutation endpoints must accept `?slug=` |
| [0003](0003-bundle-dedup-prefer-duplicates.md) | Prefer duplicate children over losing a bundle |
| [0004](0004-format-priority-dedup.md) | Format-priority dedup against owned + in-flight + held siblings |
| [0005](0005-backfill-attempted-set.md) | Backfill workers track an in-process attempted-set |
| [0006](0006-mam-not-found-is-permanent.md) | Treat MAM "not found in search results" as permanent |
| [0007](0007-development-main-release-flow.md) | All work on `development`; `main` gets merges + tags only |
| [0008](0008-book-authors-authoritative-on-reads.md) | `book_authors` authoritative on reads; backfill-all (not fallback) |
| [0009](0009-merge-union-prune-overlap.md) | Merge unions the contributor set; prune-linkage matches by overlap |
| [0010](0010-series-author-mode-taxonomy.md) | Series author_mode (per/multi/shared) by contributor-set intersection |
| [0011](0011-owner-incidental-on-read.md) | Owner-vs-incidental computed on read (count-equality), not persisted |
| [0012](0012-drop-books-author-id-position-0-canonical.md) | Drop `books.author_id`; position 0 is the sole canonical primary author |
| [0013](0013-claim-for-owned-contributor-aware.md) | Claim-for-owned is contributor-aware (announce-primary × owned-any-contributor) |
| [0014](0014-heal-contributors-on-scan-convergence.md) | Heal unowned discovered books' contributors on scan-convergence (union, owned-guarded) |
| [0015](0015-source-id-aware-author-identity.md) | Source-ID-aware author identity: persist co-author IDs fill-if-empty, match ID-first, surface conflicts, consolidate-by-ID |
