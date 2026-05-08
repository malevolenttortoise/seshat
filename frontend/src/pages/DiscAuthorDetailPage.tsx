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
  Series,
} from "../types";

// ─── Types for the author-detail response ─────────────────
// The /authors/{id} endpoint returns an Author row with two extra
// projections (`series`, `standalone_books`) + optional cross-library
// fields when `include_cross_library=1` is passed. We type the fields
// the UI actually reads; fields the server returns but we don't
// display stay unlisted.
interface AuthorDetail extends Author {
  series?: Series[];
  standalone_books?: Book[];
  active_library_slug?: string;
  active_content_type?: string;
  cross_library?: Record<string, CrossLibraryEntry>;
}

interface CrossLibraryEntry {
  library_name: string;
  content_type: string;
  app_type?: string;
  author: AuthorDetail;
}

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
}: ISProps) {
  const t = useTheme();
  const [ld, setLd] = useState(false);
  const [bks, setBks] = useState<Book[] | null>(null);
  const lkey = `${librarySlug || "active"}:${series.id}`;

  const load = () => {
    if (bks) return;
    setLd(true);
    const qs = librarySlug ? `?slug=${encodeURIComponent(librarySlug)}` : "";
    api
      .get<{ books?: Book[] }>(`/discovery/series/${series.id}${qs}`)
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

  const isMulti = !!series.multi_author;
  const header = isMulti ? (
    <span>
      {series.name}{" "}
      <span
        style={{
          fontSize: 11,
          color: t.cyant,
          fontWeight: 600,
          textTransform: "none",
          background: t.cyan + "22",
          padding: "2px 8px",
          borderRadius: 4,
          marginLeft: 4,
        }}
      >
        shared series
      </span>
    </span>
  ) : (
    series.name
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
    : isMulti
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
              showAuthor={isMulti}
              highlightAuthorId={authorId}
              selMode={selMode}
              sel={sel}
              onToggleSel={onToggleSel}
            />
          ) : (
            <BGrid
              books={regular || []}
              onAction={onAction}
              onBookClick={onBookClick}
              showAuthor={isMulti}
              highlightAuthorId={authorId}
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
                  showAuthor={isMulti}
                  highlightAuthorId={authorId}
                  selMode={selMode}
                  sel={sel}
                  onToggleSel={onToggleSel}
                />
              ) : (
                <BGrid
                  books={omnibus}
                  onAction={onAction}
                  onBookClick={onBookClick}
                  showAuthor={isMulti}
                  highlightAuthorId={authorId}
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
}: SAProps) {
  const t = useTheme();
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
          selMode={selMode}
          sel={sel}
          onToggleSel={onToggleSel}
        />
      ) : (
        <BGrid
          books={books}
          onAction={onAction}
          onBookClick={onBookClick}
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
  const [penResults, setPenResults] = useState<Author[]>([]);
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
        .get<AuthorsResponse>(
          `/discovery/authors?search=${encodeURIComponent(penQ)}`,
        )
        .then((r) =>
          setPenResults(
            (r.authors || []).filter((x) => x.id !== authorIdNum),
          ),
        )
        .catch(() => {});
    }, 300);
    return () => clearTimeout(tm);
  }, [penQ, authorIdNum]);

  const linkPen = async (aliasId: number) => {
    setPenBusy(true);
    try {
      await api.post("/discovery/authors/link-pen-names", {
        canonical_author_id: authorIdNum,
        alias_author_id: aliasId,
        link_type: penType,
      });
      const r = await api.get<PenNamesResponse>(
        `/discovery/authors/${authorIdNum}/pen-names`,
      );
      setPenLinks(r.links || []);
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
      const qs = authorSlug
        ? `?include_cross_library=1&slug=${encodeURIComponent(authorSlug)}`
        : `?include_cross_library=1`;
      return api
        .get<AuthorDetail>(`/discovery/authors/${authorIdNum}${qs}`, signal)
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
      await api.post("/discovery/authors/clear-scan-data", {
        author_ids: [authorIdNum],
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

  const scanAudiobookSources = async () => {
    if (!authorIdNum || ref) return;
    if (!confirm("Scan this author across every audiobook library?")) return;
    setRef(true);
    try {
      await api.post("/discovery/authors/scan-sources", {
        author_ids: [authorIdNum],
        content_type: "audiobook",
      });
      toast.info("Audiobook scan started");
      window.dispatchEvent(new CustomEvent("seshat:scan-started"));
    } catch (e) {
      toast.error((e as Error).message || "Scan failed to start");
      setRef(false);
    }
  };

  // Listen for scan completion (broadcast by the unified poller in
  // App-level Dashboard) and refresh this page's author data + book
  // grid.
  useEffect(() => {
    const onDone = () => {
      loadA();
      setRk((k) => k + 1);
      setRef(false);
      setMamRef(false);
    };
    window.addEventListener("seshat:scan-completed", onDone);
    return () =>
      window.removeEventListener("seshat:scan-completed", onDone);
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
    const msg =
      kind === "delete"
        ? `Delete ${ids.length} book(s)? Calibre-synced books will be skipped.`
        : kind === "skip-mam"
        ? `Mark ${ids.length} book(s) as Not Applicable for MAM scanning?`
        : `${labels[kind]} ${ids.length} book(s)?`;
    if (!confirm(msg)) return;
    setBusy(true);
    try {
      const r = await api.post<{
        status?: string;
        count?: number;
        deleted?: number;
        skipped?: number;
        error?: string;
      }>(
        `/discovery/books/bulk-${kind}${slugQuery(a?.active_library_slug)}`,
        { book_ids: ids },
      );
      if (r.error) {
        toast.error(r.error);
      } else if (kind === "delete") {
        const skipMsg = r.skipped
          ? `, skipped ${r.skipped} Calibre-synced`
          : "";
        toast.success(`Deleted ${r.deleted || 0} book(s)${skipMsg}`);
      } else {
        toast.success(
          `${pastLabels[kind]} ${r.count ?? ids.length} book(s)`,
        );
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
  const oc = saOwned + serOwned;
  const total = saTotal + serTotal;

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
                {(a.series || []).length} series
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
              Scan Audio
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
                {penResults.map((r) => (
                  <div
                    key={r.id}
                    onClick={() => linkPen(r.id)}
                    style={{
                      padding: "8px 12px",
                      cursor: "pointer",
                      fontSize: 12,
                      color: t.text2,
                      borderBottom: `1px solid ${t.borderL}`,
                    }}
                  >
                    {r.name}{" "}
                    <span style={{ color: t.tg }}>({r.total_books || 0} books)</span>
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

  return (
    <>
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
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 16,
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
                        width: 4,
                        height: 18,
                        background: color,
                        borderRadius: 2,
                      }}
                    />
                    <span
                      style={{
                        fontSize: 13,
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
