// v2.3.3 Series Manager → Manage Members modal.
//
// Replaces the old "promote / demote" verbs with author-list
// membership semantics. The series's authority (per-author vs.
// shared) is computed server-side from the resulting distinct
// author count, so the user no longer thinks about it directly.
//
// Layout:
//   - Top: list of authors currently on this series, with a Remove
//     button per row (detaches every book by that author from the
//     series in one shot).
//   - Bottom: "Add author" flow — search authors, then pick which
//     of their books to add. Books may be on other series; adding
//     them moves them off the source. The source series's authority
//     auto-flips on the backend.

import { useEffect, useState } from "react";
import { useTheme } from "../theme";
import { api, ApiError } from "../api";
import { Btn } from "./Btn";
import { Spin } from "./Spin";

interface SeriesAuthor {
  author_id: number;
  name: string;
  book_count: number;
}

interface AuthorPick {
  id: number;
  name: string;
}

interface Book {
  id: number;
  title: string;
  series_id: number | null;
  series_name: string | null;
  series_index: number | null;
  cover_path: string | null;
  cover_url: string | null;
  audiobookshelf_id: string | null;
  owned: number;
}

interface ManageMembersModalProps {
  seriesId: number;
  seriesName: string;
  onClose: () => void;
  onChanged: () => void; // parent refresh hook (run after every mutation)
}

