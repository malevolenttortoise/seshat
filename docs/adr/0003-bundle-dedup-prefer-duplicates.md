# 0003. Prefer duplicate children over losing a bundle

- Status: Accepted
- Date: 2026-05-11

## Context

A bundle torrent (e.g. a "Books 1–10" omnibus) can overlap books the user already owns or has in flight as separate grabs. Dedup logic that aborts on overlap would skip the *whole* bundle just because one child collides. Bundles are often the only path to acquiring most of the books in a series — losing the bundle to avoid a single duplicate is a net negative.

## Decision

- **Single-torrent dedup:** free to skip on conflict — the user can re-snatch if they really want it.
- **Bundle dedup:** evaluate the bundle as a whole against the announce-level dedup key (the primary work). Per-child overlap inside the bundle is acceptable; do **not** add logic that aborts the bundle or refuses individual children when they collide with existing books. The fan-out path (`_prepare_book` → N review entries) surfaces each child for review, and the user can dismiss duplicates at review time.
- **If a future feature wants per-child dedup,** it must *prefer* the bundle (the newer/larger superset) and *retire* the older standalone — never the reverse — and only when there is zero risk of data loss (e.g. identical file content hash).

## Consequences

- The system biases toward false positives (a couple of duplicate copies in Calibre) over false negatives (a lost bundle). This is the intended trade-off.
- Reviewers occasionally see duplicate children; this is expected, not a bug.

## Related

- [0004](0004-format-priority-dedup.md) — the format-priority dedup gate, which operates on single-format announces, not bundles.
