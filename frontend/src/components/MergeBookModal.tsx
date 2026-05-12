// v2.10.0 — Manual merge modal.
//
// Lets the user resolve a duplicate that calibre_sync's INSERT-path
// merge + post-UPDATE sweep can't handle (e.g. structural title
// differences like ": Book N" vs " N", or "(Series #N)" suffixes
// that won't normalize cleanly). Opens from BookSidebar, scoped to
// the initiator book's library. Default search is scoped to the
// same author since same-author duplicates are the overwhelmingly
// common case — a "search all authors" toggle handles the
// pen-name / mis-attributed-author edge cases.
//
// Submits to `POST /api/discovery/books/{bid}/merge?slug=…` with
// `{ other_id }`. The backend deterministically picks the winner
// (calibre+owned > calibre > owned > rest) and returns the merged
// row. The modal closes on success and calls `onChanged` so the
// parent list / sidebar refetches.

import { useEffect, useRef, useState } from "react";
import { useTheme } from "../theme";
import { api, ApiError, slugQuery } from "../api";
import { toast } from "../lib/toast";
import { Btn } from "./Btn";
import { Spin } from "./Spin";
import type { Book } from "../types";

interface MergeBookModalProps {
  book: Book; // initiator — the sidebar's current book
  onClose: () => void;
  onChanged: () => void;
}

interface SearchResponse {
  books: Book[];
  total: number;
}

interface MergeResponse {
  status: string;
  winner_id: number;
  loser_id: number;
  merged_book: Book;
}

