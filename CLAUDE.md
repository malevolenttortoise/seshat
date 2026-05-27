# Seshat

Seshat is a single FastAPI backend (`app/`) + SPA frontend (`frontend/`) that unifies MAM-driven discovery, metadata enrichment, and library sync across Calibre/CWA (ebooks) and Audiobookshelf (audiobooks). Architectural decisions live in [`docs/adr/`](docs/adr/README.md).

## Conventions

Each rule below cost at least one hotfix to learn. Keep the *why* in mind so edge cases can be judged, not rote-followed.

### Backend

- **Migrations are append-only.** New `app/database.py::MIGRATIONS` entries go at the **bottom**, never inserted mid-list. The runner executes `MIGRATIONS[user_version:]`, so a mid-list insert silently never runs on upgraded DBs. To fix a prior mis-placed entry you must *grow* the list (leave the misplaced one as a tolerated no-op, append the real one) — a remove+append nets zero length and still never runs.
- **Commit before any async pause in a writer path.** `await db.commit()` before any `asyncio.sleep()` / cooperative yield. SQLite holds the single-writer lock for the whole implicit transaction; an open transaction across a rate-limit sleep makes concurrent writers die at the 30s `busy_timeout`. For rate-limited batch scanners, default to **per-iteration commit**, not per-N.
- **Pass every arg when calling a route handler as a function.** An unpassed `foo: bool = Query(False)` resolves to the truthy `Query` object, not `False`. When adding a param to an internally-called handler, `grep -rn "await <handler>("` and update every call site.
- **Audit every read/write site when adding a stored-value accessor.** Moving where a value lives (plaintext→encrypted, settings→DB) and adding `get_x()` means every direct read of the old field is now stale. Grep `app/`, `routers/`, `main.py` and switch or justify each.
- **Kill-switch settings are read per-call, never frozen at startup.** Anything meaning "stop now" (`dry_run`, `*_enabled`, `*_paused`) must be read from `load_settings()` inline in the consuming function — `load_settings()` is mtime-cached so it's free. Template: `_live_kill_switch_state()` in the dispatcher.
- **Runtime-state keys are blocked from UI PATCH.** Keys written by background jobs (circuit breakers, cookie validator, grandfather lines) go in `_RUNTIME_STATE_KEYS` (`app/routers/settings.py`), not `_PATCHABLE_KEYS` — a user clobbering them turns protective infra into a footgun.
- **Auto-adopt features need a grandfather timestamp.** Any feature that scans external state and creates rows for "unknown" items must gate on a cutoff seeded at first boot (e.g. `qbit_orphan_adoption_since`), or the first tick floods on years of pre-existing items.
- **Per-book mutation endpoints must accept `?slug=`** — see [ADR-0002](docs/adr/0002-multi-library-slug-routing.md). Book ids are per-library, not globally unique.
- **Backfill workers track an in-process attempted-set** — see [ADR-0005](docs/adr/0005-backfill-attempted-set.md).

### Frontend

- **`api.ts` auto-prefixes `/api`.** Callers pass paths **without** the leading `/api` (`api.post("/qbittorrent/test")` → `/api/qbittorrent/test`). Doubling it yields a misleading **405** (the SPA-fallback static handler claims unmatched `/api/...` paths). Routers under the legacy `/v1/...` prefix *do* include `/v1/` in the caller. Verify against a healthy call site in the same file.
- **Verify bulk find/replace before committing.** After any sed / `replace_all`, grep all variations of the old pattern (quotes, backticks, generic types like `api.get<T>`, template-literal `src`, raw `fetch()`) until zero unjustified matches. Never sed inside lockfiles — regenerate via `npm install` instead.

### Testing

- **Default to targeted test slices** matching touched files (`tests/metadata/`, `tests/orchestrator/test_pipeline.py`). The full suite sits silently for 20–35+ min.
- **Run pytest in the foreground.** A known post-test asyncio teardown hang (aiosqlite worker threads on a closed loop) means a backgrounded `pytest -q | tail` never signals exit even though tests passed. If background is unavoidable, wrap in `timeout 600 …`. Don't side-quest fixing the teardown leak. (Corollary: a `timeout`-wrapped run can exit **124 _after_** printing `N passed` — that's the teardown hang, not a failure; trust the summary line.)
- **Seed `book_authors` in any test that exercises an author/series read or the merge/recompute paths.** From v3.0.0, `book_authors` is the authoritative author↔book relation on reads ([ADR-0008](docs/adr/0008-book-authors-authoritative-on-reads.md)); author detail, per-author/series counts, the scan-dedup prefilter, `_recompute_series_author`, and `merge_books`/prune-linkage all read it. A test that seeds a `books` row with only `author_id` (no `book_authors`) will silently fall out of those queries (vanished book, no-op recompute). Mirror prod backfill in the test's seed helper — `INSERT OR IGNORE INTO book_authors (book_id, author_id, position) SELECT id, author_id, 0 FROM books WHERE author_id IS NOT NULL AND id NOT IN (SELECT book_id FROM book_authors)`, or link per-insert. Pure-unit tests that don't touch those paths don't need it.

### Release

All work lands on `development`; `main` only gets PR merges + version tags — see [ADR-0007](docs/adr/0007-development-main-release-flow.md). Versioning follows [ADR-0001](docs/adr/0001-semver-policy.md) (strict SemVer from v2.4.0). CI: development push → `:development-slim`; merge to main → `:latest-slim`; tag → `:vX.Y.Z-slim`.

## Agent skills

### Issue tracker

Issues and PRDs live as markdown files under `.scratch/<feature>/` (gitignored, local). See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical triage roles using the default vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`), recorded as `Status:` lines in each issue file. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
