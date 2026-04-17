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
  const theme = useTheme();
  const [items, setItems] = useState<TentativeItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

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

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 20 }}>
        <div>
          <h1 style={{ fontSize: 24, fontWeight: 700, color: theme.text, marginBottom: 4 }}>
            Tentative torrents
          </h1>
          <p style={{ fontSize: 14, color: theme.textDim }}>
            Announces that passed every filter except the author allow-list.
          </p>
        </div>
        {items && items.length > 0 && (
          <Btn
            variant="danger"
            disabled={busyId !== null}
            onClick={async () => {
              if (!confirm(`Clear all ${items.length} pending tentative torrents?`)) return;
              setBusyId(-1);
              try {
                await api.post("/v1/data/clear/tentative_torrents", {});
                await refresh();
              } catch (e) { setError(String(e)); }
              finally { setBusyId(null); }
            }}
          >
            Clear all
          </Btn>
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
}: {
  item: TentativeItem;
  busy: boolean;
  onApprove: () => void;
  onReject: () => void;
}) {
  const theme = useTheme();
  const mamUrl = `https://www.myanonamouse.net/t/${item.mam_torrent_id}`;
  const when = new Date(item.created_at + "Z").toLocaleString();

  const coverUrl = item.cover_path
    ? `/api/v1/covers/${encodeURIComponent(item.cover_path)}`
    : null;

  return (
    <article
      style={{
        background: theme.bg2,
        border: `1px solid ${theme.borderL}`,
        borderRadius: 12,
        padding: 16,
        display: "grid",
        gridTemplateColumns: coverUrl ? "80px 1fr auto" : "1fr auto",
        gap: 16,
        animation: "slide-up 0.2s ease-out",
      }}
    >
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
