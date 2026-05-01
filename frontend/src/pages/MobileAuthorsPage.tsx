// Mobile-native authors page. Search + format chips + sort sheet +
// scrollable list of tappable author rows.
//
// Drops the desktop alphabet-sidebar filter (the alphabet column was
// already hidden on mobile via CSS); mobile users jump via search.
// Bulk-select is also dropped for now — admin-y, can return as a
// Phase 6 polish item.
import { useEffect, useState } from "react";
import { api } from "../api";
import { useTheme } from "../theme";
import { usePersist } from "../hooks/usePersist";
import { Ic } from "../icons";
import {
  MobileInput,
  MobileChip,
  MobilePagination,
  MobileSheet,
  MobileRow,
  MobileBadge,
  MobileBackButton,
} from "../components/mobile";
import type {
  Author,
  AuthorsResponse,
  MamStatusResponse,
  NavFn,
} from "../types";

const PER_PAGE = 30;

const SORT_OPTIONS: { value: string; label: string }[] = [
  { value: "name", label: "Name (A-Z)" },
  { value: "books", label: "Total books" },
  { value: "owned", label: "Owned books" },
  { value: "missing", label: "Missing books" },
];

export default function MobileAuthorsPage({ onNav }: { onNav: NavFn }) {
  const t = useTheme();
  const [aus, setAus] = useState<Author[]>([]);
  const [ld, setLd] = useState(true);
  const [q, setQ] = usePersist<string>("ap_q", "");
  const [sort, setSort] = usePersist<string>("ap_sort", "name");
  const [fmt, setFmt] = usePersist<string>("ap_fmt", "all");
  const [pg, setPg] = usePersist<number>("ap_pg", 1);
  const [mamOn, setMamOn] = useState(false);
  const [sortSheet, setSortSheet] = useState(false);

  void mamOn; // reserved for future per-row MAM action

  useEffect(() => {
    api
      .get<MamStatusResponse>("/discovery/mam/status")
      .then((r) => setMamOn(!!r.enabled))
      .catch(() => {});
  }, []);

  useEffect(() => {
    const c = new AbortController();
    setLd(true);
    const params = new URLSearchParams({ search: q, sort, content_type: fmt });
    api
      .get<AuthorsResponse>(`/discovery/authors?${params}`, c.signal)
      .then((d) => {
        setAus(d.authors || []);
        setLd(false);
        setPg(1);
      })
      .catch((e) => {
        if (!api.isAbort(e)) setLd(false);
      });
    return () => c.abort();
  }, [q, sort, fmt]);

  const totalPages = Math.max(1, Math.ceil(aus.length / PER_PAGE));
  const page = Math.min(pg, totalPages);
  const visible = aus.slice((page - 1) * PER_PAGE, page * PER_PAGE);

  const sortLabel =
    SORT_OPTIONS.find((o) => o.value === sort)?.label || "Name (A-Z)";

  const navToAuthor = (a: Author) => {
    // The desktop page builds a "slug:id" arg when library_slug is
    // present so cross-library authors resolve in the right library.
    const arg = a.library_slug ? `${a.library_slug}:${a.id}` : a.id;
    onNav("disc-author-detail", arg);
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <MobileBackButton to="dashboard" label="Dashboard" />
      {/* Title + count */}
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          justifyContent: "space-between",
          gap: 8,
        }}
      >
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: t.text }}>
          Authors
        </h1>
        <span style={{ fontSize: 13, color: t.td }}>
          {ld ? "…" : `${aus.length.toLocaleString()} total`}
        </span>
      </div>

      {/* Search */}
      <MobileInput
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search author"
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

      {/* Format tabs */}
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

      {/* Sort */}
      <div style={{ display: "flex", gap: 6 }}>
        <MobileChip onClick={() => setSortSheet(true)} leadingIcon="↕">
          Sort: {sortLabel}
        </MobileChip>
      </div>

      {/* Author rows */}
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {visible.map((a) => {
          const owned = a.owned_count ?? 0;
          const total = a.total_books ?? 0;
          const missing = a.missing_count ?? 0;
          const isNew = (a.new_count ?? 0) > 0;
          const initials = (a.name || "?")
            .split(" ")
            .map((p) => p[0])
            .filter(Boolean)
            .slice(0, 2)
            .join("")
            .toUpperCase();
          return (
            <button
              key={`${a.library_slug || ""}-${a.id}`}
              onClick={() => navToAuthor(a)}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 12,
                padding: 10,
                width: "100%",
                background: t.bg2,
                border: `1px solid ${t.border}`,
                borderRadius: 12,
                cursor: "pointer",
                textAlign: "left",
              }}
            >
              {/* Avatar */}
              <div
                style={{
                  width: 48,
                  height: 48,
                  borderRadius: "50%",
                  background: t.bg3,
                  border: `1px solid ${t.borderL}`,
                  flexShrink: 0,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  overflow: "hidden",
                }}
              >
                {a.image_url ? (
                  <img
                    src={a.image_url}
                    alt=""
                    loading="lazy"
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
                ) : (
                  <span style={{ color: t.td, fontWeight: 700, fontSize: 16 }}>
                    {initials}
                  </span>
                )}
              </div>

              {/* Name + stats */}
              <div style={{ flex: 1, minWidth: 0 }}>
                <div
                  style={{
                    fontSize: 15,
                    fontWeight: 600,
                    color: t.text,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {a.name}
                </div>
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                    fontSize: 12,
                    color: t.td,
                    marginTop: 2,
                  }}
                >
                  <span>
                    {owned} / {total} owned
                  </span>
                  {missing > 0 && (
                    <span style={{ color: t.red }}>· {missing} missing</span>
                  )}
                </div>
              </div>

              {/* Badges */}
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "flex-end",
                  gap: 4,
                  flexShrink: 0,
                }}
              >
                {isNew && <MobileBadge tone="accent">New</MobileBadge>}
                <span style={{ color: t.tg, fontSize: 18 }}>›</span>
              </div>
            </button>
          );
        })}
      </div>

      {!ld && aus.length === 0 && (
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
          {q ? "No authors match your search." : "No authors here yet."}
        </div>
      )}

      <MobilePagination
        page={page}
        totalPages={totalPages}
        onPrev={() => setPg(page - 1)}
        onNext={() => setPg(page + 1)}
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
    </div>
  );
}
