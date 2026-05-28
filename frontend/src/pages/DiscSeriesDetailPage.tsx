// Series detail — read-only (v3.0.0 Phase 8).
//
// The canonical "full series" view: header (name + 3-way author-mode
// badge + counts), the contributing-authors section split into OWNERS
// (in every book — the series' I set) vs INCIDENTAL contributors (some
// books only), each linked to author detail, and the ordered book list
// with multi-author bylines. This is the target of Phase 7's "N of M"
// guest pill on the author-detail page.
//
// Read-only: series management (rename / delete / membership) lives on
// the Tools → "Series Manager". Per-book viewing/actions still work via
// the BookSidebar (those are book-level, not series-level).
//
// The nav arg is "slug:id" (cross-library) or a bare id; the slug scopes
// the fetch + the author back-links to the right library.
import { useCallback, useEffect, useState } from "react";
import { useTheme } from "../theme";
import { api, slugQuery } from "../api";
import { Load } from "../components/Load";
import { Btn } from "../components/Btn";
import { PB } from "../components/PB";
import { BGrid } from "../components/BookViews";
import { BookSidebar } from "../components/BookSidebar";
import type { Book, BookAction, NavFn } from "../types";

interface ContributingAuthor {
  author_id: number;
  name: string;
  book_count: number;
  is_owner: boolean;
}

interface SeriesDetail {
  id: number;
  name: string;
  author_mode?: "per_author" | "multi_author" | "shared";
  books?: Book[];
  contributing_authors?: ContributingAuthor[];
}

