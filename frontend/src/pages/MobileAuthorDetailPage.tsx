// Mobile-native author detail page.
//
// Hero (avatar + name + counts), action chips (re-sync / MAM scan),
// bio + pen-names in collapsed sections, cross-library tabs when
// the author exists in multiple libraries, then per-series sections
// (books load on first expand) and a standalone-books section.
import { useCallback, useEffect, useState } from "react";
import { api, slugQuery } from "../api";
import { useTheme } from "../theme";
import { fmtNum } from "../lib/format";
import { BookSidebar } from "../components/BookSidebar";
import { toast } from "../lib/toast";
import { type AuthorDetail } from "../lib/authorDetail";
import { SourceBadgeRow } from "../components/SourceBadgeRow";
import { SourceBreakdownPanel } from "../components/SourceBreakdownPanel";
import { useAuthorWalk } from "../hooks/useAuthorWalk";
import { AuthorCacheStatusBadge } from "../components/AuthorCacheStatusBadge";
import { GoodreadsAuthorCacheStatusBadge } from "../components/GoodreadsAuthorCacheStatusBadge";
import { useAuthorScans } from "../hooks/useAuthorScans";
import { useAuthorBulkActions } from "../hooks/useAuthorBulkActions";
import { useAuthorDetail } from "../hooks/useAuthorDetail";
import { useBulkSelection } from "../hooks/useBulkSelection";
import { usePenNames } from "../hooks/usePenNames";
import { useBookSidebar } from "../hooks/useBookSidebar";
import { useMamEnabled } from "../hooks/useMamEnabled";
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
  Book,
  BookAction,
  NavFn,
  Series,
} from "../types";

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
  authorId,
  onNav,
}: {
  series: Series;
  authorId: number | string;
  onNav: NavFn;
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

  // v3.0.0 Phase 7 (ADR-0011) — owner sees the full series; an incidental
  // guest sees only their own entries (own-entries fetch) + an "N of M"
  // subtitle. `is_owner` undefined → owner (pre-Phase-7 behavior).
  const isOwner = series.is_owner ?? true;
  const isCoauthored =
    series.author_mode === "multi_author" || !!series.multi_author;
  const showByline = !isOwner || isCoauthored;

  const load = useCallback(() => {
    if (bks) return;
    setLd(true);
    const qs = librarySlug ? `?slug=${encodeURIComponent(librarySlug)}` : "";
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
  }, [series.id, librarySlug, bks, lkey, onBooksLoaded, isOwner, authorId]);

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
  const countLabel = omnibusOnly
    ? "Omnibus"
    : isOwner
      ? `${owned}/${total}`
      : `${series.author_book_count ?? 0} of ${total}`;

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
        !isOwner
          ? "part of a larger series"
          : missing > 0
            ? `${missing} missing`
            : undefined
      }
      defaultOpen={false}
      right={quickPick}
    >
      {/* v3.0.0 Phase 8 — guest entry point to the full series detail. */}
      {!isOwner ? (
        <button
          onClick={() =>
            onNav(
              "disc-series-detail",
              librarySlug ? `${librarySlug}:${series.id}` : series.id,
            )
          }
          style={{
            alignSelf: "flex-start",
            margin: "0 0 8px",
            fontSize: 12,
            fontWeight: 600,
            color: t.cyant,
            background: t.cyan + "22",
            border: `1px solid ${t.cyan}44`,
            borderRadius: 99,
            padding: "4px 10px",
            cursor: "pointer",
          }}
        >
          View full series ({series.author_book_count ?? 0} of {series.book_count ?? 0}) →
        </button>
      ) : null}
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
            showAuthor={showByline}
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
  const t = useTheme();
  const { a, ld, loadErr, loadA, authorIdNum, authorSlug } = useAuthorDetail(authorId);
  const { sb, setSb, sbClosing, closeSb } = useBookSidebar();
  const mamOn = useMamEnabled();
  const [fmtTab, setFmtTab] = useState<string>("combined");

  // Multi-select. Mirrors the desktop wiring — page-wide selection
  // set, lazy series-book cache so "Select all" can include
  // collapsed series whose books haven't been fetched yet (mobile
  // sections start collapsed by default, so this matters more here).
  const {
    selMode, setSelMode, sel, toggle: toggleSel, selectMany, deselectMany, clear: clearSel,
  } = useBulkSelection();
  const { busy, onBooksLoaded, allVisibleIds, act: bulkAct } = useAuthorBulkActions({
    a, sel, clearSel, setSelMode, loadA,
  });

  // pen-name management (usePenNames, after loadA below)

  // Prev/next within the list the user came from. Keyed on the RAW
  // `authorId` because that's the nav-arg form the list snapshotted.
  const walk = useAuthorWalk(authorId);

  // Source + MAM scan buttons, busy until the scan finishes (G160).
  const {
    sourceBusy: ref, mamBusy: mamRef, scanSources: triggerSync, scanMam: triggerMam,
  } = useAuthorScans({ authorIdNum, authorSlug, loadA });

  // v2.20.0 Phase 4 — the search returns person hits.
  const {
    penLinks, penQ, setPenQ, penResults, penBusy,
    link: linkPenName, unlink: unlinkPenName,
  } = usePenNames({ authorIdNum, personId: a?.person_id, onLinked: loadA });


  const onAction = async (act: BookAction, id: number, slug?: string) => {
    if (act === "hide") await api.post(`/discovery/books/${id}/hide${slugQuery(slug)}`);
    if (act === "dismiss") await api.post(`/discovery/books/${id}/dismiss${slugQuery(slug)}`);
    await loadA();
  };



  const linkPen = async (aliasPersonId: number, linkType = "pen_name") => {
    if (!a?.person_id) {
      toast.error("Author not yet linked to a canonical person");
      return;
    }
    try {
      await linkPenName(aliasPersonId, linkType);
      toast.success("Linked");
    } catch (e) {
      toast.error((e as Error).message || "Link failed");
    }
  };

  const unlinkPen = async (linkId: number) => {
    if (!confirm("Remove this pen-name link?")) return;
    try {
      await unlinkPenName(linkId);
      toast.success("Unlinked");
    } catch (e) {
      toast.error((e as Error).message || "Unlink failed");
    }
  };

  if (ld && !a) {
    return (
      <div style={{ padding: 32, textAlign: "center", color: t.tg }}>
        Loading…
      </div>
    );
  }
  if (!a) {
    return (
      <div style={{ padding: 32, textAlign: "center", color: t.err }}>
        Couldn't load this author{loadErr ? `: ${loadErr}` : ""}
      </div>
    );
  }

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
      const hdrColor = block.content_type === "audiobook" ? t.pur || t.accent : t.accent;
      return (
        <div
          key={block.slug}
          id={`block-${block.slug}`}
          style={{ scrollMarginTop: 80 }}
        >
          {hasMultiLib && fmtTab === "combined" && (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                padding: "16px 4px 8px",
              }}
            >
              <div
                style={{
                  width: 5,
                  height: 22,
                  background: hdrColor,
                  borderRadius: 3,
                }}
              />
              <span
                style={{
                  fontSize: 14,
                  color: hdrColor,
                  fontWeight: 700,
                  textTransform: "uppercase",
                  letterSpacing: "0.04em",
                }}
              >
                {block.label}
              </span>
            </div>
          )}
          {(block.data.series || []).map((s) => (
            <MobileSeriesSection
              key={`${block.slug}-${s.id}`}
              series={s}
              authorId={block.data?.id ?? authorIdNum}
              onNav={onNav}
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

      {/* v3.10.0 — walk the list you came from, at parity with desktop.
          Full-width tap targets rather than the desktop's compact
          buttons; hidden entirely when there's no snapshot. */}
      {walk.total > 0 ? (
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <button
            disabled={!walk.prev}
            onClick={() => walk.prev && onNav("disc-author-detail", walk.prev)}
            style={{
              flex: 1,
              padding: "10px 12px",
              background: t.bg3,
              color: walk.prev ? t.text : t.tg,
              border: `1px solid ${t.border}`,
              borderRadius: 8,
              fontSize: 14,
              fontWeight: 600,
              cursor: walk.prev ? "pointer" : "default",
            }}
          >
            ← Prev
          </button>
          <span style={{ fontSize: 13, color: t.tm, whiteSpace: "nowrap" }}>
            {walk.index} / {walk.total}
          </span>
          <button
            disabled={!walk.next}
            onClick={() => walk.next && onNav("disc-author-detail", walk.next)}
            style={{
              flex: 1,
              padding: "10px 12px",
              background: t.bg3,
              color: walk.next ? t.text : t.tg,
              border: `1px solid ${t.border}`,
              borderRadius: 8,
              fontSize: 14,
              fontWeight: 600,
              cursor: walk.next ? "pointer" : "default",
            }}
          >
            Next →
          </button>
        </div>
      ) : null}
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
            {(() => {
              // v2.17.0 Bug B — `global_stats` sums across primary +
              // every cross_library entry. Fall back to per-library
              // computation when the field isn't present. (Pre-fix,
              // mobile read a.owned_count from the authors table
              // directly which has no such column, so the display
              // was permanently 0/0 — separate bug also closed here.)
              const gs = (a as AuthorDetail).global_stats;
              const saOwned = (a.standalone_books || []).filter(
                (b) => b.owned === 1,
              ).length;
              const saTotal = (a.standalone_books || []).length;
              const serOwned = (a.series || []).reduce(
                (n, s) => n + (s.owned_count || 0),
                0,
              );
              const serTotal = (a.series || []).reduce(
                (n, s) => n + (s.author_book_count ?? s.book_count ?? 0),
                0,
              );
              const owned = gs ? gs.owned : saOwned + serOwned;
              const total = gs ? gs.total : saTotal + serTotal;
              const missing = gs ? gs.missing : Math.max(0, total - owned);
              return (
                <>
                  <span>
                    <strong style={{ color: t.text }}>{fmtNum(owned)}</strong>
                    {" / "}
                    {fmtNum(total)} owned
                  </span>
                  {missing > 0 && (
                    <span style={{ color: t.red }}>
                      {fmtNum(missing)} missing
                    </span>
                  )}
                </>
              );
            })()}
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
            if (selMode) clearSel();
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
              onClick={clearSel}
              disabled={busy}
              style={{ minHeight: 36, fontSize: 13 }}
            >
              Deselect
            </MobileBtn>
          ) : null}
        </div>
      ) : null}

      {/* v2.20.0 Phase 3 — source-ID badges. Wraps in a MobileSection
          for mobile-native collapse UX. */}
      {a.person_id ? (
        <MobileSection title="Source IDs" defaultOpen={false}>
          <SourceBadgeRow
            personId={a.person_id}
            sourceIds={a.source_ids || {}}
            onUpdate={() => loadA()}
          />
          {/* v2.21.0 Phase F tier 3 + v3.6.0 frontend parity —
              per-author cache state per metadata source. Lives
              inside the Source IDs section so the toggle hides both
              the badges and the cache lines together on small screens. */}
          <AuthorCacheStatusBadge
            amazonAuthorId={(a.source_ids || {}).amazon as string | undefined}
          />
          <GoodreadsAuthorCacheStatusBadge
            goodreadsAuthorId={(a.source_ids || {}).goodreads as string | undefined}
          />
        </MobileSection>
      ) : null}

      {/* v3.10.0 — per-source evidence + operator blacklist, at parity
          with the desktop page. ⚠️ `authorIdNum`, NOT the raw `authorId`
          prop: the composite "slug:id" form 422s the endpoint, and a
          failed load used to render nothing at all. */}
      <SourceBreakdownPanel
        authorId={authorIdNum}
        slug={authorSlug || a.active_library_slug}
        onChanged={() => loadA()}
      />

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
          {penResults.map((p) => (
            <div
              key={p.person_id}
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
                {p.display_name}{" "}
                {p.content_types.includes("ebook") && (
                  <span title="Ebook">📖</span>
                )}
                {p.content_types.includes("audiobook") && (
                  <span title="Audiobook">🎧</span>
                )}
              </div>
              <MobileBtn
                variant="ghost"
                onClick={() => linkPen(p.person_id, "pen_name")}
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

      {/* v2.14.0 — Jump-to-section nav for Combined mode. Same intent
         as the desktop equivalent: long Combined lists hide the
         Ebook/Audiobook boundary, so offer a one-tap scroll. Only
         renders when Combined view has more than one block. */}
      {hasMultiLib && fmtTab === "combined" && blocks.length > 1 ? (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "8px 10px",
            background: t.bg4,
            border: `1px solid ${t.border}`,
            borderRadius: 6,
            fontSize: 13,
            overflowX: "auto",
            scrollbarWidth: "none",
          }}
        >
          <span style={{ color: t.tf, fontWeight: 500, whiteSpace: "nowrap" }}>
            Jump to:
          </span>
          {(() => {
            const seen = new Set<string>();
            const out: { label: string; slug: string; color: string }[] = [];
            for (const b of blocks) {
              if (seen.has(b.content_type)) continue;
              seen.add(b.content_type);
              out.push({
                label: b.label,
                slug: b.slug,
                color: b.content_type === "audiobook" ? t.pur || t.accent : t.accent,
              });
            }
            return out.map((j) => (
              <button
                key={j.slug}
                onClick={() => {
                  const el = document.getElementById(`block-${j.slug}`);
                  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
                }}
                style={{
                  padding: "6px 14px",
                  background: j.color + "22",
                  color: j.color,
                  border: `1px solid ${j.color}44`,
                  borderRadius: 5,
                  fontSize: 13,
                  fontWeight: 600,
                  cursor: "pointer",
                  whiteSpace: "nowrap",
                  minHeight: 32,
                }}
              >
                {j.label}
              </button>
            ));
          })()}
        </div>
      ) : null}

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
