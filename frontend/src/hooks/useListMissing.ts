// The author page's "List missing books" switch (D6 / F18, G200): one per
// library block, for that library's author row. Off, Seshat stops scanning
// the author and hides the missing books already listed (Hidden →
// Filtered, "Author off"); on again shows them at once.
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { toast } from "../lib/toast";

interface ListMissingStatus {
  on: boolean;
  /** On: how many missing books turning it off would hide. Off: how many it hid. */
  missing: number;
}

export function useListMissing({
  authorId, slug, authorName, onChanged,
}: {
  authorId: number | null | undefined;
  slug: string | null | undefined;
  authorName: string;
  /** After a switch (the page reloads the author: the missing books moved). */
  onChanged?: () => void;
}) {
  const [st, setSt] = useState<ListMissingStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const path = authorId
    ? `/discovery/authors/${authorId}/list-missing${slug ? `?slug=${encodeURIComponent(slug)}` : ""}`
    : null;

  const load = useCallback(
    (signal?: AbortSignal) => {
      if (!path) return Promise.resolve();
      return api
        .get<ListMissingStatus>(path, signal)
        .then((d) => {
          setSt(d);
          setErr(null);
        })
        .catch((e) => {
          if (api.isAbort(e)) return;
          setErr((e as Error).message || String(e));
        });
    },
    [path],
  );

  useEffect(() => {
    const c = new AbortController();
    load(c.signal);
    return () => c.abort();
  }, [load]);

  const toggle = async () => {
    if (!path || !st || busy) return;
    setBusy(true);
    try {
      const r = await api.put<{ on: boolean; hidden?: number; shown?: number }>(path, { on: !st.on });
      toast.success(
        r.on
          ? `Listing missing books for ${authorName} again: ${r.shown ?? 0} shown`
          : `${authorName}: missing books no longer listed, ${r.hidden ?? 0} hidden`,
      );
      await load();
      onChanged?.();
    } catch (e) {
      toast.error((e as Error).message || "Couldn't change the switch");
    }
    setBusy(false);
  };

  return { st, err, busy, toggle };
}
