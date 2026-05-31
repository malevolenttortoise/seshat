// Author detail page — author identity, pen-name links, series + standalone
// book sections, per-library format tabs when the author exists in more than
// one library, plus the Re-sync / Full / Scan MAM triggers.
//
// Cross-library support: if the URL arg is `slug:id`, the `slug` scopes the
// primary fetch and `?include_cross_library=1` asks the backend for
// same-normalized-name matches in every other library. The UI then renders
// tabs (Combined / Ebook / Audiobook) that show each library's copy.
import { useCallback, useEffect, useState } from "react";
import { useTheme } from "../theme";
import type { Theme } from "../theme";
import { api, slugQuery } from "../api";
import { Ic } from "../icons";
import { usePersist } from "../hooks/usePersist";
import { Btn } from "../components/Btn";
import { ClearMenu } from "../components/ClearMenu";
import { Spin } from "../components/Spin";
import { Load } from "../components/Load";
import { PB } from "../components/PB";
import { VT, type ViewMode } from "../components/VT";
import { Section } from "../components/Section";
import { BGrid, BList } from "../components/BookViews";
import { BookSidebar } from "../components/BookSidebar";
import { toast } from "../lib/toast";
import {
  loadAuthorDetailViaPerson,
  type AuthorDetail,
  type PersonHit,
  type PersonSearchResponse,
} from "../lib/authorDetail";
import { SourceBadgeRow } from "../components/SourceBadgeRow";
import { AuthorCacheStatusBadge } from "../components/AuthorCacheStatusBadge";
import { GoodreadsAuthorCacheStatusBadge } from "../components/GoodreadsAuthorCacheStatusBadge";
import { useViewport } from "../hooks/useViewport";
import { useMobileCodepath } from "../components/mobile";
import MobileAuthorDetailPage from "./MobileAuthorDetailPage";
import type {
  Author,
  AuthorsResponse,
  Book,
  BookAction,
  BookActionHandler,
  MamStatusResponse,
  NavFn,
  PenNameLink,
  PenNamesResponse,
  ScanStatusResponse,
  Series,
} from "../types";

// Response envelope for the scan / lookup / full-rescan triggers —
// they all return a small payload with the background task name and
// optional total / error. Discriminant lives in the `error` field.
interface ScanStartedResponse {
  status?: string;
  author?: string;
  total?: number;
  message?: string;
  error?: string;
}

// Shared view-mode + action shape for both inline section renderers.
// Split into its own type so IS and SA both pick it up without
// re-declaring the same callback signatures twice.
interface SectionSharedProps {
  vm: ViewMode;
  onAction?: BookActionHandler;
  onBookClick?: (book: Book) => void;
  collapsed?: boolean;
  // Multi-select plumbing — when selMode is on, BCard/BListRow flip
  // to "click toggles selection" mode. The per-section quick-pick
  // ("Select all in this series") goes into the Section header's
  // `right` slot, so each section needs to know which of its books
  // are already in the parent's selection set.
  selMode?: boolean;
  sel?: Set<number>;
  onToggleSel?: (id: number) => void;
  onSelectMany?: (ids: number[]) => void;
  onDeselectMany?: (ids: number[]) => void;
}

interface ISProps extends SectionSharedProps {
  series: Series;
  // Matches AuthorDetailPageProps.authorId — parent passes through
  // whatever the router gave it (number on the canonical nav path,
  // string via URL params).
  authorId: number | string;
  // Library slug this series belongs to, for cross-library author
  // detail. Omitted → falls back to active library (single-lib case).
  // An ABS series id lookup against the Calibre DB returns a totally
  // different series, so we MUST scope the fetch per-library here.
  librarySlug?: string | null;
  // Bubble loaded books up to the parent so the page-level "Select
  // All" can include lazy-fetched series content. Keyed by
  // `${librarySlug}:${series.id}` to disambiguate cross-library
  // namespaces.
  onBooksLoaded?: (key: string, books: Book[]) => void;
  // v3.0.0 Phase 7 — nav for the guest "N of M" pill + clickable byline names.
  onNav: NavFn;
}

