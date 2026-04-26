// TentativePage — review torrents that passed every filter except the
// author allow-list.
//
// The enricher captured these via `upsert_tentative` in the
// dispatcher when an announce was skipped with reason
// `author_not_allowlisted`. Approving fetches the .torrent and
// routes it through the normal pipeline AND trains every author on
// the blob onto the allow list. Rejecting pushes the authors onto
// the 3-tier `authors_tentative_review` list for one more weekly
// pass of manual review.
//
// No cover images yet — the Tier 4 scraper path only runs for
// tentative approvals, not for the initial capture. Phase 6b work.
import { useEffect, useState } from "react";
import { Btn } from "../components/Btn";
import { useVisibleInterval } from "../hooks/useVisibleInterval";
import { Section } from "../components/Section";
import { Spin } from "../components/Spin";
import { api } from "../api";
import { useTheme } from "../theme";
import { useViewport } from "../hooks/useViewport";
import { useMobileCodepath } from "../components/mobile";
import MobileTentativePage from "./MobileTentativePage";

interface TentativeItem {
  id: number;
  mam_torrent_id: string;
  torrent_name: string;
  author_blob: string;
  category: string | null;
  language: string | null;
  format: string | null;
  vip: boolean;
  scraped_metadata: Record<string, unknown>;
  cover_path: string | null;
  status: string;
  created_at: string;
}

interface TentativeListResponse {
  items: TentativeItem[];
}

export default function TentativePage() {
  const vp = useViewport();
  if (useMobileCodepath(vp)) return <MobileTentativePage />;
  return <DesktopTentativePage />;
}

