/**
 * v3.10.0 — prev/next walking of the authors list from the detail page.
 *
 * The list page is the only place that knows the order you actually saw.
 * `GET /authors` returns the whole filtered set unpaginated, and the page
 * then applies the **letter filter and pagination client-side**
 * (`getLetterKey`). So a server-side "neighbours" endpoint would have to
 * re-implement `getLetterKey` to reproduce that order — two copies of the
 * ordering rules, which drift, and the symptom would be "Next silently
 * skipped someone".
 *
 * Instead the list snapshots the ordered nav-args on the way out and the
 * detail page reads them back. Guaranteed to match what was on screen,
 * no extra request, no new endpoint.
 *
 * The snapshot is deliberately **frozen at click time**. For walking a
 * list that's the desired behaviour — the order stays put while you work
 * through it, even as you blacklist things or books get retracted
 * underneath. It is refreshed every time you go back and click in again.
 *
 * Entries are nav-args in the page's own `"slug:id"` / `id` form (see
 * `navArg`), not bare ids — an author id is per-library, so the slug is
 * load-bearing for cross-library rows.
 */
import { useMemo } from "react";

export type WalkEntry = string | number;

// sessionStorage, `cl_` prefix — same backing + convention as usePersist.
const KEY = "cl_author_walk";

/** Called by the list page as it hands off to a detail page. */
export function saveAuthorWalk(entries: WalkEntry[]): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(entries.map(String)));
  } catch {
    // Private mode / quota — walking is a convenience, never a
    // correctness feature, so degrade to "no prev/next" silently.
  }
}

function readWalk(): string[] {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.map(String) : [];
  } catch {
    return [];
  }
}

export interface AuthorWalk {
  /** nav-arg of the previous author, or null at the start / not in list. */
  prev: string | null;
  /** nav-arg of the next author, or null at the end / not in list. */
  next: string | null;
  /** 1-based position, or 0 when the current author isn't in the walk. */
  index: number;
  /** Size of the snapshot, or 0 when there is none. */
  total: number;
}

/**
 * Resolve prev/next for `current` within the last saved walk.
 *
 * Returns all-empty when the author isn't in the snapshot — landing on a
 * detail page directly (deep link, cross-library jump, a stale snapshot
 * from a different filter) simply shows no walk controls rather than
 * offering navigation that would jump somewhere unrelated.
 */
export function useAuthorWalk(current: WalkEntry | null | undefined): AuthorWalk {
  return useMemo(() => {
    const empty: AuthorWalk = { prev: null, next: null, index: 0, total: 0 };
    if (current === null || current === undefined) return empty;
    const entries = readWalk();
    if (entries.length === 0) return empty;

    const cur = String(current);
    let i = entries.indexOf(cur);
    if (i === -1) {
      // Tolerate the bare-id form when the snapshot holds "slug:id"
      // (and vice versa): the list emits `navArg`, but a detail page can
      // be reached by a path that dropped the slug.
      const bare = cur.includes(":") ? cur.split(":")[1] : cur;
      i = entries.findIndex(
        (e) => e === bare || (e.includes(":") && e.split(":")[1] === bare),
      );
    }
    if (i === -1) return empty;

    return {
      prev: i > 0 ? entries[i - 1] : null,
      next: i < entries.length - 1 ? entries[i + 1] : null,
      index: i + 1,
      total: entries.length,
    };
  }, [current]);
}
