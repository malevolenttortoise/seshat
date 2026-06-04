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
import { MergeBookModal } from "./MergeBookModal";
import { ReplaceAuthorModal } from "./ReplaceAuthorModal";
import { economyApi, type PreflightResponse } from "../lib/economyApi";
import type {
  Book,
  BookAction,
  BookActionHandler,
  Contributor,
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
  // v2.4.x: scan now runs as a background task; the immediate response
  // is just the start ack. The book's new mam_status arrives via the
  // page-level polling banner, not in this response.
  error?: string;
  status?: string;
  total?: number;
  // Legacy synchronous-result fields, retained for back-compat with
  // any older code path that still returns them.
  results?: MamScanResult[];
}

interface SendToPipelineResponse {
  sent: number;
  message?: string;
}

// v2.8.0 reingest types — mirror the FastAPI ProbeResponse /
// StartResponse shapes in app/discovery/routers/reingest.py.
interface ReingestCandidate {
  source: "qbit" | "fs";
  display_path: string;
  save_path: string;
  book_files: string[];
  qbit_hash: string | null;
  mtime: number;
  total_size: number;
}

interface ReingestProbeResponse {
  found: boolean;
  candidates: ReingestCandidate[];
  auto_started: boolean;
  grab_id: number | null;
  pipeline_run_id: number | null;
  // v2.8.1: when auto-start fired but the pipeline failed
  // mid-flight (qBit reported a file that wasn't on disk, sink
  // unreachable, etc.) the server returns auto_started=false +
  // error set. The UI shows the error instead of a success toast.
  error?: string | null;
  searched: string[];
  mam_torrent_name: string | null;
}

