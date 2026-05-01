// Authors page — alphabet sidebar + paginated grid/list.
import { useEffect, useMemo, useState } from "react";
import { useTheme } from "../theme";
import type { Theme } from "../theme";
import { api } from "../api";
import { usePersist } from "../hooks/usePersist";
import { Btn } from "../components/Btn";
import { ClearMenu } from "../components/ClearMenu";
import { Load } from "../components/Load";
import { SearchBar } from "../components/SearchBar";
import { VT, type ViewMode } from "../components/VT";
import { PB } from "../components/PB";
import { toast } from "../lib/toast";
import { useViewport } from "../hooks/useViewport";
import { useMobileCodepath } from "../components/mobile";
import MobileAuthorsPage from "./MobileAuthorsPage";
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
type ContentScope = "ebook" | "audiobook";

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

export default function AuthorsPage(props: { onNav: NavFn }) {
  // Mobile codepath catches phones, iPads, and any touch device.
  const vp = useViewport();
  if (useMobileCodepath(vp)) {
    return <MobileAuthorsPage {...props} />;
  }
  return <DesktopAuthorsPage {...props} />;
}

function DesktopAuthorsPage({ onNav }: { onNav: NavFn }) {
  const t = useTheme();
  const [aus, setAus] = useState<Author[]>([]);
  const [ld, setLd] = useState(true);
  const [q, setQ] = usePersist<string>("ap_q", "");
  const [sort, setSort] = usePersist<string>("ap_sort", "name");
  const [vm, setVm] = usePersist<ViewMode>("ap_vm", "list");
  const [letter, setLetter] = usePersist<string>("ap_letter", "");
  const [fmt, setFmt] = usePersist<string>("ap_fmt", "all");
  const [pg, setPg] = usePersist<number>("ap_pg", 1);
  const [selMode, setSelMode] = useState(false);
  // sel is keyed by `${library_slug}:${id}` (or just `${id}` for the
  // active-library path) so cross-library authors with the same numeric
  // id in different libraries — e.g. ABS lib id=17 (Touko Amekawa) vs
  // ebook lib id=17 (Roger Black) — don't collide. Bare-id keying
  // produced wrong-author selections + wrong-name scans because two
  // distinct merged authors would share the same numeric id.
  const [sel, setSel] = useState<Set<string>>(new Set());
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

  // Clear cross-page selection state when the filter context changes.
  // The selectAllVisible button is intentionally additive across pages
  // (so paging through 200+ authors and clicking Select All on each
  // page accumulates a multi-page selection), but if the user switches
  // letter / search / sort / format, the previous selection is no
  // longer about the same set of authors. Clearing on filter change
  // prevents stale IDs from leaking into a new scan dispatch — e.g.
  // the v2.2.1 UAT bug where Roger Black got scanned during an
  // "A last name" multi-select because he was selected earlier under
  // the "B" letter filter.
  useEffect(() => {
    setSel(new Set());
  }, [letter, q, sort, fmt]);

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

  // Globally-unique key per merged author. The `library_slug:id` form
  // is unique because each library's IDs are unique within that library
  // — different libraries can share numeric ids but different (slug, id)
  // pairs are always distinct authors.
  const authorKey = (a: Pick<Author, "id" | "library_slug">): string =>
    a.library_slug ? `${a.library_slug}:${a.id}` : String(a.id);

  const toggleSel = (a: Author) =>
    setSel((p) => {
      const k = authorKey(a);
      const n = new Set(p);
      if (n.has(k)) n.delete(k);
      else n.add(k);
      return n;
    });

  // Adds the currently-visible page slice to the selection without
  // wiping cross-page selections. Click on each page to accumulate
  // a multi-page selection.
  const selectAllVisible = () =>
    setSel((p) => new Set([...p, ...visible.map(authorKey)]));

  // Resolve sel back to merged author rows. Used wherever we need to
  // POST a list of selected authors — we filter `aus` by key instead
  // of mapping `[...sel]` to ids/names via a lookup table, because
  // the lookup table approach has the duplicate-id bug this fix is
  // about.
  const selectedAuthors = (): Author[] =>
    aus.filter((a) => sel.has(authorKey(a)));

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
    const picked = selectedAuthors();
    if (picked.length < 2) return;
    const canonical = picked[0].id;
    const aliases = picked.slice(1).map((a) => a.id);
    const canonicalName = picked[0].name || `#${canonical}`;
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

  const clearData = async (type: ClearType, scope?: ContentScope) => {
    const labels: Record<ClearType, string> = {
      source: "source scan",
      mam: "MAM scan",
      both: "all scan",
    };
    const scopeLabel = scope
      ? ` (${scope === "audiobook" ? "audiobook" : "ebook"} libraries only)`
      : "";
    if (
      !confirm(
        `Clear ${labels[type]} data${scopeLabel} for ${sel.size} author(s)?`,
      )
    )
      return;
    setClearing(true);
    try {
      const picked = selectedAuthors();
      await api.post("/discovery/authors/clear-scan-data", {
        author_ids: picked.map((a) => a.id),
        author_names: picked.map((a) => a.name),
        clear_source: type === "source" || type === "both",
        clear_mam: type === "mam" || type === "both",
        ...(scope ? { content_type: scope } : {}),
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

  const scanSources = async (scope?: ContentScope) => {
    const scopeLabel = scope
      ? ` (${scope === "audiobook" ? "audiobook" : "ebook"} libraries)`
      : "";
    if (!confirm(`Scan${scopeLabel} ${sel.size} author(s)?`)) return;
    setScanning(true);
    try {
      // Sending names alongside IDs lets the cross-library backend
      // skip the ID→name resolver step. Names are resolved by FILTER
      // on `aus` (not via a Map keyed by id), because cross-library
      // merged authors can share numeric ids across libraries — e.g.
      // ebook lib id=17 (Roger Black) collides with ABS lib id=17
      // (Touko Amekawa). A Map keyed by id loses one of the two; the
      // filter approach combined with the `library_slug:id` selection
      // key above keeps both distinct.
      const picked = selectedAuthors();
      await api.post("/discovery/authors/scan-sources", {
        author_ids: picked.map((a) => a.id),
        author_names: picked.map((a) => a.name),
        ...(scope ? { content_type: scope } : {}),
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
      const picked = selectedAuthors();
      const r = await api.post<BulkScanResponse>(
        "/discovery/authors/scan-mam",
        { author_ids: picked.map((a) => a.id) },
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
      {/* ── Alphabet Sidebar — hidden on mobile via .seshat-alphabet
          CSS rule. Touch users jump via search instead, and the
          ~80px column is dead weight on a phone. ── */}
      <div
        className="seshat-alphabet"
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
            className="page-header-row"
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
            <div className="page-header-controls" style={{ display: "flex", gap: 8, alignItems: "center" }}>
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
        {selMode && (
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
              {sel.size} author{sel.size === 1 ? "" : "s"} selected
            </span>
            {sel.size > 0 && (
              <>
                <Btn
                  size="sm"
                  onClick={() => scanSources("ebook")}
                  disabled={scanning || clearing || linking}
                  title="Scan these authors across every ebook library"
                  style={{
                    background: t.grn + "22",
                    color: t.grnt,
                    border: `1px solid ${t.grn}44`,
                  }}
                >
                  Scan Sources
                </Btn>
                <Btn
                  size="sm"
                  onClick={() => scanSources("audiobook")}
                  disabled={scanning || clearing || linking}
                  title="Scan these authors across every audiobook library"
                  style={{
                    background: t.pur + "22",
                    color: t.purt,
                    border: `1px solid ${t.pur}44`,
                  }}
                >
                  Scan Audio
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
                <ClearMenu
                  disabled={clearing}
                  options={[
                    {
                      label: "Clear Source",
                      hint: "active library",
                      onClick: () => clearData("source"),
                    },
                    {
                      label: "Clear Source",
                      hint: "all ebook libraries",
                      variant: "ebook",
                      onClick: () => clearData("source", "ebook"),
                    },
                    {
                      label: "Clear Source",
                      hint: "all audiobook libraries",
                      variant: "audio",
                      onClick: () => clearData("source", "audiobook"),
                    },
                    ...(mamOn
                      ? [
                          {
                            label: "Clear MAM",
                            divider: true,
                            onClick: () => clearData("mam"),
                          },
                          {
                            label: "Clear Both (Source + MAM)",
                            variant: "danger" as const,
                            onClick: () => clearData("both"),
                          },
                        ]
                      : []),
                  ]}
                />
                <span style={{ width: 1, height: 20, background: t.border }} />
              </>
            )}
            <Btn
              size="sm"
              onClick={selectAllVisible}
              disabled={scanning || clearing || linking}
            >
              Select All on Page
            </Btn>
            {sel.size > 0 && (
              <Btn
                size="sm"
                onClick={() => setSel(new Set())}
                disabled={scanning || clearing || linking}
              >
                Deselect All
              </Btn>
            )}
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
                key={authorKey(a)}
                a={a}
                t={t}
                selected={sel.has(authorKey(a))}
                onClick={() =>
                  selMode
                    ? toggleSel(a)
                    : onNav("disc-author-detail", navArg(a))
                }
              />
            ))}
          </div>
        ) : (
          <div className="seshat-author-cols" style={{ columns: 2, columnGap: 6 }}>
            {visible.map((a) => (
              <div
                key={authorKey(a)}
                style={{ breakInside: "avoid", marginBottom: 4 }}
              >
                <AuthorRow
                  a={a}
                  t={t}
                  selected={sel.has(authorKey(a))}
                  onClick={() =>
                    selMode
                      ? toggleSel(a)
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
