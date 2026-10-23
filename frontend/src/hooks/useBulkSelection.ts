// Multi-select state for a page's bulk actions: a select-mode flag and
// the set of selected ids, with the handful of updates the pages make.
//
// The author-detail pages (desktop + phone) and the Tentative pages
// (desktop + phone) each carried this verbatim (wave 5b, issue 22). The
// updaters are stable (`useCallback`), so passing them down to cards and
// series sections doesn't re-render those on every page render.
import { useCallback, useState, type Dispatch, type SetStateAction } from "react";

export interface BulkSelection<T> {
  selMode: boolean;
  setSelMode: Dispatch<SetStateAction<boolean>>;
  sel: Set<T>;
  /** Add the id if it's not selected, remove it if it is. */
  toggle: (id: T) => void;
  /** Add these ids, keeping whatever else is selected. */
  selectMany: (ids: T[]) => void;
  /** Remove these ids, keeping whatever else is selected. */
  deselectMany: (ids: T[]) => void;
  /** Select exactly these ids. */
  selectOnly: (ids: T[]) => void;
  /** Select nothing (select mode stays as it is). */
  clear: () => void;
}

export function useBulkSelection<T = number>(): BulkSelection<T> {
  const [selMode, setSelMode] = useState(false);
  const [sel, setSel] = useState<Set<T>>(() => new Set());

  const toggle = useCallback((id: T) => {
    setSel((p) => {
      const n = new Set(p);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }, []);
  const selectMany = useCallback((ids: T[]) => {
    setSel((p) => {
      const n = new Set(p);
      ids.forEach((i) => n.add(i));
      return n;
    });
  }, []);
  const deselectMany = useCallback((ids: T[]) => {
    setSel((p) => {
      const n = new Set(p);
      ids.forEach((i) => n.delete(i));
      return n;
    });
  }, []);
  const selectOnly = useCallback((ids: T[]) => setSel(new Set(ids)), []);
  const clear = useCallback(() => setSel(new Set()), []);

  return { selMode, setSelMode, sel, toggle, selectMany, deselectMany, selectOnly, clear };
}
