# 0002. Per-book mutation endpoints must accept `?slug=`

- Status: Accepted
- Date: 2026-05-07

## Context

Seshat book IDs are auto-increment **per library**, not globally unique. In a multi-library install (e.g. Calibre + Audiobookshelf), two different books can share the same numeric id. A per-book endpoint that resolves a book by id alone uses the *active* library — which may not be the library the caller meant.

UAT on 2026-05-07 hit data corruption from exactly this gap: editing an audiobook's MAM URL via the BookSidebar returned 200 but wrote to Calibre book id=68 ("Horizon") instead of ABS book id=68 ("Accidental Champion 5"), cross-contaminating title/description/pub_date/series fields. Calibre's authoritative `metadata.db` was untouched (thanks to the dual-storage architecture); only Seshat's working row was corrupted.

## Decision

Every per-book mutation must be library-scoped:

1. **Backend:** any `/books/{bid}/...` (and metadata-by-bid) endpoint accepts `slug: str | None = Query(None)` and passes it to `get_db(slug)`.
2. **Frontend:** any mutation on a book object appends `slugQuery(book.library_slug)` to the URL (the helper in `api.ts` returns `""` when slug is undefined, so single-library callers stay backwards-compatible).
3. **`BookActionHandler` consumers** accept and propagate the optional third `slug?: string` arg.

List views in cross-library mode already stamp `library_slug` on rows, so downstream mutations have it available — the risk is greatest when the mutation handler sits far from the list fetch (sidebar → onAction → page handler).

## Consequences

- New per-book endpoints have a non-negotiable checklist item. Forgetting it silently corrupts another library's row.
- Recovery pattern if it recurs: read the authoritative source (Calibre `metadata.db` for ebooks, ABS API for audiobooks) and rebuild the Seshat row; clear `user_edited_fields` first if populated so the next sync writes through.
