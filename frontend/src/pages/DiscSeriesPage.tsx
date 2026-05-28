// v2.3.3 Series Manager page.
//
// Lists every series across the active library with a cover thumbnail,
// author summary, ownership counts, and shared/per-author indicator.
// The user-facing actions are:
//
//   - Manage members: open a modal that lets the user add or remove
//     authors. Authority (per-author vs shared) auto-flips on the
//     backend based on the resulting distinct-author count, so the
//     old "promote / demote" verbs are no longer surfaced here.
//   - Rename: change the series name in place. 409 surfaces the
//     conflict id so the user can opt to merge into the existing.
//   - Delete: remove the series; books fall back to standalone.
//
// The legacy promote/demote endpoints still exist (used by the
// calibre_sync auto-detect path and as recovery escape hatches) but
// nothing on this page calls them directly anymore.
//
// Search matches series name, author name, AND book title — so the
// user can find a series by remembering an entry in it.

import { useEffect, useMemo, useState } from "react";
import { useTheme } from "../theme";
import { api, ApiError, slugQuery } from "../api";
import { Btn } from "../components/Btn";
import { Spin } from "../components/Spin";
import { Load } from "../components/Load";
import { usePersist } from "../hooks/usePersist";
import { ManageMembersModal } from "../components/ManageMembersModal";

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

interface SeriesListResponse {
  series: SeriesRow[];
  total: number;
  limit: number;
  offset: number;
}

type FilterMode = "all" | "shared" | "per-author";

const PAGE_SIZE = 50;

