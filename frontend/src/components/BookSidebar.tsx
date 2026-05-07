// Right-side detail panel for a single book.
//
// Opened by any list-style page (Hidden, Books, MAM, Author Detail)
// when a row is clicked. Displays full metadata, supports inline edit
// via the PUT /books/{id} endpoint, surfaces the cross-library "Also
// Available As…" row when the book has a work sibling in a different
// format, and exposes the MAM / pipeline / Calibre Web action set.
//
// The panel mounts/unmounts in place — the `closing` prop from the
// parent drives the slide-out animation because the parent owns the
// "should this sidebar exist" decision.
import { useEffect, useState } from "react";
import { useTheme } from "../theme";
import { useViewport } from "../hooks/useViewport";
import { useMobileCodepath } from "./mobile";
import { api } from "../api";
import { Ic } from "../icons";
import { fmtDate } from "../lib/format";
import { openCoverLightbox } from "../lib/lightbox";
import { toast } from "../lib/toast";
import { Btn } from "./Btn";
import { Spin } from "./Spin";
import { SBRow } from "./SBRow";
import { BufferInsufficientBanner } from "./BufferInsufficientBanner";
import { CompareModal } from "./CompareModal";
import { economyApi, type PreflightResponse } from "../lib/economyApi";
import type {
  Book,
  BookAction,
  BookActionHandler,
  MamStatusResponse,
  WorkSibling,
} from "../types";
import { EVT } from "../types";

interface BookSidebarProps {
  book: Book;
  closing: boolean;
  onClose: () => void;
  onAction: BookActionHandler;
  onEdit: () => void | Promise<void>;
}

// The PUT /books/{id} endpoint accepts a sparse patch of these keys.
// Kept as a local interface so we can type the edit-mode form state
// without polluting the shared Book type with UI-only strings.
interface EditFields {
  title: string;
  description: string;
  pub_date: string;
  expected_date: string;
  isbn: string;
  series_name: string;
  series_index: string;
  is_unreleased: boolean;
  mam_url: string;
}

// Inline series-suggestion card shape — returned by
// /discovery/series-suggestions/by-book/{id} as `{suggestion: ... | null}`.
interface SeriesSuggestion {
  id: number;
  status: string;
  current_series_name: string | null;
  current_series_index: number | null;
  suggested_series_name: string | null;
  suggested_series_index: number | null;
  sources_agreeing: string[];
}

type SuggestionAction = "apply" | "ignore" | "delete";

interface SettingsResponse {
  calibre_web_url?: string;
  abs_web_url?: string;
}

interface PipelineStatusResponse {
  configured?: boolean;
  reachable?: boolean;
}

interface MamScanResult {
  status?: "found" | "possible" | "not_found";
  match_pct?: number | string;
}

interface MamScanResponse {
  error?: string;
  results?: MamScanResult[];
}

interface SendToPipelineResponse {
  sent: number;
  message?: string;
}

// Per-source badge palette for the "Metadata" row. `manual` is the
// fallback for links that don't match a known provider.
type SourceKey =
  | "goodreads"
  | "hardcover"
  | "kobo"
  | "amazon"
  | "ibdb"
  | "google_books"
  | "manual";

interface BadgeColor {
  bg: string;
  fg: string;
  br: string;
}

