// Mobile-native tentative-review page. Card-per-torrent with
// approve / reject actions. Bulk select toggles via a chip at the
// top — tapping any card in select mode toggles its checkbox
// instead of acting.
import { useEffect, useState } from "react";
import { api } from "../api";
import { useTheme } from "../theme";
import { useVisibleInterval } from "../hooks/useVisibleInterval";
import {
  MobileBtn,
  MobileChip,
  MobileBadge,
  MobileBackButton,
} from "../components/mobile";

interface TentativeItem {
  id: number;
  mam_torrent_id: string;
  torrent_name: string;
  author_blob: string;
  category: string | null;
  language: string | null;
  format: string | null;
  vip: boolean;
  cover_path: string | null;
  status: string;
}

export default function MobileTentativePage() {
  const t = useTheme();
  const [items, setItems] = useState<TentativeItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [selMode, setSelMode] = useState(false);
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);

  const refresh = async () => {
    try {
      const r = await api.get<{ items: TentativeItem[] }>("/v1/tentative");
      setItems(r.items);
      setError(null);
    } catch (e) {
      setError(String(e));
    }
  };

  useEffect(() => { refresh(); }, []);
  useVisibleInterval(refresh, 30_000);

  const toggleSel = (id: number) => {
    setSel((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const approve = async (id: number) => {
    setBusyId(id);
    try {
      await api.post(`/v1/tentative/${id}/approve`);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyId(null);
    }
  };

  const reject = async (id: number) => {
    setBusyId(id);
    try {
      await api.post(`/v1/tentative/${id}/reject`);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyId(null);
    }
  };

  const dismiss = async (id: number) => {
    setBusyId(id);
    try {
      await api.post(`/v1/tentative/${id}/dismiss`);
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusyId(null);
    }
  };

  const bulkAction = async (action: "approve" | "reject" | "dismiss") => {
    if (sel.size === 0) return;
    const verb = action === "approve"
      ? "Approve"
      : action === "reject" ? "Reject" : "Dismiss";
    if (!confirm(`${verb} ${sel.size} torrent(s)?`)) return;
    setBulkBusy(true);
    try {
      await api.post(`/v1/tentative/bulk/${action}`, { ids: [...sel] });
      setSel(new Set());
      setSelMode(false);
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
          New Authors
        </h1>
        <p style={{ fontSize: 13, color: t.td, margin: "4px 0 0" }}>
          Torrents pending allow-list training. Approve to grab and
          train author; Reject to push to weekly review.
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

      {items && items.length > 0 && (
        <div
          style={{
            display: "flex",
            gap: 6,
            flexWrap: "wrap",
          }}
        >
          <MobileChip
            active={selMode}
            onClick={() => {
              setSelMode((m) => !m);
              setSel(new Set());
            }}
          >
            {selMode ? `Selecting (${sel.size})` : "Select"}
          </MobileChip>
          {selMode && (
            <>
              <MobileChip
                onClick={() => setSel(new Set(items.map((i) => i.id)))}
              >
                Select all
              </MobileChip>
              <MobileChip onClick={() => setSel(new Set())}>Clear</MobileChip>
            </>
          )}
        </div>
      )}

      {selMode && sel.size > 0 && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          <MobileBtn
            variant="primary"
            primary
            fullWidth
            onClick={() => bulkAction("approve")}
            disabled={bulkBusy}
          >
            Approve {sel.size}
          </MobileBtn>
          <MobileBtn
            variant="danger"
            fullWidth
            onClick={() => bulkAction("reject")}
            disabled={bulkBusy}
          >
            Reject {sel.size}
          </MobileBtn>
          <MobileBtn
            variant="ghost"
            fullWidth
            onClick={() => bulkAction("dismiss")}
            disabled={bulkBusy}
          >
            Dismiss {sel.size}
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
          No tentative torrents waiting.
        </div>
      ) : (
        items.map((item) => {
          const selected = sel.has(item.id);
          const onCardClick = () => selMode && toggleSel(item.id);
          return (
            <div
              key={item.id}
              onClick={onCardClick}
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 8,
                padding: 12,
                background: selMode && selected ? t.abg : t.bg2,
                border: `1px solid ${
                  selMode && selected ? t.accent : t.border
                }`,
                borderRadius: 12,
                cursor: selMode ? "pointer" : "default",
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "flex-start",
                  gap: 8,
                }}
              >
                {selMode && (
                  <span
                    style={{
                      width: 22,
                      height: 22,
                      flexShrink: 0,
                      borderRadius: 4,
                      border: `2px solid ${selected ? t.accent : t.border}`,
                      background: selected ? t.accent : "transparent",
                      color: t.bg,
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      fontSize: 14,
                      fontWeight: 700,
                    }}
                  >
                    {selected ? "✓" : ""}
                  </span>
                )}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      fontSize: 14,
                      fontWeight: 700,
                      color: t.text,
                      lineHeight: 1.3,
                    }}
                  >
                    {item.torrent_name}
                  </div>
                  <div
                    style={{
                      fontSize: 13,
                      color: t.td,
                      marginTop: 2,
                    }}
                  >
                    {item.author_blob}
                  </div>
                </div>
              </div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                {item.format && <MobileBadge>{item.format}</MobileBadge>}
                {item.language && (
                  <MobileBadge tone="info">{item.language}</MobileBadge>
                )}
                {item.vip && <MobileBadge tone="accent">VIP</MobileBadge>}
                <a
                  href={`https://www.myanonamouse.net/t/${item.mam_torrent_id}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={(e) => e.stopPropagation()}
                  style={{
                    fontSize: 12,
                    fontWeight: 600,
                    color: t.accent,
                    textDecoration: "none",
                    padding: "3px 8px",
                    borderRadius: 999,
                    background: t.abg,
                  }}
                >
                  MAM ↗
                </a>
              </div>
              {!selMode && (
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "1fr 1fr 1fr",
                    gap: 8,
                  }}
                >
                  <MobileBtn
                    variant="primary"
                    primary
                    fullWidth
                    onClick={() => approve(item.id)}
                    disabled={busyId === item.id}
                  >
                    {busyId === item.id ? "…" : "Approve"}
                  </MobileBtn>
                  <MobileBtn
                    variant="danger"
                    fullWidth
                    onClick={() => reject(item.id)}
                    disabled={busyId === item.id}
                  >
                    {busyId === item.id ? "…" : "Reject"}
                  </MobileBtn>
                  <MobileBtn
                    variant="ghost"
                    fullWidth
                    onClick={() => dismiss(item.id)}
                    disabled={busyId === item.id}
                  >
                    {busyId === item.id ? "…" : "Dismiss"}
                  </MobileBtn>
                </div>
              )}
            </div>
          );
        })
      )}
    </div>
  );
}
