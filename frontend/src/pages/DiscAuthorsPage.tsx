// Authors page — alphabet sidebar + paginated grid/list.
import { useEffect, useMemo, useState } from "react";
import { useTheme } from "../theme";
import type { Theme } from "../theme";
import { api } from "../api";
import { usePersist } from "../hooks/usePersist";
import { Btn } from "../components/Btn";
import { Load } from "../components/Load";
import { SearchBar } from "../components/SearchBar";
import { VT, type ViewMode } from "../components/VT";
import { PB } from "../components/PB";
import { toast } from "../lib/toast";
import type {
  Author,
  AuthorsResponse,
  MamStatusResponse,
  NavFn,
} from "../types";

const ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZ#".split("");
const PER_PAGE_GRID = 42;
const PER_PAGE_LIST = 24;

type LinkType = "pen_name" | "co_author";
type ClearType = "source" | "mam" | "both";

// Response envelopes for the bulk scan / scan-mam endpoints. `error`
// and `message` are both optional since the server uses them to
// signal "nothing eligible" without an HTTP error status.
interface BulkScanResponse {
  error?: string;
  message?: string;
  status?: string;
  total?: number;
}

function getLastName(name: string): string {
  const parts = (name || "").trim().split(/\s+/);
  return parts.length > 1 ? parts[parts.length - 1] : parts[0] || "";
}

function getLetterKey(name: string): string {
  const ln = getLastName(name);
  const ch = ln.charAt(0).toUpperCase();
  return /[A-Z]/.test(ch) ? ch : "#";
}

