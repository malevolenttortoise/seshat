// Discovery → Series — read-only card browse (v3.0.0 Phase 8).
//
// The exploration counterpart to the Tools → "Series Manager" (the
// existing DiscSeriesPage, which keeps the row-based edit affordances).
// Mirrors the Authors browse: a searchable/filterable card grid; clicking
// a series opens the read-only series detail page.
//
// Active-library-scoped with a per-library tab strip (one tab per library,
// hidden for single-library installs). Selecting a tab scopes reads via
// `?slug=` — NOT the global active-library switch, which cancels in-flight
// scans. The detail nav arg carries the slug ("slug:id") so the detail
// page fetches the right library's series.
import { useEffect, useMemo, useState } from "react";
import { useTheme } from "../theme";
import { api } from "../api";
import { Load } from "../components/Load";
import { PB } from "../components/PB";
import type { NavFn } from "../types";

interface SeriesRow {
  id: number;
  name: string;
  author_id: number | null;
  author_name: string | null;
  book_count: number;
  owned_count: number;
  missing_count: number;
  multi_author: number;
  is_shared: number;
  contributor_count: number;
  cover_book_id: number | null;
}

interface LibEntry {
  slug: string;
  display_name?: string;
  name: string;
  content_type?: string;
  active?: boolean;
}

type ModeFilter = "all" | "per_author" | "multi_author" | "shared";

export default function DiscSeriesBrowsePage({ onNav }: { onNav: NavFn }) {
  const t = useTheme();
  const [libs, setLibs] = useState<LibEntry[]>([]);
  const [slug, setSlug] = useState<string | null>(null); // null = active library
  const [rows, setRows] = useState<SeriesRow[] | null>(null);
  const [q, setQ] = useState("");
  const [mode, setMode] = useState<ModeFilter>("all");

  // Library list → tabs. Default the selected slug to the active library
  // so the first paint matches what the (active-library) endpoints return.
  useEffect(() => {
    api
      .get<{ libraries: LibEntry[] }>("/discovery/libraries")
      .then((r) => {
        setLibs(r.libraries || []);
        const active = (r.libraries || []).find((l) => l.active);
        if (active) setSlug(active.slug);
      })
      .catch(() => setLibs([]));
  }, []);

  useEffect(() => {
    setRows(null);
    const qs = new URLSearchParams({ limit: "200", sort: "name", sort_dir: "asc" });
    if (slug) qs.set("slug", slug);
    if (q.trim()) qs.set("search", q.trim());
    const c = new AbortController();
    const tm = setTimeout(() => {
      api
        .get<{ series: SeriesRow[] }>(`/discovery/series?${qs}`, c.signal)
        .then((r) => setRows(r.series || []))
        .catch((e) => {
          if (!api.isAbort(e)) setRows([]);
        });
    }, q ? 250 : 0);
    return () => {
      c.abort();
      clearTimeout(tm);
    };
  }, [slug, q]);

  const filtered = useMemo(() => {
    if (!rows) return null;
    return rows.filter((s) => {
      if (mode === "shared") return s.is_shared === 1;
      if (mode === "multi_author") return s.multi_author === 1;
      if (mode === "per_author") return s.is_shared !== 1 && s.multi_author !== 1;
      return true;
    });
  }, [rows, mode]);

  const navArg = (s: SeriesRow): string | number => (slug ? `${slug}:${s.id}` : s.id);

  const modeTabs: { key: ModeFilter; label: string }[] = [
    { key: "all", label: "All" },
    { key: "per_author", label: "Per-author" },
    { key: "multi_author", label: "Co-authored" },
    { key: "shared", label: "Shared" },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <h1 style={{ fontSize: 22, fontWeight: 700, color: t.text, margin: 0 }}>
        Series
      </h1>

      {/* Per-library tabs (hidden for single-library installs). */}
      {libs.length > 1 ? (
        <div style={{ display: "flex", gap: 4, borderBottom: `1px solid ${t.border}` }}>
          {libs.map((l) => {
            const active = slug === l.slug;
            const color = l.content_type === "audiobook" ? t.pur || t.accent : t.accent;
            return (
              <button
                key={l.slug}
                onClick={() => setSlug(l.slug)}
                style={{
                  padding: "8px 16px",
                  background: active ? color + "22" : "transparent",
                  color: active ? color : t.td,
                  border: "none",
                  borderBottom: active ? `2px solid ${color}` : "2px solid transparent",
                  cursor: "pointer",
                  fontSize: 14,
                  fontWeight: active ? 600 : 500,
                  marginBottom: -1,
                }}
              >
                {l.content_type === "audiobook" ? "🎧 " : "📖 "}
                {l.display_name || l.name}
              </button>
            );
          })}
        </div>
      ) : null}

      {/* Search + mode filter. */}
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <input
          type="search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search series, author, or book title…"
          style={{
            flex: 1,
            minWidth: 220,
            padding: "8px 12px",
            fontSize: 14,
            background: t.inp,
            color: t.text,
            border: `1px solid ${t.border}`,
            borderRadius: 6,
          }}
        />
        <div style={{ display: "inline-flex", border: `1px solid ${t.border}`, borderRadius: 6, overflow: "hidden" }}>
          {modeTabs.map((m) => (
            <button
              key={m.key}
              onClick={() => setMode(m.key)}
              style={{
                padding: "6px 12px",
                background: mode === m.key ? t.abg : "transparent",
                color: mode === m.key ? t.accent : t.td,
                border: "none",
                borderLeft: m.key === "all" ? "none" : `1px solid ${t.border}`,
                cursor: "pointer",
                fontSize: 13,
                fontWeight: mode === m.key ? 600 : 400,
              }}
            >
              {m.label}
            </button>
          ))}
        </div>
      </div>

      {filtered === null ? (
        <Load />
      ) : filtered.length === 0 ? (
        <div style={{ fontSize: 14, color: t.tf, fontStyle: "italic", padding: "24px 0" }}>
          No series{q ? " match your search" : ""}.
        </div>
      ) : (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 280px), 1fr))",
            gap: 12,
            alignItems: "start",
          }}
        >
          {filtered.map((s) => (
            <SeriesBrowseCard key={s.id} s={s} onClick={() => onNav("disc-series-detail", navArg(s))} />
          ))}
        </div>
      )}
    </div>
  );
}

