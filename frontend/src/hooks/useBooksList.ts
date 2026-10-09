// A Books page's list (Library, Missing, Upcoming, Hidden, …): search,
// sort, format / MAM / owned filters (persisted per page title), the
// paged load, and hide / unhide / dismiss / delete.
//
// DiscBooksPage + MobileBooksPage each carried this (wave 5b S12). The
// desktop page adds grouping (it passes `sortOverride`, `perPage`) and a
// sort direction (`sortDir`); the phone sends neither, and a value left
// out isn't sent. The shells keep the rest: view mode, grouping UI,
// desktop's multi-select tools and its scroll position kept across an
// action.
import { useCallback, useEffect, useState } from "react";
import { api, slugQuery } from "../api";
import { usePersist } from "./usePersist";
import type { Book, BookAction, BooksResponse } from "../types";

export interface UseBooksListOptions {
  /** Page title: keys the persisted search / sort / filters. */
  title: string;
  apiPath: string;
  extraParams: Record<string, string | number | boolean>;
  showFormatTabs: boolean;
  showOwnedFilter: boolean;
  /** Sent as `sort` instead of the chosen sort (desktop's grouping). */
  sortOverride?: string;
  /** Sent as `sort_dir` when given (desktop). */
  sortDir?: string;
  perPage?: number;
}

export function useBooksList({
  title, apiPath, extraParams, showFormatTabs, showOwnedFilter, sortOverride, sortDir, perPage = 60,
}: UseBooksListOptions) {
  const [bks, setBks] = useState<Book[]>([]);
  const [total, setTotal] = useState(0);
  const [pg, setPg] = useState(1);
  const [ld, setLd] = useState(true);
  const [q, setQ] = usePersist<string>(`bp_${title}_q`, "");
  const [sort, setSort] = usePersist<string>(`bp_${title}_sort`, "title");
  const [fmt, setFmt] = usePersist<string>(`bp_${title}_fmt`, "all");
  const [mamFilter, setMamFilter] = usePersist<string>(`bp_${title}_mam`, "");
  // v2.3.4.3: owned filter for the Hidden page. "all" → no owned param;
  // "owned" / "discovered" → owned=true / false.
  const [ownedFilter, setOwnedFilter] = usePersist<string>(`bp_${title}_owned`, "all");

  const sortParam = sortOverride ?? sort;

  const load = useCallback(
    (page: number = 1, signal?: AbortSignal) => {
      setLd(true);
      const init: Record<string, string> = { search: q, sort: sortParam };
      if (sortDir !== undefined) init.sort_dir = sortDir;
      init.per_page = String(perPage);
      init.page = String(page);
      for (const [k, v] of Object.entries(extraParams)) init[k] = String(v);
      const p = new URLSearchParams(init);
      if (mamFilter) p.set("mam_status", mamFilter);
      if (showFormatTabs) p.set("content_type", fmt);
      if (showOwnedFilter && ownedFilter === "owned") p.set("owned", "true");
      else if (showOwnedFilter && ownedFilter === "discovered") p.set("owned", "false");
      return api
        .get<BooksResponse>(`${apiPath}?${p}`, signal)
        .then((d) => {
          setBks(d.books);
          setTotal(d.total ?? d.books.length);
          setPg(page);
          setLd(false);
        })
        .catch((e) => {
          if (!api.isAbort(e)) setLd(false);
        });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [q, sortParam, sortDir, apiPath, mamFilter, fmt, showFormatTabs, showOwnedFilter, ownedFilter, perPage],
  );

  useEffect(() => {
    const c = new AbortController();
    load(1, c.signal);
    return () => c.abort();
  }, [load]);

  const totalPages = Math.max(1, Math.ceil(total / perPage));

  /** Hide / unhide / dismiss / delete a book, then reload the current page. */
  const onAction = async (act: BookAction, id: number, slug?: string) => {
    if (act === "hide") await api.post(`/discovery/books/${id}/hide${slugQuery(slug)}`);
    if (act === "unhide") await api.post(`/discovery/books/${id}/unhide${slugQuery(slug)}`);
    if (act === "dismiss") await api.post(`/discovery/books/${id}/dismiss${slugQuery(slug)}`);
    if (act === "delete") await api.del(`/discovery/books/${id}${slugQuery(slug)}`);
    await load(pg);
  };

  return {
    bks, total, totalPages, pg, setPg, ld, load, onAction,
    q, setQ, sort, setSort, fmt, setFmt, mamFilter, setMamFilter, ownedFilter, setOwnedFilter,
  };
}
