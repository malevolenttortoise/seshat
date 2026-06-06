// Load an author's cross-library detail and expose a reload handle.
//
// DiscAuthorDetailPage + MobileAuthorDetailPage each carried a verbatim
// copy of: parse the "slug:id" compound nav arg, fetch the unified
// person-scoped detail, hold {author, loading}, and reload on mount.
// This hook is the single home for that; both shells destructure the
// same surface and keep their own JSX.
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { loadAuthorDetailViaPerson, type AuthorDetail } from "../lib/authorDetail";

// Nav arg may arrive as "slug:id" when the click came from a cross-
// library merged row — the id alone is ambiguous because ABS's author 5
// and Calibre's author 5 are different people. Split so the detail
// fetch + pen-name links + scan triggers all use the right per-library
// IDs.
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
  /** The loaded cross-library author detail, or null until first load. */
  a: AuthorDetail | null;
  /** True while a load is in flight. */
  ld: boolean;
  authorIdNum: number;
  authorSlug: string | null;
  /** Reload the detail; pass an AbortSignal to cancel an in-flight load. */
  loadA: (signal?: AbortSignal) => Promise<void>;
}

export function useAuthorDetail(authorId: string | number): UseAuthorDetail {
  const { authorIdNum, authorSlug } = useMemo(() => parseAuthorId(authorId), [authorId]);
  const [a, setA] = useState<AuthorDetail | null>(null);
  const [ld, setLd] = useState(true);

  const loadA = useCallback(
    (signal?: AbortSignal) => {
      setLd(true);
      // v2.20.0 — loadAuthorDetailViaPerson resolves the author's
      // canonical person_id and fetches /discovery/persons/{person_id}
      // for the unified cross-library view, adapting the response to the
      // existing AuthorDetail shape. Falls back to the legacy
      // /authors/{aid} response when the row isn't yet linked.
      return loadAuthorDetailViaPerson(authorIdNum, authorSlug, signal)
        .then((d) => {
          setA(d);
          setLd(false);
        })
        .catch((e) => {
          // Abort = component unmounting / id changed → ignore quietly.
          if (!api.isAbort(e)) {
            console.error(e);
            setLd(false);
          }
        });
    },
    [authorIdNum, authorSlug],
  );

  useEffect(() => {
    if (!authorIdNum) return;
    const c = new AbortController();
    loadA(c.signal);
    return () => c.abort();
  }, [loadA, authorIdNum]);

  return { a, ld, authorIdNum, authorSlug, loadA };
}