function SeriesBrowseCard({ s, onClick }: { s: SeriesRow; onClick: () => void }) {
  const t = useTheme();
  const isShared = s.is_shared === 1;
  const isCoauthored = s.multi_author === 1;
  const modeLabel = isShared ? "Shared" : isCoauthored ? "Co-authored" : "Per-author";
  const accent = isShared || isCoauthored;
  const subtitle = isShared
    ? `shared across ${s.contributor_count} authors`
    : isCoauthored
      ? `co-authored by ${s.contributor_count} authors`
      : s.author_name || "—";

  return (
    <div
      onClick={onClick}
      style={{
        display: "flex",
        gap: 12,
        padding: 12,
        background: t.bg2,
        border: `1px solid ${t.border}`,
        borderRadius: 10,
        cursor: "pointer",
      }}
    >
      <div
        style={{
          width: 56,
          height: 84,
          background: t.bg3,
          borderRadius: 4,
          overflow: "hidden",
          flexShrink: 0,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: t.tg,
        }}
      >
        {s.cover_book_id ? (
          <img
            src={`/api/discovery/covers/${s.cover_book_id}`}
            loading="lazy"
            alt=""
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
            onError={(e) => {
              (e.target as HTMLImageElement).style.display = "none";
            }}
          />
        ) : (
          <span>🗂️</span>
        )}
      </div>
      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 4 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <span
            style={{
              fontSize: 15,
              fontWeight: 600,
              color: t.text,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {s.name}
          </span>
          <span
            style={{
              fontSize: 10,
              fontWeight: 600,
              padding: "1px 7px",
              borderRadius: 4,
              background: accent ? t.abg : t.bg,
              color: accent ? t.accent : t.tf,
              border: `1px solid ${accent ? t.abr : t.border}`,
              textTransform: "uppercase",
              letterSpacing: "0.04em",
              flexShrink: 0,
            }}
          >
            {modeLabel}
          </span>
        </div>
        <div style={{ fontSize: 12, color: t.tf, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {subtitle}
        </div>
        <div style={{ fontSize: 12, color: t.td }}>
          {s.book_count} book{s.book_count === 1 ? "" : "s"} — {s.owned_count} owned, {s.missing_count} missing
        </div>
        <PB owned={s.owned_count} total={s.book_count} />
      </div>
    </div>
  );
}
