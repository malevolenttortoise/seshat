# Seshat — domain context

The shared vocabulary for Seshat. When code, issues, ADRs, or hypotheses name a domain concept, use the term as defined here rather than drifting to a synonym.

This glossary is **seeded, not complete** — only the most stable, load-bearing terms are here. It grows lazily via `/grill-with-docs` as new terms get resolved during work; don't treat a missing term as an error.

## The pipeline (reactive core)

- **Announce** — an IRC announcement that a new torrent has hit MAM. The reactive trigger for the whole auto-grab pipeline; Seshat does not proactively search MAM (see `.scratch/v4-proactive-search/`).
- **Grab** — a torrent Seshat has decided to acquire and dispatched to qBittorrent. Tracked in the `grabs` table with a lifecycle of states (in-flight → … → owned/terminal).
- **Snatch** — acquiring a torrent from MAM. MAM tracks snatches; re-acquiring the same torrent has economy/ratio consequences, so the pipeline avoids redundant snatches.
- **Dispatch** — the orchestrator step that takes an allowed announce through the dedup/hold gates and submits it to qBittorrent (`app/orchestrator/dispatch.py`).
- **Filter** — the per-announce gate (media type, allowed formats, author allow-list) that decides whether an announce is even considered.
- **Auto-train** — adding an author to the `authors_allowed` filter allow-list so future announces by that author pass the **Filter**. Happens when the user approves a tentative author, and automatically for the co-authors of a grabbed book — trusted from MAM's authoritative `author_info` authorlist. Trains author *names* only; never creates discovery/owned book rows (MAM is enrichment-only, never a discovery source).
- **Claim-for-owned** — at announce time, when a new torrent matches a book the user already owns whose owned row has no confirmed MAM linkage (`mam_status != 'found'`), write the MAM linkage onto the owned row in place and skip the grab — avoiding a redundant **Snatch**. From v3.0.0 the match is **contributor-aware**: the announce's primary author may match *any* contributor of the owned book (not primary-against-primary), still gated by a canonical title match and a bail-on-ambiguity rule (see [ADR-0013](docs/adr/0013-claim-for-owned-contributor-aware.md)).

## Discovery (review surface)

- **Discovery** — the review-only surface that *proposes* books to add, distinct from reactive auto-grab. Candidates land in a queue for a human decision.
- **Possible** — a discovery candidate match awaiting review (approve / hide / dismiss). Scoring tries to keep genuine Possibles separate from phantom ones.
- **Source** — an external metadata provider (Goodreads, Amazon, Hardcover, Audnexus, OpenLibrary, Google Books, MAM itself). Each source may expose its own author/work IDs.
- **Discovery source vs. matching source** — most sources are *discovery* sources: they implement `search_author()` → `AuthorResult`/`BookResult` and flow through `lookup._merge_result`, which can create discovered book rows. **MAM is NOT a discovery source** — it has no `search_author`/`BookResult`; it only *matches* announces/owned books to torrents and *enriches* (its `author_info` feeds match/score/dedup). MAM never creates a discovered book. A grabbed MAM torrent's authors reach `book_authors` via the **owned path** (Calibre/ABS ingest → Phase 2 sync), and its full authorlist is added to the `authors_allowed` filter list by grab-completion auto-train (`train_authors_from_blob`). MAM is "trusted-create" for authors (we trust its list) but only in that enrichment path, never to discover/insert new *books*.
- **Enrichment** — fetching and merging metadata from sources onto a book/author.

## Library, identity, and sync

