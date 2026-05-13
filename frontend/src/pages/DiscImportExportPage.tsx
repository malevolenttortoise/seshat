// Import / Export page.
//
// Two flows:
//   - Import: paste a list of book URLs from any supported source
//     (Goodreads, Hardcover, Amazon, Open Library, Google Books,
//     Kobo, IBDB), preview the parsed metadata, and add the
//     survivors to the library as unowned books. The preview step
//     is intentional so the user can drop bad matches before they
//     hit the database. URL parsing + per-source fetch dispatch
//     lives in `app/discovery/url_import.py` on the backend.
//   - Export: ExportModal handles the actual download — this page
//     just opens it.
import { useState } from "react";
import { useTheme } from "../theme";
import { api } from "../api";
import { Btn } from "../components/Btn";
import { Spin } from "../components/Spin";
import { ExportModal } from "../components/ExportModal";
import { useViewport } from "../hooks/useViewport";
import { useMobileCodepath } from "../components/mobile";
import MobileImportExportPage from "./MobileImportExportPage";

// Preview-row status set emitted by /discovery/books/import-preview
// plus "added" — applied client-side after a successful add so the
// row flips badge without a refetch.
type ImportStatus = "new" | "owned" | "tracked" | "error" | "added";

interface SeriesOption {
  name: string;
  position?: string | number | null;
}

interface ImportBook {
  title?: string;
  author_name?: string;
  series_name?: string;
  series_index?: string | number;
  pub_date?: string;
  cover_url?: string;
  series_options?: SeriesOption[];
}

interface ImportPreviewRow {
  status: ImportStatus;
  book?: ImportBook;
  error?: string;
}

interface ImportPreviewResponse {
  results?: ImportPreviewRow[];
}

interface ImportAddResponse {
  added: number;
  updated: number;
  error?: boolean;
}

export default function ImportExportPage() {
  const vp = useViewport();
  if (useMobileCodepath(vp)) return <MobileImportExportPage />;
  return <DesktopImportExportPage />;
}