export default function SeriesManagerPage() {
  const t = useTheme();
  const [filter, setFilter] = usePersist<FilterMode>("sm_filter", "all");
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<SeriesListResponse | null>(null);
  const [busy, setBusy] = useState<Record<number, string>>({});
  const [manageTarget, setManageTarget] = useState<SeriesRow | null>(null);
  // v3.0.0 Phase 8 — per-library tabs scope reads + writes via ?slug=
  // (NOT the global active-library switch, which cancels scans). `slug`
  // null = active library.
  const [libs, setLibs] = useState<{ slug: string; name: string; display_name?: string; content_type?: string; active?: boolean }[]>([]);
  const [slug, setSlug] = useState<string | null>(null);

  useEffect(() => {
    api
      .get<{ libraries: typeof libs }>("/discovery/libraries")
      .then((r) => {
        setLibs(r.libraries || []);
        const active = (r.libraries || []).find((l) => l.active);
        if (active) setSlug(active.slug);
      })
      .catch(() => setLibs([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const load = () => {
    setData(null);
    const params = new URLSearchParams();
    if (search.trim()) params.set("search", search.trim());
    if (filter === "shared") params.set("shared", "true");
    if (filter === "per-author") params.set("shared", "false");
    if (slug) params.set("slug", slug);
    params.set("limit", String(PAGE_SIZE));
    params.set("offset", String(offset));
    api
      .get<SeriesListResponse>(`/discovery/series?${params}`)
      .then(setData)
      .catch((e) => {
        console.error(e);
        setData({ series: [], total: 0, limit: PAGE_SIZE, offset: 0 });
      });
  };

  // Debounce the search input so each keystroke doesn't fire a request.
  useEffect(() => {
    const tid = setTimeout(() => {
      setOffset(0);
      setSearch(searchInput);
    }, 250);
    return () => clearTimeout(tid);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchInput]);

  // Reload whenever filter / search / offset settle.
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter, search, offset, slug]);

  // Reset offset when filter changes.
  useEffect(() => {
    setOffset(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter]);

  // v2.15.1 — listen for `seshat:focus` events with kind=series from
  // the global navbar search. The event detail already carries the
  // series name (forwarded by App's navFromSearch from the
  // SearchNavTarget). Pre-fill the search input with that name so
  // the list narrows to just that series; the existing 250ms
  // debounce flushes the fetch automatically.
  useEffect(() => {
    function onFocus(e: Event) {
      const detail = (e as CustomEvent<{
        kind?: string; name?: string;
      }>).detail;
      if (detail?.kind !== "series" || !detail.name) return;
      setSearchInput(detail.name);
    }
    window.addEventListener("seshat:focus", onFocus);
    return () => window.removeEventListener("seshat:focus", onFocus);
  }, []);

  const total = data?.total ?? 0;
  const pageStart = total === 0 ? 0 : offset + 1;
  const pageEnd = Math.min(offset + PAGE_SIZE, total);
  const hasPrev = offset > 0;
  const hasNext = offset + PAGE_SIZE < total;

  const rename = async (s: SeriesRow) => {
    const next = window.prompt(`Rename "${s.name}" to:`, s.name);
    if (!next || next.trim() === s.name) return;
    setBusy((b) => ({ ...b, [s.id]: "rename" }));
    try {
      await api.patch(`/discovery/series/${s.id}${slugQuery(slug ?? undefined)}`, { name: next.trim() });
      load();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      alert(`Rename failed: ${msg}`);
    } finally {
      setBusy((b) => {
        const n = { ...b };
        delete n[s.id];
        return n;
      });
    }
  };

  const remove = async (s: SeriesRow) => {
    if (
      !window.confirm(
        `Delete "${s.name}"? ${s.book_count} book(s) will fall back to standalone.`,
      )
    )
      return;
    setBusy((b) => ({ ...b, [s.id]: "delete" }));
    try {
      await api.del(`/discovery/series/${s.id}${slugQuery(slug ?? undefined)}`);
      load();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      alert(`Delete failed: ${msg}`);
    } finally {
      setBusy((b) => {
        const n = { ...b };
        delete n[s.id];
        return n;
      });
    }
  };

  const filterTabs: { id: FilterMode; label: string }[] = useMemo(
    () => [
      { id: "all", label: "All" },
      { id: "per-author", label: "Per-Author" },
      { id: "shared", label: "Shared" },
    ],
    [],
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      {/* Header */}
      <div>
        <h1
          style={{
            fontSize: 26,
            fontWeight: 700,
            color: t.text,
            margin: 0,
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <span style={{ fontSize: 22 }}>🗂️</span> Series Manager
        </h1>
        <p style={{ fontSize: 14, color: t.td, marginTop: 4 }}>
          Browse every series in the library. Use{" "}
          <strong>Manage members</strong> to add or remove authors —
          single-author series flip to <em>shared</em> automatically when a
          second author is added, and back to <em>per-author</em> when the
          last contributing author is removed. Rename or delete from the row
          actions; books fall back to standalone when a series is deleted.
        </p>
      </div>

      {/* v3.0.0 Phase 8 — per-library tabs (scope reads + mutations via
          ?slug=, not the global active-library switch). Hidden for
          single-library installs. */}
      {libs.length > 1 ? (
        <div style={{ display: "flex", gap: 4, borderBottom: `1px solid ${t.border}` }}>
          {libs.map((l) => {
            const active = slug === l.slug;
            const color = l.content_type === "audiobook" ? t.pur || t.accent : t.accent;
            return (
              <button
                key={l.slug}
                onClick={() => { setOffset(0); setSlug(l.slug); }}
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

      {/* Filter tabs */}
      <div
        style={{
          display: "flex",
          gap: 6,
          borderBottom: `1px solid ${t.borderL}`,
        }}
      >
        {filterTabs.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setFilter(tab.id)}
            style={{
              padding: "10px 16px",
              background: "none",
              border: "none",
              borderBottom:
                filter === tab.id
                  ? `2px solid ${t.accent}`
                  : "2px solid transparent",
              color: filter === tab.id ? t.accent : t.tf,
              fontWeight: filter === tab.id ? 600 : 500,
              fontSize: 14,
              cursor: "pointer",
              marginBottom: -1,
            }}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Search bar */}
      <div
        style={{
          display: "flex",
          gap: 12,
          alignItems: "center",
          flexWrap: "wrap",
        }}
      >
        <input
          type="search"
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
          placeholder="Search by series, author, or book title…"
          style={{
            flex: "1 1 360px",
            maxWidth: 480,
            padding: "8px 12px",
            fontSize: 14,
            background: t.bg2,
            color: t.text,
            border: `1px solid ${t.border}`,
            borderRadius: 6,
          }}
        />
        {data ? (
          <span style={{ fontSize: 13, color: t.td, marginLeft: "auto" }}>
            {total === 0
              ? "no results"
              : `showing ${pageStart}–${pageEnd} of ${total}`}
          </span>
        ) : null}
      </div>

      {data === null ? <Load /> : null}

      {/* Empty state */}
      {data && data.series.length === 0 ? (
        <div
          style={{
            background: t.bg2,
            border: `1px solid ${t.border}`,
            borderRadius: 12,
            padding: 40,
            textAlign: "center",
            color: t.tg,
          }}
        >
          <div style={{ fontSize: 32, marginBottom: 8 }}>—</div>
          <div style={{ fontSize: 14 }}>
            {search.trim()
              ? "No series match this search."
              : "No series match the current filter."}
          </div>
        </div>
      ) : null}

      {/* Series list */}
      {data && data.series.length > 0 ? (
        <div
          style={{
            background: t.bg2,
            border: `1px solid ${t.border}`,
            borderRadius: 12,
            overflow: "hidden",
            display: "flex",
            flexDirection: "column",
          }}
        >
          {data.series.map((s, idx) => (
            <SeriesRowCard
              key={s.id}
              s={s}
              first={idx === 0}
              busyAction={busy[s.id]}
              onManage={() => setManageTarget(s)}
              onRename={() => rename(s)}
              onRemove={() => remove(s)}
            />
          ))}
        </div>
      ) : null}

      {/* Pagination footer */}
      {data && total > PAGE_SIZE ? (
        <div
          style={{
            display: "flex",
            gap: 10,
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <Btn
            variant="ghost"
            size="sm"
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            disabled={!hasPrev}
          >
            ← Prev
          </Btn>
          <span style={{ fontSize: 13, color: t.tf }}>
            Page {Math.floor(offset / PAGE_SIZE) + 1} of{" "}
            {Math.max(1, Math.ceil(total / PAGE_SIZE))}
          </span>
          <Btn
            variant="ghost"
            size="sm"
            onClick={() => setOffset(offset + PAGE_SIZE)}
            disabled={!hasNext}
          >
            Next →
          </Btn>
        </div>
      ) : null}

      {/* Manage Members modal */}
      {manageTarget ? (
        <ManageMembersModal
          seriesId={manageTarget.id}
          seriesName={manageTarget.name}
          seriesSlug={slug}
          onClose={() => setManageTarget(null)}
          onChanged={() => load()}
        />
      ) : null}
    </div>
  );
}

interface SeriesRowCardProps {
  s: SeriesRow;
  first: boolean;
  busyAction: string | undefined;
  onManage: () => void;
  onRename: () => void;
  onRemove: () => void;
}

function SeriesRowCard({
  s,
  first,
  busyAction,
  onManage,
  onRename,
  onRemove,
}: SeriesRowCardProps) {
  const t = useTheme();
  // v3.0.0 Phase 7 — 3-way author-mode label (ADR-0010): per_author /
  // multi_author ("Co-authored", a team on every book) / shared. The
  // backend `multi_author` + `is_shared` flags give all three; both
  // non-per_author modes use the accent badge styling.
  const isShared = s.is_shared === 1;
  const isCoauthored = s.multi_author === 1;
  const modeLabel = isShared
    ? "Shared"
    : isCoauthored
      ? "Co-authored"
      : "Per-Author";
  const accentBadge = isShared || isCoauthored;

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 16,
        padding: "12px 16px",
        borderTop: first ? "none" : `1px solid ${t.borderL}`,
      }}
    >
      {/* Cover thumbnail */}
      <div
        style={{
          width: 72,
          height: 108,
          background: t.bg3,
          borderRadius: 4,
          overflow: "hidden",
          flexShrink: 0,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: t.tg,
          fontSize: 22,
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
          <span>—</span>
        )}
      </div>

      {/* Title + meta */}
      <div
        style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          gap: 4,
          minWidth: 0,
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            flexWrap: "wrap",
          }}
        >
          <span style={{ fontSize: 16, fontWeight: 600, color: t.text }}>
            {s.name}
          </span>
          <span
            style={{
              display: "inline-block",
              padding: "2px 8px",
              borderRadius: 4,
              fontSize: 11,
              background: accentBadge ? t.abg : t.bg,
              color: accentBadge ? t.accent : t.tf,
              border: `1px solid ${accentBadge ? t.abr : t.border}`,
              textTransform: "uppercase",
              letterSpacing: "0.04em",
            }}
          >
            {modeLabel}
          </span>
        </div>
        <div style={{ fontSize: 13, color: t.tf }}>
          {isShared ? (
            <span>
              shared across{" "}
              <span style={{ color: t.accent, fontWeight: 600 }}>
                {s.contributor_count}
              </span>{" "}
              authors
            </span>
          ) : isCoauthored ? (
            <span>
              co-authored by{" "}
              <span style={{ color: t.accent, fontWeight: 600 }}>
                {s.contributor_count}
              </span>{" "}
              authors
            </span>
          ) : (
            <span>{s.author_name || "—"}</span>
          )}
        </div>
        <div style={{ fontSize: 12, color: t.td }}>
          {s.book_count} book{s.book_count === 1 ? "" : "s"} —{" "}
          {s.owned_count} owned, {s.missing_count} missing
        </div>
      </div>

      {/* Row actions */}
      <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
        <Btn
          onClick={onManage}
          disabled={!!busyAction}
          variant="accent"
          size="sm"
        >
          Manage members
        </Btn>
        <Btn
          onClick={onRename}
          disabled={!!busyAction}
          variant="ghost"
          size="sm"
        >
          {busyAction === "rename" ? <Spin /> : null} Rename
        </Btn>
        <Btn
          onClick={onRemove}
          disabled={!!busyAction}
          variant="ghost"
          size="sm"
        >
          {busyAction === "delete" ? <Spin /> : null} Delete
        </Btn>
      </div>
    </div>
  );
}