export function BookSidebar({
  book,
  closing: parentClosing,
  onClose,
  onAction,
  onEdit,
}: BookSidebarProps) {
  const t = useTheme();
  const vp = useViewport();
  // Use the full mobile codepath (phones + iPads + any touch device)
  // so iPad portrait doesn't get the cramped 420px side panel. Width
  // alone misses iPad landscape (>1024px); pointer:coarse covers it.
  const isMobile = useMobileCodepath(vp);
  const [mounted, setMounted] = useState(false);
  const [editing, setEditing] = useState(false);
  const [compareOpen, setCompareOpen] = useState(false);
  const [ef, setEf] = useState<EditFields>({
    title: "",
    description: "",
    pub_date: "",
    expected_date: "",
    isbn: "",
    series_name: "",
    series_index: "",
    is_unreleased: false,
    mam_url: "",
  });
  const [saving, setSaving] = useState(false);
  const [cwUrl, setCwUrl] = useState("");
  const [pipelineReady, setPipelineReady] = useState(false);
  const [absUrl, setAbsUrl] = useState("");
  const [mamScanning, setMamScanning] = useState(false);
  const [mamDeciding, setMamDeciding] = useState(false);
  const [mamOn, setMamOn] = useState(false);
  const [suggestion, setSuggestion] = useState<SeriesSuggestion | null>(null);
  const [sugBusy, setSugBusy] = useState<SuggestionAction | null>(null);
  const [sending, setSending] = useState(false);

  // Economy offers — the "use wedge" / "buy personal FL" checkboxes
  // only render when the user has opted into those offers via
  // MamPage. `preflight` caches the result of the most recent buffer
  // gate check for this book so the BufferInsufficientBanner has
  // something to render.
  const [offerWedge, setOfferWedge] = useState(false);
  const [offerFl, setOfferFl] = useState(false);
  const [bufferGateOn, setBufferGateOn] = useState(false);
  const [useWedgeChecked, setUseWedgeChecked] = useState(false);
  const [buyFlChecked, setBuyFlChecked] = useState(false);
  const [preflight, setPreflight] = useState<PreflightResponse | null>(null);

  useEffect(() => {
    requestAnimationFrame(() => setMounted(true));
    return () => setMounted(false);
  }, []);

  useEffect(() => {
    api
      .get<SettingsResponse>("/discovery/settings")
      .then((s) => {
        setCwUrl(s.calibre_web_url || "");
        setAbsUrl(s.abs_web_url || "");
      })
      .catch(() => {});
    api
      .get<PipelineStatusResponse>("/discovery/pipeline/status")
      .then((r) => setPipelineReady(!!r.configured && !!r.reachable))
      .catch(() => {});

    // Economy offers config — cheap one-shot fetch. Failures are
    // non-blocking (the checkboxes just stay hidden, matching the
    // pre-commit-7 UX).
    economyApi
      .getConfig()
      .then((cfg) => {
        setOfferWedge(!!cfg.mam_economy_manual_wedge_offer_enabled);
        setOfferFl(!!cfg.mam_economy_fl_wedge_offer_enabled);
        setBufferGateOn(!!cfg.mam_economy_buffer_gate_enabled);
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    api
      .get<MamStatusResponse>("/discovery/mam/status")
      .then((r) => setMamOn(!!r.enabled))
      .catch(() => {});
  }, []);

  // Fetch the active series-suggestion (if any) for this book when the
  // sidebar opens. The endpoint returns `{suggestion: null}` rather than
  // 404 when nothing exists, so we always reach a deterministic terminal
  // state without branching on HTTP status.
  useEffect(() => {
    if (!book?.id) {
      setSuggestion(null);
      return;
    }
    let cancelled = false;
    api
      .get<{ suggestion: SeriesSuggestion | null }>(
        `/discovery/series-suggestions/by-book/${book.id}`,
      )
      .then((r) => {
        if (!cancelled) setSuggestion(r.suggestion || null);
      })
      .catch(() => {
        if (!cancelled) setSuggestion(null);
      });
    return () => {
      cancelled = true;
    };
  }, [book?.id]);

  const sugAction = async (action: SuggestionAction) => {
    if (!suggestion || sugBusy) return;
    setSugBusy(action);
    try {
      if (action === "apply")
        await api.post(`/discovery/series-suggestions/${suggestion.id}/apply`);
      else if (action === "ignore")
        await api.post(`/discovery/series-suggestions/${suggestion.id}/ignore`);
      else if (action === "delete")
        await api.del(`/discovery/series-suggestions/${suggestion.id}`);
      try {
        window.dispatchEvent(new CustomEvent(EVT.SuggestionsChanged));
      } catch {
        /* ignore */
      }
      setSuggestion(null);
      if (action === "apply" && onEdit) await onEdit();
    } catch (e) {
      alert(`${action} failed: ${(e as Error).message || e}`);
    }
    setSugBusy(null);
  };

  // Approve a "possible" match as Found, or Remove the MAM link
  // entirely (clears URL and flips status to "not_found"). Uses the
  // existing PUT /discovery/books/{id} endpoint — sending mam_url=""
  // clears all three mam fields server-side (see update_book in
  // app/discovery/routers/books.py).
  const decideMam = async (action: "approve" | "remove") => {
    if (mamDeciding) return;
    if (action === "remove") {
      if (!confirm("Remove this book's MAM link? It will be marked Not Found.")) return;
    }
    setMamDeciding(true);
    try {
      const payload =
        action === "approve"
          ? { mam_url: book.mam_url || "" }
          : { mam_url: "" };
      await api.put(`/discovery/books/${book.id}`, payload);
      toast.success(action === "approve" ? "Approved as Found" : "Removed");
      // Close immediately — the sidebar's book prop won't refresh in
      // place, and awaiting onEdit() before unlocking the UI made the
      // click feel sluggish. Fire onEdit in the background so the
      // parent list reloads while the user watches the tile update.
      onClose();
      if (onEdit) {
        Promise.resolve(onEdit()).catch(() => {
          /* background refresh — errors surface on the parent list */
        });
      }
    } catch (e) {
      toast.error(`Action failed: ${(e as Error).message || e}`);
      setMamDeciding(false);
    }
  };

  const rescanMam = async () => {
    if (mamScanning) return;
    setMamScanning(true);
    try {
      const r = await api.post<MamScanResponse>("/discovery/books/scan-mam", {
        book_ids: [book.id],
      });
      if (r.error) {
        alert(`MAM scan failed: ${r.error}`);
      } else {
        const res = (r.results && r.results[0]) || {};
        const label =
          res.status === "found"
            ? "Found ✓"
            : res.status === "possible"
            ? `Possible (${res.match_pct || "?"}%)`
            : res.status === "not_found"
            ? "Not on MAM"
            : "Scan complete";
        alert(`MAM ${label}`);
        if (onEdit) await onEdit();
      }
    } catch (e) {
      alert(`MAM scan failed: ${(e as Error).message || e}`);
    }
    setMamScanning(false);
  };

  const sendToPipeline = async () => {
    if (sending) return;
    setSending(true);
    setPreflight(null);

    // Buffer-gate preflight: only when the gate is enabled AND the
    // book has a MAM torrent ID we can probe. A failed preflight
    // (no torrent ID, MAM offline, etc.) falls through to the
    // normal grab — the server-side gate is authoritative.
    if (bufferGateOn && book.mam_torrent_id) {
      try {
        const match = /(\d+)/.exec(String(book.mam_torrent_id));
        if (match) {
          const pf = await economyApi.preflight(match[1]);
          if (!pf.sufficient) {
            setPreflight(pf);
            setSending(false);
            return;
          }
        }
      } catch {
        /* preflight is best-effort — let the server decide */
      }
    }

    try {
      const r = await api.post<SendToPipelineResponse>(
        "/discovery/send-to-pipeline",
        {
          book_ids: [book.id],
          buy_personal_fl: buyFlChecked,
          use_wedge_override: useWedgeChecked,
        },
      );
      if (r.sent > 0) {
        alert("Sent to pipeline for download!");
        setUseWedgeChecked(false);
        setBuyFlChecked(false);
      } else {
        alert(r.message || "Failed to send");
      }
    } catch (e) {
      alert(`Send failed: ${(e as Error).message || e}`);
    }
    setSending(false);
  };

  if (!book) return null;

  const startEdit = () => {
    setEf({
      title: book.title || "",
      description: book.description || "",
      pub_date: book.pub_date || "",
      expected_date: book.expected_date || "",
      isbn: book.isbn || "",
      series_name: book.series_name || "",
      series_index:
        book.series_index !== undefined && book.series_index !== null
          ? String(book.series_index)
          : "",
      is_unreleased: !!book.is_unreleased,
      mam_url: book.mam_url || "",
    });
    setEditing(true);
  };

  const saveEdit = async () => {
    setSaving(true);
    try {
      await api.put(`/discovery/books/${book.id}`, ef);
      setEditing(false);
      if (onEdit) await onEdit();
    } catch (e) {
      // Surface the failure — we used to swallow this and the user
      // saw "the button does nothing" on validation errors (the
      // 2026-05-03 mam_url 400-on-search-URL bug masquerading as a
      // dead Save button). Toast the server message when there is
      // one, otherwise a generic fallback.
      const msg = (e as Error).message || "Save failed";
      toast.error(msg);
    }
    setSaving(false);
  };

  const upE = <K extends keyof EditFields>(k: K, v: EditFields[K]) =>
    setEf((p) => ({ ...p, [k]: v }));

  const ist: React.CSSProperties = {
    padding: "6px 8px",
    background: t.inp,
    border: `1px solid ${t.border}`,
    borderRadius: 6,
    color: t.text2,
    fontSize: 13,
    width: "100%",
  };

  // Cover src resolution mirrors BookViews.coverSrcFor: slug-scoped
  // endpoint when we know which library, bare endpoint otherwise, with
  // book.cover_url as a last-ditch fallback for unowned books.
  const coverSrc = (() => {
    const slugPath = book.library_slug
      ? `/api/discovery/covers/${book.library_slug}/${book.id}`
      : `/api/discovery/covers/${book.id}`;
    if (book.owned && (book.cover_path || book.audiobookshelf_id))
      return slugPath;
    return book.cover_url || slugPath;
  })();

  const hasCover = !!(
    book.cover_url ||
    book.cover_path ||
    book.audiobookshelf_id
  );

  // Cross-format availability — the current book's work is linked to
  // another format in a different library. One pill per unique sibling
  // content_type (dedupe by content_type because the same sibling may
  // appear in multiple libraries with the same format).
  const siblingPills: WorkSibling[] = (() => {
    const sibs = book.work_siblings;
    if (!Array.isArray(sibs) || sibs.length === 0) return [];
    const myType =
      book.content_type || (book.audiobookshelf_id ? "audiobook" : "ebook");
    const others = sibs.filter(
      (s) => s.content_type && s.content_type !== myType,
    );
    const seen = new Set<string>();
    return others.filter((s) => {
      if (seen.has(s.content_type)) return false;
      seen.add(s.content_type);
      return true;
    });
  })();

  // Metadata source badges. Parse `book.source_url` as either a JSON
  // object `{goodreads: url, hardcover: url, ...}` or a bare URL string
  // (legacy single-source shape) — the single-source flavor keys by
  // `book.source`. Badges render in a fixed visual order so the panel
  // doesn't reshuffle across books.
  const badgeColors: Record<SourceKey, BadgeColor> = {
    goodreads:    { bg: "#553b1a", fg: "#e8c070", br: "#88642a" },
    hardcover:    { bg: "#1a3355", fg: "#70a8e8", br: "#2a5588" },
    kobo:         { bg: "#1a4533", fg: "#70e8a8", br: "#2a8855" },
    amazon:       { bg: "#3d2e1a", fg: "#f0a83c", br: "#7a5c2a" },
    ibdb:         { bg: "#2a1a3d", fg: "#c070e8", br: "#5a2a88" },
    google_books: { bg: "#1a3333", fg: "#70c8e8", br: "#2a7788" },
    manual:       { bg: t.bg4,     fg: t.td,      br: t.border },
  };
  const metadataEntries: { name: SourceKey; url: string }[] = (() => {
    const order: SourceKey[] = [
      "goodreads", "hardcover", "kobo", "amazon", "ibdb", "google_books",
    ];
    let urls: Record<string, string> = {};
    try {
      urls = JSON.parse(book.source_url || "{}");
    } catch {
      if (book.source_url && book.source_url.startsWith("http")) {
        urls = { [book.source || "unknown"]: book.source_url };
      }
    }
    return order.filter((k) => urls[k]).map((k) => ({ name: k, url: urls[k] }));
  })();

  // Audiobook-specific block gate: fires when the row came from an
  // audiobook source OR carries any of the audiobook-only fields.
  // The second arm handles legacy cache rows that don't have a
  // library_slug stamp yet.
  const isAudiobookRow = !!(
    book.content_type === "audiobook" ||
    book.audiobookshelf_id ||
    book.narrator ||
    book.duration_sec ||
    book.asin ||
    book.audio_formats
  );

  const sourceLabel = book.owned
    ? book.source === "audiobookshelf" || book.audiobookshelf_id
      ? "Audiobookshelf"
      : book.source === "calibre" || book.calibre_id
      ? "Calibre"
      : book.source
      ? book.source[0].toUpperCase() + book.source.slice(1)
      : "Owned"
    : "Unowned";

  const fmtSuggestion = (
    name: string | null,
    idx: number | null,
  ) => (name ? (idx != null ? `${name} #${idx}` : name) : "standalone");

  return (
    <div
      style={{
        position: "fixed",
        top: 0,
        right: 0,
        // Mobile: full-viewport sheet so the sidebar fills the
        // screen instead of competing with the parent grid for ~10%
        // of the width. Desktop: 420px panel with a 90vw cap for
        // medium-narrow viewports.
        width: isMobile ? "100vw" : 420,
        maxWidth: isMobile ? "100vw" : "90vw",
        height: "100vh",
        background: t.bg2,
        borderLeft: isMobile ? "none" : `1px solid ${t.border}`,
        zIndex: 100,
        overflowY: "auto",
        padding: isMobile ? 14 : 24,
        display: "flex",
        flexDirection: "column",
        gap: 16,
        boxShadow: isMobile ? "none" : "-4px 0 20px rgba(0,0,0,0.3)",
        transform: parentClosing
          ? "translateX(100%)"
          : mounted
          ? "translateX(0)"
          : "translateX(100%)",
        transition: "transform 0.25s ease-out",
      }}
    >
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "flex-start",
          gap: 12,
        }}
      >
        <h2
          style={{
            fontSize: 18,
            fontWeight: 700,
            color: t.text,
            margin: 0,
            flex: 1,
            lineHeight: 1.3,
          }}
        >
          {editing ? (
            <input
              value={ef.title}
              onChange={(e) => upE("title", e.target.value)}
              style={{ ...ist, fontSize: 16, fontWeight: 700 }}
            />
          ) : (
            book.title
          )}
        </h2>
        <div
          className="sb-actions"
          style={{ display: "flex", gap: 8, flexShrink: 0 }}
        >
          {!editing && (
            <button
              onClick={startEdit}
              style={{
                background: t.bg4,
                border: `1px solid ${t.border}`,
                borderRadius: 8,
                cursor: "pointer",
                color: t.tg,
                padding: 8,
                minWidth: 36,
                minHeight: 36,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              {Ic.edit}
            </button>
          )}
          {!editing && (
            <button
              onClick={() => setCompareOpen(true)}
              title="Compare metadata across Seshat / Calibre / ABS"
              style={{
                background: t.bg4,
                border: `1px solid ${t.border}`,
                borderRadius: 8,
                cursor: "pointer",
                color: t.tg,
                padding: "8px 10px",
                minHeight: 36,
                fontSize: 12,
                fontWeight: 600,
              }}
            >
              Compare
            </button>
          )}
          <button
            onClick={onClose}
            style={{
              background: t.bg4,
              border: `1px solid ${t.border}`,
              borderRadius: 8,
              cursor: "pointer",
              color: t.tg,
              padding: 8,
              minWidth: 36,
              minHeight: 36,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            {Ic.x}
          </button>
        </div>
      </div>

      {hasCover ? (
        // Cover slot — fixed 2:3 portrait aspect ratio with the cover
        // letterboxed inside via object-fit:contain. Predictable size
        // regardless of the cover's natural aspect (some self-pubs
        // ship horizontal banner covers; some series ship 1:1
        // squares). Banner covers get clean letterboxing with the
        // blurred-self backdrop filling the empty space; standard 2:3
        // portrait covers fill the slot edge-to-edge.
        //
        // Click the foreground cover → PhotoSwipe lightbox for full-
        // size zoom/pan/pinch.
        <div
          style={{
            position: "relative",
            width: "100%",
            // aspect-ratio establishes the slot shape (2:3 portrait
            // is the standard book-cover ratio). On mobile / narrow
            // sidebars the natural-aspect height fits cleanly. On
            // desktop sidebars (~370px wide) 2:3 yields ~555px tall,
            // which is the right "feels like a cover" size — capped
            // at 600 so absurdly wide sidebars don't produce a
            // wall-of-cover before the metadata rows.
            aspectRatio: "2 / 3",
            maxHeight: 600,
            // Sidebar is `display: flex, flexDirection: column` —
            // without flex-shrink:0 the slot was getting compressed
            // by the surrounding metadata rows when the sidebar
            // viewport was tight. Lock the slot at its computed
            // size; the sidebar already scrolls.
            flexShrink: 0,
            borderRadius: 8,
            overflow: "hidden",
            background: t.bg4,
          }}
        >
          <div
            aria-hidden="true"
            style={{
              position: "absolute",
              inset: 0,
              backgroundImage: `url(${coverSrc})`,
              backgroundSize: "cover",
              backgroundPosition: "center",
              filter: "blur(28px) saturate(1.1)",
              transform: "scale(1.15)",
              opacity: 0.55,
            }}
          />
          <div
            aria-hidden="true"
            style={{
              position: "absolute",
              inset: 0,
              background: `linear-gradient(180deg, transparent 0%, transparent 55%, ${t.bg2} 100%)`,
            }}
          />
          <img
            src={coverSrc}
            alt=""
            onClick={() => openCoverLightbox(coverSrc)}
            onLoad={(e) => {
              (e.currentTarget as HTMLImageElement).style.opacity = "1";
            }}
            title="Click to enlarge"
            style={{
              position: "absolute",
              inset: 0,
              width: "100%",
              height: "100%",
              objectFit: "contain",
              zIndex: 1,
              cursor: "zoom-in",
              opacity: 0,
              transition: "opacity 0.35s ease-out",
            }}
          />
        </div>
      ) : null}

      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        <SBRow label="Author" value={book.author_name} />

        {book.series_name ? (
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "baseline",
            }}
          >
            <span
              style={{
                fontSize: 11,
                fontWeight: 600,
                color: t.tg,
                textTransform: "uppercase",
              }}
            >
              Series
            </span>
            <span style={{ fontSize: 13, color: t.purt, textAlign: "right" }}>
              {book.series_name}
              {book.series_index ? (
                <span style={{ color: t.td }}>
                  {" "}
                  (#{book.series_index}
                  {book.mainline_total ? ` of ${book.mainline_total}` : ""})
                </span>
              ) : null}
            </span>
          </div>
        ) : null}

        {editing ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <div>
              <span
                style={{
                  fontSize: 11,
                  fontWeight: 600,
                  color: t.tg,
                  textTransform: "uppercase",
                }}
              >
                Published
              </span>
              <input
                type="date"
                value={ef.pub_date}
                onChange={(e) => upE("pub_date", e.target.value)}
                style={ist}
              />
            </div>
            <div>
              <span
                style={{
                  fontSize: 11,
                  fontWeight: 600,
                  color: t.tg,
                  textTransform: "uppercase",
                }}
              >
                Expected Date
              </span>
              <input
                type="date"
                value={ef.expected_date}
                onChange={(e) => upE("expected_date", e.target.value)}
                style={ist}
              />
            </div>
            <div>
              <span
                style={{
                  fontSize: 11,
                  fontWeight: 600,
                  color: t.tg,
                  textTransform: "uppercase",
                }}
              >
                ISBN
              </span>
              <input
                value={ef.isbn}
                onChange={(e) => upE("isbn", e.target.value)}
                style={ist}
              />
            </div>
            <div>
              <span
                style={{
                  fontSize: 11,
                  fontWeight: 600,
                  color: t.tg,
                  textTransform: "uppercase",
                }}
              >
                Series
              </span>
              <input
                value={ef.series_name}
                onChange={(e) => upE("series_name", e.target.value)}
                placeholder="Enter series name (or leave empty for standalone)"
                style={ist}
              />
              <span
                style={{
                  fontSize: 10,
                  color: t.tg,
                  marginTop: 2,
                  display: "block",
                }}
              >
                Type a series name to assign. Matches existing series
                (case-insensitive) or creates new. Clear to make standalone.
              </span>
            </div>
            <div>
              <span
                style={{
                  fontSize: 11,
                  fontWeight: 600,
                  color: t.tg,
                  textTransform: "uppercase",
                }}
              >
                Series #
              </span>
              <input
                type="number"
                value={ef.series_index}
                onChange={(e) => upE("series_index", e.target.value)}
                style={ist}
              />
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <input
                type="checkbox"
                checked={ef.is_unreleased}
                onChange={(e) => upE("is_unreleased", e.target.checked)}
              />
              <span style={{ fontSize: 12, color: t.text2 }}>Unreleased</span>
            </div>
            <SourceUrlEditor
              bookId={book.id}
              sourceUrlJson={book.source_url}
              onChange={onEdit}
              theme={t}
              ist={ist}
            />

            <div>
              <span
                style={{
                  fontSize: 11,
                  fontWeight: 600,
                  color: t.tg,
                  textTransform: "uppercase",
                }}
              >
                MAM URL
              </span>
              <input
                value={ef.mam_url}
                onChange={(e) => upE("mam_url", e.target.value)}
                placeholder="https://www.myanonamouse.net/t/123456"
                style={ist}
              />
              <span
                style={{
                  fontSize: 10,
                  color: t.tg,
                  marginTop: 2,
                  display: "block",
                }}
              >
                Paste a MAM torrent URL to set status to Found. Clear to reset.
              </span>
            </div>
            <div>
              <span
                style={{
                  fontSize: 11,
                  fontWeight: 600,
                  color: t.tg,
                  textTransform: "uppercase",
                }}
              >
                Description
              </span>
              <textarea
                value={ef.description}
                onChange={(e) => upE("description", e.target.value)}
                rows={4}
                style={{ ...ist, resize: "vertical" }}
              />
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              <Btn
                size="sm"
                variant="accent"
                onClick={saveEdit}
                disabled={saving}
              >
                {saving ? <Spin /> : "Save"}
              </Btn>
              <Btn size="sm" variant="ghost" onClick={() => setEditing(false)}>
                Cancel
              </Btn>
            </div>
          </div>
        ) : (
          <>
            <SBRow
              label="Published"
              value={
                book.pub_date
                  ? fmtDate(book.pub_date)
                  : book.expected_date
                  ? `Expected: ${fmtDate(book.expected_date)}`
                  : "Unknown"
              }
            />
            <SBRow
              label="Status"
              value={book.owned ? "Owned" : "Missing"}
              color={book.owned ? t.grnt : t.ylwt}
            />
            <SBRow
              label="Source"
              value={sourceLabel}
              color={book.owned ? t.td : t.tg}
            />

            {cwUrl && book.owned && book.calibre_id ? (
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                }}
              >
                <span
                  style={{
                    fontSize: 11,
                    fontWeight: 600,
                    color: t.tg,
                    textTransform: "uppercase",
                  }}
                >
                  Calibre Web
                </span>
                <a
                  href={`${cwUrl.replace(/\/$/, "")}/book/${book.calibre_id}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{
                    fontSize: 13,
                    color: t.accent,
                    textDecoration: "none",
                    display: "flex",
                    alignItems: "center",
                    gap: 4,
                  }}
                >
                  Open in Calibre Web <span style={{ fontSize: 10 }}>↗</span>
                </a>
              </div>
            ) : null}

            {absUrl && book.owned && book.audiobookshelf_id ? (
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                }}
              >
                <span
                  style={{
                    fontSize: 11,
                    fontWeight: 600,
                    color: t.tg,
                    textTransform: "uppercase",
                  }}
                >
                  Audiobookshelf
                </span>
                <a
                  href={`${absUrl.replace(/\/$/, "")}/item/${book.audiobookshelf_id}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{
                    fontSize: 13,
                    color: t.pur || t.accent,
                    textDecoration: "none",
                    display: "flex",
                    alignItems: "center",
                    gap: 4,
                  }}
                >
                  Open in Audiobookshelf{" "}
                  <span style={{ fontSize: 10 }}>↗</span>
                </a>
              </div>
            ) : null}

            {/* Also Available As… — non-clickable for v1; a future pass
                can wire it to swap the sidebar to the sibling book. */}
            {siblingPills.length > 0 ? (
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  flexWrap: "wrap",
                  gap: 4,
                }}
              >
                <span
                  style={{
                    fontSize: 11,
                    fontWeight: 600,
                    color: t.tg,
                    textTransform: "uppercase",
                  }}
                >
                  Also Available
                </span>
                <div style={{ display: "flex", gap: 4 }}>
                  {siblingPills.map((s) => {
                    const isAudio = s.content_type === "audiobook";
                    const color = isAudio ? t.pur || t.accent : t.jade;
                    return (
                      <span
                        key={s.content_type}
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 4,
                          padding: "2px 8px",
                          borderRadius: 4,
                          fontSize: 12,
                          fontWeight: 600,
                          background: color + "22",
                          color,
                          border: `1px solid ${color}44`,
                        }}
                      >
                        {isAudio ? "🎧" : "📖"}{" "}
                        {isAudio ? "Audiobook" : "Ebook"}
                      </span>
                    );
                  })}
                </div>
              </div>
            ) : null}

            {metadataEntries.length > 0 ? (
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  flexWrap: "wrap",
                  gap: 4,
                }}
              >
                <span
                  style={{
                    fontSize: 11,
                    fontWeight: 600,
                    color: t.tg,
                    textTransform: "uppercase",
                  }}
                >
                  Metadata
                </span>
                <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                  {metadataEntries.map((e) => {
                    const c = badgeColors[e.name] || badgeColors.manual;
                    return (
                      <a
                        key={e.name}
                        href={e.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 4,
                          padding: "3px 10px",
                          borderRadius: 5,
                          fontSize: 12,
                          fontWeight: 600,
                          textDecoration: "none",
                          background: c.bg,
                          color: c.fg,
                          border: `1px solid ${c.br}`,
                        }}
                      >
                        {e.name}
                        <span style={{ fontSize: 10, opacity: 0.7 }}>↗</span>
                      </a>
                    );
                  })}
                </div>
              </div>
            ) : null}

            {/* Inline series-suggestion card. Only renders when an
                active (pending or ignored) suggestion exists for this
                book. Apply/Ignore/Delete hit the same endpoints
                SuggestionsPage uses and dispatch the same
                EVT.SuggestionsChanged event so the navbar badge count
                stays in sync. */}
            {suggestion
              ? (() => {
                  const isPending = suggestion.status === "pending";
                  const sources = Array.isArray(suggestion.sources_agreeing)
                    ? suggestion.sources_agreeing
                    : [];
                  return (
                    <div
                      style={{
                        background: t.accent + "12",
                        border: `1px solid ${t.accent}44`,
                        borderRadius: 10,
                        padding: "12px 14px",
                        display: "flex",
                        flexDirection: "column",
                        gap: 8,
                      }}
                    >
                      <div
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 6,
                        }}
                      >
                        <span style={{ fontSize: 14 }}>💡</span>
                        <span
                          style={{
                            fontSize: 12,
                            fontWeight: 700,
                            color: t.accent,
                            textTransform: "uppercase",
                            letterSpacing: "0.06em",
                          }}
                        >
                          Series Suggestion
                        </span>
                        {!isPending ? (
                          <span
                            style={{
                              fontSize: 10,
                              fontWeight: 600,
                              color: t.tg,
                              textTransform: "uppercase",
                              padding: "1px 6px",
                              borderRadius: 4,
                              background: t.bg4,
                              border: `1px solid ${t.borderL}`,
                            }}
                          >
                            {suggestion.status}
                          </span>
                        ) : null}
                      </div>
                      <div
                        style={{
                          fontSize: 12,
                          color: t.text2,
                          lineHeight: 1.5,
                        }}
                      >
                        <span style={{ color: t.tg }}>Currently:</span>{" "}
                        <span style={{ color: t.text2 }}>
                          {fmtSuggestion(
                            suggestion.current_series_name,
                            suggestion.current_series_index,
                          )}
                        </span>
                        <br />
                        <span style={{ color: t.tg }}>Suggested:</span>{" "}
                        <span
                          style={{ color: t.accent, fontWeight: 600 }}
                        >
                          {fmtSuggestion(
                            suggestion.suggested_series_name,
                            suggestion.suggested_series_index,
                          )}
                        </span>
                      </div>
                      <div style={{ fontSize: 11, color: t.tg }}>
                        Agreed by: {sources.join(", ") || "—"}
                      </div>
                      <div
                        style={{ display: "flex", gap: 6, flexWrap: "wrap" }}
                      >
                        {isPending ? (
                          <>
                            <Btn
                              size="sm"
                              variant="accent"
                              onClick={() => sugAction("apply")}
                              disabled={!!sugBusy}
                            >
                              {sugBusy === "apply" ? (
                                <Spin />
                              ) : (
                                <>
                                  {Ic.check} Apply
                                </>
                              )}
                            </Btn>
                            <Btn
                              size="sm"
                              variant="ghost"
                              onClick={() => sugAction("ignore")}
                              disabled={!!sugBusy}
                            >
                              {sugBusy === "ignore" ? <Spin /> : "Ignore"}
                            </Btn>
                          </>
                        ) : null}
                        <Btn
                          size="sm"
                          variant="ghost"
                          onClick={() => sugAction("delete")}
                          disabled={!!sugBusy}
                          style={{ color: t.redt }}
                        >
                          {sugBusy === "delete" ? <Spin /> : Ic.trash}
                        </Btn>
                      </div>
                    </div>
                  );
                })()
              : null}

            {mamOn || book.mam_status ? (
              <div>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    gap: 4,
                  }}
                >
                  <span
                    style={{
                      fontSize: 11,
                      fontWeight: 600,
                      color: t.tg,
                      textTransform: "uppercase",
                    }}
                  >
                    MAM
                  </span>
                  <div
                    style={{ display: "flex", alignItems: "center", gap: 6 }}
                  >
                    {book.mam_status === "not_found" ? (
                      <a
                        href={book.mam_url || "#"}
                        target="_blank"
                        rel="noopener noreferrer"
                        title="Search MAM for this title (no match found during last scan)"
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 4,
                          padding: "3px 10px",
                          borderRadius: 5,
                          fontSize: 12,
                          fontWeight: 600,
                          textDecoration: "none",
                          background: "#3a1a1a",
                          color: t.redt,
                          border: "1px solid #882a2a",
                        }}
                      >
                        {book.owned ? "Not Found (upload)" : "Not Found"}
                        <span style={{ fontSize: 10, opacity: 0.7 }}>↗</span>
                      </a>
                    ) : book.mam_url ? (
                      <a
                        href={book.mam_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 4,
                          padding: "3px 10px",
                          borderRadius: 5,
                          fontSize: 12,
                          fontWeight: 600,
                          textDecoration: "none",
                          background:
                            book.mam_status === "found"
                              ? "#1a3a1a"
                              : "#3a3a1a",
                          color:
                            book.mam_status === "found" ? t.grnt : t.ylwt,
                          border: `1px solid ${
                            book.mam_status === "found"
                              ? "#2a882a"
                              : "#88882a"
                          }`,
                        }}
                      >
                        {book.mam_status === "found" ? "Found" : "Possible"}
                        <span style={{ fontSize: 10, opacity: 0.7 }}>↗</span>
                      </a>
                    ) : (
                      <span
                        style={{
                          fontSize: 12,
                          color: t.tg,
                          fontStyle: "italic",
                        }}
                      >
                        Not scanned
                      </span>
                    )}
                    {mamOn ? (
                      <Btn
                        size="sm"
                        onClick={rescanMam}
                        disabled={mamScanning}
                        title={
                          book.mam_status
                            ? "Re-scan this book against MAM"
                            : "Scan this book against MAM"
                        }
                      >
                        {mamScanning ? <Spin /> : "↻"}{" "}
                        {book.mam_status ? "Re-scan" : "Scan"}
                      </Btn>
                    ) : null}
                    {pipelineReady &&
                    book.mam_status === "found" &&
                    !book.mam_my_snatched ? (
                      <Btn
                        size="sm"
                        onClick={sendToPipeline}
                        disabled={sending}
                        style={{
                          background: t.accent + "22",
                          color: t.accent,
                          border: `1px solid ${t.accent}44`,
                        }}
                      >
                        {sending ? <Spin /> : "⬇"} Send to pipeline
                      </Btn>
                    ) : null}
                  </div>
                </div>

                {/* Per-grab offer checkboxes (commit 7). On their
                    own row so narrow sidebar widths don't wrap the
                    Found/Re-scan/Send buttons around them. Only
                    renders when the user has enabled one of the
                    offers under MamPage → Auto-buy → Per-grab offers
                    AND the send-to-pipeline button would actually
                    show up on the row above. */}
                {pipelineReady &&
                book.mam_status === "found" &&
                !book.mam_my_snatched &&
                (offerWedge || offerFl) ? (
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "flex-end",
                      gap: 14,
                      marginTop: 4,
                      fontSize: 11,
                      color: t.tg,
                      flexWrap: "wrap",
                    }}
                  >
                    {offerWedge && (
                      <label
                        style={{
                          display: "flex",
                          gap: 4,
                          alignItems: "center",
                          cursor: "pointer",
                        }}
                      >
                        <input
                          type="checkbox"
                          checked={useWedgeChecked}
                          onChange={(e) =>
                            setUseWedgeChecked(e.target.checked)
                          }
                        />
                        Use wedge
                      </label>
                    )}
                    {offerFl && (
                      <label
                        style={{
                          display: "flex",
                          gap: 4,
                          alignItems: "center",
                          cursor: "pointer",
                        }}
                        title="Spend 50,000 BP to flag this torrent as personal freeleech on MAM"
                      >
                        <input
                          type="checkbox"
                          checked={buyFlChecked}
                          onChange={(e) => setBuyFlChecked(e.target.checked)}
                        />
                        Buy personal FL (50k BP)
                      </label>
                    )}
                  </div>
                ) : null}

                {book.mam_url &&
                (book.mam_formats ||
                  book.mam_has_multiple ||
                  book.mam_my_snatched) ? (
                  <div
                    style={{
                      display: "flex",
                      gap: 8,
                      alignItems: "center",
                      justifyContent: "flex-end",
                      marginTop: 3,
                      flexWrap: "wrap",
                    }}
                  >
                    {book.mam_formats ? (
                      <span
                        style={{
                          fontSize: 11,
                          color: t.td,
                          fontWeight: 500,
                          textTransform: "uppercase",
                          letterSpacing: "0.03em",
                        }}
                      >
                        {book.mam_formats.split(",").join(" · ")}
                      </span>
                    ) : null}
                    {book.mam_my_snatched ? (
                      <span
                        title="You've already snatched this torrent on MAM"
                        style={{
                          fontSize: 11,
                          padding: "1px 6px",
                          borderRadius: 4,
                          background: t.grn + "22",
                          color: t.grnt,
                          border: `1px solid ${t.grn}44`,
                        }}
                      >
                        Already snatched
                      </span>
                    ) : null}
                    {book.mam_has_multiple ? (
                      <span
                        style={{
                          fontSize: 11,
                          padding: "1px 6px",
                          borderRadius: 4,
                          background: t.ylw + "22",
                          color: t.ylwt,
                          border: `1px solid ${t.ylw}33`,
                        }}
                      >
                        Multiple uploads
                      </span>
                    ) : null}
                  </div>
                ) : null}
              </div>
            ) : null}

            {book.rating ? (
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "baseline",
                }}
              >
                <span
                  style={{
                    fontSize: 11,
                    fontWeight: 600,
                    color: t.tg,
                    textTransform: "uppercase",
                  }}
                >
                  Rating
                </span>
                <span style={{ fontSize: 13, color: t.ylwt }}>
                  {"★".repeat(Math.round(book.rating))}
                  {"☆".repeat(5 - Math.round(book.rating))}{" "}
                  <span style={{ fontSize: 11, color: t.td }}>
                    ({book.rating})
                  </span>
                </span>
              </div>
            ) : null}

            {book.isbn ? <SBRow label="ISBN" value={book.isbn} /> : null}
            {book.page_count ? (
              <SBRow label="Pages" value={String(book.page_count)} />
            ) : null}
            {book.language ? (
              <SBRow label="Language" value={book.language} />
            ) : null}
            {book.publisher ? (
              <SBRow label="Publisher" value={book.publisher} />
            ) : null}
            {book.formats ? (
              <SBRow label="Formats" value={book.formats} />
            ) : null}

            {/* Audiobook-specific rows — only populated when the row
                came from an audiobook source or carries any of the
                audiobook-only fields. */}
            {isAudiobookRow ? (
              <>
                {book.narrator ? (
                  <SBRow label="Narrator" value={book.narrator} />
                ) : null}
                {book.duration_sec ? (
                  <SBRow
                    label="Duration"
                    value={`${Math.floor(book.duration_sec / 3600)}h ${Math.round(
                      (book.duration_sec % 3600) / 60,
                    )}m`}
                  />
                ) : null}
                {book.audio_formats ? (
                  <SBRow label="Audio Formats" value={book.audio_formats} />
                ) : null}
                {book.asin ? (
                  <SBRow
                    label="ASIN"
                    value={
                      <a
                        href={`https://www.audible.com/pd/${book.asin}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        style={{ color: t.accent, textDecoration: "none" }}
                      >
                        {book.asin}{" "}
                        <span style={{ fontSize: 10, opacity: 0.7 }}>↗</span>
                      </a>
                    }
                  />
                ) : null}
                {book.abridged ? (
                  <SBRow
                    label="Edition"
                    value={
                      <span style={{ color: t.ylwt, fontWeight: 600 }}>
                        Abridged
                      </span>
                    }
                  />
                ) : null}
              </>
            ) : null}

            {book.library_name ? (
              <SBRow
                label="Library"
                value={
                  <span
                    style={{
                      fontSize: 12,
                      padding: "1px 8px",
                      borderRadius: 4,
                      background:
                        book.content_type === "audiobook" ? t.purb : t.cyanb,
                      color:
                        book.content_type === "audiobook" ? t.purt : t.cyant,
                      border: `1px solid ${
                        book.content_type === "audiobook"
                          ? t.pur + "33"
                          : t.cyan + "33"
                      }`,
                    }}
                  >
                    {book.content_type === "audiobook" ? "🎧" : "📖"}{" "}
                    {book.library_name}
                  </span>
                }
              />
            ) : null}

            {book.tags ? (
              <div style={{ marginTop: 4 }}>
                <div
                  style={{
                    fontSize: 11,
                    fontWeight: 600,
                    color: t.tg,
                    textTransform: "uppercase",
                    marginBottom: 4,
                  }}
                >
                  Tags
                </div>
                <div
                  style={{ display: "flex", flexWrap: "wrap", gap: 4 }}
                >
                  {book.tags.split(", ").map((tag) => (
                    <span
                      key={tag}
                      style={{
                        padding: "2px 8px",
                        borderRadius: 4,
                        fontSize: 11,
                        background: t.purb,
                        color: t.purt,
                        border: `1px solid ${t.pur}33`,
                      }}
                    >
                      {tag}
                    </span>
                  ))}
                </div>
              </div>
            ) : null}

            {book.description ? (
              <div style={{ marginTop: 4 }}>
                <div
                  style={{
                    fontSize: 11,
                    fontWeight: 600,
                    color: t.tg,
                    textTransform: "uppercase",
                    marginBottom: 4,
                  }}
                >
                  Description
                </div>
                <p
                  style={{
                    fontSize: 13,
                    color: t.td,
                    lineHeight: 1.5,
                    margin: 0,
                    maxHeight: 200,
                    overflow: "auto",
                  }}
                >
                  {book.description}
                </p>
              </div>
            ) : null}
          </>
        )}
      </div>

      {/* Possible-MAM decision row — only when scan returned a
          candidate that's waiting on user approval. Approve flips the
          existing URL's status to Found; Remove clears the URL
          entirely and marks Not Found. Sits just above the standard
          action row so it's visible without scrolling through the
          metadata rows. */}
      {!editing && book.mam_status === "possible" && book.mam_url ? (
        <div
          className="sb-actions"
          style={{
            display: "flex",
            gap: 8,
            marginTop: "auto",
            paddingTop: 12,
            borderTop: `1px solid ${t.borderL}`,
            flexWrap: "wrap",
          }}
        >
          <Btn
            size="sm"
            onClick={() => decideMam("approve")}
            disabled={mamDeciding}
            title="Confirm this match as Found without editing the URL"
            style={{
              background: t.grn + "22",
              color: t.grnt,
              border: `1px solid ${t.grn}44`,
            }}
          >
            {mamDeciding ? <Spin /> : null} Approve MAM
          </Btn>
          <Btn
            size="sm"
            onClick={() => decideMam("remove")}
            disabled={mamDeciding}
            title="Discard this match and mark the book as Not Found on MAM"
            style={{
              background: t.red + "22",
              color: t.redt,
              border: `1px solid ${t.red}44`,
            }}
          >
            {mamDeciding ? <Spin /> : null} Remove MAM
          </Btn>
        </div>
      ) : null}

      {/* Action row varies by book state:
          - hidden: Unhide-only
          - not-owned: Dismiss / Hide / Delete
          - owned (default): no actions, the top Edit button covers it */}
      {!editing && book.hidden ? (
        <div
          className="sb-actions"
          style={{
            display: "flex",
            gap: 8,
            marginTop: "auto",
            paddingTop: 12,
            borderTop: `1px solid ${t.borderL}`,
            flexWrap: "wrap",
          }}
        >
          <Btn
            size="sm"
            variant="accent"
            onClick={() => {
              onAction("unhide" as BookAction, book.id);
              onClose();
            }}
          >
            Unhide
          </Btn>
        </div>
      ) : !editing && !book.owned ? (
        <div
          className="sb-actions"
          style={{
            display: "flex",
            gap: 8,
            marginTop: "auto",
            paddingTop: 12,
            borderTop: `1px solid ${t.borderL}`,
            flexWrap: "wrap",
          }}
        >
          <Btn
            size="sm"
            onClick={() => {
              onAction("dismiss" as BookAction, book.id);
              onClose();
            }}
          >
            Dismiss
          </Btn>
          <Btn
            size="sm"
            onClick={() => {
              onAction("hide" as BookAction, book.id);
              onClose();
            }}
          >
            {Ic.hide} Hide
          </Btn>
          <Btn
            size="sm"
            onClick={() => {
              if (
                confirm(
                  `Delete "${book.title}" permanently? This cannot be undone.`,
                )
              ) {
                onAction("delete" as BookAction, book.id);
                onClose();
              }
            }}
            style={{
              background: "#6b2020",
              borderColor: "#8b3030",
              color: "#ff9090",
            }}
          >
            Delete
          </Btn>
        </div>
      ) : null}

      {preflight && (
        <div style={{ margin: "12px 14px 0" }}>
          <BufferInsufficientBanner
            preflight={preflight}
            onBufferReady={() => {
              setPreflight(null);
              sendToPipeline();
            }}
            onCancel={() => setPreflight(null)}
          />
        </div>
      )}

      {compareOpen ? (
        <CompareModal
          bookId={book.id}
          bookTitle={book.title}
          onClose={() => setCompareOpen(false)}
          onChanged={() => {
            // Refresh the parent list so the sidebar's `book` prop
            // catches up to whatever was pulled. Best-effort — the
            // CompareModal re-fetches its own state on every action.
            if (onEdit) {
              Promise.resolve(onEdit()).catch(() => {
                /* background refresh — surfaces on parent list */
              });
            }
          }}
        />
      ) : null}
    </div>
  );
}


// ─── v2.3.2 source URL editor ───────────────────────────────────────
//
// Replaces the single-input free-text source_url field. User sees a
// labeled row per existing source with a remove button, plus a single
// "paste any source URL + Add" row at the bottom. Backend
// (POST/DELETE /api/discovery/books/{bid}/source-urls) handles
// canonicalization (Goodreads slug-stripping, Amazon /dp/<ASIN>
// normalization, etc.) so the user just pastes whatever shape the
// source actually shows.

interface SourceUrlEditorProps {
  bookId: number;
  sourceUrlJson?: string | null;
  onChange?: () => Promise<void> | void;
  theme: ReturnType<typeof useTheme>;
  ist: React.CSSProperties;
}

const SOURCE_LABELS: Record<string, string> = {
  goodreads: "Goodreads",
  hardcover: "Hardcover",
  kobo: "Kobo",
  amazon: "Amazon",
  audible: "Audible",
  ibdb: "IBDB",
  google_books: "Google Books",
};

function parseSourceUrlField(raw: string | null | undefined): Record<string, string> {
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      // Strip non-string values (defensive — shouldn't happen
      // but legacy rows occasionally have weird shapes).
      const out: Record<string, string> = {};
      for (const [k, v] of Object.entries(parsed)) {
        if (typeof v === "string" && v) out[k] = v;
      }
      return out;
    }
  } catch {
    /* fall through to legacy bare-URL handling */
  }
  // Legacy plain-string format from pre-v1.x — surface it under
  // "manual" so the user can see + remove it. The remove call won't
  // actually match a known source key but the backend tolerates that
  // case.
  if (typeof raw === "string" && raw.startsWith("http")) {
    return { manual: raw };
  }
  return {};
}

function SourceUrlEditor({
  bookId, sourceUrlJson, onChange, theme: t, ist,
}: SourceUrlEditorProps) {
  const [urls, setUrls] = useState<Record<string, string>>(() =>
    parseSourceUrlField(sourceUrlJson),
  );
  const [pendingUrl, setPendingUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Re-sync when the parent passes a different book.
  useEffect(() => {
    setUrls(parseSourceUrlField(sourceUrlJson));
    setPendingUrl("");
    setErr(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bookId, sourceUrlJson]);

  const handleAdd = async () => {
    const url = pendingUrl.trim();
    if (!url) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await api.post<{ source_url: Record<string, string> }>(
        `/discovery/books/${bookId}/source-urls`,
        { url },
      );
      setUrls(r.source_url || {});
      setPendingUrl("");
      if (onChange) await onChange();
    } catch (e) {
      const msg = (e as Error).message || "Failed to add URL";
      // Backend returns a 400 with a useful message on unrecognized
      // URLs — surface it inline rather than via toast so the user
      // can fix the paste in place.
      setErr(msg);
    }
    setBusy(false);
  };

  const handleRemove = async (sourceName: string) => {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.del<{ source_url: Record<string, string> }>(
        `/discovery/books/${bookId}/source-urls/${encodeURIComponent(sourceName)}`,
      );
      setUrls(r.source_url || {});
      if (onChange) await onChange();
    } catch (e) {
      toast.error((e as Error).message || "Failed to remove URL");
    }
    setBusy(false);
  };

  const orderedKeys = [
    "goodreads", "hardcover", "kobo", "amazon", "audible",
    "ibdb", "google_books",
  ].filter((k) => urls[k]);
  // Any unknown keys (legacy "manual" or future sources we don't
  // recognize) get appended at the end so they're visible + removable.
  const otherKeys = Object.keys(urls).filter(
    (k) => !orderedKeys.includes(k),
  );
  const allKeys = [...orderedKeys, ...otherKeys];

  const labelStyle: React.CSSProperties = {
    fontSize: 11,
    fontWeight: 600,
    color: t.tg,
    textTransform: "uppercase",
    minWidth: 90,
  };
  const removeBtnStyle: React.CSSProperties = {
    background: "transparent",
    border: `1px solid ${t.border}`,
    color: t.td,
    borderRadius: 6,
    padding: "4px 8px",
    cursor: "pointer",
    fontSize: 12,
  };

  return (
    <div>
      <span
        style={{
          fontSize: 11,
          fontWeight: 600,
          color: t.tg,
          textTransform: "uppercase",
          display: "block",
          marginBottom: 6,
        }}
      >
        Source URLs
      </span>
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {allKeys.length === 0 ? (
          <div style={{ fontSize: 12, color: t.tg, fontStyle: "italic" }}>
            No source URLs yet. Paste one below to get started.
          </div>
        ) : null}

        {allKeys.map((src) => (
          <div
            key={src}
            style={{ display: "flex", alignItems: "center", gap: 6 }}
          >
            <span style={labelStyle}>
              {SOURCE_LABELS[src] || src}
            </span>
            <input
              value={urls[src]}
              readOnly
              style={{ ...ist, flex: 1, color: t.td }}
            />
            <button
              type="button"
              onClick={() => handleRemove(src)}
              disabled={busy}
              title={`Remove ${SOURCE_LABELS[src] || src} URL`}
              style={removeBtnStyle}
            >
              ✕
            </button>
          </div>
        ))}

        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={labelStyle}>Add</span>
          <input
            value={pendingUrl}
            onChange={(e) => {
              setPendingUrl(e.target.value);
              if (err) setErr(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                handleAdd();
              }
            }}
            placeholder="Paste any source URL (Goodreads, Hardcover, …)"
            style={{ ...ist, flex: 1 }}
          />
          <button
            type="button"
            onClick={handleAdd}
            disabled={busy || !pendingUrl.trim()}
            title="Add this URL"
            style={{
              ...removeBtnStyle,
              background: t.accent,
              color: t.bg,
              border: `1px solid ${t.accent}`,
              fontWeight: 700,
              opacity: busy || !pendingUrl.trim() ? 0.5 : 1,
            }}
          >
            {busy ? <Spin /> : "+"}
          </button>
        </div>

        {err ? (
          <div style={{
            fontSize: 12,
            color: t.err,
            padding: "4px 8px",
            background: `${t.err}11`,
            borderRadius: 6,
            border: `1px solid ${t.err}33`,
          }}>
            {err}
          </div>
        ) : null}
      </div>
    </div>
  );
}