function DesktopImportExportPage() {
  const t = useTheme();
  const [urls, setUrls] = useState("");
  const [results, setResults] = useState<ImportPreviewRow[] | null>(null);
  const [fetching, setFetching] = useState(false);
  const [progress, setProgress] = useState("");
  const [adding, setAdding] = useState(false);
  const [addResult, setAddResult] = useState<ImportAddResponse | null>(null);
  const [showExp, setShowExp] = useState(false);

  const fetchPreview = async () => {
    const lines = urls
      .split("\n")
      .map((u) => u.trim())
      .filter((u) => u.startsWith("http"));
    if (!lines.length) return;
    setFetching(true);
    setResults(null);
    setAddResult(null);
    setProgress(`Fetching ${lines.length} book(s)...`);
    try {
      const d = await api.post<ImportPreviewResponse>(
        "/discovery/books/import-preview",
        { urls: lines },
      );
      setResults(d.results || []);
      setProgress("");
    } catch {
      setProgress("Error fetching books");
    }
    setFetching(false);
  };

  const addBooks = async (books: ImportBook[]) => {
    setAdding(true);
    setAddResult(null);
    try {
      const d = await api.post<ImportAddResponse>(
        "/discovery/books/import-add",
        { books },
      );
      setAddResult(d);
      // Re-check: mark added ones in results.
      if (results) {
        setResults((prev) =>
          (prev || []).map((r) => {
            if (
              r.status === "new" &&
              books.some((b) => b.title === r.book?.title)
            )
              return { ...r, status: "added" };
            return r;
          }),
        );
      }
    } catch {
      setAddResult({ added: 0, updated: 0, error: true });
    }
    setAdding(false);
  };

  const newBooks: ImportPreviewRow[] = results
    ? results.filter((r) => r.status === "new" && r.book)
    : [];
  const statusColors: Record<ImportStatus, string> = {
    new: t.grnt,
    owned: t.cyant,
    tracked: t.ylwt,
    error: t.redt,
    added: t.grnt,
  };
  const statusLabels: Record<ImportStatus, string> = {
    new: "New — will be added",
    owned: "Already in Calibre",
    tracked: "Already tracked (missing)",
    error: "Error",
    added: "✓ Added",
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
      <h1 style={{ fontSize: 24, fontWeight: 700, color: t.text, margin: 0 }}>
        Import / Export
      </h1>

      {/* Import Section */}
      <div style={{ background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: 24 }}>
        <h2 style={{ fontSize: 18, fontWeight: 600, color: t.text, margin: "0 0 4px" }}>
          Import Books
        </h2>
        <p style={{ fontSize: 13, color: t.td, margin: "0 0 16px" }}>
          Paste book URLs below, one per line. Supported sources: Goodreads, Hardcover, Amazon (<code style={{ background: t.bg3, padding: "1px 4px", borderRadius: 3 }}>/dp/&#123;ASIN&#125;</code>), Open Library (<code style={{ background: t.bg3, padding: "1px 4px", borderRadius: 3 }}>/works</code>, <code style={{ background: t.bg3, padding: "1px 4px", borderRadius: 3 }}>/books</code>, <code style={{ background: t.bg3, padding: "1px 4px", borderRadius: 3 }}>/isbn</code>), Google Books, Kobo, IBDB. Books will be checked against your library before adding.
        </p>
        <textarea
          value={urls}
          onChange={(e) => setUrls(e.target.value)}
          placeholder={
            "https://www.goodreads.com/book/show/12345\nhttps://hardcover.app/books/some-book\nhttps://www.amazon.com/dp/B0CJDP9MNL\nhttps://openlibrary.org/works/OL15161W\nhttps://books.google.com/books?id=ABC"
          }
          rows={6}
          style={{
            width: "100%",
            padding: 12,
            background: t.bg3,
            border: `1px solid ${t.borderL}`,
            borderRadius: 8,
            color: t.text2,
            fontSize: 13,
            fontFamily: "monospace",
            resize: "vertical",
          }}
        />
        <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 12 }}>
          <Btn variant="accent" onClick={fetchPreview} disabled={fetching || !urls.trim()}>
            {fetching ? (
              <>
                <Spin /> Fetching...
              </>
            ) : (
              "Fetch & Preview"
            )}
          </Btn>
          {progress ? <span style={{ fontSize: 12, color: t.td }}>{progress}</span> : null}
        </div>
      </div>

      {/* Preview Results */}
      {results ? (
        <div style={{ background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: 24 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
            <h2 style={{ fontSize: 18, fontWeight: 600, color: t.text, margin: 0 }}>
              Preview ({results.length} books)
            </h2>
            {newBooks.length > 0 ? (
              <Btn
                variant="accent"
                onClick={() =>
                  addBooks(
                    newBooks
                      .map((r) => r.book)
                      .filter((b): b is ImportBook => !!b),
                  )
                }
                disabled={adding}
              >
                {adding ? (
                  <>
                    <Spin /> Adding...
                  </>
                ) : (
                  `Add ${newBooks.length} New Book${newBooks.length > 1 ? "s" : ""}`
                )}
              </Btn>
            ) : null}
          </div>
          {addResult ? (
            <div
              style={{
                padding: 10,
                borderRadius: 8,
                background: addResult.error ? t.bg4 : t.grn + "22",
                border: `1px solid ${addResult.error ? t.redt : t.grn}`,
                marginBottom: 12,
                fontSize: 13,
                color: addResult.error ? t.redt : t.grnt,
              }}
            >
              {addResult.error
                ? "Error adding books"
                : `Added ${addResult.added} new, updated ${addResult.updated} existing`}
            </div>
          ) : null}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {results.map((r, i) => (
              <div
                key={i}
                style={{
                  display: "flex",
                  gap: 12,
                  alignItems: "center",
                  padding: "10px 14px",
                  background: t.bg3,
                  border: `1px solid ${t.borderL}`,
                  borderRadius: 8,
                }}
              >
                {r.book?.cover_url ? (
                  <img
                    src={r.book.cover_url}
                    alt=""
                    style={{ width: 40, height: 60, objectFit: "cover", borderRadius: 4, flexShrink: 0 }}
                  />
                ) : (
                  <div style={{ width: 40, height: 60, background: t.bg4, borderRadius: 4, flexShrink: 0 }} />
                )}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      fontSize: 14,
                      fontWeight: 600,
                      color: t.text2,
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {r.book?.title || "Unknown"}
                  </div>
                  <div style={{ fontSize: 12, color: t.td }}>
                    {r.book?.author_name || ""}
                    {r.book?.series_options ? (
                      <span style={{ marginLeft: 6 }}>
                        <select
                          value={r.book.series_name || ""}
                          onChange={(e) => {
                            const picked = r.book!.series_options!.find((o) => o.name === e.target.value);
                            setResults((prev) =>
                              (prev || []).map((p, j) =>
                                j === i
                                  ? {
                                      ...p,
                                      book: {
                                        ...(p.book as ImportBook),
                                        series_name: picked?.name || "",
                                        series_index: picked?.position || "",
                                      },
                                    }
                                  : p,
                              ),
                            );
                          }}
                          style={{
                            padding: "1px 4px",
                            borderRadius: 3,
                            border: `1px solid ${t.border}`,
                            background: t.inp,
                            color: t.purt,
                            fontSize: 11,
                          }}
                        >
                          {r.book.series_options.map((o) => (
                            <option key={o.name} value={o.name}>
                              {o.name}
                              {o.position ? ` #${o.position}` : ""}
                            </option>
                          ))}
                          <option value="">None</option>
                        </select>
                      </span>
                    ) : r.book?.series_name ? (
                      <span style={{ color: t.purt }}>
                        {" "}
                        · {r.book.series_name}
                        {r.book.series_index ? ` #${r.book.series_index}` : ""}
                      </span>
                    ) : null}
                  </div>
                  {r.book?.pub_date ? (
                    <div style={{ fontSize: 11, color: t.tg }}>{r.book.pub_date}</div>
                  ) : null}
                  {r.error ? (
                    <div style={{ fontSize: 11, color: t.redt }}>{r.error}</div>
                  ) : null}
                </div>
                <span
                  style={{
                    fontSize: 11,
                    fontWeight: 700,
                    padding: "3px 10px",
                    borderRadius: 5,
                    flexShrink: 0,
                    background: (statusColors[r.status] || t.tg) + "22",
                    color: statusColors[r.status] || t.tg,
                    border: `1px solid ${statusColors[r.status] || t.tg}44`,
                  }}
                >
                  {statusLabels[r.status] || r.status}
                </span>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {/* Export Section */}
      <div style={{ background: t.bg2, border: `1px solid ${t.border}`, borderRadius: 12, padding: 24 }}>
        <h2 style={{ fontSize: 18, fontWeight: 600, color: t.text, margin: "0 0 4px" }}>
          Export Books
        </h2>
        <p style={{ fontSize: 13, color: t.td, margin: "0 0 16px" }}>
          Export your book list as CSV or text file with title, author, dates, and source URLs.
        </p>
        <Btn onClick={() => setShowExp(true)}>Open Export Tool</Btn>
      </div>
      {showExp ? <ExportModal onClose={() => setShowExp(false)} defaultFilter="missing" /> : null}
    </div>
  );
}
