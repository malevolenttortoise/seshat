// Mobile-native review page. Card-per-book with cover, merged
// metadata, approve/reject buttons, and a collapsed inline edit
// form for the essential metadata fields. Bulk approve/reject
// chips at top.
//
// The desktop page surfaces a richer multi-source cover picker and
// per-source confidence chips; mobile keeps it focused on what the
// user actually does daily — verify and approve.
import { useEffect, useState } from "react";
import { api } from "../api";
import { useTheme } from "../theme";
import { useVisibleInterval } from "../hooks/useVisibleInterval";
import {
  MobileBtn,
  MobileChip,
  MobileSection,
  MobileBackButton,
} from "../components/mobile";

interface ReviewItem {
  id: number;
  grab_id: number;
  staged_path: string;
  book_filename: string;
  book_format: string | null;
  // v2.7.0 bundle awareness — see ReviewPage.tsx for the desktop
  // grouping wrapper. On mobile each sibling is its own swipeable
  // card with a 1/3-style badge; the user reviews them sequentially.
  bundle_group_id?: string | null;
  bundle_index?: number;
  bundle_total?: number;
  bundle_parent_grab_id?: number | null;
  metadata: Record<string, unknown> & {
    title?: string;
    author?: string;
    series?: string;
    series_index?: number;
    description?: string;
    isbn?: string;
    publisher?: string;
    pub_date?: string;
    page_count?: number;
    enriched?: {
      title?: string;
      authors?: string[];
      cover_url?: string;
      source?: string;
      confidence?: number;
    };
    // v2.17.7 — owned-book duplicate hints. See ReviewPage.tsx.
    duplicate_of_owned?: {
      library_slug: string;
      book_id: number;
      title: string;
      mam_status: string;
      calibre_id: number | null;
    }[];
  };
  cover_path: string | null;
  status: string;
  created_at: string;
}

interface ReviewListResponse {
  items: ReviewItem[];
  pending_count: number;
}

function metaString(item: ReviewItem, key: string): string {
  const v = item.metadata[key];
  return typeof v === "string" ? v : v == null ? "" : String(v);
}

export default function MobileReviewPage() {
  const t = useTheme();
  const [items, setItems] = useState<ReviewItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);

  const refresh = async () => {
    try {
      const r = await api.get<ReviewListResponse>("/v1/review");
      setItems(r.items);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  };

  useEffect(() => { refresh(); }, []);
  useVisibleInterval(refresh, 30_000);

  const approve = async (
    id: number,
    metadata?: Record<string, unknown>,
  ) => {
    setBusyId(id);
    try {
      await api.post(`/v1/review/${id}/approve`, {
        metadata: metadata || null,
      });
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyId(null);
    }
  };

  const reject = async (id: number) => {
    if (!confirm("Reject this book? Staging dir will be deleted.")) return;
    setBusyId(id);
    try {
      await api.post(`/v1/review/${id}/reject`, { note: "rejected via UI" });
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyId(null);
    }
  };

  const claimForOwned = async (
    id: number, library_slug: string, book_id: number,
  ) => {
    if (!confirm("Pin this MAM URL to the existing owned book and reject this duplicate import?")) return;
    setBusyId(id);
    try {
      await api.post(`/v1/review/${id}/claim-for-owned`, {
        library_slug, book_id, note: "claimed for owned via mobile UI",
      });
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyId(null);
    }
  };

  const saveEdits = async (id: number, metadata: Record<string, unknown>) => {
    setBusyId(id);
    try {
      await api.post(`/v1/review/${id}/save`, { metadata });
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyId(null);
    }
  };

  const reEnrich = async (
    id: number,
    metadata: Record<string, unknown>,
  ): Promise<boolean> => {
    setBusyId(id);
    setError(null);
    try {
      await api.post(`/v1/review/${id}/re-enrich`, { metadata });
      await refresh();
      return true;
    } catch (e) {
      setError(String(e));
      return false;
    } finally {
      setBusyId(null);
    }
  };

  const bulkAction = async (action: "approve" | "reject") => {
    if (!items || items.length === 0) return;
    if (
      !confirm(
        `${action === "approve" ? "Approve" : "Reject"} all ${items.length} pending review(s)?`,
      )
    )
      return;
    setBulkBusy(true);
    try {
      await api.post(`/v1/review/bulk/${action}`);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBulkBusy(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <MobileBackButton to="dashboard" label="Dashboard" />

      <div>
        <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: t.text }}>
          Review Queue
        </h1>
        <p style={{ fontSize: 13, color: t.td, margin: "4px 0 0" }}>
          Pending books waiting to land in your library.
        </p>
      </div>

      {error && (
        <div
          style={{
            padding: "10px 14px",
            background: t.redb,
            border: `1px solid ${t.redt}`,
            color: t.red,
            borderRadius: 10,
            fontSize: 13,
          }}
        >
          {error}
        </div>
      )}

      {items && items.length > 1 && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          <MobileBtn
            variant="primary"
            primary
            fullWidth
            onClick={() => bulkAction("approve")}
            disabled={bulkBusy}
          >
            Approve all ({items.length})
          </MobileBtn>
          <MobileBtn
            variant="danger"
            fullWidth
            onClick={() => bulkAction("reject")}
            disabled={bulkBusy}
          >
            Reject all
          </MobileBtn>
        </div>
      )}

      {items === null ? (
        <div style={{ padding: 24, textAlign: "center", color: t.tg }}>
          Loading…
        </div>
      ) : items.length === 0 ? (
        <div
          style={{
            padding: 24,
            textAlign: "center",
            color: t.tg,
            fontSize: 13,
            background: t.bg2,
            border: `1px solid ${t.borderL}`,
            borderRadius: 12,
          }}
        >
          Review queue is empty.
        </div>
      ) : (
        items.map((item) => (
          <ReviewCard
            key={item.id}
            item={item}
            onApprove={approve}
            onReject={reject}
            onSave={saveEdits}
            onReEnrich={reEnrich}
            onClaimForOwned={claimForOwned}
            busy={busyId === item.id}
          />
        ))
      )}
    </div>
  );
}