// Per-source badge palette for the "Metadata" row. `manual` is the
// fallback for links that don't match a known provider.
type SourceKey =
  | "goodreads"
  | "hardcover"
  | "kobo"
  | "amazon"
  | "audible"
  | "ibdb"
  | "google_books"
  | "openlibrary"
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
  // v2.3.4.4: slug query suffix for every per-book mutation.
  // Without it the backend uses the active library, which can write
  // to a different library's row that happens to share the numeric
  // book id (Mark's UAT canary 2026-05-07: an audiobook MAM URL
  // edit landed on the same-id Calibre ebook row).
  const slugQs = book.library_slug
    ? `?slug=${encodeURIComponent(book.library_slug)}`
    : "";
  const [mounted, setMounted] = useState(false);
  const [editing, setEditing] = useState(false);
  const [compareOpen, setCompareOpen] = useState(false);
  const [mergeOpen, setMergeOpen] = useState(false);
  // Contributor editing — a local copy of the byline so removals reflect
  // instantly without waiting on the parent list refresh. `removingId`
  // drives the per-chip spinner; `replaceTarget` opens the
  // last-author replacement modal.
  const [contribs, setContribs] = useState<Contributor[]>(
    book.contributors ?? [],
  );
  const [removingId, setRemovingId] = useState<number | null>(null);
  const [replaceTarget, setReplaceTarget] = useState<Contributor | null>(null);
  // Re-seed when a different book is selected into the same sidebar
  // instance (the parent reuses one BookSidebar and swaps `book`).
  useEffect(() => {
    setContribs(book.contributors ?? []);
    setReplaceTarget(null);
    setRemovingId(null);
  }, [book.id, book.library_slug, book.contributors]);

  // DELETE one contributor. With >1 author it removes immediately; with
  // exactly 1 it opens the replacement modal (the backend refuses to
  // leave a book authorless). `replacementId` is supplied only from the
  // modal path.
  const removeContributor = async (c: Contributor, replacementId?: number) => {
    if (contribs.length <= 1 && replacementId === undefined) {
      setReplaceTarget(c);
      return;
    }
    setRemovingId(c.author_id);
    try {
      const repQs =
        replacementId !== undefined
          ? `${slugQs ? "&" : "?"}replacement_author_id=${replacementId}`
          : "";
      const res = await api.del<{
        contributors: Contributor[];
        removed_author_orphaned: boolean;
      }>(`/discovery/books/${book.id}/contributors/${c.author_id}${slugQs}${repQs}`);
      setContribs(res.contributors);
      setReplaceTarget(null);
      toast.success(
        res.removed_author_orphaned
          ? `Removed ${c.name} — now-empty author will be cleaned up automatically`
          : `Removed ${c.name} from this book`,
      );
      // Refresh the parent list/counts (byline, author totals) in the
      // background — the local `contribs` already updated the sidebar.
      if (onEdit) {
        Promise.resolve(onEdit()).catch(() => {
          /* background refresh — surfaces on parent list */
        });
      }
    } catch (e) {
      toast.error(
        `Remove failed: ${e instanceof Error ? e.message : String(e)}`,
      );
      throw e; // let the modal surface it too
    } finally {
      setRemovingId(null);
    }
  };
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
  // v2.8.0 reingest state. `reingestBusy` mirrors `sending` for the
  // parallel button; `reingestCandidates` holds the picker payload
  // when probe returned >1 result; `reingestError` shows the
  // not-found / failure toast inline near the button.
  const [reingestBusy, setReingestBusy] = useState(false);
  const [reingestCandidates, setReingestCandidates] = useState<
    ReingestCandidate[] | null
  >(null);
  const [reingestError, setReingestError] = useState<string | null>(null);

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

  // Approve a "possible" match as Found, Remove the MAM link
  // entirely (clears URL → status flips to "not_found"), or Skip
  // the book entirely (status → "not_applicable" so MAM scans never
  // touch it again). All three flow through PUT /discovery/books/{id}.
  // Approve sends the same URL back (server flips status — see the
  // 'possible' branch in update_book). Remove sends empty URL.
  // Skip sends mam_status='not_applicable' which is allowlisted in
  // update_book to also clear the URL.
  const decideMam = async (action: "approve" | "remove" | "skip") => {
    if (mamDeciding) return;
    if (action === "remove") {
      if (!confirm("Remove this book's MAM link? It will be marked Not Found.")) return;
    }
    setMamDeciding(true);
    try {
      const payload =
        action === "approve"
          ? { mam_url: book.mam_url || "" }
          : action === "skip"
          ? { mam_status: "not_applicable" }
          : { mam_url: "" };
      await api.put(`/discovery/books/${book.id}${slugQs}`, payload);
      toast.success(
        action === "approve"
          ? "Approved as Found"
          : action === "skip"
          ? "Marked Not Applicable"
          : "Removed"
      );
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
      const r = await api.post<MamScanResponse>(
        `/discovery/books/scan-mam${slugQs}`,
        { book_ids: [book.id] },
      );
      if (r.error) {
        toast.error(`MAM scan failed: ${r.error}`);
      } else {
        // v2.4.x: backend spawns the scan as a background task and
        // returns immediately. The result lands asynchronously — the
        // page-level banner shows live progress; we just acknowledge
        // the start here. Caller's onEdit() refresh runs in the
        // background as the scan completes (the banner triggers a
        // page reload on the running→done transition).
        toast.info("MAM scan started — track progress in the page banner.");
        window.dispatchEvent(new CustomEvent("seshat:scan-started"));
      }
    } catch (e) {
      toast.error(`MAM scan failed: ${(e as Error).message || e}`);
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

  // ── Reingest from disk (v2.8.0) ────────────────────────────
  // For books MAM reports as already-snatched: probe qBit + the
  // configured download folder for the existing files and either
  // auto-start the pipeline (one candidate) or prompt the user to
  // pick among multiple matches.
  const probeReingest = async () => {
    if (reingestBusy) return;
    setReingestBusy(true);
    setReingestCandidates(null);
    setReingestError(null);
    try {
      const slug = book.library_slug
        ? `?slug=${encodeURIComponent(book.library_slug)}`
        : "";
      const r = await api.post<ReingestProbeResponse>(
        `/discovery/books/${book.id}/reingest/probe${slug}`,
      );
      if (!r.found) {
        // Per the v2.8.0 design (option a): hard-fail with a clear
        // message when the file isn't on disk anywhere. NO automatic
        // fallback to re-snatch — the snatch-safety rule forbids it.
        setReingestError(
          `Could not find this snatch anywhere we looked: ${(r.searched || []).join(", ") || "no sources searched"}.`,
        );
        return;
      }
      // v2.8.1: auto-start that ran but failed mid-pipeline returns
      // auto_started=false + error set. Surface that instead of a
      // misleading success toast.
      if (r.error) {
        setReingestError(r.error);
        return;
      }
      if (r.auto_started) {
        toast.success(
          `Reingest started: grab #${r.grab_id}, run #${r.pipeline_run_id}. Check the Review queue.`,
        );
        onEdit?.();
        return;
      }
      // Multi-candidate → show picker.
      setReingestCandidates(r.candidates || []);
    } catch (e) {
      setReingestError(
        `Reingest probe failed: ${(e as Error).message || e}`,
      );
    } finally {
      setReingestBusy(false);
    }
  };

  const startReingestWithCandidate = async (
    candidate: ReingestCandidate,
  ) => {
    if (reingestBusy) return;
    setReingestBusy(true);
    setReingestError(null);
    try {
      const slug = book.library_slug
        ? `?slug=${encodeURIComponent(book.library_slug)}`
        : "";
      const r = await api.post<{
        ok: boolean;
        grab_id: number;
        pipeline_run_id: number;
        error?: string | null;
      }>(
        `/discovery/books/${book.id}/reingest/start${slug}`,
        { candidate },
      );
      // v2.8.1: surface mid-pipeline failures (qBit file moved,
      // sink unreachable, etc.) instead of a misleading success
      // toast. The grab/run rows still exist as audit trail.
      if (!r.ok) {
        setReingestError(
          r.error || `Reingest pipeline_run #${r.pipeline_run_id} failed.`,
        );
        return;
      }
      toast.success(
        `Reingest started: grab #${r.grab_id}, run #${r.pipeline_run_id}. Check the Review queue.`,
      );
      setReingestCandidates(null);
      onEdit?.();
    } catch (e) {
      setReingestError(
        `Reingest start failed: ${(e as Error).message || e}`,
      );
    } finally {
      setReingestBusy(false);
    }
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
      await api.put(`/discovery/books/${book.id}${slugQs}`, ef);
      toast.success("Edit saved");
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

  // Audiobook-specific block gate: fires when the row came from an
  // audiobook source OR carries any of the audiobook-only fields.
  // The second arm handles legacy cache rows that don't have a
  // library_slug stamp yet.
  //
  // Hoisted above the metadata-badge block so the Audible badge's
  // asin fallback can reference it: ABS-sourced audiobooks usually
  // have `asin` set but no `audible_id` (no discovery scan yet).
  const isAudiobookRow = !!(
    book.content_type === "audiobook" ||
    book.audiobookshelf_id ||
    book.narrator ||
    book.duration_sec ||
    book.asin ||
    book.audio_formats
  );

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
    audible:      { bg: "#3d2010", fg: "#f08838", br: "#7a4218" },
    ibdb:         { bg: "#2a1a3d", fg: "#c070e8", br: "#5a2a88" },
    google_books: { bg: "#1a3333", fg: "#70c8e8", br: "#2a7788" },
    openlibrary:  { bg: "#3a1d1a", fg: "#e87a6a", br: "#88332a" },
    manual:       { bg: t.bg4,     fg: t.td,      br: t.border },
  };
  // v2.11.1 N7: fallback URL derivation from the per-source `*_id`
  // columns. Clearing source-scan data wipes `source_url` JSON but
  // preserves the ID columns; without this fallback, badges
  // disappear after a data-clear even though the underlying ID
  // links are intact (UAT 2026-05-13 on Hasekura).
  //
  // Sources whose stored ID maps cleanly to a canonical URL fall
  // back via `idDerivedUrl` below. Sources whose URLs are slug-based
  // (Hardcover, Kobo) fall back via `slugDerivedUrl` against the
  // separately-stored `*_slug` column (v2.12.0). Pre-v2.12.0 only
  // the ID-derivable sources had fallback; HC/Kobo badges
  // disappeared when source_url JSON was missing.
  const idDerivedUrl: Partial<Record<SourceKey, (id: string) => string>> = {
    goodreads: (id) => `https://www.goodreads.com/book/show/${id}`,
    amazon: (id) => `https://www.amazon.com/dp/${id}`,
    audible: (id) => `https://www.audible.com/pd/${id}`,
    google_books: (id) =>
      `https://books.google.com/books?id=${encodeURIComponent(id)}`,
    ibdb: (id) => `https://ibdb.dev/book/${id}`,
    // OL keys can be work-keys (OL...W) or edition-keys (OL...M).
    // Works live under /works/, editions under /books/; suffix-route
    // so both URL imports and discovery-source writes resolve.
    openlibrary: (id) =>
      id.endsWith("M")
        ? `https://openlibrary.org/books/${id}`
        : `https://openlibrary.org/works/${id}`,
  };
  const idColumn: Partial<Record<SourceKey, keyof Book>> = {
    goodreads: "goodreads_id",
    amazon: "amazon_id",
    audible: "audible_id",
    google_books: "google_books_id",
    ibdb: "ibdb_id",
    openlibrary: "openlibrary_id",
  };
  // v2.12.0 — slug-based fallback for Hardcover + Kobo.
  const slugDerivedUrl: Partial<Record<SourceKey, (slug: string) => string>> = {
    hardcover: (slug) => `https://hardcover.app/books/${slug}`,
    kobo: (slug) => `https://www.kobo.com/us/en/ebook/${slug}`,
  };
  const slugColumn: Partial<Record<SourceKey, keyof Book>> = {
    hardcover: "hardcover_slug",
    kobo: "kobo_slug",
  };
  const metadataEntries: { name: SourceKey; url: string }[] = (() => {
    const order: SourceKey[] = [
      "goodreads", "hardcover", "kobo", "amazon", "audible",
      "ibdb", "google_books", "openlibrary",
    ];
    let urls: Record<string, string> = {};
    try {
      urls = JSON.parse(book.source_url || "{}");
    } catch {
      if (book.source_url && book.source_url.startsWith("http")) {
        urls = { [book.source || "unknown"]: book.source_url };
      }
    }
    const out: { name: SourceKey; url: string }[] = [];
    for (const k of order) {
      // v2.16.0 Gap 2 — Hardcover regenerates slugs when titles change
      // on their side, so stored slug-based URLs (and `hardcover_slug`)
      // can go stale and 404. The singular `/book/<id>` form is a
      // stable permalink that 302s to the current canonical slug
      // (probed live 2026-05-17). When we know `hardcover_id`, always
      // prefer it — even over a stored source_url that may be stale.
      if (k === "hardcover" && typeof book.hardcover_id === "string" && book.hardcover_id) {
        out.push({ name: k, url: `https://hardcover.app/book/${book.hardcover_id}` });
        continue;
      }
      if (urls[k]) {
        out.push({ name: k, url: urls[k] });
        continue;
      }
      // ID-derived URL fallback (numeric/UUID IDs).
      const builder = idDerivedUrl[k];
      const idKey = idColumn[k];
      if (builder && idKey) {
        const id = book[idKey];
        if (typeof id === "string" && id) {
          out.push({ name: k, url: builder(id) });
          continue;
        }
      }
      // Slug-derived URL fallback (HC + Kobo).
      const slugBuilder = slugDerivedUrl[k];
      const slugKey = slugColumn[k];
      if (slugBuilder && slugKey) {
        const slug = book[slugKey];
        if (typeof slug === "string" && slug) {
          out.push({ name: k, url: slugBuilder(slug) });
          continue;
        }
      }
      // Audible-only fallback: ABS-sourced audiobook rows usually
      // carry the ASIN in `asin` (from Audnexus) but lack a
      // populated `audible_id` until a discovery scan runs. Treat
      // `asin` on audiobook rows as Audible's ID — same value, just
      // a different column path. Guarded on isAudiobookRow because
      // ebook rows put a Kindle mobi-asin in `asin`, which would
      // resolve to an unrelated product on audible.com.
      if (k === "audible" && isAudiobookRow && book.asin) {
        out.push({ name: "audible", url: `https://www.audible.com/pd/${book.asin}` });
      }
    }
    return out;
  })();

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
          {!editing && (
            <button
              onClick={() => setMergeOpen(true)}
              title="Merge this book with another (duplicate row resolution)"
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
              Merge
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
        {/* v3.0.0: show the full contributor set (position order), not
            just the primary. Falls back to author_name on stale payloads
            with no contributors[]. */}
        {/* v3.x — contributors are individually removable. Each is a
            chip with an × ; with >1 author the removal is immediate
            (after a confirm), with exactly 1 it opens the replacement
            modal since a book can't be left authorless. */}
        <SBRow
          label={contribs.length > 1 ? "Authors" : "Author"}
          value={
            contribs.length ? (
              <span
                style={{
                  display: "inline-flex",
                  flexWrap: "wrap",
                  gap: 6,
                  justifyContent: "flex-end",
                }}
              >
                {contribs.map((c) => (
                  <span
                    key={`${c.author_id}-${c.position}`}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 4,
                      padding: "1px 4px 1px 8px",
                      background: t.bg4,
                      border: `1px solid ${t.border}`,
                      borderRadius: 11,
                      fontSize: 12,
                      lineHeight: 1.5,
                    }}
                  >
                    {c.name}
                    {c.role ? <span style={{ color: t.td }}>({c.role})</span> : null}
                    <button
                      title={`Remove ${c.name} from this book`}
                      aria-label={`Remove ${c.name}`}
                      disabled={removingId !== null}
                      onClick={() => {
                        if (
                          contribs.length > 1 &&
                          !window.confirm(
                            `Remove ${c.name} from "${book.title}"?`,
                          )
                        )
                          return;
                        removeContributor(c).catch(() => {
                          /* toast already surfaced in handler */
                        });
                      }}
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        justifyContent: "center",
                        width: 15,
                        height: 15,
                        padding: 0,
                        borderRadius: "50%",
                        border: "none",
                        background: "none",
                        color: t.red,
                        cursor: removingId !== null ? "default" : "pointer",
                        fontSize: 14,
                        opacity: removingId === c.author_id ? 0.4 : 0.8,
                      }}
                    >
                      ×
                    </button>
                  </span>
                ))}
              </span>
            ) : (
              book.author_name
            )
          }
        />

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
              librarySlug={book.library_slug}
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
                    {book.mam_status === "not_applicable" ? (
                      <span
                        title="MAM scans skip this book (Not Applicable)"
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 4,
                          padding: "3px 10px",
                          borderRadius: 5,
                          fontSize: 12,
                          fontWeight: 600,
                          background: t.bg2,
                          color: t.td,
                          border: `1px solid ${t.borderL}`,
                        }}
                      >
                        N/A
                      </span>
                    ) : book.mam_status === "not_found" ? (
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
                    {/* v2.8.0 reingest: same MAM-found + not-owned
                        case as Send to pipeline, but specifically
                        for books MAM reports as already snatched.
                        Skips the MAM .torrent fetch + qBit submit
                        and pulls the existing files off disk into
                        the enrichment + review flow. */}
                    {pipelineReady &&
                    book.mam_status === "found" &&
                    book.mam_my_snatched &&
                    !book.owned ? (
                      <Btn
                        size="sm"
                        onClick={probeReingest}
                        disabled={reingestBusy}
                        title="Already on disk from a prior snatch — find the files and run them through enrichment + review without re-downloading from MAM."
                        style={{
                          background: t.ok + "22",
                          color: t.ok,
                          border: `1px solid ${t.ok}44`,
                        }}
                      >
                        {reingestBusy ? <Spin /> : "♻"} Reingest from disk
                      </Btn>
                    ) : null}
                  </div>
                </div>
                {/* Reingest error banner — shown inline below the
                    button row when probe/start fails. Includes the
                    list of sources searched so the user can debug
                    a missing drive or wrong download_path setting. */}
                {reingestError ? (
                  <div
                    style={{
                      marginTop: 6,
                      padding: "6px 10px",
                      borderRadius: 6,
                      background: t.err + "15",
                      border: `1px solid ${t.err}55`,
                      color: t.err,
                      fontSize: 12,
                    }}
                  >
                    {reingestError}
                  </div>
                ) : null}
                {/* Reingest candidate picker — appears when probe
                    returned multiple matches. Shows up to 5 with
                    path + file count + size; user clicks one and
                    we POST /reingest/start with the chosen entry. */}
                {reingestCandidates && reingestCandidates.length > 0 ? (
                  <div
                    style={{
                      marginTop: 8,
                      padding: 10,
                      borderRadius: 8,
                      background: t.bg3,
                      border: `1px solid ${t.borderL}`,
                    }}
                  >
                    <div
                      style={{
                        fontSize: 12,
                        fontWeight: 700,
                        color: t.text2,
                        marginBottom: 8,
                      }}
                    >
                      Multiple matches found — pick one:
                    </div>
                    <div
                      style={{
                        display: "flex",
                        flexDirection: "column",
                        gap: 6,
                      }}
                    >
                      {reingestCandidates.map((c, i) => (
                        <button
                          key={`${c.source}:${c.save_path}:${i}`}
                          disabled={reingestBusy}
                          onClick={() => startReingestWithCandidate(c)}
                          style={{
                            textAlign: "left",
                            padding: "6px 10px",
                            borderRadius: 6,
                            background: t.bg2,
                            border: `1px solid ${t.borderL}`,
                            color: t.text,
                            cursor: reingestBusy ? "wait" : "pointer",
                            fontSize: 12,
                          }}
                        >
                          <div style={{ fontWeight: 600 }}>
                            [{c.source}] {c.display_path}
                          </div>
                          <div style={{ color: t.textDim, fontSize: 11 }}>
                            {c.book_files.length} file
                            {c.book_files.length === 1 ? "" : "s"}
                            {c.total_size > 0
                              ? ` · ${(c.total_size / 1024 / 1024).toFixed(1)} MB`
                              : ""}
                          </div>
                        </button>
                      ))}
                      <button
                        disabled={reingestBusy}
                        onClick={() => setReingestCandidates(null)}
                        style={{
                          marginTop: 4,
                          padding: "4px 10px",
                          borderRadius: 6,
                          background: "transparent",
                          border: "none",
                          color: t.textDim,
                          cursor: "pointer",
                          fontSize: 11,
                          textAlign: "left",
                        }}
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : null}

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
                  book.mam_my_snatched ||
                  book.mam_is_bundle) ? (
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
                    {book.mam_is_bundle ? (
                      <span
                        title="This MAM URL points at a series collection / bundle, not a single book"
                        style={{
                          fontSize: 11,
                          padding: "1px 6px",
                          borderRadius: 4,
                          background: t.ylw + "22",
                          color: t.ylwt,
                          border: `1px solid ${t.ylw}33`,
                        }}
                      >
                        Series bundle
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

      {/* MAM-status decision row — visible buttons depend on the
          current mam_status (v2.3.7 matrix):
            possible       → Approve | Remove | Skip
            found          →           Remove | Skip
            not_found      →                    Skip
            null/unscanned →                    Skip
            not_applicable →           Remove
          Approve flips a 'possible' URL to Found. Remove clears the
          URL → 'not_found' (rescannable next tick). Skip sets
          'not_applicable' so the rescan loop stops visiting it. */}
      {(() => {
        if (editing) return null;
        const s = book.mam_status;
        const showApprove = s === "possible";
        const showRemove =
          s === "possible" || s === "found" || s === "not_applicable";
        const showSkip = s !== "not_applicable";
        if (!showApprove && !showRemove && !showSkip) return null;
        return (
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
            {showApprove ? (
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
            ) : null}
            {showRemove ? (
              <Btn
                size="sm"
                onClick={() => decideMam("remove")}
                disabled={mamDeciding}
                title={
                  s === "not_applicable"
                    ? "Clear the Not Applicable mark and let MAM scan this book again"
                    : "Discard this match and mark the book as Not Found on MAM"
                }
                style={{
                  background: t.red + "22",
                  color: t.redt,
                  border: `1px solid ${t.red}44`,
                }}
              >
                {mamDeciding ? <Spin /> : null} Remove MAM
              </Btn>
            ) : null}
            {showSkip ? (
              <Btn
                size="sm"
                onClick={() => decideMam("skip")}
                disabled={mamDeciding}
                title="Mark this book as Not Applicable so MAM scans skip it (e.g. free-to-read, never on MAM)"
                style={{
                  background: t.bg2,
                  color: t.td,
                  border: `1px solid ${t.borderL}`,
                }}
              >
                {mamDeciding ? <Spin /> : null} Skip MAM
              </Btn>
            ) : null}
          </div>
        );
      })()}

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
              onAction("unhide" as BookAction, book.id, book.library_slug);
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
              onAction("dismiss" as BookAction, book.id, book.library_slug);
              onClose();
            }}
          >
            Dismiss
          </Btn>
          <Btn
            size="sm"
            onClick={() => {
              onAction("hide" as BookAction, book.id, book.library_slug);
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
                onAction("delete" as BookAction, book.id, book.library_slug);
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
          librarySlug={book.library_slug}
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

      {mergeOpen ? (
        <MergeBookModal
          book={book}
          onClose={() => setMergeOpen(false)}
          onChanged={() => {
            // Successful merge — the initiator's id may or may not
            // have survived (the backend picks the winner). Closing
            // the sidebar avoids leaving a sidebar open on a row
            // that was just deleted. The parent list refetch via
            // onEdit surfaces the merged row.
            if (onEdit) {
              Promise.resolve(onEdit()).catch(() => {
                /* background refresh — surfaces on parent list */
              });
            }
            onClose();
          }}
        />
      ) : null}

      {replaceTarget ? (
        <ReplaceAuthorModal
          bookTitle={book.title}
          slug={book.library_slug}
          removingName={replaceTarget.name}
          removingAuthorId={replaceTarget.author_id}
          onCancel={() => setReplaceTarget(null)}
          onConfirm={(replacementId) =>
            removeContributor(replaceTarget, replacementId)
          }
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
  librarySlug?: string;
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
  bookId, librarySlug, sourceUrlJson, onChange, theme: t, ist,
}: SourceUrlEditorProps) {
  const slugQs = librarySlug
    ? `?slug=${encodeURIComponent(librarySlug)}`
    : "";
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
        `/discovery/books/${bookId}/source-urls${slugQs}`,
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
        `/discovery/books/${bookId}/source-urls/${encodeURIComponent(sourceName)}${slugQs}`,
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