function DesktopTentativePage() {
  const theme = useTheme();
  const [items, setItems] = useState<TentativeItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [selMode, setSelMode] = useState(false);
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);

  async function refresh() {
    try {
      const r = await api.get<TentativeListResponse>("/v1/tentative");
      setItems(r.items);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  }

  useEffect(() => { refresh(); }, []);
  useVisibleInterval(refresh, 30_000);

  function toggleSel(id: number) {
    setSel(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function selectAllVisible() {
    setSel(new Set((items || []).map(i => i.id)));
  }

  function clearSel() {
    setSel(new Set());
  }

  async function approve(id: number) {
    setBusyId(id);
    try {
      await api.post(`/v1/tentative/${id}/approve`);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyId(null);
    }
  }

  async function reject(id: number) {
    setBusyId(id);
    try {
      await api.post(`/v1/tentative/${id}/reject`);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyId(null);
    }
  }

  async function bulkAction(action: "approve" | "reject", ids: number[] | null) {
    const count = ids === null ? (items?.length ?? 0) : ids.length;
    if (count === 0) return;
    const label = action === "approve" ? "Approve" : "Reject";
    const warning = action === "approve"
      ? `${label} ${count} tentative torrent(s)? Each approval burns a MAM snatch.`
      : `${label} ${count} tentative torrent(s)? Rejected authors land on the weekly review list.`;
    if (!confirm(warning)) return;
    setBulkBusy(true);
    setError(null);
    try {
      const r = await api.post<{ processed: number; failed: number; errors: string[] }>(
        `/v1/tentative/bulk/${action}`,
        ids === null ? {} : { ids },
      );
      if (r.failed > 0) {
        setError(
          `${label}d ${r.processed}, ${r.failed} failed. First errors: ${r.errors.slice(0, 3).join("; ")}`,
        );
      }
      clearSel();
      setSelMode(false);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBulkBusy(false);
    }
  }

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 20, gap: 12, flexWrap: "wrap" }}>
        <div>
          <h1 style={{ fontSize: 24, fontWeight: 700, color: theme.text, marginBottom: 4 }}>
            Tentative Torrents
          </h1>
          <p style={{ fontSize: 14, color: theme.textDim }}>
            Announces that passed every filter except the author allow-list.
          </p>
        </div>
        {items && items.length > 0 && (
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            {selMode ? (
              <>
                <span style={{ fontSize: 13, color: theme.textDim }}>
                  {sel.size} selected
                </span>
                <Btn
                  variant="ghost" size="sm"
                  onClick={selectAllVisible}
                  disabled={bulkBusy}
                >
                  Select all
                </Btn>
                <Btn
                  variant="primary" size="sm"
                  onClick={() => bulkAction("approve", [...sel])}
                  disabled={bulkBusy || sel.size === 0}
                >
                  {bulkBusy ? <Spin size={14} /> : `Approve Selected`}
                </Btn>
                <Btn
                  variant="danger" size="sm"
                  onClick={() => bulkAction("reject", [...sel])}
                  disabled={bulkBusy || sel.size === 0}
                >
                  Reject Selected
                </Btn>
                <Btn
                  variant="ghost" size="sm"
                  onClick={() => { setSelMode(false); clearSel(); }}
                  disabled={bulkBusy}
                >
                  Cancel
                </Btn>
              </>
            ) : (
              <>
                <Btn
                  variant="ghost" size="sm"
                  onClick={() => setSelMode(true)}
                  disabled={bulkBusy || busyId !== null}
                >
                  Select…
                </Btn>
                <Btn
                  variant="primary" size="sm"
                  onClick={() => bulkAction("approve", null)}
                  disabled={bulkBusy || busyId !== null}
                >
                  {bulkBusy ? <Spin size={14} /> : `Approve All (${items.length})`}
                </Btn>
                <Btn
                  variant="danger" size="sm"
                  onClick={() => bulkAction("reject", null)}
                  disabled={bulkBusy || busyId !== null}
                >
                  Reject All
                </Btn>
                <Btn
                  variant="danger" size="sm"
                  disabled={busyId !== null || bulkBusy}
                  onClick={async () => {
                    if (!confirm(`Clear all ${items.length} pending tentative torrents?`)) return;
                    setBusyId(-1);
                    try {
                      await api.post("/v1/data/clear/tentative_torrents", {});
                      await refresh();
                    } catch (e) { setError(String(e)); }
                    finally { setBusyId(null); }
                  }}
                  title="Clear without adding authors to any review list"
                >
                  Clear all
                </Btn>
              </>
            )}
          </div>
        )}
      </div>

      {error && (
        <div
          style={{
            background: theme.err + "22",
            border: `1px solid ${theme.err}55`,
            color: theme.err,
            padding: "10px 14px",
            borderRadius: 8,
            fontSize: 13,
            marginBottom: 16,
          }}
        >
          {error}
        </div>
      )}

      {items === null ? (
        <div style={{ display: "flex", justifyContent: "center", padding: 40 }}>
          <Spin />
        </div>
      ) : items.length === 0 ? (
        <Section title="Nothing pending" subtitle="No tentative torrents captured.">
          <p style={{ fontSize: 13, color: theme.textDim }}>
            When an announce comes in for an unknown author, it lands here
            for your decision instead of being dropped.
          </p>
        </Section>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {items.map((item) => (
            <TentativeCard
              key={item.id}
              item={item}
              busy={busyId === item.id}
              onApprove={() => approve(item.id)}
              onReject={() => reject(item.id)}
              selMode={selMode}
              selected={sel.has(item.id)}
              onToggleSel={() => toggleSel(item.id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function TentativeCard({
  item,
  busy,
  onApprove,
  onReject,
  selMode,
  selected,
  onToggleSel,
}: {
  item: TentativeItem;
  busy: boolean;
  onApprove: () => void;
  onReject: () => void;
  selMode: boolean;
  selected: boolean;
  onToggleSel: () => void;
}) {
  const theme = useTheme();
  const mamUrl = `https://www.myanonamouse.net/t/${item.mam_torrent_id}`;
  const when = new Date(item.created_at + "Z").toLocaleString();

  const coverUrl = item.cover_path
    ? `/api/v1/covers/${encodeURIComponent(item.cover_path)}`
    : null;

  // When selection mode is active, clicking anywhere on the card
  // toggles selection instead of rendering per-card buttons — makes
  // large batches quick to flip through.
  const cardBorder = selected
    ? `2px solid ${theme.accent}`
    : `1px solid ${theme.borderL}`;

  return (
    <article
      onClick={selMode ? onToggleSel : undefined}
      style={{
        background: theme.bg2,
        border: cardBorder,
        borderRadius: 12,
        padding: 16,
        display: "grid",
        gridTemplateColumns: selMode
          ? (coverUrl ? "24px 80px 1fr" : "24px 1fr")
          : (coverUrl ? "80px 1fr auto" : "1fr auto"),
        gap: 16,
        animation: "slide-up 0.2s ease-out",
        cursor: selMode ? "pointer" : "default",
        userSelect: selMode ? "none" : "auto",
      }}
    >
      {selMode && (
        <input
          type="checkbox"
          checked={selected}
          onChange={onToggleSel}
          onClick={(e) => e.stopPropagation()}
          style={{ width: 20, height: 20, alignSelf: "center", cursor: "pointer" }}
        />
      )}
      {coverUrl && (
        <div style={{ width: 80, height: 120, borderRadius: 6, background: theme.bg3, overflow: "hidden", flexShrink: 0 }}>
          <img
            src={coverUrl}
            alt="Cover"
            style={{
              width: 80,
              height: 120,
              objectFit: "cover",
              borderRadius: 6,
              border: `1px solid ${theme.borderL}`,
            }}
            onError={(e) => {
              (e.target as HTMLImageElement).style.display = "none";
            }}
          />
        </div>
      )}
      <div style={{ minWidth: 0 }}>
        <div
          style={{
            display: "flex",
            alignItems: "baseline",
            gap: 10,
            flexWrap: "wrap",
            marginBottom: 4,
          }}
        >
          <h3
            style={{
              fontSize: 16,
              fontWeight: 700,
              color: theme.text,
              wordBreak: "break-word",
            }}
          >
            {item.torrent_name}
          </h3>
          {item.vip && (
            <span
              style={{
                fontSize: 11,
                color: theme.bg,
                background: theme.warn,
                padding: "2px 8px",
                borderRadius: 99,
                fontWeight: 700,
              }}
            >
              VIP
            </span>
          )}
        </div>
        <div style={{ fontSize: 14, color: theme.text2, marginBottom: 8 }}>
          {item.author_blob || "Unknown author"}
        </div>
        <dl
          style={{
            display: "grid",
            gridTemplateColumns: "auto 1fr",
            gap: "4px 12px",
            fontSize: 12,
          }}
        >
          {item.category && <Field label="Category">{item.category}</Field>}
          {item.language && <Field label="Language">{item.language}</Field>}
          {item.format && <Field label="Format">{item.format}</Field>}
          <Field label="MAM ID">
            <a
              href={mamUrl}
              target="_blank"
              rel="noreferrer"
              style={{ color: theme.accent, textDecoration: "none" }}
            >
              {item.mam_torrent_id}
            </a>
          </Field>
          <Field label="Captured">{when}</Field>
        </dl>
      </div>

      {!selMode && (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 8,
            alignItems: "stretch",
            minWidth: 110,
          }}
        >
          <Btn variant="primary" disabled={busy} onClick={onApprove}>
            {busy ? <Spin size={14} /> : "Approve"}
          </Btn>
          <Btn variant="danger" disabled={busy} onClick={onReject}>
            Reject
          </Btn>
        </div>
      )}
    </article>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  const theme = useTheme();
  return (
    <>
      <dt
        style={{
          color: theme.textDim,
          fontWeight: 600,
          textTransform: "uppercase",
          letterSpacing: 0.3,
        }}
      >
        {label}
      </dt>
      <dd style={{ color: theme.text2, wordBreak: "break-word" }}>{children}</dd>
    </>
  );
}
