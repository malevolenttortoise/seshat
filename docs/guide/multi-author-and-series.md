# Multi-author and series

Seshat treats authorship as a set, not a single name. Every book carries an ordered list of credited [contributors](../../CONTEXT.md#library-identity-and-sync); a [series](../../CONTEXT.md#series) is classified by which of those contributors appear in *every* one of its books. The whole model — reads, merges, prune, scan-convergence, claim-for-owned, series mode — is built on that set rather than a single primary-author column.

This chapter explains the parts an operator sees and the rules behind them. If you joined Seshat before v3.0.0, the upgrade-relevant sections are flagged.

## Contributors, position 0, and roles

A book has one or more contributors, each with a **position** and an optional **role**. Position 0 is the **primary author** — the name shown in single-author UI cells, the sort key, and the anchor used by the dedup key. Positions 1…N are co-authors. The author at position 0 is whichever author the upstream source listed first (for owned books, the order Calibre or Audiobookshelf hand back; for discovered books, the source-emitted byline order).

Roles come from the source byline. Seshat keeps **author-equivalent** roles only — co-author, "with," "by." Roles that aren't authorial (translator, illustrator, narrator, foreword writer) are dropped on ingest. They don't enter `book_authors`, they don't appear in series owner computations, and they don't auto-train into the [Filter](../../CONTEXT.md#the-pipeline-reactive-core). A narrator is interesting in other surfaces (the audiobook quality scorer cares about narration) but not as a credited author.

**Upgrade note for pre-3.0 installs.** Before v3.0.0, each book stored exactly one author in `books.author_id`. v3.0.0 introduced the `book_authors` join, backfilled position 0 from the old column for every existing book, and then **dropped `books.author_id` entirely** ([ADR-0008](../adr/0008-book-authors-authoritative-on-reads.md), [ADR-0012](../adr/0012-drop-books-author-id-position-0-canonical.md)). The drop is irreversible and runs on first boot of the v3.0.0 image — back the metadata DB up before upgrading. After upgrade there is no denormalized primary author column anywhere; position 0 is the only source of truth.

## Where contributors come from

Contributors are captured at scan time, per source, from the same DOM/JSON node as the author name. Each source supplies what it natively exposes:

- **Goodreads** — detail-page co-author list (`a.authorName` entries beyond the primary), with the source author ID parsed from each `/author/show/<id>` URL.
- **Hardcover** — GraphQL `book.contributions[]`, including the Hardcover author ID per contributor.
- **Amazon** — the byline widget under the title, with the author-store ASIN per byline link.
- **Audible / Audnexus** — co-narrators are dropped (narrator role), co-authors kept, with the Audnexus ASIN per author.
- **Google Books** — names only, link-only behavior; no IDs because Google Books doesn't expose stable author IDs.

OpenLibrary is scoped out of contributor capture on purpose. OL exposes author *keys* on the work record but not author *names* — getting the names would cost an extra API call per contributor per book, which doesn't justify the marginal coverage gain.

Every source's contributor capture is **link-only** for co-authors: when a co-author's name matches an existing per-library author row, Seshat reuses it; when it doesn't, the co-author is minted as a new author row and linked. Sources never *create* new books for their co-authors — only the source actively being scanned drives book creation.

## MAM is enrichment, never discovery

[MAM](../../CONTEXT.md#discovery-review-surface) is not a discovery source. There is no `search_author` against MAM and no path by which a MAM call inserts a new book row into Seshat. MAM enters the contributor model through two distinct paths:

- **Auto-train from grabs.** When a grab completes, MAM's authoritative `author_info` is read and every credited author is added to the `authors_allowed` filter so future announces by those authors pass the [Filter](../../CONTEXT.md#the-pipeline-reactive-core). This trains *names* into the allow-list. It does not create discovery books and it does not write to `book_authors` for any book Seshat doesn't already own.
- **Owned-book ingest.** When a grabbed torrent lands in Calibre or Audiobookshelf, the **owned path** sync (`train_authors_from_blob` and Phase-2 reconciliation) is what writes `book_authors` rows. Authority on owned books is the library — MAM's authorlist feeds it via the Calibre/ABS ingest, not directly.

If you see an unfamiliar author in `book_authors` for a book you don't own, it came from a discovery source's contributor parser, not from MAM.

## Merge unions, prune matches by overlap

Two write-time operations fold one book row into another, and they treat the contributor set asymmetrically on purpose ([ADR-0009](../adr/0009-merge-union-prune-overlap.md)).

**Merge unions.** When two book rows are merged (one is recognized as the same work as another), the survivor's contributor set becomes the union — winner's contributors first in their existing positions, then any of the loser's contributors not already present, appended at the end. No co-author is silently dropped. The cost of unioning is bounded: a spurious co-author the loser carried lands on the survivor too, but the role allowlist drops the non-author noise (translators, illustrators) and a stray link is recoverable by hand. The priority is "never lose a real co-author."

**Prune searches by overlap.** When a Calibre row disappears (typically because CWA consolidated a duplicate inside Calibre) and Seshat needs to move its MAM linkage onto the surviving owned sibling, the sibling is found by **shared-contributor** match: any owned Calibre book that shares at least one contributor with the disappearing row, plus a canonical title match, qualifies. That replaces the old "must share primary author" rule, which broke for co-authored books where the survivor's primary differs from the loser's. The disappearing row's contributors are **not** unioned onto the survivor: the survivor is the Calibre row the user kept, so its author tuple is authoritative and pulling in stale links would re-introduce authors the user deliberately removed.

The asymmetry — union on merge, no-union on prune — comes from a single question: *is the survivor authoritative over the other side's authorship?* Merge between a discovered duplicate and an owned winner: neither side is authoritative, so union. Prune onto a hand-curated Calibre survivor: Calibre is authoritative, so don't union.

The merge audit row snapshots the loser's pre-merge contributors, so if a union ever needs to be reversed it's forensically traceable.

## Series author_mode

Every series has an `author_mode` — `per_author`, `multi_author`, or `shared` — decided by the **intersection of contributor sets** across the series' visible books. Let `I` be the set of contributors that appear in *every* visible book ([ADR-0010](../adr/0010-series-author-mode-taxonomy.md)):

| `\|I\|` | `author_mode` | UI label   | Meaning                                                                 |
| ------- | ------------- | ---------- | ----------------------------------------------------------------------- |
| 1       | `per_author`  | Per-author | One author writes every book. Guest co-authors on individual books don't change the mode. |
| ≥ 2     | `multi_author`| Co-authored| A team of two or more is on every book (Galaxy's Edge — Chaney + Anspach).|
| 0       | `shared`      | Shared     | No author is in every book (a shared-world franchise like Halo).         |

Keying on the *intersection* — not the union, and not "every book has the identical set" — is what correctly handles the guest case. A Sanderson series with one novella co-written with a guest stays per-author Sanderson, because Sanderson is still in every book even though the guest is in only one.

A series row also keeps `author_id` as a stored **owner pointer**:

- per_author → the sole `I` member.
- multi_author → a deterministic **anchor** (most-common position-0 author across the series' books; tiebreak lowest author id). Non-NULL.
- shared → NULL.

The non-NULL anchor for multi_author keeps `is_shared = (author_id IS NULL)` meaning *only* shared, so older code paths and indexes keyed on `author_id` keep working. `author_mode` is the new explicit discriminator; the anchor is bookkeeping.

`author_mode` recomputes whenever a book joins, leaves, or has its contributors changed. After the v3.0.0 upgrade the recompute runs once over every existing series.

## Owner vs incidental — computed on read

Within a series, an author is either an **owner** (in `I`: present in every visible book) or **incidental** (a contributor to some but not all). The distinction drives the author-detail view ([ADR-0011](../adr/0011-owner-incidental-on-read.md)):

- **Owners** see the series in full. Galaxy's Edge on Chaney's detail page shows all books, no badge.
- **Incidental contributors** see only the books they're actually on, badged "**N of M**" — the books visible plus the total — so it's clear they're seeing a slice.

Ownership is computed on each read with a count-equality test: an author owns the series exactly when the count of the series' visible books they contribute to equals the count of the series' visible books. There is no stored owner-set table. The test is folded into the existing author-detail aggregate query (one extra conditional `COUNT` per series row, covered by the existing index), so it doesn't add a round-trip.

No persistence means no second derived structure to keep in sync across merge, prune, sync, and add-to-series — ownership is always exactly what the current `book_authors` says it is, with no drift to debug. If the read cost ever becomes real in production, the escape hatch is to materialise owner sets server-side and invalidate them on membership change. Browser/session caches are explicitly not the fallback — their invalidation surface is wider than the recompute they'd try to avoid.

## Heal-on-convergence — pre-3.0 thin rows self-correct

[Discovered](../../CONTEXT.md#discovery-review-surface) (unowned) books created before contributor parsing existed carry a single contributor — the author Seshat was scanning when the book was first seen. They have no co-authors in `book_authors` because the source-side parser didn't yet exist. That undercounts series intersections: an Anspach-only thin row in a Chaney+Anspach series drags `I` down to `{Anspach}` and the series gets mislabeled per-author Anspach.

On scan-convergence — when a discovery scan re-encounters one of these thin rows rather than inserting a new book — Seshat now **heals** the contributor set by **unioning** the source's role-filtered contributors into the existing set, existing positions preserved and new contributors appended ([ADR-0014](../adr/0014-heal-contributors-on-scan-convergence.md)). Position 0 stays whichever author was originally scanned.

Operator-visible effect: a series previously misclassified per-author flips to co-authored (or shared) the next time a scan touches one of its members. No bulk migration is needed; healing is opportunistic and runs as a side effect of normal author rescans. Once a thin row heals to its full contributor set, scans of either co-author find it, and the row stops accumulating duplicates over time.

Owned books are **never** healed by this path. Owned `book_authors` is Calibre/ABS-authoritative; reconciling owned-book authorship to a source's contributors is an operator-reviewed flow via [Metadata Manager](./metadata-manager.md) (authors proposed-change), not a silent side effect of scanning.

The healing is delta-only: if the source contributes nothing new, the row is left alone. That preserves the original "convergence doesn't churn links" guarantee for the multi-source case while still healing thin pre-3.0 rows.

## Claim-for-owned is contributor-aware

[Claim-for-owned](../../CONTEXT.md#the-pipeline-reactive-core) runs on the announce hot path: when a newly-announced MAM torrent matches a book the user already owns whose owned row has no confirmed MAM linkage, Seshat writes the MAM URL/torrent ID onto the owned row in place and skips the grab — gaining the linkage without paying buffer ratio for a redundant [Snatch](../../CONTEXT.md#the-pipeline-reactive-core).

The match is **contributor-aware** ([ADR-0013](../adr/0013-claim-for-owned-contributor-aware.md)): the announce's primary author may match *any* contributor of the owned book, not just the owned row's stored primary. The title still has to canonically match, and the existing ambiguity gate still bails if more than one owned book matches.

The asymmetry is deliberate. The announce side stays primary-only (only the announce's first author is considered) — widening it to all announce authors would enlarge the wrong-claim surface without proportionate benefit. The owned side is widened (any contributor) so co-authored books whose announce lists a different author first than the owned row's stored primary still claim cleanly. No `torrent_info` fetch happens on the claim path; the announce-supplied `author_blob` is all it uses, so per-announce MAM-economy cost stays at zero.

## Operator surfaces

Three places in the UI surface the contributor and series model directly.

**Series Manager** (Tools → Series Manager). One panel per [library](../../CONTEXT.md#library-identity-and-sync) [slug](../../CONTEXT.md#library-identity-and-sync). Each series row shows its computed `author_mode` as the **Per-author / Co-authored / Shared** label, the owner anchor, and member count. From here you can add a contributor to a series (validated against `book_authors`, so co-authors can be added; not gated to primary-only), rename or delete a series, and trigger an explicit recompute. Series Manager is per-library because series IDs are per-library — series in your Calibre slug are not series in your Audiobookshelf slug, even when the names match.

**Discovery → Series browse.** A list view of every series across libraries, with a 3-way author-mode filter (Per-author / Co-authored / Shared) and free-text search over series name. Useful for finding a specific co-authored series or surveying everything classified shared.

**Discovery → Series detail.** Series page with the full member list, the author-mode label, and a contributors panel split into **Owners** (members of `I`) and **Incidental contributors** (everyone else who shows up on at least one member book). Clicking an owner takes you to their author detail viewing the full series; clicking an incidental contributor takes you to their author detail showing only their slice with the "N of M" pill.

## Persons & IDs — cross-library author identity

A per-library author row is per-library. A **Person** is the canonical cross-library identity that links an author's Calibre row to their Audiobookshelf row and any other library siblings ([ADR-0015](../adr/0015-source-id-aware-author-identity.md)). The link is stored in `author_links`; the canonical row lives in `persons`.

Identity resolution is **ID-aware** and **ID-first**. When Seshat resolves an author — either to pick an existing per-library row or to consolidate two libraries' rows into one Person — it tries:

1. **Source author ID match** first. If the incoming author carries a `goodreads_id` (or Hardcover ID, Amazon ASIN, Audnexus ASIN) and any already-known author row carries the same ID for that source, that's the same person. A stable per-source author ID is stronger identity evidence than any name string.
2. **Exact name** match.
3. **Normalized name** match.
4. **Fuzzy name** match, flagged low-confidence.
5. **Mint** a new row.

This closes the long-standing **split-person gap** where Calibre's "Robert Heinlein" and Audiobookshelf's "Robert A. Heinlein" used to land on two different persons even when both rows already carried the same `goodreads_id`. ID-first matching catches that case before name resolution ever runs.

Co-author IDs are persisted on the per-library author row when first seen, with a **fill-if-empty** write policy: a co-author's freshly-captured ID writes to the row only when that column is currently NULL. It never overwrites an existing ID. The scanned-author path (the author you actively searched) does overwrite, because the scanned author is the high-confidence subject of the search; a co-author is byline-derived (lower confidence), and silently overwriting a canonical ID on a name collision is how identity gets corrupted.

### The Persons & IDs page

Tools → Persons & IDs. One row per Person, with their linked per-library author rows, their per-source IDs (Goodreads, Hardcover, Amazon, Audnexus), and per-row counts. You can:

- **Edit a source ID** directly on a Person — useful when a scan picked up the wrong source profile.
- **Merge two Persons** that should be one (typically when fuzzy-name minted two rows before IDs were available).
- **Review source-ID conflicts** in the conflicts panel.

### Source-ID conflicts

A **source-ID conflict** is recorded when a scan's captured author ID disagrees with an ID already on file for that source: the incoming ID matched no existing row, but the incoming *name* matched a row that already holds a *different* ID for the same source. That's a genuine identity ambiguity — either a wrong name-match upstream, or the source itself split one author across two profiles (Goodreads occasionally does this).

Conflicts are **recorded, not auto-resolved**. The on-file ID is never silently overwritten. The conflicts panel surfaces them with a **Dismiss** action; actual resolution goes through the existing manual person-merge or edit-source-ID tools. Trading automatic correction (risky on identity) for visibility (safe) is the deliberate posture.

### One-shot consolidation on first run after upgrade

Production already holds thousands of populated source IDs from the scanned-author path that, pre-v3.0.1, weren't being used for Person matching. **Hygiene Job 9** (Consolidate persons by shared source ID) is the one-shot pass that applies ID-aware matching across existing `author_links` to merge separate Persons that share a source ID. It runs on first boot after upgrade, then never runs again unless triggered manually.

Person merges are hard to undo by hand. **Back the metadata DB up before the first run** — Job 9's pre-flight backup recipe is documented in [Hygiene jobs](./hygiene-jobs.md). The same Job 9 reference also records any conflicts it surfaces; after first run, check the conflicts panel.

Sources that don't expose stable author IDs (Google Books) simply skip the ID rung — name resolution still applies. There's no penalty for a source without IDs, just no upgrade path beyond name matching.

## Further reading

- [ADR-0008](../adr/0008-book-authors-authoritative-on-reads.md), [ADR-0009](../adr/0009-merge-union-prune-overlap.md), [ADR-0010](../adr/0010-series-author-mode-taxonomy.md), [ADR-0011](../adr/0011-owner-incidental-on-read.md), [ADR-0012](../adr/0012-drop-books-author-id-position-0-canonical.md), [ADR-0013](../adr/0013-claim-for-owned-contributor-aware.md), [ADR-0014](../adr/0014-heal-contributors-on-scan-convergence.md), [ADR-0015](../adr/0015-source-id-aware-author-identity.md) — the decisions behind everything above.
- [Hygiene jobs](./hygiene-jobs.md) — Job 9 backup recipe and first-run cost; Job 8 source-ID mirror within an existing Person.
- [Metadata Manager](./metadata-manager.md) — the owned-author proposed-change writeback flow that reconciles owned books to source contributors without a discovery scan silently overwriting library data.
- [CONTEXT.md](../../CONTEXT.md) — canonical domain glossary for the terms used throughout this chapter.