export default function AuthorsPage({ onNav }: { onNav: NavFn }) {
  const t = useTheme();
  const [aus, setAus] = useState<Author[]>([]);
  const [ld, setLd] = useState(true);
  const [q, setQ] = usePersist<string>("ap_q", "");
  const [sort, setSort] = usePersist<string>("ap_sort", "name");
  const [vm, setVm] = usePersist<ViewMode>("ap_vm", "list");
  const [letter, setLetter] = usePersist<string>("ap_letter", "");
  const [fmt, setFmt] = usePersist<string>("ap_fmt", "all");
  const [pg, setPg] = useState(1);
  const [selMode, setSelMode] = useState(false);
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [clearing, setClearing] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [mamOn, setMamOn] = useState(false);
  const [linking, setLinking] = useState(false);

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
      })
      .catch((e) => {
        if (!api.isAbort(e)) setLd(false);
      });
    return () => c.abort();
  }, [q, sort, fmt]);

  // Filter by letter
  const filtered = useMemo(() => {
    if (!letter) return aus;
    return aus.filter((a) => getLetterKey(a.name) === letter);
  }, [aus, letter]);

  // Letter counts for sidebar badges
  const letterCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    aus.forEach((a) => {
      const k = getLetterKey(a.name);
      counts[k] = (counts[k] || 0) + 1;
    });
    return counts;
  }, [aus]);

  // Pagination
  const perPage = vm === "grid" ? PER_PAGE_GRID : PER_PAGE_LIST;
  const totalPages = Math.max(1, Math.ceil(filtered.length / perPage));
  const page = Math.min(pg, totalPages);
  const visible = filtered.slice((page - 1) * perPage, page * perPage);

  const toggleSel = (id: number) =>
    setSel((p) => {
      const n = new Set(p);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const reload = () => {
    setLd(true);
    const params = new URLSearchParams({ search: q, sort, content_type: fmt });
    api
      .get<AuthorsResponse>(`/discovery/authors?${params}`)
      .then((d) => {
        setAus(d.authors || []);
        setLd(false);
      })
      .catch(() => setLd(false));
  };

  const linkAuthors = async (linkType: LinkType) => {
    if (sel.size < 2) return;
    const ids = [...sel];
    const canonical = ids[0];
    const aliases = ids.slice(1);
    const canonicalName =
      (aus.find((a) => a.id === canonical) || { name: `#${canonical}` }).name ||
      `#${canonical}`;
    const label = linkType === "co_author" ? "co-author" : "pen name";
    if (
      !confirm(
        `Link ${aliases.length} author(s) as ${label}${aliases.length > 1 ? "s" : ""} of ${canonicalName}?`,
      )
    )
      return;
    setLinking(true);
    let ok = 0;
    let failed = 0;
    for (const aliasId of aliases) {
      try {
        await api.post("/discovery/authors/link-pen-names", {
          canonical_author_id: canonical,
          alias_author_id: aliasId,
          link_type: linkType,
        });
        ok++;
      } catch {
        failed++;
      }
    }
    setLinking(false);
    if (ok) toast.success(`Linked ${ok} author(s)`);
    if (failed) toast.error(`${failed} link(s) failed`);
    setSel(new Set());
    setSelMode(false);
    reload();
  };

  const clearData = async (type: ClearType) => {
    const labels: Record<ClearType, string> = {
      source: "source scan",
      mam: "MAM scan",
      both: "all scan",
    };
    if (!confirm(`Clear ${labels[type]} data for ${sel.size} author(s)?`))
      return;
    setClearing(true);
    try {
      await api.post("/discovery/authors/clear-scan-data", {
        author_ids: [...sel],
        clear_source: type === "source" || type === "both",
        clear_mam: type === "mam" || type === "both",
      });
      toast.success("Cleared data");
      setSel(new Set());
      setSelMode(false);
      reload();
    } catch (e) {
      toast.error((e as Error).message || "Error");
    }
    setClearing(false);
  };

  const scanSources = async () => {
    if (!confirm(`Scan ${sel.size} author(s)?`)) return;
    setScanning(true);
    try {
      await api.post("/discovery/authors/scan-sources", {
        author_ids: [...sel],
      });
      toast.info("Scan started");
      setSel(new Set());
      setSelMode(false);
      window.dispatchEvent(new CustomEvent("seshat:scan-started"));
    } catch (e) {
      toast.error((e as Error).message || "Failed");
    }
    setScanning(false);
  };

  const scanMam = async () => {
    if (!confirm(`MAM scan for ${sel.size} author(s)?`)) return;
    setScanning(true);
    try {
      const r = await api.post<BulkScanResponse>(
        "/discovery/authors/scan-mam",
        { author_ids: [...sel] },
      );
      toast.info(r.message || "Scan started");
      setSel(new Set());
      setSelMode(false);
    } catch (e) {
      toast.error((e as Error).message || "Failed");
    }
    setScanning(false);
  };

  // Nav arg — when the row came from cross-library aggregation
  // (a.library_slug is set by run_across_libraries), send "slug:id"
  // so the detail page resolves in the right library. Without this,
  // ABS's author id 5 (Troy Denning) gets looked up in Calibre where
  // id 5 is Jack Bryce.
  const navArg = (a: Author): string | number =>
    a.library_slug ? `${a.library_slug}:${a.id}` : a.id;

  return (
    <div style={{ display: "flex", gap: 0 }}>
      {/* ── Alphabet Sidebar ── */}
      <div
        style={{
          width: 80,
          flexShrink: 0,
          position: "sticky",
          top: 56,
          alignSelf: "flex-start",
          maxHeight: "calc(100vh - 100px)",
          overflowY: "auto",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          paddingTop: 10,
          paddingBottom: 10,
          paddingLeft: 12,
          paddingRight: 12,
          marginRight: 16,
          background: t.bg2,
          border: `1px solid ${t.borderL}`,
          borderRadius: 10,
        }}
      >
        <button
          onClick={() => {
            setLetter("");
            setPg(1);
          }}
          style={{
            background: !letter ? t.abg : "transparent",
            color: !letter ? t.accent : t.td,
            border: "none",
            borderRadius: 6,
            width: 52,
            padding: "6px 0",
            fontSize: 13,
            fontWeight: 700,
            cursor: "pointer",
            marginBottom: 6,
          }}
        >
          All
        </button>
        {ALPHA.map((ch) => {
          const cnt = letterCounts[ch] || 0;
          const active = letter === ch;
          return (
            <button
              key={ch}
              onClick={() => {
                setLetter(active ? "" : ch);
                setPg(1);
              }}
              style={{
                background: active ? t.abg : "transparent",
                color: cnt === 0 ? t.tg : active ? t.accent : t.td,
                border: "none",
                borderRadius: 6,
                width: 52,
                padding: "4px 0",
                fontSize: 15,
                fontWeight: active ? 700 : 500,
                cursor: cnt === 0 ? "default" : "pointer",
                opacity: cnt === 0 ? 0.3 : 1,
              }}
            >
              {ch}
              <span style={{ fontSize: 9, color: t.tf, display: "block" }}>
                {cnt || ""}
              </span>
            </button>
          );
        })}
      </div>

      {/* ── Main Content ── */}
      <div style={{ flex: 1, minWidth: 0, paddingLeft: 12 }}>
        {/* Sticky header */}
        <div
          style={{
            position: "sticky",
            top: 56,
            zIndex: 20,
            background: t.bg + "ee",
            backdropFilter: "blur(8px)",
            padding: "8px 0",
            marginBottom: 8,
          }}
        >
          {/* Format tabs — same semantics as DiscBooksPage: "all" is
              the cross-library union, "ebook" / "audiobook" narrow to
              authors who have books in that library type. */}
          <div style={{ display: "flex", gap: 4, marginBottom: 8 }}>
            {[
              { id: "all", label: "All", icon: "" },
              { id: "ebook", label: "Ebooks", icon: "📖" },
              { id: "audiobook", label: "Audiobooks", icon: "🎧" },
            ].map((tab) => (
              <button
                key={tab.id}
                onClick={() => {
                  setFmt(tab.id);
                  setPg(1);
                }}
                style={{
                  background: fmt === tab.id ? t.abg : "transparent",
                  color: fmt === tab.id ? t.accent : t.tm,
                  border: `1px solid ${fmt === tab.id ? t.abr : "transparent"}`,
                  borderRadius: 6,
                  padding: "4px 12px",
                  fontSize: 13,
                  fontWeight: fmt === tab.id ? 600 : 500,
                  cursor: "pointer",
                  display: "flex",
                  alignItems: "center",
                  gap: 5,
                }}
              >
                {tab.icon ? <span>{tab.icon}</span> : null}
                <span>{tab.label}</span>
              </button>
            ))}
          </div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 12,
            }}
          >
            <h1
              style={{
                fontSize: 24,
                fontWeight: 800,
                color: t.accent,
                margin: 0,
                flexShrink: 0,
              }}
            >
              Authors{" "}
              <span
                style={{
                  fontSize: 15,
                  fontWeight: 600,
                  color: t.td,
                  marginLeft: 6,
                }}
              >
                {letter
                  ? `${filtered.length} in "${letter}"`
                  : `${aus.length} total`}
              </span>
            </h1>
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <SearchBar
                value={q}
                onChange={(v) => {
                  setQ(v);
                  setPg(1);
                }}
              />
              <select
                value={sort}
                onChange={(e) => {
                  setSort(e.target.value);
                  setPg(1);
                }}
                style={{
                  padding: "6px 10px",
                  borderRadius: 6,
                  border: `1px solid ${t.border}`,
                  background: t.inp,
                  color: t.text2,
                  fontSize: 12,
                }}
              >
                <option value="name">Sort: Name</option>
                <option value="books">Sort: Books</option>
                <option value="missing">Sort: Missing</option>
              </select>
              <VT mode={vm} setMode={setVm} />
              <Btn
                size="sm"
                variant={selMode ? "accent" : "default"}
                onClick={() => {
                  setSelMode(!selMode);
                  if (selMode) setSel(new Set());
                }}
              >
                {selMode ? "Cancel" : "Select"}
              </Btn>
            </div>
          </div>
          {/* Pagination */}
          {totalPages > 1 && (
            <div
              style={{
                display: "flex",
                gap: 6,
                alignItems: "center",
                marginTop: 6,
              }}
            >
              <Btn size="sm" disabled={page <= 1} onClick={() => setPg(1)}>
                «
              </Btn>
              <Btn
                size="sm"
                disabled={page <= 1}
                onClick={() => setPg((p) => p - 1)}
              >
                ‹ Prev
              </Btn>
              <span
                style={{
                  fontSize: 13,
                  color: t.td,
                  fontWeight: 500,
                  padding: "0 4px",
                }}
              >
                Page {page} of {totalPages}
              </span>
              <Btn
                size="sm"
                disabled={page >= totalPages}
                onClick={() => setPg((p) => p + 1)}
              >
                Next ›
              </Btn>
              <Btn
                size="sm"
                disabled={page >= totalPages}
                onClick={() => setPg(totalPages)}
              >
                »
              </Btn>
            </div>
          )}
        </div>

        {/* Selection bar */}
        {selMode && sel.size > 0 && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              padding: "10px 14px",
              background: t.bg2,
              border: `1px solid ${t.border}`,
              borderRadius: 8,
              flexWrap: "wrap",
              marginBottom: 12,
            }}
          >
            <span style={{ fontSize: 13, fontWeight: 600, color: t.text2 }}>
              {sel.size} selected
            </span>
            <Btn
              size="sm"
              onClick={scanSources}
              disabled={scanning || clearing || linking}
              style={{
                background: t.grn + "22",
                color: t.grnt,
                border: `1px solid ${t.grn}44`,
              }}
            >
              Scan Sources
            </Btn>
            {mamOn && (
              <Btn
                size="sm"
                onClick={scanMam}
                disabled={scanning || clearing || linking}
                style={{
                  background: t.accent + "22",
                  color: t.accent,
                  border: `1px solid ${t.accent}44`,
                }}
              >
                Scan MAM
              </Btn>
            )}
            {sel.size >= 2 && (
              <>
                <span
                  style={{ width: 1, height: 20, background: t.border }}
                />
                <Btn
                  size="sm"
                  onClick={() => linkAuthors("pen_name")}
                  disabled={linking}
                  style={{
                    background: t.purb || t.bg4,
                    color: t.purt,
                    border: `1px solid ${t.pur}44`,
                  }}
                >
                  Link Pen Names
                </Btn>
                <Btn
                  size="sm"
                  onClick={() => linkAuthors("co_author")}
                  disabled={linking}
                  style={{
                    background: t.cyan + "22",
                    color: t.cyant,
                    border: `1px solid ${t.cyan}44`,
                  }}
                >
                  Link Co-Authors
                </Btn>
              </>
            )}
            <span style={{ width: 1, height: 20, background: t.border }} />
            <Btn
              size="sm"
              onClick={() => clearData("source")}
              disabled={clearing}
              style={{
                background: t.ylw + "22",
                color: t.ylwt,
                border: `1px solid ${t.ylw}44`,
              }}
            >
              Clear Source
            </Btn>
            {mamOn && (
              <Btn
                size="sm"
                onClick={() => clearData("mam")}
                disabled={clearing}
                style={{
                  background: t.cyan + "22",
                  color: t.cyant,
                  border: `1px solid ${t.cyan}44`,
                }}
              >
                Clear MAM
              </Btn>
            )}
            <Btn size="sm" onClick={() => setSel(new Set())}>
              Deselect
            </Btn>
          </div>
        )}

        {/* Author list/grid */}
        {ld ? (
          <Load />
        ) : vm === "grid" ? (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
              gap: 10,
            }}
          >
            {visible.map((a) => (
              <AuthorCard
                key={a.id}
                a={a}
                t={t}
                selected={sel.has(a.id)}
                onClick={() =>
                  selMode
                    ? toggleSel(a.id)
                    : onNav("disc-author-detail", navArg(a))
                }
              />
            ))}
          </div>
        ) : (
          <div style={{ columns: 2, columnGap: 6 }}>
            {visible.map((a) => (
              <div
                key={a.id}
                style={{ breakInside: "avoid", marginBottom: 4 }}
              >
                <AuthorRow
                  a={a}
                  t={t}
                  selected={sel.has(a.id)}
                  onClick={() =>
                    selMode
                      ? toggleSel(a.id)
                      : onNav("disc-author-detail", navArg(a))
                  }
                />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Author Card (grid view) ──────────────────────────────────

interface AuthorRowProps {
  a: Author;
  t: Theme;
  selected: boolean;
  onClick: () => void;
}

function AuthorCard({ a, t, selected, onClick }: AuthorRowProps) {
  const owned = a.owned_count || 0;
  const missing = a.missing_count || 0;
  const total = a.total_books || 0;
  return (
    <div
      onClick={onClick}
      style={{
        background: selected ? t.accent + "15" : t.bg2,
        border: `1px solid ${selected ? t.accent : t.borderL}`,
        borderRadius: 10,
        padding: "16px 14px",
        cursor: "pointer",
        transition: "border-color 0.15s",
      }}
    >
      <div>
        {/* Name + link badge */}
        <div
          style={{
            fontSize: 16,
            fontWeight: 700,
            color: t.text,
            marginBottom: 8,
            lineHeight: 1.3,
          }}
        >
          {a.name}
          {(a.link_count || 0) > 0 && (
            <span
              style={{
                display: "inline-flex",
                padding: "1px 5px",
                borderRadius: 4,
                fontSize: 9,
                fontWeight: 500,
                background: t.purb || t.bg4,
                color: t.purt,
                border: `1px solid ${t.pur}33`,
                marginLeft: 6,
                verticalAlign: "middle",
              }}
            >
              ↔{a.link_count}
            </span>
          )}
        </div>

        {/* Stats */}
        <div
          style={{
            display: "flex",
            gap: 14,
            fontSize: 13,
            marginBottom: 10,
          }}
        >
          <span style={{ color: t.grnt, fontWeight: 600 }}>
            {owned}{" "}
            <span style={{ fontWeight: 400, color: t.td }}>owned</span>
          </span>
          <span style={{ color: t.ylwt, fontWeight: 600 }}>
            {missing}{" "}
            <span style={{ fontWeight: 400, color: t.td }}>missing</span>
          </span>
        </div>

        {/* Progress bar */}
        <PB owned={owned} total={total} />

        {/* Series count */}
        {(a.series_count || 0) > 0 && (
          <div style={{ fontSize: 12, color: t.tf, marginTop: 6 }}>
            {a.series_count} series
          </div>
        )}
      </div>
    </div>
  );
}

// ── Author Row (list view) ───────────────────────────────────

function AuthorRow({ a, t, selected, onClick }: AuthorRowProps) {
  const owned = a.owned_count || 0;
  const missing = a.missing_count || 0;
  const total = a.total_books || 0;
  return (
    <div
      onClick={onClick}
      style={{
        display: "grid",
        gridTemplateColumns: "1fr auto auto auto 90px",
        alignItems: "center",
        gap: 14,
        padding: "10px 14px",
        borderRadius: 8,
        cursor: "pointer",
        background: selected ? t.accent + "15" : t.bg2,
        border: `1px solid ${selected ? t.accent : t.borderL}`,
        transition: "border-color 0.15s",
      }}
    >
      {/* Name */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          minWidth: 0,
        }}
      >
        {a.image_url ? (
          <img
            src={a.image_url}
            alt=""
            style={{
              width: 36,
              height: 36,
              borderRadius: "50%",
              objectFit: "cover",
              flexShrink: 0,
            }}
          />
        ) : (
          <div
            style={{
              width: 36,
              height: 36,
              borderRadius: "50%",
              background: `${t.accent}18`,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 15,
              fontWeight: 700,
              color: t.accent,
              flexShrink: 0,
            }}
          >
            {a.name?.charAt(0)}
          </div>
        )}
        <div style={{ minWidth: 0 }}>
          <div
            style={{
              fontSize: 15,
              fontWeight: 600,
              color: t.text,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            {a.name}
            {(a.link_count || 0) > 0 && (
              <span
                style={{
                  display: "inline-flex",
                  padding: "1px 5px",
                  borderRadius: 4,
                  fontSize: 9,
                  fontWeight: 500,
                  background: t.purb || t.bg4,
                  color: t.purt,
                  border: `1px solid ${t.pur}33`,
                }}
              >
                ↔{a.link_count}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Owned */}
      <div style={{ textAlign: "center", minWidth: 60 }}>
        <div style={{ fontSize: 15, fontWeight: 700, color: t.grnt }}>
          {owned}
        </div>
        <div style={{ fontSize: 10, color: t.td }}>owned</div>
      </div>

      {/* Missing */}
      <div style={{ textAlign: "center", minWidth: 60 }}>
        <div
          style={{
            fontSize: 15,
            fontWeight: 700,
            color: missing > 0 ? t.ylwt : t.td,
          }}
        >
          {missing}
        </div>
        <div style={{ fontSize: 10, color: t.td }}>missing</div>
      </div>

      {/* Series */}
      <div style={{ textAlign: "center", minWidth: 50 }}>
        <div style={{ fontSize: 15, fontWeight: 700, color: t.purt }}>
          {a.series_count || 0}
        </div>
        <div style={{ fontSize: 10, color: t.td }}>series</div>
      </div>

      {/* Progress bar */}
      <PB owned={owned} total={total} />
    </div>
  );
}
