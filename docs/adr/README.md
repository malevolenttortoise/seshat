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