// ─── Inline Series (for Author Detail) ─────────────────────
// NOTE: defined at module level (NOT inside AuthorDetailPage) to
// preserve component identity across re-renders. Inlining inside the
// parent would remount inputs on every keystroke (the focus-loss bug).
function IS({
  series,
  vm,
  onAction,
  onBookClick,
  collapsed,
  authorId,
  librarySlug,
  selMode,
  sel,
  onToggleSel,
  onSelectMany,
  onDeselectMany,
  onBooksLoaded,
  onNav,
}: ISProps) {
  const t = useTheme();
  const [ld, setLd] = useState(false);
  const [bks, setBks] = useState<Book[] | null>(null);
  const lkey = `${librarySlug || "active"}:${series.id}`;

  // v3.0.0 Phase 7 (ADR-0011) — owner-vs-incidental display.
  // `is_owner` (computed on read by the author-detail endpoint) is true
  // when this author is in every book of the series → show the FULL
  // series, no badge. When false the author is an incidental guest →
  // show only their own entries + an "N of M" pill linking to the full
  // series. `is_owner` is undefined on stale payloads → default to owner
  // (the pre-Phase-7 full-series behavior).
  const isOwner = series.is_owner ?? true;
  // A co-authored (multi_author) series the author co-owns still shows
  // the byline so the co-author team is visible on each card; a plain
  // per-author owned series suppresses it (redundant — every book is
  // this author). `multi_author` is the legacy flag fallback.
  const isCoauthored =
    series.author_mode === "multi_author" || !!series.multi_author;
  const showByline = !isOwner || isCoauthored;
  const onAuthorClick = (aid: number) => onNav("disc-author-detail", aid);

  const load = () => {
    if (bks) return;
    setLd(true);
    const qs = librarySlug ? `?slug=${encodeURIComponent(librarySlug)}` : "";
    // Owner sees the whole series; an incidental guest sees only their
    // own entries — fetched via the now-contributor-aware
    // /discovery/books?author_id=&series_id= filter (Phase 7 flip), so a
    // co-author's entries surface without dragging in the rest of the
    // series they didn't write.
    const url = isOwner
      ? `/discovery/series/${series.id}${qs}`
      : `/discovery/books?author_id=${authorId}&series_id=${series.id}` +
        (librarySlug ? `&slug=${encodeURIComponent(librarySlug)}` : "");
    api
      .get<{ books?: Book[] }>(url)
      .then((d) => {
        const books = d.books || [];
        setBks(books);
        setLd(false);
        if (onBooksLoaded) onBooksLoaded(lkey, books);
      })
      .catch(() => setLd(false));
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Series-detail nav (carry the library slug so it resolves the right
  // per-library series).
  const seriesNav = (e: React.MouseEvent) => {
    e.stopPropagation();
    onNav("disc-series-detail", librarySlug ? `${librarySlug}:${series.id}` : series.id);
  };
  // Two header affordances, both linking to the Series detail page:
  //  - incidental guest (!isOwner): an "N of M →" count pill.
  //  - co-author who co-OWNS the series (isOwner && isCoauthored, e.g.
  //    Anspach on a Chaney+Anspach series): a "Co-authored →" badge so the
  //    co-authorship is visible + the full series is reachable. Without
  //    this, an owner of a multi_author series saw no indicator at all.
  //  - sole-author owned series: nothing (redundant).
  const pill = !isOwner ? (
    <button
      onClick={seriesNav}
      title="Part of a larger series — view the full series"
      style={{
        fontSize: 11, fontWeight: 600, color: t.cyant,
        background: t.cyan + "22", border: `1px solid ${t.cyan}44`,
        padding: "2px 8px", borderRadius: 99, marginLeft: 8,
        cursor: "pointer", textTransform: "none",
      }}
    >
      {series.author_book_count ?? 0} of {series.book_count ?? 0} →
    </button>
  ) : isCoauthored ? (
    <button
      onClick={seriesNav}
      title="Co-authored series — view all contributing authors"
      style={{
        fontSize: 11, fontWeight: 600, color: t.purt,
        background: t.pur + "22", border: `1px solid ${t.pur}44`,
        padding: "2px 8px", borderRadius: 99, marginLeft: 8,
        cursor: "pointer", textTransform: "none",
      }}
    >
      Co-authored →
    </button>
  ) : null;
  const header = (
    <span>
      {series.name}
      {pill}
    </span>
  );

  // Separate regular books from omnibus entries for display.
  const regular = bks ? bks.filter((b) => !b.is_omnibus) : null;
  const omnibus = bks ? bks.filter((b) => b.is_omnibus) : null;
  const regCount = regular ? regular.length : series.book_count || 0;
  const ownCount = regular
    ? regular.filter((b) => b.owned === 1).length
    : series.owned_count || 0;
  // Omnibus-only series (user owns/sees only a collection, no
  // individual entries by this author) — show "Omnibus" instead of
  // a misleading "0/0". Falls back to the count once the lazy fetch
  // resolves, so we use either the live `regular`/`omnibus` arrays
  // or the server-provided counts before books load.
  const omnibusOnly =
    regCount === 0 &&
    (omnibus ? omnibus.length > 0 : (series.author_omnibus_count || 0) > 0);
  const countStr = omnibusOnly
    ? "Omnibus"
    : !isOwner
      ? `${ownCount}/${regCount} · ${series.book_count || 0} total`
      : `${ownCount}/${regCount}`;

  // Per-section quick-pick. When some-but-not-all books in this
  // series are selected we treat the toggle as "select all"; clicking
  // again deselects the whole set. Computed from the loaded books, so
  // the button is only useful once the fetch resolves.
  const allBookIds = bks ? bks.map((b) => b.id) : [];
  const selectedHere = sel
    ? allBookIds.filter((id) => sel.has(id)).length
    : 0;
  const allSelected =
    allBookIds.length > 0 && selectedHere === allBookIds.length;
  const quickPick =
    selMode && bks ? (
      <button
        onClick={() => {
          if (allSelected) onDeselectMany && onDeselectMany(allBookIds);
          else onSelectMany && onSelectMany(allBookIds);
        }}
        style={{
          fontSize: 11,
          fontWeight: 600,
          padding: "3px 8px",
          borderRadius: 5,
          background: allSelected ? t.accent + "22" : "transparent",
          color: allSelected ? t.accent : t.td,
          border: `1px solid ${allSelected ? t.accent + "66" : t.border}`,
          cursor: "pointer",
        }}
      >
        {allSelected
          ? "Deselect series"
          : selectedHere > 0
            ? `Select all (${selectedHere}/${allBookIds.length})`
            : "Select series"}
      </button>
    ) : null;

  return (
    <Section
      title={header}
      count={countStr}
      defaultOpen={!collapsed}
      right={quickPick}
    >
      {ld ? (
        <Load />
      ) : bks ? (
        <>
          {vm === "list" ? (
            <BList
              books={regular || []}
              onAction={onAction}
              onBookClick={onBookClick}
              showAuthor={showByline}
              highlightAuthorId={authorId}
              onAuthorClick={onAuthorClick}
              selMode={selMode}
              sel={sel}
              onToggleSel={onToggleSel}
            />
          ) : (
            <BGrid
              books={regular || []}
              onAction={onAction}
              onBookClick={onBookClick}
              showAuthor={showByline}
              highlightAuthorId={authorId}
              onAuthorClick={onAuthorClick}
              selMode={selMode}
              sel={sel}
              onToggleSel={onToggleSel}
            />
          )}
          {omnibus && omnibus.length > 0 ? (
            <>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  margin: "12px 0 8px",
                }}
              >
                <div style={{ flex: 1, height: 1, background: t.borderL }} />
                <span
                  style={{
                    fontSize: 10,
                    fontWeight: 600,
                    color: t.tg,
                    textTransform: "uppercase",
                    letterSpacing: "0.06em",
                    flexShrink: 0,
                  }}
                >
                  Omnibus / Collections
                </span>
                <div style={{ flex: 1, height: 1, background: t.borderL }} />
              </div>
              {vm === "list" ? (
                <BList
                  books={omnibus}
                  onAction={onAction}
                  onBookClick={onBookClick}
                  showAuthor={showByline}
                  highlightAuthorId={authorId}
                  onAuthorClick={onAuthorClick}
                  selMode={selMode}
                  sel={sel}
                  onToggleSel={onToggleSel}
                />
              ) : (
                <BGrid
                  books={omnibus}
                  onAction={onAction}
                  onBookClick={onBookClick}
                  showAuthor={showByline}
                  highlightAuthorId={authorId}
                  onAuthorClick={onAuthorClick}
                  selMode={selMode}
                  sel={sel}
                  onToggleSel={onToggleSel}
                />
              )}
            </>
          ) : null}
        </>
      ) : null}
    </Section>
  );
}

// ─── Standalone Section ─────────────────────────────────────
interface SAProps extends SectionSharedProps {
  books: Book[];
  // v3.0.0 Phase 7 — nav for clickable co-author byline names.
  onNav: NavFn;
}

function SA({
  books,
  vm,
  onAction,
  onBookClick,
  collapsed,
  selMode,
  sel,
  onToggleSel,
  onSelectMany,
  onDeselectMany,
  onNav,
}: SAProps) {
  const t = useTheme();
  const onAuthorClick = (aid: number) => onNav("disc-author-detail", aid);
  const ids = books.map((b) => b.id);
  const selectedHere = sel ? ids.filter((id) => sel.has(id)).length : 0;
  const allSelected = ids.length > 0 && selectedHere === ids.length;
  const quickPick = selMode ? (
    <button
      onClick={() => {
        if (allSelected) onDeselectMany && onDeselectMany(ids);
        else onSelectMany && onSelectMany(ids);
      }}
      style={{
        fontSize: 11,
        fontWeight: 600,
        padding: "3px 8px",
        borderRadius: 5,
        background: allSelected ? t.accent + "22" : "transparent",
        color: allSelected ? t.accent : t.td,
        border: `1px solid ${allSelected ? t.accent + "66" : t.border}`,
        cursor: "pointer",
      }}
    >
      {allSelected
        ? "Deselect standalone"
        : selectedHere > 0
          ? `Select all (${selectedHere}/${ids.length})`
          : "Select standalone"}
    </button>
  ) : null;
  return (
    <Section
      title="Standalone"
      count={books.length}
      defaultOpen={!collapsed}
      right={quickPick}
    >
      {vm === "list" ? (
        <BList
          books={books}
          onAction={onAction}
          onBookClick={onBookClick}
          onAuthorClick={onAuthorClick}
          selMode={selMode}
          sel={sel}
          onToggleSel={onToggleSel}
        />
      ) : (
        <BGrid
          books={books}
          onAction={onAction}
          onBookClick={onBookClick}
          onAuthorClick={onAuthorClick}
          selMode={selMode}
          sel={sel}
          onToggleSel={onToggleSel}
        />
      )}
    </Section>
  );
}

// ─── Author Detail ──────────────────────────────────────────
interface AuthorDetailPageProps {
  authorId: number | string;
  onNav: NavFn;
}

// Rendering primitive for the per-library block in the tabbed layout.
// Each block owns one copy of the author (active library OR a
// cross-library match) and labels itself by content_type.
interface LibraryBlock {
  slug: string;
  label: string;
  content_type: string;
  data: AuthorDetail;
}

export default function AuthorDetailPage(props: AuthorDetailPageProps) {
  // Mobile codepath catches phones, iPads, and any touch device.
  const vp = useViewport();
  if (useMobileCodepath(vp)) {
    return <MobileAuthorDetailPage {...props} />;
  }
  return <DesktopAuthorDetailPage {...props} />;
}

