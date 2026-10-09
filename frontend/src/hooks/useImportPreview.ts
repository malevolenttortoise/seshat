// Import / Export → Import: look up pasted book links, show what each
// one is (new / owned / tracked / error), and add the new ones.
//
// DiscImportExportPage + MobileImportExportPage each carried this (wave
// 5b S11). The shells keep the text box, the export modal and the
// wording of the "Fetching N book(s)" line.
import { useState } from "react";
import { api } from "../api";
import { isMamLine } from "../components/manualGrab/MamLinkHint";

// Preview-row status set emitted by /discovery/books/import-preview
// plus "added" — applied client-side after a successful add so the row
// flips badge without a refetch.
export type ImportStatus = "new" | "owned" | "tracked" | "error" | "added";

export interface SeriesOption {
  name: string;
  position?: string | number | null;
}

export interface ImportBook {
  title?: string;
  author_name?: string;
  series_name?: string;
  series_index?: string | number;
  pub_date?: string;
  cover_url?: string;
  series_options?: SeriesOption[];
}

export interface ImportPreviewRow {
  status: ImportStatus;
  book?: ImportBook;
  error?: string;
}

interface ImportPreviewResponse {
  results?: ImportPreviewRow[];
}

export interface ImportAddResponse {
  added: number;
  updated: number;
  error?: boolean;
}

export function useImportPreview() {
  const [results, setResults] = useState<ImportPreviewRow[] | null>(null);
  const [fetching, setFetching] = useState(false);
  const [progress, setProgress] = useState("");
  const [adding, setAdding] = useState(false);
  const [addResult, setAddResult] = useState<ImportAddResponse | null>(null);

  /**
   * Look up every http line of `text` that isn't a MAM link (those go to
   * Manual Grab); `fetchingText(n)` is the line shown meanwhile.
   */
  const fetchPreview = async (text: string, fetchingText: (n: number) => string) => {
    const lines = text
      .split("\n")
      .map((u) => u.trim())
      .filter((u) => u.startsWith("http") && !isMamLine(u));
    if (!lines.length) return;
    setFetching(true);
    setResults(null);
    setAddResult(null);
    setProgress(fetchingText(lines.length));
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

  /** The previewed rows that would be added. */
  const newRows: ImportPreviewRow[] = results
    ? results.filter((r) => r.status === "new" && r.book)
    : [];

  /** Add `books`, then mark the new rows with those titles as added. */
  const addBooks = async (books: ImportBook[]) => {
    setAdding(true);
    setAddResult(null);
    try {
      const d = await api.post<ImportAddResponse>(
        "/discovery/books/import-add",
        { books },
      );
      setAddResult(d);
      setResults((prev) =>
        prev === null
          ? prev
          : prev.map((r) =>
              r.status === "new" && books.some((b) => b.title === r.book?.title)
                ? { ...r, status: "added" as ImportStatus }
                : r,
            ),
      );
    } catch {
      setAddResult({ added: 0, updated: 0, error: true });
    }
    setAdding(false);
  };

  /** Use one of a row's series options (desktop's per-row series picker). */
  const pickSeries = (index: number, picked: SeriesOption | undefined) =>
    setResults((prev) =>
      (prev || []).map((p, j) =>
        j === index
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

  return { results, fetching, progress, adding, addResult, fetchPreview, newRows, addBooks, pickSeries };
}
