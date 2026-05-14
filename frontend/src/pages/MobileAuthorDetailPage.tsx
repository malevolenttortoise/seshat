// Mobile-native author detail page.
//
// Hero (avatar + name + counts), action chips (re-sync / MAM scan),
// bio + pen-names in collapsed sections, cross-library tabs when
// the author exists in multiple libraries, then per-series sections
// (books load on first expand) and a standalone-books section.
import { useCallback, useEffect, useState } from "react";
import { api, slugQuery } from "../api";
import { useTheme } from "../theme";
import { fmtDuration, fmtNum } from "../lib/format";
import { BookSidebar } from "../components/BookSidebar";
import { toast } from "../lib/toast";
import {
  MobileBtn,
  MobileChip,
  MobileSection,
  MobileBookCard,
  MobileBadge,
  MobileInput,
  MobileBackButton,
} from "../components/mobile";
import type {
  Author,
  AuthorsResponse,
  Book,
  BookAction,
  MamStatusResponse,
  NavFn,
  PenNameLink,
  PenNamesResponse,
  Series,
} from "../types";

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

interface ScanStartedResponse {
  status?: string;
  author?: string;
  total?: number;
  message?: string;
  error?: string;
}

interface MobileAuthorDetailPageProps {
  authorId: number | string;
  onNav: NavFn;
}

// Per-series collapsible. Books fetch lazily when the section
// expands so a long author page stays cheap.
function MobileSeriesSection({
  series,
  librarySlug,
  onBookClick,
  showMamLink,
  selMode,
  sel,
  onToggleSel,
  onSelectMany,
  onDeselectMany,
  onBooksLoaded,
}: {
  series: Series;
  librarySlug?: string | null;
  onBookClick: (b: Book) => void;
  showMamLink: boolean;
  selMode?: boolean;
  sel?: Set<number>;
  onToggleSel?: (id: number) => void;
  onSelectMany?: (ids: number[]) => void;
  onDeselectMany?: (ids: number[]) => void;
  onBooksLoaded?: (key: string, books: Book[]) => void;
}) {
  const t = useTheme();
  const [bks, setBks] = useState<Book[] | null>(null);
  const [ld, setLd] = useState(false);
  const lkey = `${librarySlug || "active"}:${series.id}`;

  const load = useCallback(() => {
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
  }, [series.id, librarySlug, bks, lkey, onBooksLoaded]);

  // Triggered by MobileSection's open state — we use the lazy
  // pattern by rendering a tiny effect inside the children that
  // fires once when bks is null.
  useEffect(() => {
    if (bks === null && !ld) load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const owned = series.owned_count ?? 0;
  const missing = series.missing_count ?? 0;
  const total = series.book_count ?? 0;
  // Omnibus-only series — show "Omnibus" instead of "0/0" when this
  // author's only contribution to the series is a collection. See the
  // desktop IS section for the full rationale.
  const omnibusOnly =
    total === 0 &&
    (bks
      ? bks.some((b) => b.is_omnibus)
      : (series.author_omnibus_count || 0) > 0);
  const countLabel = omnibusOnly ? "Omnibus" : `${owned}/${total}`;

  const ids = bks ? bks.map((b) => b.id) : [];
  const selectedHere = sel ? ids.filter((id) => sel.has(id)).length : 0;
  const allSelected = ids.length > 0 && selectedHere === ids.length;
  const quickPick =
    selMode && bks ? (
      <button
        onClick={(e) => {
          e.stopPropagation();
          if (allSelected) onDeselectMany && onDeselectMany(ids);
          else onSelectMany && onSelectMany(ids);
        }}
        style={{
          fontSize: 11,
          fontWeight: 600,
          padding: "4px 10px",
          borderRadius: 5,
          background: allSelected ? t.accent + "22" : "transparent",
          color: allSelected ? t.accent : t.td,
          border: `1px solid ${allSelected ? t.accent + "66" : t.border}`,
          cursor: "pointer",
        }}
      >
        {allSelected
          ? "Deselect"
          : selectedHere > 0
            ? `Select all (${selectedHere}/${ids.length})`
            : "Select"}
      </button>
    ) : null;

  return (
    <MobileSection
      title={series.name}
      count={countLabel}
      subtitle={
        missing > 0 ? `${missing} missing` : undefined
      }
      defaultOpen={false}
      right={quickPick}
    >
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 360px), 1fr))",
          gap: 8,
        }}
      >
        {bks?.map((b) => (
          <MobileBookCard
            key={b.id}
            book={b}
            onClick={() => onBookClick(b)}
            showMamLink={showMamLink}
            selMode={selMode}
            selected={sel ? sel.has(b.id) : false}
            onToggleSel={onToggleSel}
          />
        ))}
      </div>
      {ld && bks === null && (
        <div style={{ padding: 8, fontSize: 13, color: "#888" }}>Loading…</div>
      )}
    </MobileSection>
  );
}

