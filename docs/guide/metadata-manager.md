# Metadata Manager

Metadata Manager is the review surface for **proposed changes to owned
books** — per-field diffs that the dual-source-of-truth pipeline has
flagged for an operator decision rather than writing through silently.
You find it under **Discovery → Tools → Metadata** in the sidebar.

A scan that touches an owned book never overwrites Calibre or
Audiobookshelf directly. Discrepancies get parked in the
`metadata_review_queue` table; you approve or dismiss them here, and
approval triggers a [push-back](../../CONTEXT.md#output--integration)
to the authoritative library.

This chapter explains the queue model, the v3.3.0 **authors
proposed-change** flow (the first set-shaped diff in the queue), the
end-to-end approve → push → re-sync chain, and the field-type filter
chips above the list. It assumes you've read
[multi-author-and-series.md](./multi-author-and-series.md) for the
underlying contributor model — terms like `book_authors`, primary
author, contributor set, and union-on-merge are used here without
being redefined.

## Where it sits in the UI

Sidebar: **Discovery → Tools → Metadata**. The page is titled
**Metadata Manager** and carries five tabs:

| Tab | Source(s) | Shape |
|---|---|---|
| **Calibre** | `calibre` | Queue (field diffs) |
| **Audiobookshelf** | `abs` | Queue (field diffs) |
| **Source scans** | `goodreads`, `hardcover`, `kobo`, `ibdb`, `google_books`, `amazon`, `audible` | Queue (field diffs) |
| **Series moves** | legacy `book_series_suggestions` | Series consensus |
| **Pending manual edits** | local `user_edited_fields` | Push-back staging |

The three queue-shaped tabs (Calibre / Audiobookshelf / Source scans)
share the same `QueuePanel` component and the same field-type filter
chip row. **Series moves** and **Pending manual edits** are a different
data shape and don't carry the chips — they're surfaced here so the
older Suggestions and pending-edits pages can retire under one roof.

Across all five tabs, a row is a *proposal*, not a write. Until you
accept it, nothing has changed.

## The review queue at the operator level

The three queue-shaped tabs all read from `metadata_review_queue`,
filtered to the tab's `source` set. Each row carries:

- **book_id** — the owned book the proposal targets
- **field** — the column being proposed (`description`, `isbn`,
  `cover_url`, `pub_date`, `expected_date`, `page_count`, or the new
  set-shaped `authors`)
- **old_value** / **new_value** — current vs. proposed; TEXT for scalar
  fields, JSON for set-shaped fields
- **source** — who proposed it (`calibre`, `abs`, or a scan source like
  `goodreads`)
- **proposed_at** — when it was enqueued

The table has a UNIQUE constraint on `(book_id, field, source)`, so a
fresh scan UPSERTs over a prior pending proposal rather than piling up
duplicates. One source has one opinion about one field of one book at
any given time.

### Where rows come from

- **Calibre / ABS tabs** — surfaced by the sync paths
  (`calibre_sync`, `audiobookshelf_sync`) when an operator manually
  edited a field upstream that Seshat hasn't matched yet. These are
  the "I changed something in Calibre, do you want to pick it up?"
  diffs.
- **Source scans tab** — surfaced by `_merge_result` when a scan
  converges on an owned book and the source disagrees with what's
  stored. These are the "an external source thinks this is wrong"
  diffs.

### Approve / dismiss

Per row: **Accept** writes the proposal through, **Reject** deletes the
queue row without writing. Both are hard-deletes — the table has no
status column, accept/reject doesn't retain history. Selecting multiple
rows enables **Accept all** / **Reject all** at the top of the list;
the bulk endpoint returns per-id success so a single failing row
doesn't abandon the rest.

For scalar fields, **Accept** writes `new_value` to the books column,
adds the field to that book's `user_edited_fields` set, and deletes the
queue row. The actual push to Calibre / ABS happens later through the
**Pending manual edits** tab — scalar accepts stage the edit; they
don't push it.

The **authors** field is different: accept runs the full chain in one
step. See the next section.

## Authors proposed-change (v3.3.0)

The headline change in v3.3.0 is that **authors** is now a proposable
field. It's also the first *set-shaped* row in `metadata_review_queue`,
which forces a few semantic decisions worth understanding.

See the [Authors proposed-change](../../CONTEXT.md#discovery-review-surface)
glossary entry for the one-line definition. The mechanics below cover
how it behaves in this UI.

### When a proposal gets enqueued

A scan-converged source path
([`_merge_result`](../adr/0009-merge-union-prune-overlap.md) MATCH
branch against `owned=1`) checks the source's role-filtered contributor
set against the book's current `book_authors` and enqueues a proposal
when **either** of these holds:

- **`source ⊄ current`** — the source proposes at least one contributor
  the book doesn't have, **or**
- **`source[position 0] ≠ current[position 0]`** — the primary author
  differs, even if the sets are otherwise equal.

A pure subset (`source ⊆ current` with the same primary) is **skipped**
— Calibre is authoritative on whether a contributor still belongs, so
removals never enter the queue. Cosmetic reorderings that leave the
primary alone are skipped too.

### Source-quality filter

Only **Goodreads, Amazon, Hardcover, Audible** enqueue authors
proposals. Link-only sources (Google Books, OpenLibrary) have weak
author data and never propose. **MAM is excluded** despite having
authoritative `author_info` for grabbed torrents — see *Deferred work*
at the end of this chapter.

### Additive-only union

The proposal's `new_value` is the **union** of the source's contributors
and the book's current ones, never a replacement. Ordering is fixed:

1. Source's primary first (position 0).
2. Source's remaining contributors, in their order.
3. Current contributors not in the source set, appended in their
   existing order.

The two consequences worth internalizing:

- **Approving a proposal never silently removes a Calibre-asserted
  contributor.** If you want to remove a contributor, do it as a
  hand-edit in Calibre — the queue is not the path.
- **Position 0 after approve = source's primary.** A pure primary-swap
  proposal (e.g. source says `[Y, X]`, current is `[X, Y]`) unions to
  `[Y, X]` — Y is now primary, X stays as a co-author. The multi-author
  display knock-on effects (sort key, library display when only one
  author shows, the "N of M" pill) all follow from there; see
  [multi-author-and-series.md](./multi-author-and-series.md) for how
  the primary surfaces across the rest of the app.

The union semantics here are the operator-reviewed counterpart to the
unattended merge union in
[ADR-0009](../adr/0009-merge-union-prune-overlap.md) — same "never
silently lose a real co-author" principle, applied to the owned-book
write-back path.

### Payload shape

Both `old_value` and `new_value` are JSON arrays of contributor
records:

```json
[
  {"name": "Author X", "source_id": "abc123"},
  {"name": "Author Y", "source_id": null}
]
```

`source_id` is namespaced to the proposal's `source` column — a
Goodreads proposal carries Goodreads IDs, an Amazon proposal carries
ASINs. `old_value` snapshots `book_authors` at proposal time and always
carries `source_id: null` (the IDs come from sources; the snapshot is
how the book looks locally). The snapshot is not live-refreshed
between proposal time and accept time; if the book drifts in the
meantime, dismiss and let the next scan re-propose.

Carrying source IDs alongside names lets the inline re-sync (below)
go through ID-aware author identity rather than name-only matching —
see [ADR-0015](../adr/0015-source-id-aware-author-identity.md) and
[multi-author-and-series.md](./multi-author-and-series.md) for why
that matters for cross-library person consolidation.

### Render

Authors proposals render as an additive list-diff with per-row visual
states:

- **Added** — appears in `new_value` but not `old_value`. Tagged
  `(new)`.
- **Position 0 change** — marked `← primary` on each side.
- **Unchanged** — plain.

The proposal's `source` column already provides provenance, so
per-author source IDs are hidden from the diff UI by default — they're
load-bearing for the re-sync, not for the operator's scan-read.

## End-to-end approve flow

Approving an authors proposal runs the full chain in one step. No
intermediate "approved locally, pending push" state exists for authors
— `book_authors` is downstream of the upstream library's authorship,
so a local-only authors write would immediately drift.

The chain:

1. **Sink dispatch.** The backend reads the book and pushes the
   position-ordered names to every applicable sink. Selection is
   per-book, not per-library-mode: if the book has a `calibre_id` the
   Calibre route fires; if it has an `audiobookshelf_id` the ABS route
   fires; both fire for a dual-library co-owned book.

2. **Sink-specific formatting.** Each sink formats the name list its
   own way:
   - **calibredb** (full image): `--field authors:"X & Y"` —
     ampersand-separated per Calibre's canonical form. The shared
     `_format_calibredb_authors` helper handles commas inside names
     (`"Smith, John"`) safely.
   - **CWA admin form** (slim image): sets the `authors` field on the
     merged form dict and submits through the existing scrape-and-post
     pattern.
   - **ABS PATCH**: `[{"name": "X"}, {"name": "Y"}]` array on the
     PATCH body. ABS reuses existing author IDs by name and creates
     missing ones on its side.

3. **Inline re-sync.** After the upstream push succeeds, the same
   request immediately:
   - Resolves each `(name, source_id)` from the payload via
     `resolve_or_create_author` — mints missing author rows; captures
     source IDs on new rows so future scans use ID-first matching.
   - Calls `write_book_authors(db, book_id, ordered_ids)` to overwrite
     the book's `book_authors` rows in position order.
   - Recomputes `series.author_mode` for the affected series (lazy
     import of `_recompute_series_author`), so the taxonomy stays
     consistent with the new contributor set
     ([ADR-0010](../adr/0010-series-author-mode-taxonomy.md)).

4. **Queue cleanup.** Deletes the queue row. The operator sees a
   consistent post-approve state on the next page load.

The push and the re-sync share the same primitives as the full library
sync, so there's no risk of divergence between an approve-driven write
and a scheduled sync touching the same book later.

### Push-back routing

The sink chain depends on which image you're running:

- **Slim image** — Calibre route resolves to **CWA** directly. The
  full-image `push_calibre_full` fast-fails as `PushUnavailable`
  because `calibredb` isn't on the path.
- **Full image** — Calibre route tries **calibredb** first. On
  `PushUnavailable` (calibredb not reachable / library locked) it
  **falls back to CWA**, so a full-image operator with CWA also
  configured stays covered.
- **ABS** fires independently for any book with an
  `audiobookshelf_id`, regardless of which Calibre route ran.

### Failure modes

- **No sinks configured for this book** — the book has neither
  `calibre_id` nor `audiobookshelf_id`. The approve returns 409 and
  the queue row stays; this is a degenerate state (an "owned" book
  with no upstream library is something to investigate, not retry).
- **All sinks failed** — every push attempt returned `PushFailed` or
  `PushUnavailable`. Returns 502, the local state is **untouched**,
  the queue row stays. Retry is safe once you've fixed the upstream
  cause.
- **Re-sync resolved to empty** — the proposal payload had no usable
  names after stripping (degenerate). Returns 502 with the
  `book_authors` table untouched. Dismiss the row.

Partial-sink success **does** advance the local state — `book_authors`
gets rewritten and the queue row is deleted as long as at least one
sink succeeded. The `push_errors` field in the response surfaces which
sinks failed so you can chase down the laggard without re-doing the
approve.

## The field-type filter chip row

Above the queue list, the **field-type filter chips** let you narrow
the visible rows to specific field kinds. Seven chips:

**Authors / Description / ISBN / Cover / Pub date / Expected date /
Page count**

The chips render on the three queue-shaped tabs only — they don't
appear on Series moves or Pending manual edits, which carry different
data shapes.

### Semantics

- **OR-within multi-select.** Clicking multiple chips shows rows
  matching **any** of the selected types. Clicking Authors + ISBN
  shows everything that's either an authors proposal or an ISBN
  proposal.
- **No active chips** = show all (the default).
- **Counts per chip** come from the full fetched list, not the
  currently-filtered subset, so the counts stay stable as you click
  through filters.
- **Disabled when count=0.** A chip with zero pending rows greys out
  rather than disappearing, so the chip set is predictable across
  loads.
- **Clear (N)** button appears when at least one chip is active.
- **Filter state is local to the panel** — `useState`, not persisted —
  and resets on tab change. Bulk-select survives filter changes
  (filter is view scope, selection is action target).

### Filter-aware empty state

The panel distinguishes two empty cases:

- **No rows at all on this tab** — generic empty state ("No pending
  diffs from \<sources\>"). Filtering is irrelevant here.
- **Rows exist but none match the active filter** — dedicated empty
  state with an inline **Clear filter** button so you don't get stuck
  wondering why the list is empty after filtering yourself into a
  corner.

### Divergence from Persons & IDs

Persons & IDs (sibling page under Tools) carries a superficially
similar chip row with **AND-across, 3-state cycle** semantics
(off / required / forbidden per chip). Metadata Manager's chips are
**OR-within multi-select** instead. The visual vocabulary is
intentionally similar (theme tokens, wrapping flex row, "Clear (N)")
so the surface feels related — the mechanic diverges because the
underlying data shape diverges:

- A Persons & IDs row carries a **vector** of properties (this person
  has Goodreads ID, has Amazon ID, has bio, …); chip semantics there
  are property-membership predicates that compose naturally with AND.
- A Metadata Manager row is **one field** (this row is an `authors`
  diff, or a `description` diff — not both); AND-across would be
  vacuously empty.

The divergence is deliberate. Operators who use both pages have to
learn the distinction once.

## Deferred work

**MAM as an enqueue source.** MAM has authoritative `author_info` for
grabbed torrents — including the full role-aware authorlist for
co-authored works — and would be the single strongest signal for an
authors proposal. It's excluded from v3.3.0 because MAM doesn't flow
through `_merge_result` for owned books; threading it requires a
separate trigger off grab completion (`pipeline.py:train_authors_from_blob`),
which is held for v5.x. The payload shape and the rest of the chain
don't change — only the trigger point is new work.

## Related

- [ADR-0017](../adr/0017-owned-author-discrepancy-review-writeback.md) —
  the design decision: enqueue at scan-convergence, union write-back,
  operator review.
- [ADR-0009](../adr/0009-merge-union-prune-overlap.md) — the union
  semantics this chapter inherits from the unattended merge path.
- [ADR-0010](../adr/0010-series-author-mode-taxonomy.md) — why the
  re-sync recomputes `series.author_mode` after an authors approve.
- [ADR-0015](../adr/0015-source-id-aware-author-identity.md) — why
  proposal payloads carry source IDs alongside names.
- [multi-author-and-series.md](./multi-author-and-series.md) —
  contributor model, primary author, ID-aware author identity, the
  "N of M" pill, Persons & IDs page.
- [metadata-sources.md](./metadata-sources.md) — which sources can
  enqueue authors proposals and how their `author_info` is gathered.
