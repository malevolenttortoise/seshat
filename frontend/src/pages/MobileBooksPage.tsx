// Mobile-native books page. Replaces DiscBooksPage rendering for
// Library, Missing, and Upcoming on phones / iPads.
//
// Strips the desktop view toggle, grouping mode, and bulk-select
// bar. Mobile shows a single-column card list, search row, format
// chips (Ebook/Audiobook/All), sort + MAM filter chips, and bottom-
// only pagination. Tapping a card opens BookSidebar (already
// mobile-aware — renders full-screen on phones).
//
// Data-fetching is duplicated from the desktop page rather than
// shared via a hook. Kept additive while we iterate; eligible for
// a useBooksPageData() extraction in a follow-up.
import { useState } from "react";
import { useTheme } from "../theme";
import { useBookSidebar } from "../hooks/useBookSidebar";
import { useMamEnabled } from "../hooks/useMamEnabled";
import { useBooksList } from "../hooks/useBooksList";
import { BookSidebar } from "../components/BookSidebar";
import { Ic } from "../icons";
import {
  MobileInput,
  MobileChip,
  MobilePagination,
  MobileBookCard,
  MobileSheet,
  MobileRow,
  MobileBackButton,
} from "../components/mobile";

export interface MobileBooksPageProps {
  title: string;
  subtitle?: string;
  apiPath?: string;
  extraParams?: Record<string, string | number | boolean>;
  showAuthor?: boolean;
  showFormatTabs?: boolean;
  showOwnedFilter?: boolean;
}

const SORT_OPTIONS: { value: string; label: string }[] = [
  { value: "title", label: "Title" },
  { value: "author", label: "Author" },
  { value: "series", label: "Series" },
  { value: "pub_date", label: "Pub Date" },
  { value: "added_at", label: "Added" },
];

const MAM_FILTER_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "All MAM" },
  { value: "found", label: "✓ Found" },
  { value: "possible", label: "? Possible" },
  { value: "not_found", label: "✗ Not on MAM" },
  { value: "not_applicable", label: "⊘ N/A" },
];

export default function MobileBooksPage({
  title,
  apiPath = "/books",
  extraParams = {},
  showAuthor = true,
  showFormatTabs = true,
  showOwnedFilter = false,
}: MobileBooksPageProps) {
  const t = useTheme();
  const {
    bks, total, totalPages, pg, ld, load, onAction,
    q, setQ, sort, setSort, fmt, setFmt, mamFilter, setMamFilter, ownedFilter, setOwnedFilter,
  } = useBooksList({ title, apiPath, extraParams, showFormatTabs, showOwnedFilter });
  const mamOn = useMamEnabled();
  const { sb, setSb, sbClosing, closeSb } = useBookSidebar();
  const [sortSheet, setSortSheet] = useState(false);



  const sortLabel =
    SORT_OPTIONS.find((o) => o.value === sort)?.label || "Title";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <MobileBackButton to="dashboard" label="Dashboard" />
      {/* Page title + count */}
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: 8,
        }}
      >
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: t.text }}>
          {title}
        </h1>
        <span style={{ fontSize: 13, color: t.td }}>
          {ld ? "…" : `${total.toLocaleString()} total`}
        </span>
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

      {/* Format tabs (Ebook/Audiobook/All) */}
      {showFormatTabs && (
        <div
          style={{
            display: "flex",
            gap: 6,
            overflowX: "auto",
            scrollbarWidth: "none",
          }}
        >
          {[
            { v: "all", label: "All" },
            { v: "ebook", label: "📖 Ebooks" },
            { v: "audiobook", label: "🎧 Audiobooks" },
          ].map((opt) => (
            <MobileChip
              key={opt.v}
              active={fmt === opt.v}
              onClick={() => setFmt(opt.v)}
            >
              {opt.label}
            </MobileChip>
          ))}
        </div>
      )}

      {/* v2.3.4.3 owned filter — Hidden page only. */}
      {showOwnedFilter && (
        <div
          style={{
            display: "flex",
            gap: 6,
            overflowX: "auto",
            scrollbarWidth: "none",
          }}
        >
          {[
            { v: "all", label: "All" },
            { v: "owned", label: "✓ Owned only" },
            { v: "discovered", label: "○ Discovered only" },
          ].map((opt) => (
            <MobileChip
              key={opt.v}
              active={ownedFilter === opt.v}
              onClick={() => setOwnedFilter(opt.v)}
            >
              {opt.label}
            </MobileChip>
          ))}
        </div>
      )}

      {/* Sort + MAM filter chips */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 6,
        }}
      >
        <MobileChip
          onClick={() => setSortSheet(true)}
          leadingIcon="↕"
        >
          Sort: {sortLabel}
        </MobileChip>
        {mamOn &&
          MAM_FILTER_OPTIONS.map((opt) => (
            <MobileChip
              key={opt.value}
              active={mamFilter === opt.value}
              onClick={() => setMamFilter(opt.value)}
            >
              {opt.label}
            </MobileChip>
          ))}
      </div>

      {/* Book card grid — single column on phones, 2 columns on tablets */}
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
            showAuthor={showAuthor}
            showMamLink={mamOn}
          />
        ))}
      </div>

      {/* Empty state */}
      {!ld && bks.length === 0 && (
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
          {q ? "No books match your search." : "No books here yet."}
        </div>
      )}

      {/* Pagination */}
      <MobilePagination
        page={pg}
        totalPages={totalPages}
        onPrev={() => load(pg - 1)}
        onNext={() => load(pg + 1)}
      />

      {/* Sort selection sheet */}
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

      {/* Book detail sidebar — already mobile-aware (renders full-screen
          on phones, side panel on desktop). */}
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
