// Mobile-native hidden books page. Tiny — just a card list of
// hidden books with the Unhide action surfaced via the BookSidebar.
import { useEffect, useState } from "react";
import { api } from "../api";
import { useTheme } from "../theme";
import { BookSidebar } from "../components/BookSidebar";
import { MobileBookCard } from "../components/mobile";
import type { Book, BookAction, BooksResponse, NavFn } from "../types";

export default function MobileHiddenPage({ onNav }: { onNav: NavFn }) {
  void onNav;
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
      .catch(() => setLd(false));
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

  const onAction = async (act: BookAction, id: number) => {
    if (act === "unhide") await api.post(`/discovery/books/${id}/unhide`);
    await load();
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: 8,
        }}
      >
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: t.text }}>
          Hidden Books
        </h1>
        <span style={{ fontSize: 13, color: t.td }}>{bks.length} hidden</span>
      </div>

      {!ld && bks.length === 0 ? (
        <div
          style={{
            padding: 40,
            textAlign: "center",
            color: t.tg,
            fontSize: 14,
            background: t.bg2,
            border: `1px solid ${t.borderL}`,
            borderRadius: 12,
          }}
        >
          No hidden books.
        </div>
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 360px), 1fr))",
            gap: 8,
          }}
        >
          {bks.map((b) => (
            <MobileBookCard
              key={b.id}
              book={b}
              onClick={() => setSb(b)}
              showAuthor
            />
          ))}
        </div>
      )}

      {sb && (
        <BookSidebar
          book={sb}
          closing={sbClosing}
          onClose={closeSb}
          onAction={onAction}
          onEdit={load}
        />
      )}
    </div>
  );
}
