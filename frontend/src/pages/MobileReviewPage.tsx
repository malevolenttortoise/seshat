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
  busy,
}: {
  item: ReviewItem;
  onApprove: (id: number, metadata?: Record<string, unknown>) => void;
  onReject: (id: number) => void;
  onSave: (id: number, metadata: Record<string, unknown>) => void;
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

      {/* Action buttons */}
      <div style={{ display: "grid", gridTemplateColumns: editing ? "1fr 1fr" : "1fr 1fr 1fr", gap: 8 }}>
        {!editing && (
          <MobileChip onClick={() => setEditing(true)}>Edit</MobileChip>
        )}
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
