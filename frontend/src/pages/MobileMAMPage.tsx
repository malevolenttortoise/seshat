// Mobile-native MAM page. Three tabs (upload / download / missing
// everywhere), per-library scoping when multi-lib, search + sort,
// manual scan controls (collapsed), and live scan progress.
//
// Features intentionally dropped from the mobile surface:
//   - View toggle (always card list)
//   - Bulk-select mode (admin-y; revisit in Phase 6)
// Send-to-pipeline is still available per-card via MobileBookCard.
import { useState } from "react";
import { runBatchJob } from "../lib/batchJob";
import { useTheme } from "../theme";
import { useBookSidebar } from "../hooks/useBookSidebar";
import { useMamSection } from "../hooks/useMamSection";
import { BookSidebar } from "../components/BookSidebar";
import { Ic } from "../icons";
import {
  MobileInput,
  MobileChip,
  MobileBookCard,
  MobilePagination,
  MobileSection,
  MobileBtn,
  MobileSheet,
  MobileRow,
  MobileBackButton,
} from "../components/mobile";
import type {
  NavFn,
  SendToPipelineFn,
} from "../types";






interface SendToPipelineResponse {
  sent?: number;
  skipped?: number;
  message?: string;
}


const TAB_OPTIONS: { value: string; label: string; icon: string }[] = [
  { value: "upload", label: "Upload", icon: "↑" },
  { value: "download", label: "Available", icon: "↓" },
  { value: "missing_everywhere", label: "Missing", icon: "∅" },
];

const SORT_OPTIONS: { value: string; label: string }[] = [
  { value: "title", label: "Title" },
  { value: "author", label: "Author" },
  { value: "series", label: "Series" },
  { value: "pub_date", label: "Pub Date" },
];

