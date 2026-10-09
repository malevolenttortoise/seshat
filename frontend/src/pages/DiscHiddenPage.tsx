// Hidden books page.
//
// Lists every book the user has hidden via the "Hide" action elsewhere
// in the app. Each row exposes an Unhide button that flips `hidden=0`
// and refreshes the list. Hidden books DO get re-fetched on Full
// Re-Scan so their metadata stays fresh if the user later un-hides
// them.
//
// v2.17.0 — full multi-select bar with bulk Unhide + bulk Delete.
// Hidden is the one book-listing page whose bulk operation is Unhide
// (not Hide); Delete remains available because the user may want to
// hard-purge hidden discoveries rather than just hide them.
import { useState, useEffect } from "react";
import { useTheme } from "../theme";
import { useBookSidebar } from "../hooks/useBookSidebar";
import { api, slugQuery } from "../api";
import { Btn } from "../components/Btn";
import { Load } from "../components/Load";
import { BList } from "../components/BookViews";
import { BookSidebar } from "../components/BookSidebar";
import { toast } from "../lib/toast";
import { useViewport } from "../hooks/useViewport";
import { useMobileCodepath } from "../components/mobile";
import MobileHiddenPage from "./MobileHiddenPage";
import type { NavFn, Book, BooksResponse, BookAction } from "../types";

export default function HiddenPage(props: { onNav: NavFn }) {
  // Mobile codepath catches phones, iPads, and any touch device.
  const vp = useViewport();
  if (useMobileCodepath(vp)) {
    return <MobileHiddenPage {...props} />;
  }
  return <DesktopHiddenPage {...props} />;
}

function DesktopHiddenPage({ onNav }: { onNav: NavFn }) {
  const t = useTheme();
  const [bks, setBks] = useState<Book[]>([]);
  const [ld, setLd] = useState(true);
  const { sb, sbClosing, closeSb, toggleSb } = useBookSidebar();
  // v2.17.0 — multi-select state.
  const [selMode, setSelMode] = useState(false);
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);

  const load = () => {
    setLd(true);
    return api
      .get<BooksResponse>("/discovery/books/hidden")
      .then((d) => {
        setBks(d.books);
        setLd(false);
      })
      .catch(console.error);
  };

  useEffect(() => {
    load();
  }, []);

  const onAction = async (act: BookAction, id: number, slug?: string) => {
    const scrollY = window.scrollY;
    if (act === "unhide") await api.post(`/discovery/books/${id}/unhide${slugQuery(slug)}`);
    await load();
    setTimeout(() => window.scrollTo(0, scrollY), 100);
  };

  const toggleSel = (id: number) => {
    setSel((p) => {
      const n = new Set(p);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  };
  const selectAll = () => setSel(new Set(bks.map((b) => b.id)));
  const deselectAll = () => setSel(new Set());

  const bulkUnhide = async () => {
    if (!confirm(`Un-hide ${sel.size} book(s)? They'll return to their original listings.`)) return;
    setBusy(true);
    try {
      const r = await api.post<{ count?: number; error?: string }>(
        "/discovery/books/bulk-unhide",
        { book_ids: [...sel] },
      );
      if (r.error) toast.error(r.error);
      else {
        toast.success(`Un-hid ${r.count ?? sel.size} book(s)`);
        setSel(new Set());
        setSelMode(false);
        load();
      }
    } catch (e) {
      toast.error((e as Error).message || "Error un-hiding books");
    }
    setBusy(false);
  };

  const bulkDelete = async () => {
    if (!confirm(
      `Delete ${sel.size} hidden book(s)? Calibre / Audiobookshelf-synced books will be skipped.`,
    )) return;
    setBusy(true);
    try {
      const r = await api.post<{ deleted?: number; skipped?: number; error?: string }>(
        "/discovery/books/bulk-delete",
        { book_ids: [...sel] },
      );
      if (r.error) toast.error(r.error);
      else {
        const parts = [`Deleted ${r.deleted ?? 0}`];
        if (r.skipped) parts.push(`skipped ${r.skipped} library-synced`);
        toast.success(parts.join(", "));
        setSel(new Set());
        setSelMode(false);
        load();
      }
    } catch (e) {
      toast.error((e as Error).message || "Error deleting books");
    }
    setBusy(false);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <Btn variant="ghost" onClick={() => onNav("dashboard")}>
          ← Dashboard
        </Btn>
        <h1 style={{ fontSize: 22, fontWeight: 700, color: t.text, margin: 0 }}>
          Hidden Books
        </h1>
        <span style={{ fontSize: 13, color: t.tg }}>({bks.length})</span>
        <div style={{ flex: 1 }} />
        {bks.length > 0 && (
          <Btn
            size="sm"
            variant={selMode ? "accent" : "default"}
            onClick={() => {
              setSelMode(!selMode);
              if (selMode) setSel(new Set());
            }}
          >
            {selMode ? "Cancel" : "Select"}
          </Btn>
        )}
      </div>
      {selMode && (
        <div
          style={{
            display: "flex",
            gap: 8,
            alignItems: "center",
            padding: "10px 12px",
            background: t.bg2,
            border: `1px solid ${t.borderL}`,
            borderRadius: 8,
            flexWrap: "wrap",
          }}
        >
          <span style={{ fontSize: 13, fontWeight: 600, color: t.text2 }}>
            {sel.size} book{sel.size === 1 ? "" : "s"} selected
          </span>
          {sel.size > 0 ? (
            <>
              <Btn
                size="sm"
                onClick={bulkUnhide}
                disabled={busy}
                title="Un-hide selected books (they return to their original listings)"
                style={{
                  background: t.grn + "22",
                  color: t.grnt,
                  border: `1px solid ${t.grn}44`,
                }}
              >
                Unhide
              </Btn>
              <Btn
                size="sm"
                onClick={bulkDelete}
                disabled={busy}
                title="Delete selected hidden books (Calibre / ABS-synced skipped)"
                style={{
                  background: t.red + "22",
                  color: t.red,
                  border: `1px solid ${t.red}44`,
                }}
              >
                Delete
              </Btn>
              <span
                style={{
                  width: 1,
                  height: 20,
                  background: t.border,
                  margin: "0 4px",
                }}
              />
            </>
          ) : null}
          <Btn size="sm" onClick={selectAll} disabled={busy}>
            Select All
          </Btn>
          {sel.size > 0 ? (
            <Btn size="sm" onClick={deselectAll} disabled={busy}>
              Deselect All
            </Btn>
          ) : null}
        </div>
      )}
      {ld ? (
        <Load />
      ) : bks.length === 0 ? (
        <div style={{ textAlign: "center", padding: 60, color: t.tg }}>
          No hidden books
        </div>
      ) : (
        <BList
          books={bks}
          showAuthor
          onAction={onAction}
          onBookClick={toggleSb}
          selMode={selMode}
          sel={sel}
          onToggleSel={toggleSel}
        />
      )}
      {sb ? (
        <BookSidebar
          book={sb}
          closing={sbClosing}
          onClose={closeSb}
          onAction={onAction}
          onEdit={load}
        />
      ) : null}
    </div>
  );
}