- **Library** — a connected collection (Calibre/CWA for ebooks, Audiobookshelf for audiobooks). Each has a **slug**.
- **Slug** — the per-library identifier. Book ids are auto-increment **per library**, not globally unique — hence slug-scoped mutations (see [ADR-0002](docs/adr/0002-multi-library-slug-routing.md)).
- **Owned** — a book present in a user library (`owned=1` on the per-library `books` row).
- **Contributor** — an author credited on a book, with an ordered **position** and an optional **role** (translator, illustrator, narrator, …). A book has one or more contributors; this is the domain concept behind the `book_authors` relation. Role-filtering drops non-author contributors on ingest (see `.scratch/v3.0.0-multi-author/`).
- **Primary author** — the contributor at **position 0**: the one shown when a single author is displayed, and the sort key for a book. Distinct from co-authors at positions 1…N.
- **Contributor set** — a book's full set of contributors (its `book_authors` rows). Write-time operations reason over the set, not the single primary: merge **unions** the two books' sets (never drops a co-author), and prune-linkage finds an owned sibling by **overlapping** contributor set (≥1 shared contributor), not strict primary-author equality (see [ADR-0009](docs/adr/0009-merge-union-prune-overlap.md)).
- **Co-authored ownership** — a book is **owned for every one of its contributors**, not just its primary author. Hiding it hides it for all of them (per-book, not per-contributor). From v3.0.0, the author↔book relation is authoritative via contributors, not the single-author column (see [ADR-0008](docs/adr/0008-book-authors-authoritative-on-reads.md)).
- **Person** — a canonical, cross-library author identity (the `persons` table), linked to per-library author rows via **author links**. Resolves "same author across Calibre + ABS." Distinct from a **contributor**: a person is *who* the author is across libraries; a contributor is *that author's credited role + position on one book*.
- **Mirror** — write-through that propagates a canonical value (bio, image) to per-library author siblings and the canonical `persons` row (`mirror_bio`, planned `mirror_image_url`).
- **Sync** — reconciling Seshat's working rows against the authoritative sources (Calibre `metadata.db`, ABS API).

## Bundles & dedup

- **Bundle** — a single torrent containing multiple works (an omnibus / "Books 1–10"). Fanned out into N review entries; bias is to keep the bundle even at the cost of duplicate children (see [ADR-0003](docs/adr/0003-bundle-dedup-prefer-duplicates.md)).
- **Fan-out** — `_prepare_book` expanding a bundle into one review entry per child work.
- **Dedup key** — the normalized `match_key(first_author, title)` used to recognize the same work across announces, grabs, holds, and owned books.
- **Merge** — folding one book row (loser) into another (winner) when they're the same work; the loser is deleted and its linkage redirected. The winner's identity (title, primary author) is canonical, but the **contributor set is unioned** so no co-author is lost (see [ADR-0009](docs/adr/0009-merge-union-prune-overlap.md)).

## Series

- **Series** — a named, ordered set of books. Each series has an **author mode** that classifies its authorship shape (see [ADR-0010](docs/adr/0010-series-author-mode-taxonomy.md)).
- **Author mode** — `per_author` / `multi_author` / `shared`, decided by which authors appear in **every** book of the series (the intersection of the books' contributor sets): exactly one → per_author; a team of two or more → multi_author; none in common → shared. Surfaced in the UI as **Per-author** / **Co-authored** / **Shared** respectively.
- **Series owner** — an author in that intersection (present in every book); owns the series and sees it in full. A **shared** series has no owner. A contributor who appears in only some books is *incidental*, not an owner — they see only their own entries, badged.
- **Hold** — a deferred announce parked in `pending_holds` during the format-dedup window so slow split-uploads don't lose the preferred format (see [ADR-0004](docs/adr/0004-format-priority-dedup.md)).

## Quality & replacement

- **Quality metadata** — extracted per-edition quality facts (audio bitrate/channels/encoding, ebook source type, file completeness). Stored in `torrent_quality_metadata`.
- **Quality scoring** — the multi-axis generalization of format priority that ranks editions.
- **Active replacement** — upgrading an owned book to a higher-quality edition by soft-deleting the old copy to `<library>/.seshat-replaced/<timestamp>/` (reversible within a retention window). Opt-in per library.
- **Unavailable stub** — a marker row (`source="unavailable"`) written when a torrent has been removed from MAM's index, to stop retry storms (see [ADR-0006](docs/adr/0006-mam-not-found-is-permanent.md)).

## Output & integration

- **Sink** — an output target that delivers or removes a book: `CalibreSink` (calibredb CLI), `CWASink` (Calibre-Web-Automated admin form), `AudiobookshelfSink`. Selection mirrors `metadata.book_push`.
- **CWA** — Calibre-Web-Automated, the ebook ingest path used by the slim image (Mark's prod).
- **Push-back** — user-triggered write of metadata changes back to authoritative Calibre, via the dual CWA/calibredb path.
- **Reingest** — re-adding an already-snatched book from qBittorrent/disk into a library without re-snatching from MAM.

## Cross-cutting

- **MAM economy** — the discipline of minimizing MAM API and tracker calls to respect rate limits and ToS. Drives the attempted-set ([ADR-0005](docs/adr/0005-backfill-attempted-set.md)), unavailable stubs, and qBit add-stagger.
- **Path aliasing** — translating a qBittorrent `save_path` (`/data/...`) to Seshat's local view (`/downloads/...`) via `translate_path()`.