export default function DiscSeriesDetailPage({
  seriesId,
  onNav,
}: {
  seriesId: number | string;
  onNav: NavFn;
}) {
  const t = useTheme();
  const [data, setData] = useState<SeriesDetail | null>(null);
  const [ld, setLd] = useState(true);
  const [sb, setSb] = useState<Book | null>(null);
  const [sbClosing, setSbClosing] = useState(false);

  // Nav arg may be "slug:id" (cross-library) or a bare id.
  const { sid, slug } = (() => {
    const s = String(seriesId);
    if (s.includes(":")) {
      const [sl, id] = s.split(":");
      return { sid: parseInt(id) || 0, slug: sl as string | null };
    }
    return { sid: parseInt(s) || (typeof seriesId === "number" ? seriesId : 0), slug: null };
  })();

  const load = useCallback(
    (signal?: AbortSignal) => {
      setLd(true);
      const qs = slug ? `?slug=${encodeURIComponent(slug)}` : "";
      api
        .get<SeriesDetail>(`/discovery/series/${sid}${qs}`, signal)
        .then((d) => {
          setData(d);
          setLd(false);
        })
        .catch((e) => {
          if (!api.isAbort(e)) setLd(false);
        });
    },
    [sid, slug],
  );

  useEffect(() => {
    const c = new AbortController();
    load(c.signal);
    return () => c.abort();
  }, [load]);

  const closeSb = () => {
    if (!sb) return;
    setSbClosing(true);
    setTimeout(() => {
      setSb(null);
      setSbClosing(false);
    }, 200);
  };

  const onAction = async (act: BookAction, id: number, s?: string) => {
    if (act === "hide") await api.post(`/discovery/books/${id}/hide${slugQuery(s)}`);
    if (act === "dismiss") await api.post(`/discovery/books/${id}/dismiss${slugQuery(s)}`);
    if (act === "delete") await api.del(`/discovery/books/${id}${slugQuery(s)}`);
    load();
  };

  // Author back-links carry the series' library slug so the author detail
  // opens the right per-library row (mirrors the "slug:id" nav arg shape).
  const authorArg = (aid: number): string | number => (slug ? `${slug}:${aid}` : aid);

  if (ld) return <Load />;
  if (!data) return <div style={{ color: t.tf }}>Series not found</div>;

  const books = data.books || [];
  const owned = books.filter((b) => b.owned === 1).length;
  const authors = data.contributing_authors || [];
  const owners = authors.filter((a) => a.is_owner);
  const incidental = authors.filter((a) => !a.is_owner);
  const mode = data.author_mode;
  const modeLabel =
    mode === "shared" ? "Shared" : mode === "multi_author" ? "Co-authored" : "Per-author";
  const accent = mode === "shared" || mode === "multi_author";

  const authorChip = (a: ContributingAuthor, isOwner: boolean) => (
    <button
      key={a.author_id}
      onClick={() => onNav("disc-author-detail", authorArg(a.author_id))}
      title={`${a.book_count} book${a.book_count === 1 ? "" : "s"} in this series`}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "4px 10px",
        borderRadius: 6,
        fontSize: 13,
        background: isOwner ? t.abg : t.bg2,
        color: isOwner ? t.accent : t.text2,
        border: `1px solid ${isOwner ? t.abr : t.border}`,
        cursor: "pointer",
      }}
    >
      {a.name}
      <span style={{ fontSize: 11, color: isOwner ? t.accent : t.tg }}>
        · {a.book_count}
      </span>
    </button>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <Btn
        onClick={() => onNav("disc-series-browse")}
        style={{
          alignSelf: "flex-start",
          background: t.bg4,
          border: `1px solid ${t.border}`,
          borderRadius: 8,
          padding: "8px 16px",
          fontSize: 14,
        }}
      >
        ← Back to Series
      </Btn>

      {/* Header */}
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <h1 style={{ fontSize: 22, fontWeight: 700, color: t.text, margin: 0 }}>
            {data.name}
          </h1>
          <span
            style={{
              fontSize: 11,
              fontWeight: 600,
              padding: "2px 8px",
              borderRadius: 4,
              background: accent ? t.abg : t.bg,
              color: accent ? t.accent : t.tf,
              border: `1px solid ${accent ? t.abr : t.border}`,
              textTransform: "uppercase",
              letterSpacing: "0.04em",
            }}
          >
            {modeLabel}
          </span>
        </div>
        <div style={{ display: "flex", gap: 16, fontSize: 13 }}>
          <span style={{ color: t.grnt }}>{owned} owned</span>
          <span style={{ color: t.ylwt }}>{books.length - owned} missing</span>
          <span style={{ color: t.purt }}>{books.length} books</span>
        </div>
        <div style={{ maxWidth: 320 }}>
          <PB owned={owned} total={books.length} />
        </div>
      </div>

      {/* Contributing authors — owners vs incidental. */}
      {authors.length > 0 ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {owners.length > 0 ? (
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span
                style={{
                  fontSize: 11,
                  fontWeight: 700,
                  color: t.tg,
                  textTransform: "uppercase",
                  letterSpacing: "0.05em",
                  marginRight: 4,
                }}
              >
                {owners.length === 1 ? "Author" : "Authors"}
              </span>
              {owners.map((a) => authorChip(a, true))}
            </div>
          ) : null}
          {incidental.length > 0 ? (
            <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span
                style={{
                  fontSize: 11,
                  fontWeight: 700,
                  color: t.tg,
                  textTransform: "uppercase",
                  letterSpacing: "0.05em",
                  marginRight: 4,
                }}
              >
                Contributors
              </span>
              {incidental.map((a) => authorChip(a, false))}
            </div>
          ) : null}
        </div>
      ) : null}

      {/* Book list with multi-author bylines. */}
      {books.length > 0 ? (
        <BGrid
          books={books}
          onBookClick={(b) => {
            setSbClosing(false);
            setSb(b);
          }}
          showAuthor
          onAuthorClick={(aid) => onNav("disc-author-detail", authorArg(aid))}
        />
      ) : (
        <div style={{ fontSize: 14, color: t.tf, fontStyle: "italic" }}>
          No visible books in this series.
        </div>
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