export default function MobileMAMPage({ onNav }: { onNav: NavFn }) {
  const t = useTheme();
  void onNav;

  const {
    tab, switchTab, libSlug, setLibSlug, libs, books, total, totalPages, pg, q, setQ, sort, setSort,
    ld, counts, load, scanStarting, mamScan, startScan: startMamScan, cancelScan, pipelineReady, onAction,
  } = useMamSection();
  const [scanLimit, setScanLimit] = useState<number>(100);
  const { sb, setSb, sbClosing, closeSb } = useBookSidebar();
  const [sortSheet, setSortSheet] = useState(false);

  const startScan = async () => {
    const err = await startMamScan(scanLimit);
    if (err) alert(err);
  };



  const sendToPipeline: SendToPipelineFn = async (bookIds) => {
    if (!bookIds || !bookIds.length) return;
    try {
      const r = await runBatchJob<SendToPipelineResponse>(
        "/discovery/send-to-pipeline",
        { book_ids: bookIds },
      );
      if ((r.sent || 0) > 0) {
        alert(
          `Sent ${r.sent} book(s) to pipeline!${r.skipped ? ` (${r.skipped} skipped — not Found)` : ""}`,
        );
      } else {
        alert(r.message || "No books sent");
      }
    } catch (e) {
      alert(`Send failed: ${(e as Error).message || e}`);
    }
  };

  const sortLabel =
    SORT_OPTIONS.find((o) => o.value === sort)?.label || "Title";

  const tabCount = (v: string): number => {
    if (v === "upload") return counts.upload;
    if (v === "download") return counts.download;
    if (v === "missing_everywhere") return counts.missing;
    return 0;
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <MobileBackButton to="dashboard" label="Dashboard" />
      {/* Page title */}
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: 8,
        }}
      >
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: t.text }}>
          MAM Search
        </h1>
        <span style={{ fontSize: 13, color: t.td }}>
          {ld ? "…" : `${total.toLocaleString()} in tab`}
        </span>
      </div>

      {/* Library selector — only when multi-library */}
      {libs.length > 1 && (
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: 6,
          }}
        >
          <MobileChip
            active={libSlug === null}
            onClick={() => setLibSlug(null)}
          >
            All
          </MobileChip>
          {libs.map((lib) => (
            <MobileChip
              key={lib.slug}
              active={libSlug === lib.slug}
              onClick={() => setLibSlug(lib.slug)}
            >
              {lib.content_type === "audiobook" ? "🎧 " : "📖 "}
              {lib.label}
            </MobileChip>
          ))}
        </div>
      )}

      {/* Tab chips */}
      <div
        style={{
          display: "flex",
          gap: 6,
          overflowX: "auto",
          scrollbarWidth: "none",
        }}
      >
        {TAB_OPTIONS.map((opt) => (
          <MobileChip
            key={opt.value}
            active={tab === opt.value}
            onClick={() => switchTab(opt.value)}
          >
            {opt.icon} {opt.label} ({tabCount(opt.value).toLocaleString()})
          </MobileChip>
        ))}
      </div>

      {/* Search */}
      <MobileInput
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search title or author"
        leadingIcon={Ic.search}
        trailing={
          q ? (
            <button
              onClick={() => setQ("")}
              style={{
                background: "none",
                border: "none",
                cursor: "pointer",
                color: t.tg,
                padding: 4,
                display: "flex",
                width: 32,
                height: 32,
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              {Ic.x}
            </button>
          ) : undefined
        }
      />

      {/* Sort chip */}
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        <MobileChip onClick={() => setSortSheet(true)} leadingIcon="↕">
          Sort: {sortLabel}
        </MobileChip>
        {counts.unscanned > 0 && (
          <MobileChip>
            {counts.unscanned.toLocaleString()} unscanned
          </MobileChip>
        )}
      </div>

      {/* Manual scan section — collapsed by default */}
      <MobileSection
        title="Manual Scan"
        subtitle={
          mamScan?.running
            ? `Scanning… ${mamScan.scanned ?? 0}/${mamScan.total ?? "?"}`
            : "Scan unscanned books against MAM"
        }
        defaultOpen={!!mamScan?.running}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {mamScan?.running ? (
            <>
              <div
                style={{
                  height: 6,
                  background: t.bg3,
                  borderRadius: 999,
                  overflow: "hidden",
                }}
              >
                <div
                  style={{
                    width: `${
                      mamScan.total
                        ? Math.min(
                            100,
                            ((mamScan.scanned ?? 0) / mamScan.total) * 100,
                          )
                        : 0
                    }%`,
                    height: "100%",
                    background: t.accent,
                    transition: "width 0.3s",
                  }}
                />
              </div>
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(4, 1fr)",
                  gap: 6,
                  fontSize: 12,
                }}
              >
                <div style={{ color: t.grn }}>✓ {mamScan.found ?? 0}</div>
                <div style={{ color: t.ylw }}>? {mamScan.possible ?? 0}</div>
                <div style={{ color: t.red }}>✗ {mamScan.not_found ?? 0}</div>
                <div style={{ color: t.tg }}>! {mamScan.errors ?? 0}</div>
              </div>
              <MobileBtn variant="ghost" onClick={cancelScan}>
                Cancel scan
              </MobileBtn>
            </>
          ) : (
            <>
              <label
                style={{
                  fontSize: 13,
                  color: t.td,
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                }}
              >
                Limit
                <select
                  value={scanLimit}
                  onChange={(e) => setScanLimit(Number(e.target.value))}
                  style={{
                    flex: 1,
                    minHeight: 44,
                    padding: "0 12px",
                    background: t.inp,
                    color: t.text,
                    border: `1px solid ${t.border}`,
                    borderRadius: 10,
                    fontSize: 16,
                  }}
                >
                  <option value={50}>50 books</option>
                  <option value={100}>100 books</option>
                  <option value={250}>250 books</option>
                  <option value={500}>500 books</option>
                  <option value={1000}>1,000 books</option>
                </select>
              </label>
              <MobileBtn
                variant="primary"
                primary
                fullWidth
                onClick={startScan}
                disabled={scanStarting || counts.unscanned === 0}
              >
                {scanStarting
                  ? "Starting…"
                  : counts.unscanned === 0
                    ? "All scanned"
                    : `Scan ${Math.min(scanLimit, counts.unscanned)} books`}
              </MobileBtn>
            </>
          )}
        </div>
      </MobileSection>

      {/* Book list */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 360px), 1fr))",
          gap: 8,
        }}
      >
        {books.map((b) => (
          <MobileBookCard
            key={b.id}
            book={b}
            onClick={() => setSb(b)}
            showAuthor
            showMamLink
            onSendToPipeline={pipelineReady ? sendToPipeline : undefined}
          />
        ))}
      </div>

      {!ld && books.length === 0 && (
        <div
          style={{
            padding: 24,
            textAlign: "center",
            color: t.tg,
            fontSize: 14,
            background: t.bg2,
            border: `1px solid ${t.borderL}`,
            borderRadius: 12,
          }}
        >
          {q ? "No books match your search." : "Nothing in this tab."}
        </div>
      )}

      <MobilePagination
        page={pg}
        totalPages={totalPages}
        onPrev={() => load(pg - 1)}
        onNext={() => load(pg + 1)}
      />

      <MobileSheet
        open={sortSheet}
        onClose={() => setSortSheet(false)}
        title="Sort by"
        height="auto"
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {SORT_OPTIONS.map((opt) => (
            <MobileRow
              key={opt.value}
              title={opt.label}
              active={sort === opt.value}
              hideChevron
              onClick={() => {
                setSort(opt.value);
                setSortSheet(false);
              }}
            />
          ))}
        </div>
      </MobileSheet>

      {sb && (
        <BookSidebar
          book={sb}
          closing={sbClosing}
          onClose={closeSb}
          onAction={onAction}
          onEdit={() => load(pg)}
        />
      )}
    </div>
  );
}
