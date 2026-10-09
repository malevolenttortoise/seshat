// The author pages' bulk actions (hide / dismiss / delete / skip MAM on
// the selected books), for DiscAuthorDetailPage and MobileAuthorDetailPage
// (wave 5b S17, issue 23), which carried them line for line, confirm and
// toasts included (G159: the same wording on both, so it lives here).
//
// Book ids are per library, so the selection is split by the library
// each book came from and each library gets its own bulk request
// (v2.12.1 #1: one request with every id hit the wrong library's books
// by id collision). Series books are only known once their section has
// loaded them: `onBooksLoaded` caches them, for Select all and the
// split.
import { useCallback, useState } from "react";
import { api, slugQuery } from "../api";
import { toast } from "../lib/toast";
import type { AuthorDetail } from "../lib/authorDetail";
import type { Book } from "../types";

export type BulkKind = "hide" | "dismiss" | "delete" | "skip-mam";

const LABELS = { hide: "Hide", dismiss: "Dismiss", delete: "Delete", "skip-mam": "Skip MAM" } as const;
// Past tense for the success toast ("Hided" / "Dismissd" before
// v2.3.4.3); Skip MAM doesn't tense well.
const PAST = { hide: "Hidden", dismiss: "Dismissed", delete: "Deleted", "skip-mam": "Marked N/A" } as const;

// One response shape for success and failure, so the totals below can
// read deleted / skipped / count on either.
type BulkResp = {
  status?: string;
  count?: number;
  deleted?: number;
  skipped?: number;
  error?: string;
};

export function useAuthorBulkActions({
  a, sel, clearSel, setSelMode, loadA, onDone,
}: {
  a: AuthorDetail | null;
  sel: Set<number>;
  clearSel: () => void;
  setSelMode: (on: boolean) => void;
  loadA: () => Promise<void> | void;
  /** After the page reloads (desktop bumps its series refresh key). */
  onDone?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  // Books the lazy series sections loaded, keyed
  // "{librarySlug || 'active'}:{series.id}".
  const [seriesBooks, setSeriesBooks] = useState<Record<string, Book[]>>({});

  const onBooksLoaded = useCallback((key: string, books: Book[]) => {
    setSeriesBooks((p) => ({ ...p, [key]: books }));
  }, []);

  // Every book on screen: standalones of every library block, plus the
  // series books loaded so far (a series nobody opened isn't selected).
  const allVisibleIds = (): number[] => {
    const ids = new Set<number>();
    if (a) {
      (a.standalone_books || []).forEach((b) => ids.add(b.id));
      Object.values(a.cross_library || {}).forEach((c) => {
        (c.author.standalone_books || []).forEach((b) => ids.add(b.id));
      });
    }
    Object.values(seriesBooks).forEach((arr) =>
      arr.forEach((b) => ids.add(b.id)),
    );
    return [...ids];
  };

  // bookId → the slug of the library it came from.
  const buildBookSlugMap = (): Map<number, string> => {
    const out = new Map<number, string>();
    const activeSlug = a?.active_library_slug || "active";
    (a?.standalone_books || []).forEach((b) => out.set(b.id, activeSlug));
    Object.entries(a?.cross_library || {}).forEach(([slug, entry]) => {
      (entry.author.standalone_books || []).forEach((b) => out.set(b.id, slug));
    });
    Object.entries(seriesBooks).forEach(([key, books]) => {
      const prefix = key.split(":", 1)[0];
      const slug = prefix === "active" ? activeSlug : prefix;
      books.forEach((b) => {
        if (!out.has(b.id)) out.set(b.id, slug);
      });
    });
    return out;
  };

  // The upstream app a library syncs from, for "skipped N X-synced".
  const slugToSyncedLabel = (slug: string): string => {
    if (slug === a?.active_library_slug) {
      return a?.active_content_type === "audiobook"
        ? "Audiobookshelf-synced"
        : "Calibre-synced";
    }
    const entry = a?.cross_library?.[slug];
    if (entry?.content_type === "audiobook") return "Audiobookshelf-synced";
    return "Calibre-synced";
  };

  const act = async (kind: BulkKind) => {
    const ids = [...sel];
    if (ids.length === 0) return;

    const slugMap = buildBookSlugMap();
    const partition = new Map<string, number[]>();
    for (const id of ids) {
      const slug = slugMap.get(id) || a?.active_library_slug || "active";
      const arr = partition.get(slug) || [];
      arr.push(id);
      partition.set(slug, arr);
    }
    const slugs = [...partition.keys()];
    const syncedLabelsInvolved = [...new Set(slugs.map(slugToSyncedLabel))];
    const syncedLabelText = syncedLabelsInvolved.length === 1
      ? syncedLabelsInvolved[0]
      : syncedLabelsInvolved.join(" / ");

    const msg =
      kind === "delete"
        ? `Delete ${ids.length} book(s)? ${syncedLabelText} books will be skipped.`
        : kind === "skip-mam"
        ? `Mark ${ids.length} book(s) as Not Applicable for MAM scanning?`
        : `${LABELS[kind]} ${ids.length} book(s)?`;
    if (!confirm(msg)) return;
    setBusy(true);
    try {
      const results = await Promise.all(
        slugs.map((slug): Promise<{ slug: string; r: BulkResp }> => {
          const slugIds = partition.get(slug)!;
          return api.post<BulkResp>(
            `/discovery/books/bulk-${kind}${slugQuery(slug)}`,
            { book_ids: slugIds },
          ).then((r) => ({ slug, r }))
            .catch((e): { slug: string; r: BulkResp } => ({
              slug, r: { error: (e as Error).message || "failed" },
            }));
        }),
      );

      // Every library failed: the first error. Otherwise any failures,
      // then the totals (deletes with a per-library skip breakdown).
      const errors = results.filter((x) => x.r.error);
      if (errors.length > 0 && errors.length === results.length) {
        toast.error(errors[0].r.error || "Bulk action failed");
      } else {
        if (errors.length > 0) {
          toast.warn(
            `Partial failure: ${errors.length} of ${results.length} ${
              errors.length === 1 ? "library" : "libraries"
            } errored. ${errors[0].r.error || ""}`,
          );
        }
        if (kind === "delete") {
          const totalDeleted = results.reduce(
            (acc, x) => acc + (x.r.deleted || 0), 0,
          );
          const skipParts = results
            .filter((x) => (x.r.skipped || 0) > 0)
            .map((x) => `${x.r.skipped} ${slugToSyncedLabel(x.slug)}`);
          const skipMsg = skipParts.length > 0
            ? `, skipped ${skipParts.join(", ")}`
            : "";
          toast.success(`Deleted ${totalDeleted} book(s)${skipMsg}`);
        } else {
          const totalCount = results.reduce(
            (acc, x) => acc + (x.r.count ?? 0), 0,
          );
          toast.success(`${PAST[kind]} ${totalCount || ids.length} book(s)`);
        }
      }
      clearSel();
      setSelMode(false);
      // Series sections re-fetch: deleted books disappear, hidden /
      // dismissed ones come back with their flags.
      setSeriesBooks({});
      await loadA();
      onDone?.();
    } catch (e) {
      toast.error((e as Error).message || `${LABELS[kind]} failed`);
    }
    setBusy(false);
  };

  return { busy, onBooksLoaded, allVisibleIds, act };
}
