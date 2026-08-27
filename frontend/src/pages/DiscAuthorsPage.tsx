// Authors page — alphabet sidebar + paginated grid/list.
import { useEffect, useMemo, useState } from "react";
import { useTheme } from "../theme";
import type { Theme } from "../theme";
import { api } from "../api";
import { usePersist } from "../hooks/usePersist";
import { saveAuthorWalk } from "../hooks/useAuthorWalk";
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
  // v2.17.0 — sort direction toggle (parity with DiscBooksPage). The
  // "Owned" / "Missing" / "Books" sorts implicitly favored DESC in the
  // backend's `sort_fn` lambdas (negative-key trick), but the user-
  // facing sort dropdown only had one entry per metric and no way to
  // invert. Now backend + frontend both honor `sort_dir`.
  const [sortDir, setSortDir] = usePersist<string>("ap_sort_dir", "asc");
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
  // v2.17.0 — generic busy flag for bulk Hide / Delete cascades
  // (distinct from `scanning` / `clearing` so the UI can show the
  // right spinner per action class).
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .get<MamStatusResponse>("/discovery/mam/status")
      .then((r) => setMamOn(!!r.enabled))
      .catch(() => {});
  }, []);

  useEffect(() => {
    const c = new AbortController();
    setLd(true);
    const params = new URLSearchParams({ search: q, sort, sort_dir: sortDir, content_type: fmt });
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
  }, [q, sort, sortDir, fmt]);

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
  }, [letter, q, sort, sortDir, fmt]);

  // Globally-unique key per merged author. The `library_slug:id` form
  // is unique because each library's IDs are unique within that library
  // — different libraries can share numeric ids but different (slug, id)
  // pairs are always distinct authors.
  const authorKey = (a: Pick<Author, "id" | "library_slug">): string =>
    a.library_slug ? `${a.library_slug}:${a.id}` : String(a.id);

  // v3.10.0 — ids whose sources include a `review`-flagged record. One
  // request for the whole library (the endpoint aggregates in a single
  // pass), refreshed alongside the list so blacklisting an author drops
  // them out of the worklist on the next load.
  // Keyed by `authorKey` ("slug:id"), NOT bare id — an authors.id is
  // only unique within its library, and the "All" tab mixes libraries,
  // so integer keying would attach one library's flag to another
  // library's author with the same id.
  const [reviewIds, setReviewIds] = useState<Set<string>>(new Set());
  const [reviewOnly, setReviewOnly] = usePersist<boolean>("ap_review", false);

  useEffect(() => {
    const c = new AbortController();
    api
      .get<{ author_keys: string[] }>(
        "/discovery/authors/source-review",
        c.signal,
      )
      .then((d) => setReviewIds(new Set(d.author_keys || [])))
      .catch((e) => {
        // Non-fatal: the tab just won't appear. Logged rather than
        // swallowed — a silently-absent control is undebuggable.
        if (!api.isAbort(e)) console.error("source-review failed", e);
      });
    return () => c.abort();
  }, [fmt]);

  // Filter by letter, then by the review worklist. Applied in that order
  // so the "Needs review" count reflects the letter you're looking at.
  const filtered = useMemo(() => {
    let out = aus;
    if (letter) out = out.filter((a) => getLetterKey(a.name) === letter);
    // `reviewOnly` persists across sessions but the toggle is hidden
    // when nothing is flagged — so guard on the set being non-empty, or
    // clearing the last flagged author strands the user on a blank list
    // with no visible control to turn the filter back off.
    if (reviewOnly && reviewIds.size > 0) {
      out = out.filter((a) => reviewIds.has(authorKey(a)));
    }
    return out;
  }, [aus, letter, reviewOnly, reviewIds]);

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
    const params = new URLSearchParams({ search: q, sort, sort_dir: sortDir, content_type: fmt });
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
      // v2.12.0 — gate the "started" toast on actually-started work.
      // The backend returns {status: "ok", total: 0, message: "No
      // matching authors..."} when the cross-library author-name
      // resolver finds zero matches; previously the frontend fired
      // toast.info("Scan started") regardless, lying about a scan that
      // never actually ran. Now we read `total` and toast accordingly.
      const r = await api.post<{status?: string; total?: number; message?: string}>(
        "/discovery/authors/scan-sources",
        {
          author_ids: picked.map((a) => a.id),
          author_names: picked.map((a) => a.name),
          ...(scope ? { content_type: scope } : {}),
        },
      );
      if ((r.total ?? 0) > 0) {
        // v2.12.1 #3 — plain English with scope label.
        const scopeWord = scope === "audiobook" ? "audiobook" : "ebook";
        toast.info(
          `Scanning ${scopeWord} sources for ${r.total} author(s).`,
        );
        window.dispatchEvent(new CustomEvent("seshat:scan-started"));
      } else {
        toast.warn(r.message || "Nothing to scan — no matching authors found.");
      }
      setSel(new Set());
      setSelMode(false);
    } catch (e) {
      toast.error((e as Error).message || "Failed");
    }
    setScanning(false);
  };

  // v2.17.0 Feat C — bulk Hide / Delete cascade to each selected
  // author's books across every library. "Hide" sets `hidden=1` on
  // all their books (per-library tile drops out of non-Hidden
  // listings); "Delete" removes their unowned discovery rows
  // (Calibre / ABS-synced skipped). Author rows themselves stay
  // intact so the v2.12.1 dual-row mirror pattern isn't disturbed.
  const bulkHideBooks = async () => {
    const picked = selectedAuthors();
    if (
      !confirm(
        `Hide every book by ${picked.length} author(s)? They'll move to the Hidden page across every library.`,
      )
    )
      return;
    setBusy(true);
    try {
      const r = await api.post<{
        books_hidden?: number;
        libraries_touched?: number;
        error?: string;
      }>("/discovery/authors/bulk-hide-books", {
        author_names: picked.map((a) => a.name),
      });
      if (r.error) toast.error(r.error);
      else {
        toast.success(
          `Hid ${r.books_hidden ?? 0} book(s) across ${r.libraries_touched ?? 0} library/libraries`,
        );
        setSel(new Set());
        setSelMode(false);
        reload();
      }
    } catch (e) {
      toast.error((e as Error).message || "Error hiding");
    }
    setBusy(false);
  };

  const bulkDeleteBooks = async () => {
    const picked = selectedAuthors();
    if (
      !confirm(
        `Delete every UNOWNED book by ${picked.length} author(s)? Calibre / ABS-synced books will be skipped. Author rows stay intact.`,
      )
    )
      return;
    setBusy(true);
    try {
      const r = await api.post<{
        books_deleted?: number;
        books_skipped?: number;
        libraries_touched?: number;
        error?: string;
      }>("/discovery/authors/bulk-delete-books", {
        author_names: picked.map((a) => a.name),
      });
      if (r.error) toast.error(r.error);
      else {
        const parts = [`Deleted ${r.books_deleted ?? 0}`];
        if (r.books_skipped) parts.push(`skipped ${r.books_skipped} library-synced`);
        parts.push(`across ${r.libraries_touched ?? 0} libraries`);
        toast.success(parts.join(", "));
        setSel(new Set());
        setSelMode(false);
        reload();
      }
    } catch (e) {
      toast.error((e as Error).message || "Error deleting");
    }
    setBusy(false);
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

  // Bulk Skip MAM — marks every book under the selected authors as
  // mam_status='not_applicable' across ALL libraries so the rescan
  // loop never visits them again. Used for free-on-the-web authors
  // (Snekguy etc.) whose works almost never end up on MAM, where
  // v2.3.6's widened rescan predicate would otherwise keep retrying
  // each tick. content_type='all' so a single click covers both
  // ebook + audiobook libraries (matches Mark's "scan everywhere"
  // expectation for bulk verbs).
  const skipMam = async () => {
    if (
      !confirm(
        `Skip MAM for all books under ${sel.size} author(s)? They'll be marked Not Applicable across every library.`,
      )
    )
      return;
    setClearing(true);
    try {
      const picked = selectedAuthors();
      const r = await api.post<{ books_skipped?: number; libraries_touched?: number }>(
        "/discovery/authors/skip-mam",
        {
          author_ids: picked.map((a) => a.id),
          author_names: picked.map((a) => a.name),
          content_type: "all",
        },
      );
      toast.success(
        `Skipped MAM for ${r.books_skipped ?? 0} book(s) across ${r.libraries_touched ?? 0} libraries`,
      );
      setSel(new Set());
      setSelMode(false);
      reload();
    } catch (e) {
      toast.error((e as Error).message || "Failed");
    }
    setClearing(false);
  };

  // Nav arg — when the row came from cross-library aggregation
  // (a.library_slug is set by run_across_libraries), send "slug:id"
  // so the detail page resolves in the right library. Without this,
  // ABS's author id 5 (Troy Denning) gets looked up in Calibre where
  // id 5 is Jack Bryce.
  const navArg = (a: Author): string | number =>
    a.library_slug ? `${a.library_slug}:${a.id}` : a.id;

  // v3.10.0 — hand the detail page the order it should walk. Snapshot
  // `filtered`, NOT `visible`: prev/next should cross page boundaries,
  // otherwise "next" dead-ends every `perPage` authors. Taken at click
  // time so it reflects the filters in force when you left the list.
  const openAuthor = (a: Author) => {
    saveAuthorWalk(filtered.map(navArg));
    onNav("disc-author-detail", navArg(a));
  };

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

            {/* v3.10.0 — "Needs review" worklist. Orthogonal to the
                format tabs (it narrows whatever they selected), so it's
                a separate toggle rather than a fourth tab. Hidden when
                nothing is flagged, so it never nags on a clean library. */}
            {reviewIds.size > 0 ? (
              <button
                onClick={() => {
                  setReviewOnly(!reviewOnly);
                  setPg(1);
                }}
                title={
                  "Authors where a source contributed a lot of books that " +
                  "nothing else confirms — worth checking the titles."
                }
                style={{
                  marginLeft: 8,
                  background: reviewOnly ? t.ylwb : "transparent",
                  color: reviewOnly ? t.ylwt : t.tm,
                  border: `1px solid ${reviewOnly ? t.ylw : "transparent"}`,
                  borderRadius: 6,
                  padding: "4px 12px",
                  fontSize: 13,
                  fontWeight: reviewOnly ? 600 : 500,
                  cursor: "pointer",
                  display: "flex",
                  alignItems: "center",
                  gap: 5,
                }}
              >
                <span>⚠</span>
                <span>Needs review {reviewIds.size}</span>
              </button>
            ) : null}
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
                <option value="total">Sort: Books</option>
                <option value="owned">Sort: Owned</option>
                <option value="missing">Sort: Missing</option>
              </select>
              {/* v2.17.0 — sort-direction toggle (parity with DiscBooksPage). */}
              <button
                onClick={() => {
                  setSortDir(sortDir === "asc" ? "desc" : "asc");
                  setPg(1);
                }}
                title={
                  sortDir === "asc"
                    ? "Ascending — click for descending"
                    : "Descending — click for ascending"
                }
                style={{
                  padding: "6px 10px",
                  borderRadius: 6,
                  border: `1px solid ${t.border}`,
                  background: t.inp,
                  color: t.text2,
                  fontSize: 12,
                  fontWeight: 700,
                  cursor: "pointer",
                  minWidth: 28,
                }}
              >
                {sortDir === "asc" ? "↑" : "↓"}
              </button>
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
                  Scan Ebooks
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
                  Scan Audiobooks
                </Btn>
                {mamOn && (
                  <>
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
                    <Btn
                      size="sm"
                      onClick={skipMam}
                      disabled={scanning || clearing || linking}
                      title="Mark every book for these authors as Not Applicable so MAM scans skip them (all libraries)"
                      style={{
                        background: t.bg2,
                        color: t.td,
                        border: `1px solid ${t.borderL}`,
                      }}
                    >
                      Skip MAM
                    </Btn>
                  </>
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
                {/* v2.17.0 Feat C — bulk Hide / Delete cascade to all
                    selected authors' books across every library. Hide
                    is reversible; Delete only touches unowned rows. */}
                <Btn
                  size="sm"
                  onClick={bulkHideBooks}
                  disabled={busy}
                  title="Hide every book by selected authors (moves them to Hidden)"
                  style={{
                    background: t.ylw + "22",
                    color: t.ylwt,
                    border: `1px solid ${t.ylw}44`,
                  }}
                >
                  Hide
                </Btn>
                <Btn
                  size="sm"
                  onClick={bulkDeleteBooks}
                  disabled={busy}
                  title="Delete every UNOWNED book by selected authors (Calibre / ABS-synced skipped)"
                  style={{
                    background: t.red + "22",
                    color: t.red,
                    border: `1px solid ${t.red}44`,
                  }}
                >
                  Delete
                </Btn>
                <span style={{ width: 1, height: 20, background: t.border }} />
              </>
            )}
            <Btn
              size="sm"
              onClick={selectAllVisible}
              disabled={scanning || clearing || linking || busy}
            >
              Select All on Page
            </Btn>
            {sel.size > 0 && (
              <Btn
                size="sm"
                onClick={() => setSel(new Set())}
                disabled={scanning || clearing || linking || busy}
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
                onClick={() => (selMode ? toggleSel(a) : openAuthor(a))}
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
                  onClick={() => (selMode ? toggleSel(a) : openAuthor(a))}
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

// v2.17.0 — emoji string per content_types set. Cross-library merge
// stamps `content_types: ["ebook"]` / `["audiobook"]` / both. The
// flat list view + grid view both call this so an audiobook-bearing
// author is visually distinguishable on the "All" tab without
// hunting through tabs. Empty string when content_types is missing
// (single-library mode falls through without a badge).
function formatBadge(ct: string[] | undefined): string {
  if (!ct || ct.length === 0) return "";
  const hasEbook = ct.includes("ebook");
  const hasAudio = ct.includes("audiobook");
  if (hasEbook && hasAudio) return "📖🎧";
  if (hasAudio) return "🎧";
  if (hasEbook) return "📖";
  return "";
}

function AuthorCard({ a, t, selected, onClick }: AuthorRowProps) {
  const owned = a.owned_count || 0;
  const missing = a.missing_count || 0;
  const total = a.total_books || 0;
  const badge = formatBadge(a.content_types);
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
          {badge && (
            <span
              title={
                badge === "📖🎧"
                  ? "Ebook + audiobook"
                  : badge === "🎧"
                  ? "Audiobook"
                  : "Ebook"
              }
              style={{
                fontSize: 12,
                marginLeft: 6,
                verticalAlign: "middle",
              }}
            >
              {badge}
            </span>
          )}
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
  const badge = formatBadge(a.content_types);
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
            {badge && (
              <span
                title={
                  badge === "📖🎧"
                    ? "Ebook + audiobook"
                    : badge === "🎧"
                    ? "Audiobook"
                    : "Ebook"
                }
                style={{ fontSize: 11, flexShrink: 0 }}
              >
                {badge}
              </span>
            )}
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