export default function MobileAuthorDetailPage({
  authorId,
  onNav,
}: MobileAuthorDetailPageProps) {
  void onNav;
  const t = useTheme();
  const [a, setA] = useState<AuthorDetail | null>(null);
  const [ld, setLd] = useState(true);
  const [ref, setRef] = useState(false);
  const [mamRef, setMamRef] = useState(false);
  const [sb, setSb] = useState<Book | null>(null);
  const [sbClosing, setSbClosing] = useState(false);
  const [mamOn, setMamOn] = useState(false);
  const [fmtTab, setFmtTab] = useState<string>("combined");

  // Multi-select. Mirrors the desktop wiring — page-wide selection
  // set, lazy series-book cache so "Select all" can include
  // collapsed series whose books haven't been fetched yet (mobile
  // sections start collapsed by default, so this matters more here).
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

  // pen-name management
  const [penLinks, setPenLinks] = useState<PenNameLink[]>([]);
  const [penQ, setPenQ] = useState("");
  const [penResults, setPenResults] = useState<Author[]>([]);
  const [penBusy, setPenBusy] = useState(false);

  // Parse "slug:id" arg shape for cross-library nav.
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
        .catch(() => setLd(false));
    },
    [authorIdNum, authorSlug],
  );

  useEffect(() => {
    if (!authorIdNum) return;
    const c = new AbortController();
    loadA(c.signal);
    return () => c.abort();
  }, [loadA, authorIdNum]);

  useEffect(() => {
    api
      .get<MamStatusResponse>("/discovery/mam/status")
      .then((r) => setMamOn(!!r.enabled))
      .catch(() => {});
  }, []);

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
          setPenResults((r.authors || []).filter((x) => x.id !== authorIdNum)),
        )
        .catch(() => {});
    }, 300);
    return () => clearTimeout(tm);
  }, [penQ, authorIdNum]);

  const closeSb = () => {
    if (!sb) return;
    setSbClosing(true);
    setTimeout(() => {
      setSb(null);
      setSbClosing(false);
    }, 200);
  };

  const onAction = async (act: BookAction, id: number, slug?: string) => {
    if (act === "hide") await api.post(`/discovery/books/${id}/hide${slugQuery(slug)}`);
    if (act === "dismiss") await api.post(`/discovery/books/${id}/dismiss${slugQuery(slug)}`);
    await loadA();
  };

  // Page-wide selectable IDs — see the desktop sibling for the
  // matching helper. Mobile sections default to closed, so this is
  // skewed toward "standalone is always available; series only
  // counts after the user has expanded that section at least once
  // and IS fetched its books."
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
  // operations. See DiscAuthorDetailPage.tsx for the full rationale
  // (cross-library id-collision can delete unrelated books on the
  // wrong library when the user selects from Combined tab).
  // Mirrors the desktop refactor.
  const buildBookSlugMap = (): Map<number, string> => {
    const out = new Map<number, string>();
    const activeSlug = a?.active_library_slug || "active";
    (a?.standalone_books || []).forEach((b) => out.set(b.id, activeSlug));
    Object.entries(a?.cross_library || {}).forEach(([slug, entry]) => {
      (entry.author.standalone_books || []).forEach((b) => out.set(b.id, slug));
    });
    Object.entries(seriesBooks).forEach(([key, books]) => {
      const prefix = key.split(":", 1)[0];
      const slug = prefix === "active" ? activeSlug : prefix;
      books.forEach((b) => {
        if (!out.has(b.id)) out.set(b.id, slug);
      });
    });
    return out;
  };
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
    const pastLabels = {
      hide: "Hidden", dismiss: "Dismissed", delete: "Deleted",
      "skip-mam": "Marked N/A",
    } as const;

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
      const results = await Promise.all(
        slugs.map((slug) => {
          const slugIds = partition.get(slug)!;
          return api.post<{
            status?: string;
            count?: number;
            deleted?: number;
            skipped?: number;
            error?: string;
          }>(
            `/discovery/books/bulk-${kind}${slugQuery(slug)}`,
            { book_ids: slugIds },
          ).then((r) => ({ slug, r }))
            .catch((e) => ({ slug, r: { error: (e as Error).message || "failed" } }));
        }),
      );
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
      setSeriesBooks({});
      await loadA();
    } catch (e) {
      toast.error((e as Error).message || `${labels[kind]} failed`);
    }
    setBusy(false);
  };

  const scanQs = authorSlug ? `?slug=${encodeURIComponent(authorSlug)}` : "";

  const triggerSync = async () => {
    if (ref) return;
    setRef(true);
    try {
      const r = await api.post<ScanStartedResponse>(
        `/discovery/authors/${authorIdNum}/lookup${scanQs}`,
      );
      toast.info(`Source scan started for ${r.author || "author"}`);
      window.dispatchEvent(new CustomEvent("seshat:scan-started"));
    } catch (e) {
      toast.error((e as Error).message || "Scan failed to start");
    }
    setRef(false);
  };

  const triggerMam = async () => {
    if (mamRef) return;
    setMamRef(true);
    try {
      const r = await api.post<ScanStartedResponse>(
        `/discovery/mam/scan-author/${authorIdNum}${scanQs}`,
      );
      if (r.status === "complete") {
        toast.info(r.message || "No un-scanned books for this author");
      } else {
        toast.info(`MAM scan started — ${r.total || 0} books`);
        window.dispatchEvent(new CustomEvent("seshat:scan-started"));
      }
    } catch (e) {
      toast.error((e as Error).message || "MAM scan failed to start");
    }
    setMamRef(false);
  };

  const linkPen = async (aliasId: number, linkType = "pen_name") => {
    setPenBusy(true);
    try {
      await api.post("/discovery/authors/link-pen-names", {
        canonical_author_id: authorIdNum,
        alias_author_id: aliasId,
        link_type: linkType,
      });
      const r = await api.get<PenNamesResponse>(
        `/discovery/authors/${authorIdNum}/pen-names`,
      );
      setPenLinks(r.links || []);
      setPenQ("");
      setPenResults([]);
      toast.success("Linked");
    } catch (e) {
      toast.error((e as Error).message || "Link failed");
    }
    setPenBusy(false);
  };

  const unlinkPen = async (linkId: number) => {
    if (!confirm("Remove this pen-name link?")) return;
    setPenBusy(true);
    try {
      await api.del(`/discovery/authors/pen-name-links/${linkId}`);
      const r = await api.get<PenNamesResponse>(
        `/discovery/authors/${authorIdNum}/pen-names`,
      );
      setPenLinks(r.links || []);
      toast.success("Unlinked");
    } catch (e) {
      toast.error((e as Error).message || "Unlink failed");
    }
    setPenBusy(false);
  };

  if (ld && !a) {
    return (
      <div style={{ padding: 32, textAlign: "center", color: t.tg }}>
        Loading…
      </div>
    );
  }
  if (!a) return null;

  // Build the list of library blocks for cross-library tabs.
  const blocks: { slug: string; label: string; content_type: string; data: AuthorDetail }[] = [
    {
      slug: a.active_library_slug || authorSlug || "active",
      label: a.active_content_type === "audiobook" ? "🎧 Audio" : "📖 Ebook",
      content_type: a.active_content_type || "ebook",
      data: a,
    },
  ];
  if (a.cross_library) {
    for (const [slug, entry] of Object.entries(a.cross_library)) {
      blocks.push({
        slug,
        label: entry.content_type === "audiobook" ? "🎧 Audio" : "📖 Ebook",
        content_type: entry.content_type,
        data: entry.author,
      });
    }
  }
  const hasMultiLib = blocks.length > 1;

  // The currently selected block (for non-combined tabs).
  const activeBlock =
    fmtTab === "combined"
      ? null
      : blocks.find((b) => b.content_type === fmtTab) || blocks[0];

  // Helper to render the books for a given block (or all blocks combined).
  const renderBlocks = (blocksToRender: typeof blocks) =>
    blocksToRender.map((block) => {
      const standalone = block.data.standalone_books || [];
      const stIds = standalone.map((b) => b.id);
      const stSelected = stIds.filter((id) => sel.has(id)).length;
      const stAllSelected = stIds.length > 0 && stSelected === stIds.length;
      const stQuickPick =
        selMode && stIds.length > 0 ? (
          <button
            onClick={(e) => {
              e.stopPropagation();
              if (stAllSelected) deselectMany(stIds);
              else selectMany(stIds);
            }}
            style={{
              fontSize: 11,
              fontWeight: 600,
              padding: "4px 10px",
              borderRadius: 5,
              background: stAllSelected ? t.accent + "22" : "transparent",
              color: stAllSelected ? t.accent : t.td,
              border: `1px solid ${stAllSelected ? t.accent + "66" : t.border}`,
              cursor: "pointer",
            }}
          >
            {stAllSelected
              ? "Deselect"
              : stSelected > 0
                ? `Select all (${stSelected}/${stIds.length})`
                : "Select"}
          </button>
        ) : null;
      return (
        <div key={block.slug}>
          {hasMultiLib && fmtTab === "combined" && (
            <div
              style={{
                fontSize: 12,
                color: t.tg,
                fontWeight: 700,
                textTransform: "uppercase",
                letterSpacing: "0.04em",
                padding: "12px 4px 6px",
              }}
            >
              {block.label}
            </div>
          )}
          {(block.data.series || []).map((s) => (
            <MobileSeriesSection
              key={`${block.slug}-${s.id}`}
              series={s}
              librarySlug={block.slug}
              onBookClick={setSb}
              showMamLink={mamOn}
              selMode={selMode}
              sel={sel}
              onToggleSel={toggleSel}
              onSelectMany={selectMany}
              onDeselectMany={deselectMany}
              onBooksLoaded={onBooksLoaded}
            />
          ))}
          {standalone.length > 0 && (
            <MobileSection
              title="Standalone"
              count={standalone.length}
              defaultOpen={true}
              right={stQuickPick}
            >
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 360px), 1fr))",
                  gap: 8,
                }}
              >
                {standalone.map((b) => (
                  <MobileBookCard
                    key={b.id}
                    book={b}
                    onClick={() => setSb(b)}
                    showMamLink={mamOn}
                    selMode={selMode}
                    selected={sel.has(b.id)}
                    onToggleSel={toggleSel}
                  />
                ))}
              </div>
            </MobileSection>
          )}
        </div>
      );
    });

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <MobileBackButton to="disc-authors" label="Authors" />
      {/* Hero card */}
      <div
        style={{
          display: "flex",
          gap: 12,
          padding: 12,
          background: t.bg2,
          border: `1px solid ${t.border}`,
          borderRadius: 12,
        }}
      >
        <div
          style={{
            width: 72,
            height: 72,
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
              style={{ width: "100%", height: "100%", objectFit: "cover" }}
              onError={(e) => {
                (e.currentTarget as HTMLImageElement).style.display = "none";
              }}
            />
          ) : (
            <span style={{ color: t.td, fontWeight: 700, fontSize: 22 }}>
              {(a.name || "?")[0]?.toUpperCase()}
            </span>
          )}
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              fontSize: 18,
              fontWeight: 700,
              color: t.text,
              lineHeight: 1.2,
            }}
          >
            {a.name}
          </div>
          <div
            style={{
              display: "flex",
              gap: 12,
              marginTop: 6,
              fontSize: 13,
              color: t.td,
              flexWrap: "wrap",
            }}
          >
            <span>
              <strong style={{ color: t.text }}>{fmtNum(a.owned_count ?? 0)}</strong>
              {" / "}
              {fmtNum(a.total_books ?? 0)} owned
            </span>
            {(a.missing_count ?? 0) > 0 && (
              <span style={{ color: t.red }}>
                {fmtNum(a.missing_count ?? 0)} missing
              </span>
            )}
            {(a.new_count ?? 0) > 0 && (
              <MobileBadge tone="accent">{a.new_count} new</MobileBadge>
            )}
          </div>
        </div>
      </div>

      {/* Action chips */}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <MobileBtn
          variant="secondary"
          onClick={triggerSync}
          disabled={ref}
        >
          {ref ? "Syncing…" : "Re-scan sources"}
        </MobileBtn>
        {mamOn && (
          <MobileBtn
            variant="secondary"
            onClick={triggerMam}
            disabled={mamRef}
          >
            {mamRef ? "MAM scanning…" : "Scan MAM"}
          </MobileBtn>
        )}
        <MobileBtn
          variant={selMode ? "primary" : "secondary"}
          onClick={() => {
            setSelMode(!selMode);
            if (selMode) setSel(new Set());
          }}
        >
          {selMode ? "Cancel" : "Select"}
        </MobileBtn>
      </div>

      {/* Bulk action bar (visible only in select mode) */}
      {selMode ? (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "10px 12px",
            background: t.bg2,
            border: `1px solid ${t.border}`,
            borderRadius: 10,
            flexWrap: "wrap",
          }}
        >
          <span style={{ fontSize: 13, fontWeight: 600, color: t.text2 }}>
            {sel.size} book{sel.size === 1 ? "" : "s"}
          </span>
          {sel.size > 0 ? (
            <>
              <MobileBtn
                variant="secondary"
                onClick={() => bulkAct("hide")}
                disabled={busy}
                style={{ minHeight: 36, fontSize: 13 }}
              >
                Hide
              </MobileBtn>
              <MobileBtn
                variant="secondary"
                onClick={() => bulkAct("dismiss")}
                disabled={busy}
                style={{ minHeight: 36, fontSize: 13 }}
              >
                Dismiss
              </MobileBtn>
              <MobileBtn
                variant="danger"
                onClick={() => bulkAct("delete")}
                disabled={busy}
                style={{ minHeight: 36, fontSize: 13 }}
              >
                Delete
              </MobileBtn>
              <MobileBtn
                variant="secondary"
                onClick={() => bulkAct("skip-mam")}
                disabled={busy}
                style={{ minHeight: 36, fontSize: 13 }}
              >
                Skip MAM
              </MobileBtn>
            </>
          ) : null}
          <MobileBtn
            variant="ghost"
            onClick={() => selectMany(allVisibleIds())}
            disabled={busy}
            style={{ minHeight: 36, fontSize: 13 }}
          >
            Select all
          </MobileBtn>
          {sel.size > 0 ? (
            <MobileBtn
              variant="ghost"
              onClick={() => setSel(new Set())}
              disabled={busy}
              style={{ minHeight: 36, fontSize: 13 }}
            >
              Deselect
            </MobileBtn>
          ) : null}
        </div>
      ) : null}

      {/* Bio */}
      {a.bio && (
        <MobileSection title="Bio" defaultOpen={false}>
          <div
            style={{
              fontSize: 14,
              color: t.text2,
              lineHeight: 1.5,
              whiteSpace: "pre-wrap",
            }}
          >
            {a.bio}
          </div>
        </MobileSection>
      )}

      {/* Pen-name management */}
      <MobileSection title="Pen names" count={penLinks.length} defaultOpen={false}>
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {penLinks.length === 0 && (
            <div style={{ fontSize: 13, color: t.tg }}>
              No pen-name links yet. Search for an author below to link.
            </div>
          )}
          {penLinks.map((link) => {
            const otherName =
              link.canonical_author_id === authorIdNum
                ? link.alias_name
                : link.canonical_name;
            return (
              <div
                key={link.id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: 8,
                  padding: "8px 12px",
                  background: t.bg3,
                  border: `1px solid ${t.borderL}`,
                  borderRadius: 10,
                }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      fontSize: 14,
                      fontWeight: 600,
                      color: t.text,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {otherName}
                  </div>
                  <div style={{ fontSize: 11, color: t.tg, marginTop: 2 }}>
                    {link.link_type === "pen_name" ? "Pen name" : "Co-author"}
                  </div>
                </div>
                <MobileBtn
                  variant="ghost"
                  onClick={() => unlinkPen(link.id)}
                  disabled={penBusy}
                  style={{ minHeight: 36, fontSize: 13 }}
                >
                  Unlink
                </MobileBtn>
              </div>
            );
          })}
          <MobileInput
            value={penQ}
            onChange={(e) => setPenQ(e.target.value)}
            placeholder="Search authors to link"
          />
          {penResults.map((author) => (
            <div
              key={author.id}
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 8,
                padding: "8px 12px",
                background: t.bg3,
                border: `1px solid ${t.borderL}`,
                borderRadius: 10,
              }}
            >
              <div
                style={{
                  flex: 1,
                  minWidth: 0,
                  fontSize: 14,
                  color: t.text,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {author.name}
              </div>
              <MobileBtn
                variant="ghost"
                onClick={() => linkPen(author.id, "pen_name")}
                disabled={penBusy}
                style={{ minHeight: 36, fontSize: 13 }}
              >
                Link
              </MobileBtn>
            </div>
          ))}
        </div>
      </MobileSection>

      {/* Cross-library format tabs */}
      {hasMultiLib && (
        <div
          style={{
            display: "flex",
            gap: 6,
            overflowX: "auto",
            scrollbarWidth: "none",
          }}
        >
          <MobileChip
            active={fmtTab === "combined"}
            onClick={() => setFmtTab("combined")}
          >
            Combined
          </MobileChip>
          {blocks.map((b) => (
            <MobileChip
              key={b.slug}
              active={fmtTab === b.content_type}
              onClick={() => setFmtTab(b.content_type)}
            >
              {b.label}
            </MobileChip>
          ))}
        </div>
      )}

      {/* Series sections + standalone */}
      {fmtTab === "combined" ? renderBlocks(blocks) : activeBlock ? renderBlocks([activeBlock]) : null}

      {sb && (
        <BookSidebar
          book={sb}
          closing={sbClosing}
          onClose={closeSb}
          onAction={onAction}
          onEdit={loadA}
        />
      )}
    </div>
  );
}