function ReviewCard({
  item,
  onApprove,
  onReject,
  onSave,
  onReEnrich,
  onClaimForOwned,
  busy,
}: {
  item: ReviewItem;
  onApprove: (id: number, metadata?: Record<string, unknown>) => void;
  onReject: (id: number) => void;
  onSave: (id: number, metadata: Record<string, unknown>) => void;
  onReEnrich: (
    id: number,
    metadata: Record<string, unknown>,
  ) => Promise<boolean>;
  onClaimForOwned: (id: number, library_slug: string, book_id: number) => void;
  busy: boolean;
}) {
  const t = useTheme();
  const [editing, setEditing] = useState(false);
  const [edits, setEdits] = useState({
    title: metaString(item, "title"),
    author: metaString(item, "author"),
    series: metaString(item, "series"),
    series_index: metaString(item, "series_index"),
    isbn: metaString(item, "isbn"),
    publisher: metaString(item, "publisher"),
  });

  const cover = item.metadata.enriched?.cover_url || item.cover_path;
  const enriched = item.metadata.enriched;

  const saveAndApprove = () => {
    const metadata: Record<string, unknown> = { ...edits };
    if (edits.series_index)
      metadata.series_index = parseInt(edits.series_index) || null;
    onApprove(item.id, metadata);
  };

  const saveOnly = () => {
    const metadata: Record<string, unknown> = { ...edits };
    if (edits.series_index)
      metadata.series_index = parseInt(edits.series_index) || null;
    onSave(item.id, metadata);
    setEditing(false);
  };

  const reEnrich = async () => {
    // Use the current edits if the user has the form open; otherwise
    // seed the re-scrape with the saved metadata so the search has
    // something to anchor on.
    const seed: Record<string, unknown> = editing
      ? { ...edits }
      : {
          title: metaString(item, "title"),
          author: metaString(item, "author"),
          series: metaString(item, "series"),
          isbn: metaString(item, "isbn"),
        };
    if (
      !confirm(
        "Re-scrape metadata from sources using the current title/author? This will overwrite the enriched metadata and you'll review again.",
      )
    )
      return;
    const ok = await onReEnrich(item.id, seed);
    if (ok) setEditing(false);
  };

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 10,
        padding: 12,
        background: t.bg2,
        border: `1px solid ${t.border}`,
        borderRadius: 12,
      }}
    >
      {/* Bundle indicator — only rendered for bundle children
          (bundle_total >= 2). Single-book grabs (the common case)
          show no extra chrome. */}
      {(item.bundle_total ?? 1) > 1 && (
        <div style={{
          display: "flex", alignItems: "center", gap: 8,
          padding: "6px 10px",
          background: t.accent + "15",
          border: `1px solid ${t.accent}44`,
          borderRadius: 8,
          fontSize: 11, fontWeight: 600, color: t.accent,
          letterSpacing: 0.3,
        }}>
          <span>📚</span>
          <span>Bundle {(item.bundle_index ?? 0) + 1} of {item.bundle_total}</span>
          <span style={{ color: t.tg, fontWeight: 400 }}>· grab #{item.grab_id}</span>
        </div>
      )}
      <div style={{ display: "flex", gap: 12 }}>
        {/* Cover */}
        <div
          style={{
            width: 80,
            height: 120,
            flexShrink: 0,
            background: t.bg3,
            borderRadius: 8,
            overflow: "hidden",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          {cover ? (
            <img
              src={
                cover.startsWith("http")
                  ? cover
                  : `/api/v1/review/${item.id}/cover`
              }
              alt=""
              style={{ width: "100%", height: "100%", objectFit: "cover" }}
              onError={(e) => {
                (e.currentTarget as HTMLImageElement).style.display = "none";
              }}
            />
          ) : (
            <span style={{ color: t.tg, fontSize: 28 }}>?</span>
          )}
        </div>

        {/* Metadata column */}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              fontSize: 15,
              fontWeight: 700,
              color: t.text,
              lineHeight: 1.3,
            }}
          >
            {metaString(item, "title") || item.book_filename}
          </div>
          <div style={{ fontSize: 13, color: t.td, marginTop: 2 }}>
            {metaString(item, "author") || "(unknown author)"}
          </div>
          {metaString(item, "series") && (
            <div style={{ fontSize: 12, color: t.purt, marginTop: 2 }}>
              {metaString(item, "series")}
              {item.metadata.series_index ? ` #${item.metadata.series_index}` : ""}
            </div>
          )}
          <div
            style={{
              display: "flex",
              gap: 4,
              marginTop: 6,
              flexWrap: "wrap",
              fontSize: 11,
            }}
          >
            <span style={{ color: t.tg }}>Grab #{item.grab_id}</span>
            {item.book_format && (
              <span style={{ color: t.td }}>· {item.book_format}</span>
            )}
            {enriched?.source && (
              <span style={{ color: t.cyant }}>
                · {enriched.source}
                {enriched.confidence != null &&
                  ` ${Math.round(enriched.confidence * 100)}%`}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Edit form */}
      {editing && (
        <MobileSection title="Edit metadata" defaultOpen={true}>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {(
              [
                ["title", "Title"],
                ["author", "Author"],
                ["series", "Series"],
                ["series_index", "Series #"],
                ["isbn", "ISBN"],
                ["publisher", "Publisher"],
              ] as const
            ).map(([k, label]) => (
              <label
                key={k}
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: 4,
                  fontSize: 12,
                  color: t.td,
                }}
              >
                {label}
                <input
                  value={edits[k]}
                  onChange={(e) =>
                    setEdits((p) => ({ ...p, [k]: e.target.value }))
                  }
                  style={{
                    minHeight: 44,
                    padding: "0 12px",
                    background: t.inp,
                    color: t.text,
                    border: `1px solid ${t.border}`,
                    borderRadius: 8,
                    fontSize: 16,
                  }}
                />
              </label>
            ))}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
              <MobileBtn
                variant="ghost"
                fullWidth
                onClick={() => setEditing(false)}
              >
                Cancel
              </MobileBtn>
              <MobileBtn
                variant="secondary"
                fullWidth
                onClick={saveOnly}
                disabled={busy}
              >
                Save
              </MobileBtn>
            </div>
          </div>
        </MobileSection>
      )}

      {/* v2.17.7 — duplicate-of-owned banner. Surfaces owned rows
          missing a MAM URL whose title+author matches this grab. */}
      {item.metadata.duplicate_of_owned && item.metadata.duplicate_of_owned.length > 0 && (
        <div
          style={{
            padding: "8px 10px",
            borderRadius: 8,
            background: t.accent + "15",
            border: `1px solid ${t.accent}44`,
            fontSize: 12,
            color: t.text2,
            lineHeight: 1.4,
          }}
        >
          <div style={{ fontWeight: 600, color: t.accent, marginBottom: 4 }}>
            You already own this book
          </div>
          <div>
            The owned copy is in <code style={{ fontSize: 11 }}>
              {item.metadata.duplicate_of_owned[0].library_slug}
            </code>
            {item.metadata.duplicate_of_owned[0].calibre_id != null && (
              <span> · calibre #{item.metadata.duplicate_of_owned[0].calibre_id}</span>
            )}
            . Claim pins the MAM URL to that row and rejects this duplicate.
          </div>
        </div>
      )}

      {/* Secondary action chips — Edit toggle + Re-enrich. Re-enrich
          is available whether the form is open or not so the user
          can re-scrape with edits as the seed without committing. */}
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {!editing && (
          <MobileChip onClick={() => setEditing(true)}>Edit</MobileChip>
        )}
        <MobileChip onClick={reEnrich} leadingIcon="↻">
          Re-enrich
        </MobileChip>
        {item.metadata.duplicate_of_owned && item.metadata.duplicate_of_owned.length === 1 && (
          <MobileChip
            onClick={() => onClaimForOwned(
              item.id,
              item.metadata.duplicate_of_owned![0].library_slug,
              item.metadata.duplicate_of_owned![0].book_id,
            )}
          >
            Claim for owned
          </MobileChip>
        )}
      </div>

      {/* Primary action buttons */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <MobileBtn
          variant="primary"
          primary
          fullWidth
          onClick={editing ? saveAndApprove : () => onApprove(item.id)}
          disabled={busy}
        >
          {busy ? "…" : editing ? "Save & Approve" : "Approve"}
        </MobileBtn>
        <MobileBtn
          variant="danger"
          fullWidth
          onClick={() => onReject(item.id)}
          disabled={busy}
        >
          Reject
        </MobileBtn>
      </div>
    </div>
  );
}
