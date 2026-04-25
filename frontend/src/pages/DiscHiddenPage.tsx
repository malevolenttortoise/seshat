// Hidden books page.
//
// Lists every book the user has hidden via the "Hide" action elsewhere
// in the app. Each row exposes an Unhide button that flips `hidden=0`
// and refreshes the list. Hidden books DO get re-fetched on Full
// Re-Scan so their metadata stays fresh if the user later un-hides
// them.
import { useState, useEffect } from "react";
import { useTheme } from "../theme";
import { api } from "../api";
import { Btn } from "../components/Btn";
import { Load } from "../components/Load";
import { BList } from "../components/BookViews";
import { BookSidebar } from "../components/BookSidebar";
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
  const [sb, setSb] = useState<Book | null>(null);
  const [sbClosing, setSbClosing] = useState(false);

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

  const closeSb = () => {
    if (!sb) return;
    setSbClosing(true);
    setTimeout(() => {
      setSb(null);
      setSbClosing(false);
    }, 200);
  };

  const toggleSb = (b: Book) => {
    if (sb && sb.id === b.id) closeSb();
    else {
      setSbClosing(false);
      setSb(b);
    }
  };

  const onAction = async (act: BookAction, id: number) => {
    const scrollY = window.scrollY;
    if (act === "unhide") await api.post(`/discovery/books/${id}/unhide`);
    await load();
    setTimeout(() => window.scrollTo(0, scrollY), 100);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <Btn variant="ghost" onClick={() => onNav("dashboard")}>
          ← Dashboard
        </Btn>
        <h1 style={{ fontSize: 22, fontWeight: 700, color: t.text, margin: 0 }}>
          Hidden Books
        </h1>
        <span style={{ fontSize: 13, color: t.tg }}>({bks.length})</span>
      </div>
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