function DesktopAuthorDetailPage({
  authorId,
  onNav,
}: AuthorDetailPageProps) {
  const t = useTheme();
  const [a, setA] = useState<AuthorDetail | null>(null);
  const [ld, setLd] = useState(true);
  const [ref, setRef] = useState(false);
  const [mamRef, setMamRef] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [vm, setVm] = usePersist<ViewMode>("adp_vm", "grid");
  const [rk, setRk] = useState(0);
  const [sb, setSb] = useState<Book | null>(null);
  const [sbClosing, setSbClosing] = useState(false);
  const [allCol, setAllCol] = useState(false);
  const [mamOn, setMamOn] = useState(false);
  const [fmtTab, setFmtTab] = useState<string>("combined");

  // Multi-select state. `selMode` flips the cards into checkbox-like
  // toggles; `sel` is the set of selected book IDs spanning every
  // visible series + standalone (and every cross-library tab — IDs
  // are page-wide). `seriesBooks` is a cache of books loaded by the
  // lazy IS sections so the page-level Select All can include them.
  const [selMode, setSelMode] = useState(false);
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [seriesBooks, setSeriesBooks] = useState<Record<string, Book[]>>({});

  const toggleSel = useCallback((id: number) => {
    setSel((p) => {
      const n = new Set(p);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }, []);
  const selectMany = useCallback((ids: number[]) => {
    setSel((p) => {
      const n = new Set(p);
      ids.forEach((i) => n.add(i));
      return n;
    });
  }, []);
  const deselectMany = useCallback((ids: number[]) => {
    setSel((p) => {
      const n = new Set(p);
      ids.forEach((i) => n.delete(i));
      return n;
    });
  }, []);
  const onBooksLoaded = useCallback((key: string, books: Book[]) => {
    setSeriesBooks((p) => ({ ...p, [key]: books }));
  }, []);

  // Nav arg may arrive as "slug:id" when the click came from a cross-
  // library merged row — the id alone is ambiguous because ABS's
  // author 5 and Calibre's author 5 are different people. Split here
  // so the detail fetch + pen-name links + scan triggers all use the
  // right per-library IDs.
  const parsed = (() => {
    const s = String(authorId);
    if (s.includes(":")) {
      const [slug, id] = s.split(":");
      return { slug, id: parseInt(id) || 0 };
    }
    return {
      slug: null as string | null,
      id: parseInt(s) || (typeof authorId === "number" ? authorId : 0),
    };
  })();
  const authorIdNum = parsed.id;
  const authorSlug = parsed.slug;

  const [penLinks, setPenLinks] = useState<PenNameLink[]>([]);
  const [penQ, setPenQ] = useState("");
  // v2.20.0 Phase 4 — search returns person hits (deduped across
  // libraries) instead of per-library author rows.
  const [penResults, setPenResults] = useState<PersonHit[]>([]);
  const [penBusy, setPenBusy] = useState(false);
  const [penType, setPenType] = usePersist<string>("adp_pen_type", "pen_name");

  useEffect(() => {
    if (!authorIdNum) return;
    api
      .get<PenNamesResponse>(`/discovery/authors/${authorIdNum}/pen-names`)
      .then((r) => setPenLinks(r.links || []))
      .catch(() => {});
  }, [authorIdNum]);

  useEffect(() => {
    if (penQ.length < 2) {
      setPenResults([]);
      return;
    }
    const tm = setTimeout(() => {
      api
        .get<PersonSearchResponse>(
          `/discovery/persons/search?q=${encodeURIComponent(penQ)}`,
        )
        .then((r) =>
          setPenResults(
            (r.persons || []).filter((x) => x.person_id !== a?.person_id),
          ),
        )
        .catch(() => {});
    }, 300);
    return () => clearTimeout(tm);
  }, [penQ, a?.person_id]);

  const linkPen = async (aliasPersonId: number) => {
    if (!a?.person_id) {
      toast.error("Author not yet linked to a canonical person");
      return;
    }
    setPenBusy(true);
    try {
      await api.post("/discovery/persons/link-pen-names", {
        canonical_person_id: a.person_id,
        alias_person_id: aliasPersonId,
        link_type: penType,
      });
      // Refresh both the legacy per-library chip list AND the unified
      // person view (which reads pen_names from v2). Reloading the
      // page-level state pulls in the fresh person.pen_names too.
      const r = await api.get<PenNamesResponse>(
        `/discovery/authors/${authorIdNum}/pen-names`,
      );
      setPenLinks(r.links || []);
      await loadA();
      setPenQ("");
      setPenResults([]);
      toast.success(
        penType === "co_author" ? "Co-author linked" : "Pen name linked",
      );
    } catch (e) {
      toast.error((e as Error).message || "Link failed");
    }
    setPenBusy(false);
  };

  const unlinkPen = async (linkId: number) => {
    try {
      await api.del(`/discovery/authors/pen-name-link/${linkId}`);
      setPenLinks(penLinks.filter((l) => l.id !== linkId));
      toast.success("Author unlinked");
    } catch {
      /* ignore — user sees no change */
    }
  };

  useEffect(() => {
    api
      .get<MamStatusResponse>("/discovery/mam/status")
      .then((r) => setMamOn(!!r.enabled))
      .catch(() => {});
  }, []);

  const closeSb = () => {
    if (!sb) return;
    setSbClosing(true);
    setTimeout(() => {
      setSb(null);
      setSbClosing(false);
    }, 200);
  };
  const toggleSb = (b: Book) => {
    if (sb && sb.id === b.id) closeSb();
    else {
      setSbClosing(false);
      setSb(b);
    }
  };

  const loadA = useCallback(
    (signal?: AbortSignal) => {
      setLd(true);
      // v2.20.0 — `loadAuthorDetailViaPerson` resolves the author's
      // canonical person_id and fetches /discovery/persons/{person_id}
      // for the unified cross-library view, adapting the response to
      // the existing AuthorDetail shape. Falls back to the legacy
      // /authors/{aid} response when the row isn't yet linked.
      return loadAuthorDetailViaPerson(authorIdNum, authorSlug, signal)
        .then((d) => {
          setA(d);
          setLd(false);
        })
        .catch((e) => {
          if (!api.isAbort(e)) console.error(e);
        });
    },
    [authorIdNum, authorSlug],
  );

  useEffect(() => {
    const c = new AbortController();
    loadA(c.signal);
    return () => c.abort();
  }, [loadA]);

  // Author scans run as background tasks on the server. The flow:
  //   1. Dispatch `seshat:scan-started` so the Dashboard widget
  //      shows it immediately.
  //   2. Fire the POST without awaiting completion (the server
  //      returns `{status: "started"}`).
  //   3. Listen for `seshat:scan-completed` from App's unified
  //      poller and refresh the page data when it fires.
  const scanQs = authorSlug ? `?slug=${encodeURIComponent(authorSlug)}` : "";

  const refresh = async (full: boolean = false) => {
    if (ref) return;
    setRef(true);
    try {
      const r = await api.post<ScanStartedResponse>(
        `/discovery/authors/${authorIdNum}/${full ? "full-rescan" : "lookup"}${scanQs}`,
      );
      toast.info(
        `${full ? "Full re-scan" : "Source scan"} started for ${r.author || "author"}`,
      );
      window.dispatchEvent(new CustomEvent("seshat:scan-started"));
    } catch (e) {
      toast.error((e as Error).message || "Scan failed to start");
      setRef(false);
    }
  };

  const scanMam = async () => {
    if (mamRef) return;
    setMamRef(true);
    try {
      const r = await api.post<ScanStartedResponse>(
        `/discovery/mam/scan-author/${authorIdNum}${scanQs}`,
      );
      if (r.status === "complete") {
        toast.info(r.message || "No un-scanned books for this author");
        setMamRef(false);
      } else {
        toast.info(`MAM scan started — ${r.total || 0} books`);
        window.dispatchEvent(new CustomEvent("seshat:scan-started"));
      }
    } catch (e) {
      toast.error((e as Error).message || "MAM scan failed to start");
      setMamRef(false);
    }
  };

  const clearData = async (
    type: "source" | "mam" | "both",
    scope?: "ebook" | "audiobook",
  ) => {
    if (!authorIdNum) return;
    const labels = {
      source: "source scan",
      mam: "MAM scan",
      both: "all scan",
    } as const;
    const scopeLabel = scope
      ? ` (${scope === "audiobook" ? "audiobook" : "ebook"} libraries only)`
      : "";
    if (!confirm(`Clear ${labels[type]} data${scopeLabel} for this author?`))
      return;
    setClearing(true);
    try {
      // v2.12.0 — when scoping to a content_type, include author_names
      // alongside author_ids. The cross-library clear path resolves
      // each target library's local author rows by NAME (the
      // author_id sent here is scoped to whichever library the page
      // was loaded from; using it as-is in another library finds 0
      // rows). Without names the audiobook-scope clear would silently
      // touch nothing. Mirrors the scan-sources cross-library
      // collision-avoidance pattern from earlier.
      await api.post("/discovery/authors/clear-scan-data", {
        author_ids: [authorIdNum],
        ...(a?.name && scope ? { author_names: [a.name] } : {}),
        clear_source: type === "source" || type === "both",
        clear_mam: type === "mam" || type === "both",
        ...(scope ? { content_type: scope } : {}),
      });
      toast.success("Cleared data");
      loadA();
      setRk((k) => k + 1);
    } catch (e) {
      toast.error((e as Error).message || "Error");
    } finally {
      setClearing(false);
    }
  };

  // v2.12.0 — paired cross-library scan triggers for the author
  // detail page. Each fans across every library of the named
  // content_type by NAME (so audiobook-origin authors can be
  // re-discovered in every ebook library and vice-versa). The
  // backend resolves the local author row per library; empty
  // matches return total=0 with a polite message.
  const _crossLibraryAuthorScan = async (scope: "ebook" | "audiobook") => {
    if (!authorIdNum || ref) return;
    const label = scope === "audiobook" ? "audiobook" : "ebook";
    if (!confirm(`Scan this author across every ${label} library?`)) return;
    setRef(true);
    try {
      const r = await api.post<{status?: string; total?: number; message?: string}>(
        "/discovery/authors/scan-sources",
        {
          author_ids: [authorIdNum],
          ...(a?.name ? { author_names: [a.name] } : {}),
          content_type: scope,
        },
      );
      if ((r.total ?? 0) > 0) {
        // v2.12.1 #3 — plain English. Single-library is the common
        // case; pluralize for >1.
        const libCount = r.total;
        const libWord = libCount === 1 ? "library" : "libraries";
        const authorPart = a?.name ? ` for '${a.name}'` : "";
        toast.info(
          `Scanning ${label} sources${authorPart} across ${libCount} ${libWord}.`,
        );
        window.dispatchEvent(new CustomEvent("seshat:scan-started"));
      } else {
        // v2.12.1 #3 — backend's `message` already names the author
        // + suggests a remedy. Frontend just relays it.
        toast.warn(r.message || `No ${label}-library match for this author.`);
        setRef(false);
      }
    } catch (e) {
      toast.error((e as Error).message || "Scan failed to start");
      setRef(false);
    }
  };
  const scanEbookSources = () => _crossLibraryAuthorScan("ebook");
  const scanAudiobookSources = () => _crossLibraryAuthorScan("audiobook");

  // v2.14.0 — page-local scan-completion poll. UAT 2026-05-14 surfaced
  // that after triggering an audiobook scan, this page didn't refresh
  // when the scan finished: spinners stayed up and newly-merged books
  // didn't appear until manual reload. Root cause: the prior
  // implementation listened for `seshat:scan-completed`, but that
  // event is never dispatched anywhere in the frontend — the
  // app-wide unified poller it referenced only runs while the user
  // is on the Dashboard.
  //
  // Fix: poll `/discovery/scan-status` directly while on this page
  // (3s cadence, mirrors `DiscBooksPage`'s MAM-scan poller). On a
  // running→idle transition for `lookup` or `mam`, clear the
  // corresponding local spinner state, call `loadA()` to refresh the
  // author + book data, and bump `rk` so child series components
  // re-mount with fresh keys.
  useEffect(() => {
    let active = true;
    let prevLookup = false;
    let prevMam = false;
    const tick = async () => {
      try {
        const r = await api.get<ScanStatusResponse>("/discovery/scan-status");
        if (!active) return;
        const scans = r.scans || [];
        const lookupRunning = scans.some((s) => s.kind === "lookup" && s.running);
        const mamRunning = scans.some((s) => s.kind === "mam" && s.running);
        const lookupDone = prevLookup && !lookupRunning;
        const mamDone = prevMam && !mamRunning;
        if (lookupDone || mamDone) {
          if (lookupDone) setRef(false);
          if (mamDone) setMamRef(false);
          loadA();
          setRk((k) => k + 1);
        }
        prevLookup = lookupRunning;
        prevMam = mamRunning;
      } catch {
        /* ignore — scan-status is non-critical */
      }
    };
    tick();
    const id = window.setInterval(tick, 3000);
    return () => {
      active = false;
      window.clearInterval(id);
    };
  }, [loadA]);

  const onAction = async (act: BookAction, id: number, slug?: string) => {
    const scrollY = window.scrollY;
    if (act === "hide") await api.post(`/discovery/books/${id}/hide${slugQuery(slug)}`);
    if (act === "dismiss") await api.post(`/discovery/books/${id}/dismiss${slugQuery(slug)}`);
    if (act === "delete") await api.del(`/discovery/books/${id}${slugQuery(slug)}`);
    await loadA();
    setTimeout(() => window.scrollTo(0, scrollY), 100);
  };

  // Page-wide selectable IDs = standalone books on every visible
  // library block + every series whose IS section has loaded its
  // books. Series that haven't been mounted yet (e.g. a tab the user
  // hasn't switched to) won't appear; that's intentional — we only
  // select what's on screen.
  const allVisibleIds = (): number[] => {
    const ids = new Set<number>();
    if (a) {
      (a.standalone_books || []).forEach((b) => ids.add(b.id));
      Object.values(a.cross_library || {}).forEach((c) => {
        (c.author.standalone_books || []).forEach((b) => ids.add(b.id));
      });
    }
    Object.values(seriesBooks).forEach((arr) =>
      arr.forEach((b) => ids.add(b.id)),
    );
    return [...ids];
  };

  // v2.12.1 #1 — per-library partition for cross-library bulk
  // operations. Pre-v2.12.1 the bulk-{op} request sent ALL selected
  // book_ids to a single endpoint with `slug=a.active_library_slug`.
  // When the user selected books from a cross-library tab in the
  // Combined view, their IDs were scoped to a different library, and
  // the request hit the wrong DB — finding arbitrary unrelated books
  // by id collision (UAT 2026-05-14: "The Far Reaches" id=288 in
  // ABS lib collided with "God Hammer" id=288 in Calibre lib; the
  // delete request skipped "God Hammer" as Calibre-synced while
  // "The Far Reaches" was never touched).
  //
  // Build a bookId → libSlug map from the data we have on hand, then
  // partition `sel` by slug and fire one bulk-{op} request per
  // library. Aggregates responses into a single user-facing toast.
  // Works for all four bulk endpoints (delete / hide / dismiss /
  // skip-mam) — they share the same {slug, book_ids} contract.
  const buildBookSlugMap = (): Map<number, string> => {
    const out = new Map<number, string>();
    const activeSlug = a?.active_library_slug || "active";
    // Active library: standalone books on the AuthorDetail root.
    (a?.standalone_books || []).forEach((b) => out.set(b.id, activeSlug));
    // Cross-library entries: each carries its own slug as the map key.
    Object.entries(a?.cross_library || {}).forEach(([slug, entry]) => {
      (entry.author.standalone_books || []).forEach((b) => out.set(b.id, slug));
    });
    // Lazy-loaded series books: `seriesBooks` keys are
    // "{librarySlug || 'active'}:{series.id}" so we can recover the
    // owning slug from the key. Books not yet covered above (e.g.
    // series in a cross-lib tab the user hasn't expanded? — defensive)
    // get the prefix's slug.
    Object.entries(seriesBooks).forEach(([key, books]) => {
      const prefix = key.split(":", 1)[0];
      const slug = prefix === "active" ? activeSlug : prefix;
      books.forEach((b) => {
        if (!out.has(b.id)) out.set(b.id, slug);
      });
    });
    return out;
  };

  // Map slug → human-readable upstream-app label for the "skipped N
  // X-synced" toast. Calibre + Audiobookshelf is the full known set
  // today; revisit when a third app type lands.
  const slugToSyncedLabel = (slug: string): string => {
    if (slug === a?.active_library_slug) {
      return a?.active_content_type === "audiobook"
        ? "Audiobookshelf-synced"
        : "Calibre-synced";
    }
    const entry = a?.cross_library?.[slug];
    if (entry?.content_type === "audiobook") return "Audiobookshelf-synced";
    return "Calibre-synced";
  };

  const bulkAct = async (kind: "hide" | "dismiss" | "delete" | "skip-mam") => {
    const ids = [...sel];
    if (ids.length === 0) return;
    const labels = {
      hide: "Hide", dismiss: "Dismiss", delete: "Delete",
      "skip-mam": "Skip MAM",
    } as const;
    // Past-tense forms for the success toast — pre-v2.3.4.3 used
    // `${labels[kind]}d` which produced "Hided" / "Dismissd"
    // (grammatically wrong on both). Skip MAM uses a different
    // form ("Marked Not Applicable") since it doesn't tense well.
    const pastLabels = {
      hide: "Hidden", dismiss: "Dismissed", delete: "Deleted",
      "skip-mam": "Marked N/A",
    } as const;

    // v2.12.1 #1 — partition selection by library slug. For delete,
    // the confirmation copy needs to list all upstream-app labels
    // that COULD be skipped (so the user knows the protection
    // semantics even on cross-library deletes). Build the per-slug
    // book lists up front so the confirmation message + the bulk
    // dispatch both consume the same partition.
    const slugMap = buildBookSlugMap();
    const partition = new Map<string, number[]>();
    for (const id of ids) {
      const slug = slugMap.get(id) || a?.active_library_slug || "active";
      const arr = partition.get(slug) || [];
      arr.push(id);
      partition.set(slug, arr);
    }
    const slugs = [...partition.keys()];
    const syncedLabelsInvolved = [
      ...new Set(slugs.map(slugToSyncedLabel)),
    ];
    const syncedLabelText = syncedLabelsInvolved.length === 1
      ? syncedLabelsInvolved[0]
      : syncedLabelsInvolved.join(" / ");

    const msg =
      kind === "delete"
        ? `Delete ${ids.length} book(s)? ${syncedLabelText} books will be skipped.`
        : kind === "skip-mam"
        ? `Mark ${ids.length} book(s) as Not Applicable for MAM scanning?`
        : `${labels[kind]} ${ids.length} book(s)?`;
    if (!confirm(msg)) return;
    setBusy(true);
    try {
      // Fire one bulk-{kind} request per library slug; gather results
      // into per-slug map so the aggregate toast can label the skip
      // count with the right upstream-app name (Calibre / ABS).
      // Type the response uniformly so the `.catch` branch carries
      // the same shape as the success branch (all fields optional);
      // otherwise TS narrows the union and the aggregate step can't
      // access r.deleted / r.skipped without disjoint discrimination.
      type BulkResp = {
        status?: string;
        count?: number;
        deleted?: number;
        skipped?: number;
        error?: string;
      };
      const results = await Promise.all(
        slugs.map((slug): Promise<{ slug: string; r: BulkResp }> => {
          const slugIds = partition.get(slug)!;
          return api.post<BulkResp>(
            `/discovery/books/bulk-${kind}${slugQuery(slug)}`,
            { book_ids: slugIds },
          ).then((r) => ({ slug, r }))
            .catch((e): { slug: string; r: BulkResp } => ({
              slug, r: { error: (e as Error).message || "failed" },
            }));
        }),
      );

      // Aggregate. First surface any errors; otherwise build a
      // success toast with per-slug skip breakdown.
      const errors = results.filter((x) => x.r.error);
      if (errors.length > 0 && errors.length === results.length) {
        toast.error(errors[0].r.error || "Bulk action failed");
      } else {
        if (errors.length > 0) {
          toast.warn(
            `Partial failure: ${errors.length} of ${results.length} ${
              errors.length === 1 ? "library" : "libraries"
            } errored. ${errors[0].r.error || ""}`,
          );
        }
        if (kind === "delete") {
          const totalDeleted = results.reduce(
            (acc, x) => acc + (x.r.deleted || 0), 0,
          );
          // Per-slug skip breakdown, labeled by upstream app type.
          const skipParts = results
            .filter((x) => (x.r.skipped || 0) > 0)
            .map((x) => `${x.r.skipped} ${slugToSyncedLabel(x.slug)}`);
          const skipMsg = skipParts.length > 0
            ? `, skipped ${skipParts.join(", ")}`
            : "";
          toast.success(`Deleted ${totalDeleted} book(s)${skipMsg}`);
        } else {
          const totalCount = results.reduce(
            (acc, x) => acc + (x.r.count ?? 0), 0,
          );
          toast.success(`${pastLabels[kind]} ${totalCount || ids.length} book(s)`);
        }
      }
      setSel(new Set());
      setSelMode(false);
      // Invalidate the lazy series cache so series sections re-fetch
      // (deleted books should disappear; hidden/dismissed books stay
      // in the list but with their flags cleared by the refresh).
      setSeriesBooks({});
      await loadA();
      setRk((k) => k + 1);
    } catch (e) {
      toast.error((e as Error).message || `${labels[kind]} failed`);
    }
    setBusy(false);
  };

  if (ld) return <Load />;
  if (!a) return <div style={{ color: t.tf }}>Not found</div>;

  // v2.17.0 Bug B — when the backend ran the cross_library fan-out
  // and computed `global_stats`, the top-of-page tile uses those
  // (summed across every library this author has rows in). Without
  // it, an audiobook-only author with fresh ebook discoveries on
  // the Calibre side renders "1 owned, 0 missing" — the per-library
  // counts that mislead users (UAT 2026-05-18). Fall back to the
  // per-library computed values when global_stats isn't on the
  // response (single-library installs, older cached responses).
  const saOwned = (a.standalone_books || []).filter(
    (b) => b.owned === 1,
  ).length;
  const saTotal = (a.standalone_books || []).length;
  const serOwned = (a.series || []).reduce(
    (n: number, s: Series) => n + (s.owned_count || 0),
    0,
  );
  const serTotal = (a.series || []).reduce(
    (n: number, s: Series) => n + (s.author_book_count ?? s.book_count ?? 0),
    0,
  );
  const gs = a.global_stats;
  const oc = gs ? gs.owned : saOwned + serOwned;
  const total = gs ? gs.total : saTotal + serTotal;
  const globalSeriesCount = gs ? gs.series_count : (a.series || []).length;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
      {/* Sticky author header */}
      <div
        style={{
          position: "sticky",
          top: 56,
          zIndex: 40,
          background: t.bg + "ee",
          backdropFilter: "blur(8px)",
          padding: "12px 0",
        }}
      >
        <Btn
          onClick={() => onNav("disc-authors")}
          style={{
            marginBottom: 12,
            background: t.bg4,
            border: `1px solid ${t.border}`,
            borderRadius: 8,
            padding: "8px 16px",
            fontSize: 14,
          }}
        >
          ← Back to Authors
        </Btn>
        <div
          className="author-header"
          style={{ display: "flex", gap: 20, alignItems: "flex-start" }}
        >
          {a.image_url ? (
            <img
              src={a.image_url}
              alt=""
              style={{
                width: 72,
                height: 72,
                borderRadius: "50%",
                objectFit: "cover",
              }}
            />
          ) : (
            <div
              style={{
                width: 72,
                height: 72,
                borderRadius: "50%",
                background: t.bg4,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 28,
                fontWeight: 700,
                color: t.tg,
              }}
            >
              {a.name.charAt(0)}
            </div>
          )}
          <div style={{ flex: 1 }}>
            <h1
              style={{ fontSize: 22, fontWeight: 700, color: t.text, margin: 0 }}
            >
              {a.name}
            </h1>
            {a.bio ? (
              <p
                style={{
                  fontSize: 13,
                  color: t.td,
                  marginTop: 6,
                  lineHeight: 1.5,
                  maxHeight: 60,
                  overflow: "hidden",
                }}
              >
                {a.bio}
              </p>
            ) : null}
            <div
              style={{ display: "flex", gap: 16, marginTop: 8, fontSize: 13 }}
            >
              <span style={{ color: t.grnt }}>{oc} owned</span>
              <span style={{ color: t.ylwt }}>{total - oc} missing</span>
              <span style={{ color: t.purt }}>
                {globalSeriesCount} series
              </span>
            </div>
            {/* Author-link chips inline with identity. The
                relationship label ("aka" for pen_name, "with" for
                co_author) is purely UX — backend dedup behavior is
                identical for both link types. */}
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                marginTop: 6,
                flexWrap: "wrap",
              }}
            >
              {penLinks.map((l) => {
                const other =
                  l.canonical_author_id === authorIdNum
                    ? { id: l.alias_author_id, name: l.alias_name }
                    : { id: l.canonical_author_id, name: l.canonical_name };
                const isCo = l.link_type === "co_author";
                const tone = isCo
                  ? {
                      bg: t.cyan ? t.cyan + "22" : t.bg4,
                      fg: t.cyant || t.text2,
                      br: (t.cyan || t.tf) + "33",
                      label: "with",
                    }
                  : {
                      bg: t.purb,
                      fg: t.purt,
                      br: t.pur + "33",
                      label: "aka",
                    };
                return (
                  <span
                    key={l.id}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 4,
                      padding: "2px 8px",
                      borderRadius: 4,
                      fontSize: 11,
                      background: tone.bg,
                      color: tone.fg,
                      border: `1px solid ${tone.br}`,
                    }}
                  >
                    <span style={{ color: t.tg, fontSize: 10 }}>{tone.label}</span>{" "}
                    <button
                      onClick={() => onNav("disc-author-detail", other.id)}
                      style={{
                        background: "none",
                        border: "none",
                        color: tone.fg,
                        cursor: "pointer",
                        padding: 0,
                        fontSize: 11,
                        fontWeight: 500,
                      }}
                    >
                      {other.name}
                    </button>
                    <button
                      onClick={() => unlinkPen(l.id)}
                      style={{
                        background: "none",
                        border: "none",
                        color: t.tg,
                        cursor: "pointer",
                        padding: 0,
                        fontSize: 12,
                      }}
                    >
                      ×
                    </button>
                  </span>
                );
              })}
              <button
                onClick={() => setPenQ(penQ || " ")}
                style={{
                  background: "none",
                  color: t.td,
                  cursor: "pointer",
                  padding: "4px 10px",
                  fontSize: 12,
                  borderRadius: 5,
                  border: `1.5px dashed ${t.tf}`,
                }}
              >
                + link author
              </button>
            </div>
            <div style={{ marginTop: 8 }}>
              <PB owned={oc} total={total} />
            </div>
          </div>
          <div
            className="author-controls"
            style={{
              display: "flex",
              gap: 6,
              alignItems: "center",
              flexShrink: 0,
            }}
          >
            <Btn
              size="sm"
              variant="ghost"
              onClick={() => loadA()}
              title="Refresh"
              style={{
                height: 38,
                width: 34,
                padding: 0,
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              {Ic.refresh}
            </Btn>
            <Btn
              size="sm"
              variant="ghost"
              onClick={() => setAllCol(!allCol)}
              title={allCol ? "Expand All" : "Collapse All"}
              style={{
                height: 38,
                width: 34,
                padding: 0,
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              {allCol ? Ic.expand : Ic.collapse}
            </Btn>
            <VT mode={vm} setMode={setVm} />
            <Btn
              size="sm"
              variant={selMode ? "accent" : "default"}
              onClick={() => {
                setSelMode(!selMode);
                if (selMode) setSel(new Set());
              }}
              style={{ height: 38 }}
            >
              {selMode ? "Cancel" : "Select"}
            </Btn>
            <Btn
              size="sm"
              onClick={() => refresh(false)}
              disabled={ref}
              style={{ height: 38 }}
            >
              {ref ? <Spin /> : Ic.sync} Re-sync
            </Btn>
            <Btn
              size="sm"
              onClick={() => {
                if (
                  confirm(
                    "Full Re-Scan visits every book page to refresh metadata. This may take a few minutes. Continue?",
                  )
                )
                  refresh(true);
              }}
              disabled={ref}
              style={{
                height: 38,
                background: t.ylw + "22",
                color: t.ylwt,
                border: `1px solid ${t.ylw}44`,
              }}
            >
              {ref ? <Spin /> : Ic.refresh} Full
            </Btn>
            <Btn
              size="sm"
              onClick={scanEbookSources}
              disabled={ref}
              title="Scan this author across every ebook library"
              style={{
                height: 38,
                background: t.grn + "22",
                color: t.grnt,
                border: `1px solid ${t.grn}44`,
              }}
            >
              Scan Ebooks
            </Btn>
            <Btn
              size="sm"
              onClick={scanAudiobookSources}
              disabled={ref}
              title="Scan this author across every audiobook library"
              style={{
                height: 38,
                background: t.pur + "22",
                color: t.purt,
                border: `1px solid ${t.pur}44`,
              }}
            >
              Scan Audiobooks
            </Btn>
            {mamOn ? (
              <Btn
                size="sm"
                onClick={scanMam}
                disabled={mamRef}
                title="Scan all un-scanned books for this author against MAM"
                style={{
                  height: 38,
                  background: t.cyan + "22",
                  color: t.cyant,
                  border: `1px solid ${t.cyan}44`,
                }}
              >
                {mamRef ? <Spin /> : null} Scan MAM
              </Btn>
            ) : null}
            <span
              style={{
                width: 1,
                height: 20,
                background: t.border,
                margin: "0 2px",
              }}
            />
            <ClearMenu
              disabled={clearing}
              align="right"
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
          </div>
        </div>
      </div>

      {/* v2.20.0 Phase 3 — source-ID badges. Only renders when the
          author is linked to a canonical person (person_id present);
          otherwise this row stays hidden. Clicking a badge opens the
          edit modal which writes through to every linked per-library
          row via the PATCH endpoint. */}
      {a.person_id ? (
        <SourceBadgeRow
          personId={a.person_id}
          sourceIds={a.source_ids || {}}
          onUpdate={() => loadA()}
        />
      ) : null}

      {/* v2.21.0 Phase F tier 3 + v3.6.0 frontend parity —
          per-author cache status for each metadata source. Renders
          only when the matching source ID is resolved (no ID →
          nothing to look up). Shows one line per library the author
          exists in. */}
      <AuthorCacheStatusBadge
        amazonAuthorId={(a.source_ids || {}).amazon as string | undefined}
      />
      <GoodreadsAuthorCacheStatusBadge
        goodreadsAuthorId={(a.source_ids || {}).goodreads as string | undefined}
      />


      {/* ── Author-Link Search (shown when user clicks "+ link author") ── */}
      {penQ.length > 0 ? (
        <div
          style={{
            background: t.bg2,
            border: `1px solid ${t.border}`,
            borderRadius: 10,
            padding: "10px 14px",
            display: "flex",
            alignItems: "center",
            gap: 8,
            fontSize: 12,
            flexWrap: "wrap",
          }}
        >
          <span
            style={{
              fontWeight: 600,
              color: t.tg,
              textTransform: "uppercase",
              letterSpacing: "0.05em",
              flexShrink: 0,
            }}
          >
            Link As
          </span>
          <div
            style={{
              display: "inline-flex",
              border: `1px solid ${t.border}`,
              borderRadius: 6,
              overflow: "hidden",
              flexShrink: 0,
            }}
          >
            <button
              onClick={() => setPenType("pen_name")}
              style={{
                padding: "5px 10px",
                background: penType === "pen_name" ? t.purb || t.bg4 : "transparent",
                color: penType === "pen_name" ? t.purt : t.td,
                border: "none",
                cursor: "pointer",
                fontSize: 12,
                fontWeight: penType === "pen_name" ? 600 : 400,
              }}
            >
              Pen name
            </button>
            <button
              onClick={() => setPenType("co_author")}
              style={{
                padding: "5px 10px",
                background:
                  penType === "co_author"
                    ? t.cyan
                      ? t.cyan + "22"
                      : t.bg4
                    : "transparent",
                color:
                  penType === "co_author" ? t.cyant || t.text2 : t.td,
                border: "none",
                borderLeft: `1px solid ${t.border}`,
                cursor: "pointer",
                fontSize: 12,
                fontWeight: penType === "co_author" ? 600 : 400,
              }}
            >
              Co-author
            </button>
          </div>
          <div
            style={{
              position: "relative",
              flex: 1,
              maxWidth: 280,
              minWidth: 200,
            }}
          >
            <input
              autoFocus
              value={penQ.trim() ? penQ : ""}
              onChange={(e) => setPenQ(e.target.value)}
              placeholder={
                penType === "co_author"
                  ? "Search co-author..."
                  : "Search pen name..."
              }
              disabled={penBusy}
              style={{
                padding: "6px 10px",
                background: t.inp,
                border: `1px solid ${t.border}`,
                borderRadius: 6,
                color: t.text2,
                fontSize: 12,
                width: "100%",
              }}
            />
            {penResults.length > 0 ? (
              <div
                style={{
                  position: "absolute",
                  top: "100%",
                  left: 0,
                  right: 0,
                  background: t.bg2,
                  border: `1px solid ${t.border}`,
                  borderRadius: "0 0 6px 6px",
                  zIndex: 10,
                  boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
                  maxHeight: 160,
                  overflowY: "auto",
                }}
              >
                {penResults.map((p) => (
                  <div
                    key={p.person_id}
                    onClick={() => linkPen(p.person_id)}
                    style={{
                      padding: "8px 12px",
                      cursor: "pointer",
                      fontSize: 12,
                      color: t.text2,
                      borderBottom: `1px solid ${t.borderL}`,
                      display: "flex",
                      alignItems: "center",
                      gap: 6,
                    }}
                  >
                    <span style={{ flex: 1 }}>{p.display_name}</span>
                    {p.content_types.includes("ebook") && (
                      <span title="Ebook">📖</span>
                    )}
                    {p.content_types.includes("audiobook") && (
                      <span title="Audiobook">🎧</span>
                    )}
                    <span style={{ color: t.tg, fontSize: 10 }}>
                      {p.library_slugs.length === 1
                        ? "1 library"
                        : `${p.library_slugs.length} libraries`}
                    </span>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
          <button
            onClick={() => {
              setPenQ("");
              setPenResults([]);
            }}
            style={{
              background: "none",
              border: "none",
              color: t.tg,
              cursor: "pointer",
              fontSize: 14,
              padding: "0 4px",
            }}
          >
            ×
          </button>
        </div>
      ) : null}

      {/* ── Bulk action bar (visible only in select mode) ── */}
      {selMode ? (
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
          }}
        >
          <span style={{ fontSize: 13, fontWeight: 600, color: t.text2 }}>
            {sel.size} book{sel.size === 1 ? "" : "s"} selected
          </span>
          {sel.size > 0 ? (
            <>
              <Btn
                size="sm"
                onClick={() => bulkAct("hide")}
                disabled={busy}
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
                onClick={() => bulkAct("dismiss")}
                disabled={busy}
                style={{
                  background: t.cyan + "22",
                  color: t.cyant,
                  border: `1px solid ${t.cyan}44`,
                }}
              >
                Dismiss
              </Btn>
              <Btn
                size="sm"
                onClick={() => bulkAct("delete")}
                disabled={busy}
                style={{
                  background: t.red + "22",
                  color: t.redt,
                  border: `1px solid ${t.red}44`,
                }}
              >
                Delete
              </Btn>
              <Btn
                size="sm"
                onClick={() => bulkAct("skip-mam")}
                disabled={busy}
                title="Mark these books as Not Applicable so MAM scans skip them"
                style={{
                  background: t.bg2,
                  color: t.td,
                  border: `1px solid ${t.borderL}`,
                }}
              >
                Skip MAM
              </Btn>
              <span
                style={{
                  width: 1,
                  height: 20,
                  background: t.border,
                  margin: "0 4px",
                }}
              />
            </>
          ) : null}
          <Btn
            size="sm"
            onClick={() => selectMany(allVisibleIds())}
            disabled={busy}
          >
            Select All
          </Btn>
          {sel.size > 0 ? (
            <Btn size="sm" onClick={() => setSel(new Set())} disabled={busy}>
              Deselect All
            </Btn>
          ) : null}
        </div>
      ) : null}

      {/* ── Format tabs (only shown when author exists in >1 library) ── */}
      <FormatTabs a={a} fmtTab={fmtTab} setFmtTab={setFmtTab} t={t} />

      {/* ── Per-tab content ── */}
      <PerLibraryBlocks
        a={a}
        fmtTab={fmtTab}
        authorIdNum={authorIdNum}
        allCol={allCol}
        vm={vm}
        onAction={onAction}
        toggleSb={toggleSb}
        rk={rk}
        t={t}
        selMode={selMode}
        sel={sel}
        onToggleSel={toggleSel}
        onSelectMany={selectMany}
        onDeselectMany={deselectMany}
        onBooksLoaded={onBooksLoaded}
        onNav={onNav}
      />

      {sb ? (
        <BookSidebar
          book={sb}
          closing={sbClosing}
          onClose={closeSb}
          onAction={onAction}
          onEdit={loadA}
        />
      ) : null}
    </div>
  );
}

// ─── Format tabs ────────────────────────────────────────────
// Renders a tab strip (Combined + one tab per library) only when the
// author has content in more than one library. Single-library installs
// get nothing and fall through to the flat Combined layout.
function FormatTabs({
  a,
  fmtTab,
  setFmtTab,
  t,
}: {
  a: AuthorDetail;
  fmtTab: string;
  setFmtTab: (v: string) => void;
  t: Theme;
}) {
  const crossLib = a.cross_library || {};
  const crossSlugs = Object.keys(crossLib);
  if (crossSlugs.length === 0) return null;

  const tabs: { key: string; label: string; content_type: string; slug: string | null }[] = [
    { key: "combined", label: "Combined", content_type: "combined", slug: null },
    {
      key: a.active_library_slug || "active",
      label: a.active_content_type === "audiobook" ? "Audiobook" : "Ebook",
      content_type: a.active_content_type || "ebook",
      slug: a.active_library_slug || null,
    },
    ...crossSlugs.map((slug) => ({
      key: slug,
      label:
        crossLib[slug].content_type === "audiobook" ? "Audiobook" : "Ebook",
      content_type: crossLib[slug].content_type,
      slug,
    })),
  ];

  return (
    <div
      style={{
        display: "flex",
        gap: 4,
        borderBottom: `1px solid ${t.border}`,
        paddingBottom: 0,
      }}
    >
      {tabs.map((tab) => {
        const active = fmtTab === tab.key;
        const color =
          tab.content_type === "audiobook" ? t.pur || t.accent : t.accent;
        return (
          <button
            key={tab.key}
            onClick={() => setFmtTab(tab.key)}
            style={{
              padding: "10px 18px",
              background: active ? color + "22" : "transparent",
              color: active ? color : t.td,
              border: "none",
              borderBottom: active
                ? `2px solid ${color}`
                : "2px solid transparent",
              cursor: "pointer",
              fontSize: 14,
              fontWeight: active ? 600 : 500,
              marginBottom: -1,
            }}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}

// ─── Per-library blocks ─────────────────────────────────────
// Renders the selected-tab subset of the author's library copies.
// Combined mode shows every library block (with a content-type header
// each); single-tab mode narrows to the matching slug only.
function PerLibraryBlocks({
  a,
  fmtTab,
  authorIdNum,
  allCol,
  vm,
  onAction,
  toggleSb,
  rk,
  t,
  selMode,
  sel,
  onToggleSel,
  onSelectMany,
  onDeselectMany,
  onBooksLoaded,
  onNav,
}: {
  a: AuthorDetail;
  fmtTab: string;
  authorIdNum: number;
  allCol: boolean;
  vm: ViewMode;
  onAction: BookActionHandler;
  toggleSb: (b: Book) => void;
  rk: number;
  t: Theme;
  selMode: boolean;
  sel: Set<number>;
  onToggleSel: (id: number) => void;
  onSelectMany: (ids: number[]) => void;
  onDeselectMany: (ids: number[]) => void;
  onBooksLoaded: (key: string, books: Book[]) => void;
  onNav: NavFn;
}) {
  const crossLib = a.cross_library || {};
  const crossSlugs = Object.keys(crossLib);
  const hasTabs = crossSlugs.length > 0;

  const activeBlock: LibraryBlock = {
    slug: a.active_library_slug || "active",
    label: a.active_content_type === "audiobook" ? "Audiobook" : "Ebook",
    content_type: a.active_content_type || "ebook",
    data: a,
  };
  const crossBlocks: LibraryBlock[] = crossSlugs.map((slug) => ({
    slug,
    label:
      crossLib[slug].content_type === "audiobook" ? "Audiobook" : "Ebook",
    content_type: crossLib[slug].content_type,
    data: crossLib[slug].author,
  }));

  let blocksToRender: LibraryBlock[];
  if (!hasTabs || fmtTab === "combined") {
    blocksToRender = [activeBlock, ...crossBlocks];
  } else {
    blocksToRender = [...crossBlocks, activeBlock].filter(
      (b) => b.slug === fmtTab,
    );
  }

  // Per-block scan trigger. Fires the same author-lookup endpoint the
  // top-right Re-sync button uses, but with the block's slug so it
  // always scans THIS library's copy of the author — even when the
  // URL slug is a different library.
  const scanBlock = async (block: LibraryBlock) => {
    const bAuthorId = block.data?.id;
    if (!bAuthorId) return;
    try {
      const r = await api.post<ScanStartedResponse>(
        `/discovery/authors/${bAuthorId}/lookup?slug=${encodeURIComponent(block.slug)}`,
      );
      toast.info(`${block.label} scan started for ${r.author || "author"}`);
      window.dispatchEvent(new CustomEvent("seshat:scan-started"));
    } catch (e) {
      toast.error((e as Error).message || "Scan failed to start");
    }
  };

  // v2.14.0 — jump-to-section nav for Combined mode. UAT 2026-05-14
  // surfaced: with multi-hundred-item Combined tabs (e.g. Sanderson:
  // ~400 entries), the boundary between the Ebook block and the
  // Audiobook block disappears into the scroll. The mini-nav scrolls
  // straight to the first block of each content_type. Only renders
  // when blocksToRender has >1 block AND the user is on Combined.
  const showJumpNav = hasTabs && fmtTab === "combined" && blocksToRender.length > 1;
  const jumpTargets: { label: string; slug: string; color: string }[] = (() => {
    if (!showJumpNav) return [];
    const seen = new Set<string>();
    const out: { label: string; slug: string; color: string }[] = [];
    for (const b of blocksToRender) {
      const key = b.content_type;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({
        label: b.label,
        slug: b.slug,
        color: b.content_type === "audiobook" ? t.pur || t.accent : t.accent,
      });
    }
    return out;
  })();
  const jumpTo = (slug: string) => {
    const el = document.getElementById(`block-${slug}`);
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <>
      {showJumpNav && jumpTargets.length > 1 ? (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "8px 12px",
            background: t.bg4,
            border: `1px solid ${t.border}`,
            borderRadius: 6,
            fontSize: 13,
          }}
        >
          <span style={{ color: t.tf, fontWeight: 500 }}>Jump to:</span>
          {jumpTargets.map((j) => (
            <button
              key={j.slug}
              onClick={() => jumpTo(j.slug)}
              style={{
                padding: "4px 12px",
                background: j.color + "22",
                color: j.color,
                border: `1px solid ${j.color}44`,
                borderRadius: 5,
                fontSize: 13,
                fontWeight: 600,
                cursor: "pointer",
              }}
            >
              {j.label}
            </button>
          ))}
        </div>
      ) : null}
      {blocksToRender.map((block) => {
        const series = block.data?.series || [];
        const standalone = block.data?.standalone_books || [];
        // Show the section header when multiple library blocks are
        // visible at once (Combined mode). Single-tab view renders
        // one block, so the header would be redundant with the tab
        // strip.
        const showHdr = hasTabs && fmtTab === "combined";
        const color =
          block.content_type === "audiobook" ? t.pur || t.accent : t.accent;
        // Per-block Scan button: always show when hasTabs (i.e.
        // cross-library case) so the user can trigger "Scan Audiobook
        // copy of this author" separately from "Scan Ebook copy".
        const showScan = hasTabs;
        return (
          <div
            key={block.slug}
            id={`block-${block.slug}`}
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 16,
              // Leave room for the sticky author header so smooth-
              // scrolling lands the section heading below it instead
              // of clipped underneath.
              scrollMarginTop: 200,
            }}
          >
            {showHdr || showScan ? (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  marginTop: 4,
                }}
              >
                {showHdr && (
                  <>
                    <div
                      style={{
                        width: 6,
                        height: 26,
                        background: color,
                        borderRadius: 3,
                      }}
                    />
                    <span
                      style={{
                        fontSize: 16,
                        fontWeight: 700,
                        color,
                        textTransform: "uppercase",
                        letterSpacing: "0.05em",
                      }}
                    >
                      {block.label}
                    </span>
                  </>
                )}
                <div style={{ flex: 1, height: 1, background: t.borderL }} />
                {showScan && (
                  <button
                    onClick={() => scanBlock(block)}
                    title={`Scan ${block.label.toLowerCase()} sources for this author`}
                    style={{
                      fontSize: 11,
                      fontWeight: 600,
                      padding: "4px 10px",
                      borderRadius: 5,
                      background: color + "22",
                      color,
                      border: `1px solid ${color}44`,
                      cursor: "pointer",
                      whiteSpace: "nowrap",
                    }}
                  >
                    Scan {block.label}
                  </button>
                )}
              </div>
            ) : null}
            {series.map((s: Series) => (
              <IS
                key={`${block.slug}_${s.id}_${rk}`}
                series={s}
                vm={vm}
                onAction={onAction}
                onBookClick={toggleSb}
                collapsed={allCol}
                authorId={block.data?.id ?? authorIdNum}
                librarySlug={block.slug}
                selMode={selMode}
                sel={sel}
                onToggleSel={onToggleSel}
                onSelectMany={onSelectMany}
                onDeselectMany={onDeselectMany}
                onBooksLoaded={onBooksLoaded}
                onNav={onNav}
              />
            ))}
            {standalone.length > 0 && (
              <SA
                books={standalone}
                vm={vm}
                onAction={onAction}
                onBookClick={toggleSb}
                collapsed={allCol}
                selMode={selMode}
                sel={sel}
                onToggleSel={onToggleSel}
                onSelectMany={onSelectMany}
                onDeselectMany={onDeselectMany}
                onNav={onNav}
              />
            )}
            {series.length === 0 && standalone.length === 0 && (
              <div
                style={{
                  fontSize: 13,
                  color: t.tf,
                  fontStyle: "italic",
                  padding: "20px 0",
                }}
              >
                No{" "}
                {block.content_type === "audiobook" ? "audiobooks" : "ebooks"}{" "}
                in this library for this author.
              </div>
            )}
          </div>
        );
      })}
    </>
  );
}