export function ManageMembersModal({
  seriesId,
  seriesName,
  onClose,
  onChanged,
}: ManageMembersModalProps) {
  const t = useTheme();
  const [authors, setAuthors] = useState<SeriesAuthor[] | null>(null);
  // v3.0.0 Phase 7 — stored author_mode (ADR-0010) drives the 3-way header label.
  const [authorMode, setAuthorMode] = useState<string | null>(null);
  const [busyAuthorId, setBusyAuthorId] = useState<number | null>(null);
  const [err, setErr] = useState("");

  // Add-author flow state.
  const [authorQuery, setAuthorQuery] = useState("");
  const [authorMatches, setAuthorMatches] = useState<AuthorPick[]>([]);
  const [pickedAuthor, setPickedAuthor] = useState<AuthorPick | null>(null);
  const [pickedBooks, setPickedBooks] = useState<Book[] | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [adding, setAdding] = useState(false);

  const refresh = () => {
    api
      .get<{ series_id: number; author_mode?: string | null; authors: SeriesAuthor[] }>(
        `/discovery/series/${seriesId}/authors`,
      )
      .then((r) => {
        setAuthors(r.authors);
        setAuthorMode(r.author_mode ?? null);
      })
      .catch((e) => {
        console.error(e);
        setErr(`Failed to load authors: ${(e as Error).message}`);
        setAuthors([]);
      });
  };

  useEffect(refresh, [seriesId]);

  // Author autocomplete — debounced.
  useEffect(() => {
    if (pickedAuthor) return; // already chose; stop searching
    const q = authorQuery.trim();
    if (!q) {
      setAuthorMatches([]);
      return;
    }
    const timer = setTimeout(() => {
      api
        .get<{ authors: AuthorPick[] }>(
          `/discovery/authors?search=${encodeURIComponent(q)}&include_orphans=true`,
        )
        .then((r) => setAuthorMatches(r.authors.slice(0, 10)))
        .catch(() => setAuthorMatches([]));
    }, 200);
    return () => clearTimeout(timer);
  }, [authorQuery, pickedAuthor]);

  // Once the user picks an author, load all their books (any series,
  // standalone too — Mark wants a single click to move books across
  // series).
  useEffect(() => {
    if (!pickedAuthor) {
      setPickedBooks(null);
      setSelected(new Set());
      return;
    }
    api
      .get<{ books: Book[] }>(
        `/discovery/books?author_id=${pickedAuthor.id}&per_page=500&sort=title&sort_dir=asc`,
      )
      .then((r) => setPickedBooks(r.books))
      .catch((e) => {
        console.error(e);
        setErr(`Failed to load books: ${(e as Error).message}`);
        setPickedBooks([]);
      });
  }, [pickedAuthor]);

  const removeAuthor = async (a: SeriesAuthor) => {
    if (
      !window.confirm(
        `Detach ${a.book_count} book${a.book_count === 1 ? "" : "s"} ` +
          `by ${a.name} from "${seriesName}"?`,
      )
    )
      return;
    setBusyAuthorId(a.author_id);
    setErr("");
    try {
      await api.del(
        `/discovery/series/${seriesId}/authors/${a.author_id}`,
      );
      onChanged();
      refresh();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      setErr(`Remove failed: ${msg}`);
    } finally {
      setBusyAuthorId(null);
    }
  };

  const addPickedBooks = async () => {
    if (!pickedAuthor || selected.size === 0) return;
    setAdding(true);
    setErr("");
    try {
      await api.post(`/discovery/series/${seriesId}/authors`, {
        author_id: pickedAuthor.id,
        book_ids: Array.from(selected),
      });
      // Reset add-flow state and refresh both panels.
      setAuthorQuery("");
      setAuthorMatches([]);
      setPickedAuthor(null);
      setPickedBooks(null);
      setSelected(new Set());
      onChanged();
      refresh();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      setErr(`Add failed: ${msg}`);
    } finally {
      setAdding(false);
    }
  };

  const toggleBook = (id: number) =>
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const totalBooks = authors
    ? authors.reduce((sum, a) => sum + a.book_count, 0)
    : 0;
  // v3.0.0 Phase 7 — 3-way label from the stored author_mode (ADR-0010);
  // fall back to a count guess only until the mode loads.
  const modeLabel =
    authorMode === "multi_author"
      ? "Co-authored"
      : authorMode === "shared"
        ? "Shared"
        : authorMode === "per_author"
          ? "Per-author"
          : authors && authors.length >= 2
            ? "Shared"
            : "Per-author";
  const accentBadge =
    authorMode === "shared" ||
    authorMode === "multi_author" ||
    (!authorMode && !!authors && authors.length >= 2);

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.5)",
        zIndex: 200,
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
          width: 720,
          maxWidth: "92vw",
          maxHeight: "85vh",
          overflowY: "auto",
          display: "flex",
          flexDirection: "column",
          gap: 18,
        }}
      >
        {/* Header */}
        <div>
          <h2
            style={{
              fontSize: 18,
              fontWeight: 700,
              color: t.text,
              margin: 0,
              display: "flex",
              alignItems: "center",
              gap: 10,
              flexWrap: "wrap",
            }}
          >
            <span>Manage Members — {seriesName}</span>
            <span
              style={{
                fontSize: 11,
                fontWeight: 600,
                padding: "2px 8px",
                borderRadius: 4,
                background: accentBadge ? t.abg : t.bg,
                color: accentBadge ? t.accent : t.tf,
                border: `1px solid ${accentBadge ? t.abr : t.border}`,
                textTransform: "uppercase",
                letterSpacing: "0.04em",
              }}
            >
              {modeLabel}
            </span>
          </h2>
          <div style={{ fontSize: 12, color: t.td, marginTop: 4 }}>
            {authors === null
              ? "Loading…"
              : `${authors.length} author${authors.length === 1 ? "" : "s"}, ` +
                `${totalBooks} book${totalBooks === 1 ? "" : "s"}`}
          </div>
        </div>

        {err ? (
          <div
            style={{
              fontSize: 13,
              color: t.redt || t.red,
              background: `${t.red}22`,
              border: `1px solid ${t.red}66`,
              borderRadius: 6,
              padding: "8px 10px",
            }}
          >
            {err}
          </div>
        ) : null}

        {/* Section A: current authors */}
        <section style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div
            style={{
              fontSize: 12,
              fontWeight: 700,
              color: t.tg,
              textTransform: "uppercase",
              letterSpacing: "0.06em",
            }}
          >
            Current authors
          </div>
          {authors === null ? (
            <Spin />
          ) : authors.length === 0 ? (
            <div style={{ fontSize: 13, color: t.tg, fontStyle: "italic" }}>
              No books on this series yet. Add an author below to populate it.
            </div>
          ) : (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 6,
                background: t.bg,
                border: `1px solid ${t.border}`,
                borderRadius: 8,
                overflow: "hidden",
              }}
            >
              {authors.map((a) => (
                <div
                  key={a.author_id}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    padding: "10px 14px",
                    borderBottom: `1px solid ${t.borderL}`,
                    gap: 12,
                  }}
                >
                  <div style={{ display: "flex", flexDirection: "column" }}>
                    <span
                      style={{ fontSize: 14, fontWeight: 500, color: t.text }}
                    >
                      {a.name}
                    </span>
                    <span style={{ fontSize: 12, color: t.tf }}>
                      {a.book_count} book{a.book_count === 1 ? "" : "s"}
                    </span>
                  </div>
                  <Btn
                    onClick={() => removeAuthor(a)}
                    disabled={busyAuthorId !== null}
                    variant="ghost"
                    size="sm"
                  >
                    {busyAuthorId === a.author_id ? <Spin /> : null} Remove
                  </Btn>
                </div>
              ))}
            </div>
          )}
        </section>

        {/* Section B: add author */}
        <section style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div
            style={{
              fontSize: 12,
              fontWeight: 700,
              color: t.tg,
              textTransform: "uppercase",
              letterSpacing: "0.06em",
            }}
          >
            Add an author
          </div>

          {!pickedAuthor ? (
            <div style={{ position: "relative" }}>
              <input
                type="search"
                value={authorQuery}
                onChange={(e) => setAuthorQuery(e.target.value)}
                placeholder="Search authors…"
                style={{
                  width: "100%",
                  padding: "8px 12px",
                  fontSize: 14,
                  background: t.inp,
                  color: t.text,
                  border: `1px solid ${t.border}`,
                  borderRadius: 6,
                  boxSizing: "border-box",
                }}
              />
              {authorMatches.length > 0 ? (
                <div
                  style={{
                    marginTop: 4,
                    background: t.bg,
                    border: `1px solid ${t.border}`,
                    borderRadius: 6,
                    overflow: "hidden",
                    maxHeight: 220,
                    overflowY: "auto",
                  }}
                >
                  {authorMatches.map((a) => (
                    <button
                      key={a.id}
                      onClick={() => {
                        setPickedAuthor(a);
                        setAuthorQuery(a.name);
                        setAuthorMatches([]);
                      }}
                      style={{
                        display: "block",
                        width: "100%",
                        textAlign: "left",
                        padding: "8px 12px",
                        background: "none",
                        border: "none",
                        color: t.text,
                        cursor: "pointer",
                        fontSize: 13,
                        borderBottom: `1px solid ${t.borderL}`,
                      }}
                      onMouseEnter={(e) =>
                        (e.currentTarget.style.background = t.bg2)
                      }
                      onMouseLeave={(e) =>
                        (e.currentTarget.style.background = "none")
                      }
                    >
                      {a.name}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          ) : (
            <BookPicker
              author={pickedAuthor}
              books={pickedBooks}
              selected={selected}
              onToggle={toggleBook}
              currentSeriesId={seriesId}
              onClear={() => {
                setPickedAuthor(null);
                setAuthorQuery("");
                setSelected(new Set());
              }}
            />
          )}
        </section>

        {/* Footer */}
        <div
          style={{
            display: "flex",
            gap: 8,
            justifyContent: "flex-end",
            borderTop: `1px solid ${t.borderL}`,
            paddingTop: 14,
          }}
        >
          <Btn variant="ghost" onClick={onClose}>
            Close
          </Btn>
          {pickedAuthor ? (
            <Btn
              variant="accent"
              onClick={addPickedBooks}
              disabled={adding || selected.size === 0}
            >
              {adding ? <Spin /> : null}{" "}
              {selected.size > 0
                ? `Add ${selected.size} book${selected.size === 1 ? "" : "s"}`
                : "Pick books to add"}
            </Btn>
          ) : null}
        </div>
      </div>
    </div>
  );
}

interface BookPickerProps {
  author: AuthorPick;
  books: Book[] | null;
  selected: Set<number>;
  onToggle: (id: number) => void;
  currentSeriesId: number;
  onClear: () => void;
}

function BookPicker({
  author,
  books,
  selected,
  onToggle,
  currentSeriesId,
  onClear,
}: BookPickerProps) {
  const t = useTheme();
  const [titleFilter, setTitleFilter] = useState("");

  const visible = (books || []).filter(
    (b) =>
      !titleFilter.trim() ||
      b.title.toLowerCase().includes(titleFilter.trim().toLowerCase()),
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 10,
        }}
      >
        <div style={{ fontSize: 13, color: t.tf }}>
          Picking books by{" "}
          <span style={{ color: t.text, fontWeight: 600 }}>{author.name}</span>
        </div>
        <Btn variant="ghost" size="sm" onClick={onClear}>
          Change author
        </Btn>
      </div>
      <input
        type="search"
        value={titleFilter}
        onChange={(e) => setTitleFilter(e.target.value)}
        placeholder="Filter by title…"
        style={{
          padding: "6px 10px",
          fontSize: 13,
          background: t.inp,
          color: t.text,
          border: `1px solid ${t.border}`,
          borderRadius: 6,
        }}
      />
      {books === null ? (
        <Spin />
      ) : visible.length === 0 ? (
        <div style={{ fontSize: 13, color: t.tg, fontStyle: "italic" }}>
          {books.length === 0
            ? "This author has no books in the active library."
            : "No books match your filter."}
        </div>
      ) : (
        <div
          style={{
            maxHeight: 320,
            overflowY: "auto",
            background: t.bg,
            border: `1px solid ${t.border}`,
            borderRadius: 8,
          }}
        >
          {visible.map((b) => {
            const isSelected = selected.has(b.id);
            const onCurrent = b.series_id === currentSeriesId;
            return (
              <label
                key={b.id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                  padding: "8px 12px",
                  borderBottom: `1px solid ${t.borderL}`,
                  cursor: onCurrent ? "default" : "pointer",
                  background: isSelected ? `${t.accent}18` : undefined,
                  opacity: onCurrent ? 0.55 : 1,
                }}
              >
                <input
                  type="checkbox"
                  checked={isSelected}
                  disabled={onCurrent}
                  onChange={() => onToggle(b.id)}
                />
                {/* Cover thumbnail. Falls back gracefully if the
                    cover endpoint 404s for this book. */}
                <div
                  style={{
                    width: 36,
                    height: 54,
                    background: t.bg3,
                    borderRadius: 3,
                    overflow: "hidden",
                    flexShrink: 0,
                  }}
                >
                  <img
                    src={`/api/discovery/covers/${b.id}`}
                    loading="lazy"
                    alt=""
                    style={{
                      width: "100%",
                      height: "100%",
                      objectFit: "cover",
                    }}
                    onError={(e) => {
                      (e.target as HTMLImageElement).style.display = "none";
                    }}
                  />
                </div>
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    flex: 1,
                    minWidth: 0,
                  }}
                >
                  <span
                    style={{
                      fontSize: 13,
                      color: t.text,
                      fontWeight: 500,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {b.title}
                  </span>
                  <span style={{ fontSize: 11, color: t.tf }}>
                    {onCurrent ? (
                      <em>already on this series</em>
                    ) : b.series_name ? (
                      <>
                        currently in: <strong>{b.series_name}</strong>
                      </>
                    ) : (
                      <em>standalone</em>
                    )}
                  </span>
                </div>
              </label>
            );
          })}
        </div>
      )}
    </div>
  );
}
