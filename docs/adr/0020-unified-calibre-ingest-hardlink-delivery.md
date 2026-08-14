# 0020. Unify ebook ingest on direct `calibredb add`; hardlink delivery; freeze slim image as LTS

- Status: Accepted (design-locked 2026-06-25; implementation pending — strict-SemVer MAJOR)
- Date: 2026-06-25

> Number note: `0019` is reserved for the refactor-era long-lived-branch ADR authored on `refactor/v3.5.x`; this decision takes `0020` to avoid a merge collision.

## Context

A MAM forum user asked for two things, *arr-style: (1) deliver the book file **byte-identical** (metadata + cover travelling alongside, not embedded), and (2) **hardlink** the library file to the still-seeding torrent file to avoid duplicating bytes on disk.

Seshat already never modifies the *seeding* original — it works on copies (`file_copier.copy_to_staging` → `pipeline._prepare_book` patches a temp epub via `patch_epub_metadata` → sink copies it). So "deliver unmodified" really means **stop embedding enrichment into the delivered copy** and convey it some other way. And hardlinking is only meaningful where the library reads the file **in place**.

A grill with a live CWA probe (dev-stack `crocodilestick/calibre-web-automated:latest`) established the constraints:

- **CWA ingest re-copies and re-writes.** `ingest_processor.add_book_to_library` does `shutil.copy2`→staging then `calibredb add` — a copy, never a move/rename. So a hardlink dropped into CWA's ingest dir is **not preserved** (`os.link`-at-delivery is dead for the re-ingesting sinks). Worse, CWA ships **both byte-mutating gates ON by default** (`auto_convert=1`, `kindle_epub_fixer=1` in `cwa_settings`); the kindle-epub-fixer rewrites every epub via `EPUBFixer().process()`, so a CWA-imported file is **not byte-identical** to the torrent out of the box → it cannot be deduped to a hardlink at all.
- **The hardlink-vs-enrichment tension is CWA-specific.** CWA reads a book's **embedded** metadata and does **not** consume an OPF/cover sidecar dropped alongside (it stages only the single book file; `calibredb add` has no sidecar-read). So on the CWA path you can patch the epub (enriched, not hardlinkable) **xor** deliver unmodified (hardlinkable, enrichment lost). You cannot have both.
- **Direct `calibredb add` dissolves the tension.** `calibredb add <unmodified epub> --title/--authors/--series/--cover` writes metadata into `metadata.db` (**not** the file) and copies the book bytes **unmodified** into the managed `Author/Title (id)/` tree. When *Seshat* owns the add, *Seshat* controls the mutation policy — no surprise kindle-fixer/convert — so byte-identity is **guaranteed**, and the library file can then be relinked to the seeding inode. Enriched metadata AND a byte-identical, hardlinkable file, in one step.
- **The slim image is the obstacle.** Direct `calibredb add` needs `calibredb`, which only the **full** image bundles (Calibre tarball, ~650 MB). The **slim** image (~225 MB, Mark's prod) deliberately omits Calibre and ingests via CWA precisely to stay small.

The grill chose to unify rather than special-case: build one Seshat-owned ingest path and retire the CWA-ingest branch, accepting a major-version break. To avoid abandoning existing slim users, the slim image is frozen as an LTS rather than deleted.

## Decision

**1. Unify ebook ingest on Seshat-owned `calibredb add` (full image only); remove the CWA ingest path.**
Retire `CWASink` (delivery), `app/sinks/_cwa_throttle.py`, `push_back.push_cwa`, and the full/slim dual-path branch in `active_replacement._select_sink_for_library`. Metadata write-back and active-replacement *remove* move to direct calibredb (`set_metadata` / `remove`). **CWA remains supported only as the user's *reader*** — it serves the Calibre library Seshat writes to; Seshat no longer talks to CWA's HTTP API or ingest dir.

**2. Eliminate `patch_epub_metadata`.** Ebook metadata travels via `calibredb` flags; the epub is delivered byte-identical. The OPF-patch path in `app/metadata/writer.py` becomes dead code for ingest (confirm no other caller before removal).

**3. "Universal sidecar" is per-sink, not one OPF.** Calibre **owns** its metadata (`metadata.db` + the `metadata.opf`/`cover.jpg` calibredb auto-generates per book dir) — Seshat writes no OPF for the Calibre path; the cover travels via `--cover`. Audiobookshelf gets a real `metadata.json`(/`.abs`) + `cover.jpg` sidecar in the item dir, reusing the cover already fetched by `covers.fetch_cover`.

**4. `delivery_mode` setting (global: `copy | hardlink`, default `copy`), with two mechanisms by sink type.**
   - **Read-in-place sinks (ABS, folder):** `os.link` **at delivery** — the seeding file is linked directly to the library path. (Large win: audiobooks are multi-GB.)
   - **calibredb path:** **post-import relink** — after `calibredb add`, resolve the library file from the returned book id, confirm byte-identity, then atomically replace it with a hardlink to the seeding inode (`os.link` to a temp + `os.replace`). (Small win: ebooks are ~1 MB.)

**5. Lean safety net (no ongoing backup).** Hardlink mode does a **pre-relink byte-identity gate** (never relink unless library bytes == seed bytes; on mismatch keep the plain copy and log) and a **post-relink sanity check** (`st_nlink ≥ 2`, shared `st_ino`). No temp-backup/ongoing-verify loop: the only post-relink in-place mutator of the shared inode is **user-initiated Calibre file editing**, which is explicitly the user's responsibility — documented, not guarded. A same-filesystem preflight (`st_dev(seed) == st_dev(library)`) gates the relink; on mismatch (incl. cross-device `EXDEV`) it falls back to copy. On Unraid the `/mnt/user` shfs-FUSE same-share requirement is documented (TRaSH-guides atomic-move setup).

**6. Freeze the slim image as an LTS; all future development is full-image only.** The slim image is **not deleted** — it is pinned at its final build (target `v3.8.2`, the last release before this arc) and published as a durable LTS tag so existing slim users keep a working, supported-as-frozen image. From the major release onward, CI builds only the full image; `Dockerfile.slim` stops receiving feature work. **Prerequisite:** a real terminal slim image must actually exist in GHCR first — `:3.8.2-slim` is currently **missing** (cancelled by the CI same-SHA concurrency gate; see [reference-seshat-ci-image-builds] / backlog CI follow-ups). Publishing the LTS slim tag is a gating task, and pairs with fixing the concurrency gate.

## Consequences

- **Strict-SemVer MAJOR, breaking.** Existing slim-image users must either stay on the frozen `v3.8.2-slim` LTS (keeps CWA ingest, no new features) or migrate to the full image (switch image, point Seshat at the Calibre library via `CALIBRE_PATH`, stop using the CWA ingest dir). Needs a migration guide. Mark's prod migrates first.
- **Image grows ~225 MB → ~650 MB** for everyone going forward. Accepted: the disk a self-hoster reclaims via audiobook hardlinks dwarfs the image delta; ebook hardlinks are marginal but free once the path exists.
- **Loss of CWA ingest-time automation** for Seshat-added books: kindle-epub-fixer, auto-convert-to-target, auto-send-to-Kindle. Out of scope — users trigger these manually in CWA-the-reader. (Auto-convert is antithetical to hardlinking anyway: a converted file can't share the seed's inode.)
- **Direct `calibredb add` writes to a library CWA is actively serving** — reintroduces the cache-invalidation concern CWA ingest originally sidestepped. calibre-web/CWA is calibredb-centric and detects external `metadata.db` changes, so this is expected-fine but is now **load-bearing** and must be verified in UAT (no reader restart required to see new books).
- **Single ingest mechanism** simplifies the codebase (one sink path, no full/slim branch, no CWA HTTP coupling) at the cost of the one-time migration churn.
- **`active_replacement` stays inode-safe** — its soft-delete is a `shutil.move` (rename; inode + link count survive). The hardlink gate adds a same-`st_dev` requirement; the existing OVERLAP path-overlap gate is unaffected.

## Related

- [0001](0001-semver-policy.md) strict SemVer (this is the first MAJOR since v3.0.0) · [0007](0007-development-main-release-flow.md) release flow.
- Supersedes the CWA-ingest delivery model (no prior ADR — CWASink predates the ADR practice).
- PRD: `.scratch/hardlink-sidecar-delivery/PRD.md`. CI context: [reference-seshat-ci-image-builds] (the missing `:3.8.2-slim` blocker for the LTS freeze).
- Glossary: **Sink**, **Delivery mode**, **Hardlink relink** in [CONTEXT.md](../../CONTEXT.md).
