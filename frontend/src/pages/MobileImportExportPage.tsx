// Mobile-native import/export page. Paste-URLs textarea, preview
// rows as cards with status badges, add-new-books action. Export
// flow opens the existing ExportModal (Phase 5 will mobile-ify the
// modal itself).
import { useState } from "react";
import { useTheme } from "../theme";
import { ExportModal } from "../components/ExportModal";
import { MamLinkHint } from "../components/manualGrab/MamLinkHint";
import { useImportPreview, type ImportStatus } from "../hooks/useImportPreview";
import {
  MobileBtn,
  MobileBadge,
  MobileSection,
  MobileBackButton,
} from "../components/mobile";

const STATUS_TONE: Record<
  ImportStatus,
  "ok" | "warn" | "err" | "info" | "accent" | "neutral"
> = {
  new: "accent",
  owned: "ok",
  tracked: "info",
  error: "err",
  added: "ok",
};

export default function MobileImportExportPage() {
  const t = useTheme();
  const [urls, setUrls] = useState("");
  const { results, fetching, progress, adding, addResult, fetchPreview: fetchRows, newRows, addBooks: addRows } =
    useImportPreview();
  const [showExp, setShowExp] = useState(false);

  const fetchPreview = () => fetchRows(urls, (n) => `Fetching ${n} book(s)…`);

  const addBooks = async () => {
    if (!results) return;
    const newBooks = newRows.map((r) => r.book!);
    if (!newBooks.length) return;
    await addRows(newBooks);
  };

  const newCount = (results || []).filter((r) => r.status === "new").length;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <MobileBackButton to="dashboard" label="Dashboard" />

      <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: t.text }}>
        Import / Export
      </h1>

      <MobileSection title="Import from URLs" defaultOpen={true}>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <p style={{ fontSize: 13, color: t.td, margin: 0 }}>
            Paste book URLs (one per line) from Goodreads, Hardcover,
            Amazon, Open Library, Google Books, Kobo, or IBDB. Preview
            then add the new ones to your library as unowned.
          </p>
          <textarea
            value={urls}
            onChange={(e) => setUrls(e.target.value)}
            placeholder="https://www.goodreads.com/book/show/…"
            rows={6}
            style={{
              width: "100%",
              padding: 10,
              background: t.inp,
              color: t.text,
              border: `1px solid ${t.border}`,
              borderRadius: 10,
              fontSize: 16,
              fontFamily: "ui-monospace, monospace",
              resize: "vertical",
            }}
          />
          <MamLinkHint text={urls} />
          <MobileBtn
            variant="primary"
            primary
            fullWidth
            onClick={fetchPreview}
            disabled={fetching || !urls.trim()}
          >
            {fetching ? "Fetching…" : "Preview"}
          </MobileBtn>
          {progress && (
            <div style={{ fontSize: 13, color: t.td }}>{progress}</div>
          )}
        </div>
      </MobileSection>

      {results && results.length > 0 && (
        <MobileSection
          title="Preview"
          count={results.length}
          defaultOpen={true}
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {newCount > 0 && (
              <MobileBtn
                variant="primary"
                primary
                fullWidth
                onClick={addBooks}
                disabled={adding}
              >
                {adding ? "Adding…" : `Add ${newCount} new book(s)`}
              </MobileBtn>
            )}
            {addResult && (
              <div
                style={{
                  padding: "8px 12px",
                  background: addResult.error ? t.redb : t.grnb,
                  border: `1px solid ${addResult.error ? t.redt : t.grnt}`,
                  color: addResult.error ? t.red : t.grn,
                  borderRadius: 8,
                  fontSize: 13,
                }}
              >
                {addResult.error
                  ? "Some books failed to add."
                  : `Added ${addResult.added}, updated ${addResult.updated}.`}
              </div>
            )}
            {results.map((r, i) => (
              <div
                key={i}
                style={{
                  display: "flex",
                  gap: 10,
                  padding: 10,
                  background: t.bg2,
                  border: `1px solid ${t.border}`,
                  borderRadius: 10,
                }}
              >
                {r.book?.cover_url && (
                  <div
                    style={{
                      width: 50,
                      height: 75,
                      flexShrink: 0,
                      borderRadius: 4,
                      overflow: "hidden",
                      background: t.bg3,
                    }}
                  >
                    <img
                      src={r.book.cover_url}
                      alt=""
                      style={{
                        width: "100%",
                        height: "100%",
                        objectFit: "cover",
                      }}
                      onError={(e) => {
                        (e.currentTarget as HTMLImageElement).style.display =
                          "none";
                      }}
                    />
                  </div>
                )}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      fontSize: 14,
                      fontWeight: 600,
                      color: t.text,
                      lineHeight: 1.3,
                    }}
                  >
                    {r.book?.title || "(unknown)"}
                  </div>
                  {r.book?.author_name && (
                    <div style={{ fontSize: 12, color: t.td, marginTop: 2 }}>
                      {r.book.author_name}
                    </div>
                  )}
                  {r.book?.series_name && (
                    <div style={{ fontSize: 12, color: t.purt, marginTop: 2 }}>
                      {r.book.series_name}
                      {r.book.series_index ? ` #${r.book.series_index}` : ""}
                    </div>
                  )}
                  <div style={{ marginTop: 6 }}>
                    <MobileBadge tone={STATUS_TONE[r.status]}>
                      {r.status}
                    </MobileBadge>
                  </div>
                  {r.error && (
                    <div
                      style={{
                        fontSize: 11,
                        color: t.red,
                        marginTop: 4,
                      }}
                    >
                      {r.error}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </MobileSection>
      )}

      <MobileSection title="Export" defaultOpen={true}>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <p style={{ fontSize: 13, color: t.td, margin: 0 }}>
            Export your library or missing list as CSV / text.
          </p>
          <MobileBtn
            variant="secondary"
            fullWidth
            onClick={() => setShowExp(true)}
          >
            Open export tool
          </MobileBtn>
        </div>
      </MobileSection>

      {showExp && (
        <ExportModal onClose={() => setShowExp(false)} defaultFilter="all" />
      )}
    </div>
  );
}
