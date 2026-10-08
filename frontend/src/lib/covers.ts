// Covers Seshat stored on disk (review staging, tentative covers) are
// served by `GET /api/v1/covers/{path}`, keyed by their absolute path.

/** URL for a cover stored at absolute path `path`.
 *
 * The leading "/" is dropped (the endpoint puts it back) so requests
 * read `/api/v1/covers/staging/...`, not `/api/v1/covers//staging/...`
 * (2026-10 audit L4-04). */
export function storedCoverUrl(path: string): string {
  return `/api/v1/covers/${encodeURIComponent(path.replace(/^\/+/, ""))}`;
}
