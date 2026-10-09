// Load an author's cross-library detail and expose a reload handle.
//
// DiscAuthorDetailPage + MobileAuthorDetailPage each carried a copy of:
// parse the "slug:id" nav arg, fetch the unified person-scoped detail,
// hold {author, loading, error}, and load on mount / when the author
// changes. This hook is that single copy; both shells keep their own
// JSX (wave 5b, issue 22; redone from archive `6ed6de6` after S2a made
// the two copies agree on failures).
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { loadAuthorDetailViaPerson, type AuthorDetail } from "../lib/authorDetail";

// The nav arg may arrive as "slug:id" when the click came from a cross-
// library merged row — the id alone is ambiguous because ABS's author 5
// and Calibre's author 5 are different people. Split it so the detail
// fetch, the pen-name links and the scan triggers all use the right
// per-library ids.
export function parseAuthorId(
  authorId: string | number,
): { authorIdNum: number; authorSlug: string | null } {
  const s = String(authorId);
  if (s.includes(":")) {
    const [slug, id] = s.split(":");
    return { authorSlug: slug, authorIdNum: parseInt(id) || 0 };
  }
  return {
    authorSlug: null,
    authorIdNum: parseInt(s) || (typeof authorId === "number" ? authorId : 0),
  };
}

export interface UseAuthorDetail {
  /** The loaded cross-library author detail, or null until the first load. */
  a: AuthorDetail | null;
  /** True while a load is in flight (and before the first one). */
  ld: boolean;
  /** Why the author couldn't be loaded; null while loading or loaded. */
  loadErr: string | null;
  authorIdNum: number;
  authorSlug: string | null;
  /** Reload the detail; pass an AbortSignal to cancel an in-flight load. */
  loadA: (signal?: AbortSignal) => Promise<void>;
}

export function useAuthorDetail(authorId: string | number): UseAuthorDetail {
  const { authorIdNum, authorSlug } = parseAuthorId(authorId);
  const [a, setA] = useState<AuthorDetail | null>(null);
  const [ld, setLd] = useState(true);
  const [loadErr, setLoadErr] = useState<string | null>(null);

  const loadA = useCallback(
    (signal?: AbortSignal) => {
      setLd(true);
      // v2.20.0 — `loadAuthorDetailViaPerson` resolves the author's
      // canonical person_id and fetches /discovery/persons/{person_id}
      // for the unified cross-library view, adapting the response to
      // the existing AuthorDetail shape. Falls back to the legacy
      // /authors/{aid} response when the row isn't yet linked.
      return loadAuthorDetailViaPerson(authorIdNum, authorSlug, signal)
        .then((d) => {
          setA(d);
          setLoadErr(null);
          setLd(false);
        })
        .catch((e) => {
          if (api.isAbort(e)) return;
          console.error(e);
          setLoadErr((e as Error).message || String(e));
          setLd(false);
        });
    },
    [authorIdNum, authorSlug],
  );

  useEffect(() => {
    // An id that doesn't parse (0) has nothing to load.
    if (!authorIdNum) {
      setLoadErr("no author id");
      setLd(false);
      return;
    }
    const c = new AbortController();
    loadA(c.signal);
    return () => c.abort();
  }, [loadA, authorIdNum]);

  return { a, ld, loadErr, authorIdNum, authorSlug, loadA };
}