export function MergeBookModal({
  book,
  onClose,
  onChanged,
}: MergeBookModalProps) {
  const t = useTheme();
  const slugQs = slugQuery(book.library_slug);

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Book[]>([]);
  const [selected, setSelected] = useState<Book | null>(null);
  const [loading, setLoading] = useState(false);
  const [merging, setMerging] = useState(false);
  const [err, setErr] = useState("");
  const [scopeAllAuthors, setScopeAllAuthors] = useState(false);

  // Debounce text input → live search.
  const debounceRef = useRef<number | null>(null);
  useEffect(() => {
    if (debounceRef.current !== null) {
      window.clearTimeout(debounceRef.current);
    }
    if (!query.trim()) {
      setResults([]);
      return;
    }
    debounceRef.current = window.setTimeout(() => {
      void runSearch(query);
    }, 250);
    return () => {
      if (debounceRef.current !== null) {
        window.clearTimeout(debounceRef.current);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, scopeAllAuthors]);

  const runSearch = async (q: string) => {
    setLoading(true);
    setErr("");
    try {
      const params = new URLSearchParams();
      params.set("search", q);
      params.set("per_page", "20");
      params.set("include_hidden", "true"); // a hidden row is still a valid merge partner
      if (!scopeAllAuthors && book.author_id) {
        params.set("author_id", String(book.author_id));
      }
      const slugParam = book.library_slug
        ? `&slug=${encodeURIComponent(book.library_slug)}`
        : "";
      const r = await api.get<SearchResponse>(
        `/discovery/books?${params.toString()}${slugParam}`,
      );
      // Strip the initiator itself out — you can't merge with yourself.
      setResults((r.books ?? []).filter((b) => b.id !== book.id));
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      setErr(`Search failed: ${msg}`);
      setResults([]);
    } finally {
      setLoading(false);
    }
  };

  const approveMerge = async () => {
    if (!selected) return;
    setMerging(true);
    setErr("");
    try {
      const r = await api.post<MergeResponse>(
        `/discovery/books/${book.id}/merge${slugQs}`,
        { other_id: selected.id },
      );
      toast.success(
        `Merged "${selected.title}" into "${r.merged_book.title}"`,
      );
      onChanged();
      onClose();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      setErr(`Merge failed: ${msg}`);
    } finally {
      setMerging(false);
    }
  };

  const flag = (b: Book): string => {
    // Short summary tag — what shape this row has.
    const parts: string[] = [];
    if (b.source === "calibre") parts.push("Calibre");
    else if (b.source) parts.push(b.source);
    if (b.owned) parts.push("owned");
    if (b.calibre_id) parts.push(`cal_id=${b.calibre_id}`);
    if (b.mam_torrent_id) parts.push(`mam=${b.mam_torrent_id}`);
    return parts.join(" · ");
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.5)",
        zIndex: 220,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        animation: "fadeOverlay 0.2s ease-out",
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="modal-panel"
        style={{
          background: t.bg2,
          border: `1px solid ${t.border}`,
          borderRadius: 12,
          padding: 24,
          animation: "fadeIn 0.2s ease-out",
          width: 700,
          maxWidth: "95vw",
          maxHeight: "85vh",
          overflowY: "auto",
          display: "flex",
          flexDirection: "column",
          gap: 16,
        }}
      >
        <div>
          <h2
            style={{
              fontSize: 18,
              fontWeight: 700,
              color: t.text,
              margin: 0,
            }}
          >
            Merge book — {book.title}
          </h2>
          <div style={{ fontSize: 12, color: t.td, marginTop: 4 }}>
            Search for the other row of this duplicate. The winner is
            chosen automatically (the Calibre / owned row survives).
          </div>
        </div>

        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input
            type="text"
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search by title or partial title…"
            style={{
              flex: 1,
              background: t.inp,
              border: `1px solid ${t.border}`,
              borderRadius: 8,
              padding: "8px 12px",
              color: t.text,
              fontSize: 14,
            }}
          />
          <label
            style={{
              fontSize: 12,
              color: t.td,
              display: "flex",
              gap: 6,
              alignItems: "center",
              whiteSpace: "nowrap",
            }}
          >
            <input
              type="checkbox"
              checked={scopeAllAuthors}
              onChange={(e) => setScopeAllAuthors(e.target.checked)}
            />
            All authors
          </label>
        </div>

        {err ? (
          <div
            style={{
              background: t.redb,
              border: `1px solid ${t.err}`,
              borderRadius: 8,
              padding: 12,
              color: t.err,
              fontSize: 13,
            }}
          >
            {err}
          </div>
        ) : null}

        <div style={{ minHeight: 200, display: "flex", flexDirection: "column", gap: 8 }}>
          {loading ? <Spin /> : null}
          {!loading && results.length === 0 && query.trim() ? (
            <div style={{ color: t.td, fontSize: 13 }}>
              No matches{!scopeAllAuthors ? " for this author" : ""}.
            </div>
          ) : null}
          {results.map((r) => {
            const isSel = selected?.id === r.id;
            return (
              <div
                key={r.id}
                onClick={() => setSelected(r)}
                style={{
                  background: isSel ? t.abg : t.bg3,
                  border: `1px solid ${isSel ? t.accent : t.border}`,
                  borderRadius: 8,
                  padding: "10px 12px",
                  cursor: "pointer",
                  display: "flex",
                  flexDirection: "column",
                  gap: 4,
                }}
              >
                <div
                  style={{ fontSize: 14, fontWeight: 600, color: t.text }}
                >
                  {r.title}
                </div>
                <div style={{ fontSize: 12, color: t.td }}>
                  id={r.id} · {flag(r) || "discovery"}
                </div>
              </div>
            );
          })}
        </div>

        <div
          style={{
            display: "flex",
            gap: 8,
            justifyContent: "flex-end",
            borderTop: `1px solid ${t.border}`,
            paddingTop: 12,
          }}
        >
          <Btn onClick={onClose} disabled={merging}>
            Cancel
          </Btn>
          <Btn
            onClick={approveMerge}
            disabled={!selected || merging}
            variant="primary"
          >
            {merging ? "Merging…" : "Approve Merge"}
          </Btn>
        </div>
      </div>
    </div>
  );
}
